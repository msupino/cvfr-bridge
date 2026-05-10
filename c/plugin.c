/* cvfr-bridge.xpl - X-Plane plugin that serves aircraft pose as JSON
 * over HTTP on port CVFR_PORT (2020 by default). Drop-in compatible
 * with python/cvfrmap-bridge.py - both serve the same wire format
 * defined in the repo-root schema.json.
 *
 * Architecture:
 *   - flight-loop callback (runs at ~10 Hz on X-Plane's main thread)
 *     reads datarefs into a `snapshot` struct under a mutex.
 *   - HTTP server thread (BSD sockets, single-threaded accept loop)
 *     reads the same struct under the mutex and serves JSON.
 *   - shutdown: XPluginStop closes the listen socket (which unblocks
 *     accept), signals the thread to exit, joins.
 *
 * The JSON wire format is defined in ../schema.json. CMake invokes
 * tools/gen_c_schema.py before compilation to generate schema.h with
 * the field names, format strings, and JSON template; the plugin's
 * format_json() is then a single snprintf using CVFR_JSON_TEMPLATE,
 * with no hand-maintained field-name string literals.
 *
 * No external HTTP library: the HTTP we accept is trivially simple
 * (any GET returns the JSON; no routing, no headers parsing).
 */

#include "schema.h"   /* generated from ../schema.json by tools/gen_c_schema.py */

#include <XPLMPlugin.h>
#include <XPLMDataAccess.h>
#include <XPLMProcessing.h>
#include <XPLMUtilities.h>

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#ifndef PLUGIN_API
#define PLUGIN_API
#endif

#pragma GCC diagnostic ignored "-Wunused-parameter"

/* All configurable constants come from the generated schema.h, which
 * is built from ../schema.json. CVFR_PORT is the HTTP bind port;
 * CVFR_FALLBACK_<NAME> are the LLBG fallback values used when the sim
 * isn't ready (lat == lon == 0). */

/* ------- dataref handles, resolved in XPluginStart ------------------ */

static XPLMDataRef dr_lat;        /* sim/flightmodel/position/latitude            (double, deg) */
static XPLMDataRef dr_lon;        /* sim/flightmodel/position/longitude           (double, deg) */
static XPLMDataRef dr_elev_m;     /* sim/flightmodel/position/elevation           (double, m MSL) */
static XPLMDataRef dr_mag_psi;    /* sim/flightmodel/position/mag_psi             (float,  mag deg) */
static XPLMDataRef dr_mag_var;    /* sim/flightmodel/position/magnetic_variation  (float,  deg) */
static XPLMDataRef dr_theta;      /* sim/flightmodel/position/theta               (float,  pitch deg) */
static XPLMDataRef dr_phi;        /* sim/flightmodel/position/phi                 (float,  roll  deg) */
static XPLMDataRef dr_ias;        /* sim/flightmodel/position/indicated_airspeed  (float,  KIAS) */
static XPLMDataRef dr_vsi;        /* sim/flightmodel/position/vh_ind_fpm          (float,  fpm) */
static XPLMDataRef dr_wind_kt;    /* sim/cockpit2/gauges/indicators/wind_speed_kts        (float, kt) */
static XPLMDataRef dr_wind_dir;   /* sim/cockpit2/gauges/indicators/wind_heading_deg_mag  (float, deg mag) */
static XPLMDataRef dr_qnh;        /* sim/cockpit2/gauges/actuators/barometer_setting_in_hg_pilot (float, inHg) */

/* ------- shared snapshot, written by flight loop, read by HTTP ----- */

typedef struct {
    double latitude;
    double longitude;
    int    altitude;        /* feet, MSL */
    double heading;         /* mag deg */
    double variation;       /* mag var, deg */
    double pitch;           /* deg */
    double roll;            /* deg */
    double ias;             /* kt */
    int    vsi;             /* fpm */
    double wind_dir;        /* deg mag */
    double wind_speed;      /* kt */
    double qnh;             /* inHg */
    bool   sim_ready;       /* false when lat/lon are 0,0 (cold start) */
} snapshot_t;

static pthread_mutex_t snap_mu = PTHREAD_MUTEX_INITIALIZER;
static snapshot_t      snap;       /* protected by snap_mu */

/* ------- HTTP server thread state ---------------------------------- */

static int                listen_fd = -1;
static pthread_t          http_thread;
static atomic_bool        http_should_stop = ATOMIC_VAR_INIT(false);
static bool               http_thread_started = false;

/* Convert X-Plane meters to feet rounded to int. */
static int meters_to_ft(double m) { return (int)(m * 3.28084 + 0.5); }

/* ------- flight loop callback (X-Plane main thread, 10 Hz) --------- */

