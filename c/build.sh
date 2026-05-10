#!/usr/bin/env bash
# build.sh - one-shot build + install of cvfr-bridge.xpl into X-Plane.
# Idempotent. Reads the XPLANE env var (defaults to ~/X-Plane 12).
#
# Usage:
#   ./build.sh                       # build + install + report
#   ./build.sh clean                 # rm -rf build/ then build
#   XPLANE=/path/to/X-Plane\ 12 ./build.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BUILD="$REPO/build"
XPLANE="${XPLANE:-$HOME/X-Plane 12}"

# Install destination. Two valid layouts depending on whether the user
# manages plugins through XLauncher (symlink-based profile manager) or
# installs straight into X-Plane:
#
#   1. XLauncher source dir   (~/XPlane-Plugins-Available/) — preferred when
#      that directory exists, because XLauncher will then surface this
#      plugin in its UI for per-profile enable/disable. Symlinking into
#      X-Plane is then XLauncher's responsibility.
#
#   2. X-Plane plugins dir directly — fallback for users without
#      XLauncher; the plugin is then always loaded by X-Plane on launch.
#
# Override either way with INSTALL_TO=...
if [ -n "${INSTALL_TO:-}" ]; then
    PLUGIN_DIR="$INSTALL_TO/cvfr-bridge"
elif [ -d "$HOME/XPlane-Plugins-Available" ]; then
    PLUGIN_DIR="$HOME/XPlane-Plugins-Available/cvfr-bridge"
    INSTALL_NOTE="(XLauncher source dir — tick it in XLauncher to activate)"
else
    PLUGIN_DIR="$XPLANE/Resources/plugins/cvfr-bridge"
    INSTALL_NOTE="(direct X-Plane install — loads automatically on next launch)"
fi

if [ "${1:-}" = "clean" ]; then
    rm -rf "$BUILD"
fi

if [ ! -d "$XPLANE" ]; then
    echo "ERROR: X-Plane install not found at: $XPLANE" >&2
    echo "  Override with: XPLANE=/path/to/'X-Plane 12' ./build.sh" >&2
    exit 1
fi

mkdir -p "$BUILD"
cmake -S "$REPO" -B "$BUILD" -G "Unix Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD" -j"$(sysctl -n hw.ncpu 2>/dev/null || nproc)"

# X-Plane scans Resources/plugins/<NAME>/<arch>/<NAME>.xpl. Use the
# folder layout (not single-file) so the plugin can grow companion
# files later (config, README) if needed without changing the install
# path.
ARCH_DIR="$PLUGIN_DIR/mac_x64"   # X-Plane 12 universal-binary location on macOS
mkdir -p "$ARCH_DIR"
cp "$BUILD/cvfr-bridge.xpl" "$ARCH_DIR/cvfr-bridge.xpl"

echo
echo "Installed: $ARCH_DIR/cvfr-bridge.xpl"
ls -la "$ARCH_DIR/cvfr-bridge.xpl"
echo "  ${INSTALL_NOTE:-}"
echo
echo "Next:"
echo "  1. Stop the Python cvfrmap-bridge if it's still running"
echo "     (it would conflict on port 2020):"
echo "       pkill -f cvfrmap-bridge.py"
if [ -d "$HOME/XPlane-Plugins-Available" ] && [ -z "${INSTALL_TO:-}" ]; then
    echo "  2. In XLauncher: select your profile, tick 'cvfr-bridge'"
    echo "     under the Plugins list, save."
    echo "  3. Launch X-Plane via XLauncher's Start button."
else
    echo "  2. Launch X-Plane (no further setup needed)."
fi
echo "  4. Verify: curl http://localhost:2020/"
echo "     (or check $XPLANE/Log.txt for 'cvfr-bridge: HTTP listening')"