static float flight_loop_cb(float inElapsedSinceLastCall,
                            float inElapsedTimeSinceLastFlightLoop,
                            int   inCounter,
                            void* inRefcon)
{
    snapshot_t s;
    s.latitude   = XPLMGetDatad(dr_lat);
    s.longitude  = XPLMGetDatad(dr_lon);
    double el_m  = XPLMGetDatad(dr_elev_m);
    s.altitude   = meters_to_ft(el_m);
    s.heading    = XPLMGetDataf(dr_mag_psi);
    s.variation  = XPLMGetDataf(dr_mag_var);
    s.pitch      = XPLMGetDataf(dr_theta);
    s.roll       = XPLMGetDataf(dr_phi);
    s.ias        = XPLMGetDataf(dr_ias);
    s.vsi        = (int)(XPLMGetDataf(dr_vsi) + 0.5);
    s.wind_dir   = XPLMGetDataf(dr_wind_dir);
    s.wind_speed = XPLMGetDataf(dr_wind_kt);
    s.qnh        = XPLMGetDataf(dr_qnh);
    s.sim_ready  = !(s.latitude == 0.0 && s.longitude == 0.0);
    if (!s.sim_ready) {
        s.latitude  = CVFR_FALLBACK_LATITUDE;
        s.longitude = CVFR_FALLBACK_LONGITUDE;
        s.altitude  = CVFR_FALLBACK_ALTITUDE;
        s.heading   = CVFR_FALLBACK_HEADING;
    }

    pthread_mutex_lock(&snap_mu);
    snap = s;
    pthread_mutex_unlock(&snap_mu);

    return 0.1f;  /* re-arm at 10 Hz */
}

/* ------- HTTP server thread ---------------------------------------- */

/* Format the current snapshot into a JSON body. Returns bytes written
 * (always < cap; truncated to cap-1 if it would overflow).
 *
 * The format string CVFR_JSON_TEMPLATE comes from the generated
 * schema.h; argument order MUST match the field order in
 * ../schema.json's "fields" array. If you reorder fields there, the
 * gen step regenerates the template, but you also have to reorder
 * the snprintf args here to keep their positional alignment with
 * the template's % conversions. (This is the one bit of manual
 * coupling that survives the codegen.) */
static int format_json(char* buf, size_t cap)
{
    snapshot_t s;
    pthread_mutex_lock(&snap_mu);
    s = snap;
    pthread_mutex_unlock(&snap_mu);

    return snprintf(buf, cap, CVFR_JSON_TEMPLATE,
        s.latitude,    /* latitude   */
        s.longitude,   /* longitude  */
        s.altitude,    /* altitude   */
        s.heading,     /* heading    */
        s.variation,   /* variation  */
        s.pitch,       /* pitch      */
        s.roll,        /* roll       */
        s.ias,         /* ias        */
        s.vsi,         /* vsi        */
        s.wind_dir,    /* wind_dir   */
        s.wind_speed,  /* wind_speed */
        s.qnh,         /* qnh        */
        s.sim_ready ? "true" : "false"   /* sim_ready */
    );
}

/* Serve one connection: read the request line (we don't care about
 * the path or headers — any GET returns the JSON), write the response,
 * close. */
static void serve_one(int conn_fd)
{
    /* Drain enough of the request to satisfy the client; we don't parse it. */
    char req[1024];
    (void)recv(conn_fd, req, sizeof req, MSG_DONTWAIT);

    char body[512];
    int body_len = format_json(body, sizeof body);
    if (body_len < 0 || body_len >= (int)sizeof body) {
        body_len = snprintf(body, sizeof body, "{\"error\":\"format\"}");
    }

    char hdr[256];
    int hdr_len = snprintf(hdr, sizeof hdr,
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Connection: close\r\n"
        "\r\n",
        body_len);

    /* send() can short-write but for these tiny payloads it never does
     * in practice. Explicit MSG_NOSIGNAL avoids SIGPIPE if the client
     * closed early (e.g. iPad app polling, walked away). */
    (void)send(conn_fd, hdr,  hdr_len,  MSG_NOSIGNAL);
    (void)send(conn_fd, body, body_len, MSG_NOSIGNAL);
    close(conn_fd);
}

static void* http_thread_main(void* arg)
{
    /* Block SIGPIPE on this thread for safety: closed-client writes
     * use MSG_NOSIGNAL above too, but belt-and-suspenders. */
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGPIPE);
    pthread_sigmask(SIG_BLOCK, &mask, NULL);

    while (!atomic_load(&http_should_stop)) {
        struct sockaddr_in cli;
        socklen_t cli_len = sizeof cli;
        int conn = accept(listen_fd, (struct sockaddr*)&cli, &cli_len);
        if (conn < 0) {
            /* listen_fd was closed by XPluginStop -> accept returns
             * EBADF or EINVAL; both are normal shutdown signals. */
            if (errno == EBADF || errno == EINVAL || errno == EINTR)
                break;
            /* Transient error; brief backoff and retry. */
            usleep(50 * 1000);
            continue;
        }
        serve_one(conn);
    }
    return NULL;
}

/* Bring up the listen socket + thread. Returns 0 on success. */
static int http_start(void)
{
    listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        XPLMDebugString("cvfr-bridge: socket() failed\n");
        return -1;
    }

    int one = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);

    struct sockaddr_in addr = {0};
    addr.sin_family      = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);   /* 0.0.0.0 - reachable from LAN (iPad) */
    addr.sin_port        = htons(CVFR_PORT);

    if (bind(listen_fd, (struct sockaddr*)&addr, sizeof addr) < 0) {
        char msg[128];
        snprintf(msg, sizeof msg,
            "cvfr-bridge: bind(0.0.0.0:%d) failed: %s "
            "(is cvfrmap-bridge.py still running?)\n",
            CVFR_PORT, strerror(errno));
        XPLMDebugString(msg);
        close(listen_fd);
        listen_fd = -1;
        return -1;
    }

    if (listen(listen_fd, 8) < 0) {
        XPLMDebugString("cvfr-bridge: listen() failed\n");
        close(listen_fd);
        listen_fd = -1;
        return -1;
    }

    if (pthread_create(&http_thread, NULL, http_thread_main, NULL) != 0) {
        XPLMDebugString("cvfr-bridge: pthread_create failed\n");
        close(listen_fd);
        listen_fd = -1;
        return -1;
    }
    http_thread_started = true;

    char ok[128];
    snprintf(ok, sizeof ok,
        "cvfr-bridge: HTTP listening on http://0.0.0.0:%d/ (schema v%s)\n",
        CVFR_PORT, CVFR_SCHEMA_VERSION);
    XPLMDebugString(ok);
    return 0;
}

static void http_stop(void)
{
    atomic_store(&http_should_stop, true);
    if (listen_fd >= 0) {
        /* Closing the listen fd unblocks accept() in the worker. */
        shutdown(listen_fd, SHUT_RDWR);
        close(listen_fd);
        listen_fd = -1;
    }
    if (http_thread_started) {
        pthread_join(http_thread, NULL);
        http_thread_started = false;
    }
}

/* ------- X-Plane SDK lifecycle ------------------------------------- */

PLUGIN_API int XPluginStart(char* outName, char* outSig, char* outDesc)
{
    strcpy(outName, "cvfr-bridge");
    strcpy(outSig,  "cvfr.bridge.json");
    snprintf(outDesc, 256,
        "Serves aircraft pose as JSON on http://localhost:%d/ (schema v%s) "
        "for the cvfr-map iPad/web app",
        CVFR_PORT, CVFR_SCHEMA_VERSION);

    dr_lat       = XPLMFindDataRef("sim/flightmodel/position/latitude");
    dr_lon       = XPLMFindDataRef("sim/flightmodel/position/longitude");
    dr_elev_m    = XPLMFindDataRef("sim/flightmodel/position/elevation");
    dr_mag_psi   = XPLMFindDataRef("sim/flightmodel/position/mag_psi");
    dr_mag_var   = XPLMFindDataRef("sim/flightmodel/position/magnetic_variation");
    dr_theta     = XPLMFindDataRef("sim/flightmodel/position/theta");
    dr_phi       = XPLMFindDataRef("sim/flightmodel/position/phi");
    dr_ias       = XPLMFindDataRef("sim/flightmodel/position/indicated_airspeed");
    dr_vsi       = XPLMFindDataRef("sim/flightmodel/position/vh_ind_fpm");
    dr_wind_kt   = XPLMFindDataRef("sim/cockpit2/gauges/indicators/wind_speed_kts");
    dr_wind_dir  = XPLMFindDataRef("sim/cockpit2/gauges/indicators/wind_heading_deg_mag");
    dr_qnh       = XPLMFindDataRef("sim/cockpit2/gauges/actuators/barometer_setting_in_hg_pilot");

    /* Conservative null-check: the position datarefs MUST exist on every
     * X-Plane build; the cockpit2 weather datarefs are X-Plane 11+ but
     * also universally present. If any are missing, refuse to load
     * cleanly so users see a single Log.txt error rather than a crash. */
    if (!(dr_lat && dr_lon && dr_elev_m && dr_mag_psi && dr_mag_var &&
          dr_theta && dr_phi && dr_ias && dr_vsi && dr_wind_kt &&
          dr_wind_dir && dr_qnh)) {
        XPLMDebugString("cvfr-bridge: required dataref missing; not loading\n");
        return 0;
    }

    if (http_start() != 0) {
        /* Bind failed (port busy?); plugin loads but inert. Keeps
         * X-Plane stable, user can fix the conflict and restart. */
        return 1;
    }

    XPLMRegisterFlightLoopCallback(flight_loop_cb, -1.0f, NULL);
    XPLMDebugString("cvfr-bridge: started\n");
    return 1;
}

PLUGIN_API void XPluginStop(void)
{
    XPLMUnregisterFlightLoopCallback(flight_loop_cb, NULL);
    http_stop();
    XPLMDebugString("cvfr-bridge: stopped\n");
}

PLUGIN_API int  XPluginEnable(void)         { return 1; }
PLUGIN_API void XPluginDisable(void)        {}
PLUGIN_API void XPluginReceiveMessage(XPLMPluginID inFromWho,
                                      int inMsg, void* inParam) {}
