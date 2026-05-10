cmake_minimum_required(VERSION 3.16)
project(cvfr-bridge LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# Path to the X-Plane SDK (download from
# https://developer.x-plane.com/sdk/plugin-sdk-downloads/, contains a
# top-level SDK/ directory). Override on the cmake command line:
#   cmake -B build -DSDK_XPLANE=/absolute/path/to/SDK
set(SDK_XPLANE "$ENV{HOME}/supino/XPlaneSDK/SDK"
    CACHE PATH "Path to the X-Plane SDK (containing CHeaders/ and Libraries/)")

if(NOT EXISTS "${SDK_XPLANE}/CHeaders/XPLM/XPLMPlugin.h")
    message(FATAL_ERROR
        "X-Plane SDK not found at ${SDK_XPLANE}.\n"
        "Set -DSDK_XPLANE=/path/to/SDK on the cmake command line.")
endif()

# Generate schema.h from ../schema.json before building plugin.c.
# Re-runs whenever schema.json or the generator change. The generated
# header is .gitignored to avoid drift; clean rebuild always produces
# it from scratch, so what's in the binary always matches what's in
# schema.json.
set(SCHEMA_JSON     "${CMAKE_CURRENT_SOURCE_DIR}/../schema.json")
set(SCHEMA_GEN_PY   "${CMAKE_CURRENT_SOURCE_DIR}/../tools/gen_c_schema.py")
set(SCHEMA_H        "${CMAKE_CURRENT_BINARY_DIR}/schema.h")

find_package(Python3 REQUIRED COMPONENTS Interpreter)

add_custom_command(
    OUTPUT  "${SCHEMA_H}"
    COMMAND "${Python3_EXECUTABLE}" "${SCHEMA_GEN_PY}"
            --schema "${SCHEMA_JSON}"
            --output "${SCHEMA_H}"
    DEPENDS "${SCHEMA_JSON}" "${SCHEMA_GEN_PY}"
    COMMENT "Generating cvfr-bridge schema.h from schema.json"
    VERBATIM)

# Plugin output filename: must end in .xpl so X-Plane finds it.
add_library(cvfr-bridge MODULE plugin.c "${SCHEMA_H}")
target_include_directories(cvfr-bridge PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
set_target_properties(cvfr-bridge PROPERTIES
    OUTPUT_NAME "cvfr-bridge"
    PREFIX ""
    SUFFIX ".xpl")

target_include_directories(cvfr-bridge SYSTEM PRIVATE
    "${SDK_XPLANE}/CHeaders/XPLM")

if(APPLE)
    # Universal binary so the same .xpl works on Intel and Apple Silicon
    # Macs. X-Plane 12 is universal too.
    set(CMAKE_OSX_ARCHITECTURES "arm64;x86_64" CACHE STRING "" FORCE)

    target_compile_definitions(cvfr-bridge PRIVATE APL=1 IBM=0 LIN=0
                                                    XPLM200 XPLM210
                                                    XPLM300 XPLM301)
    target_compile_options(cvfr-bridge PRIVATE
        -fvisibility=hidden -Wall -Wextra)

    # Link against XPLM.framework from the SDK. -undefined dynamic_lookup
    # mirrors what macdeployqt-style flat-namespace plugins use; XPLM
    # symbols are resolved at load time by X-Plane.
    target_link_options(cvfr-bridge PRIVATE
        "-F${SDK_XPLANE}/Libraries/Mac"
        "-framework" "XPLM"
        "-undefined" "dynamic_lookup")

elseif(UNIX)
    target_compile_definitions(cvfr-bridge PRIVATE APL=0 IBM=0 LIN=1
                                                    XPLM200 XPLM210
                                                    XPLM300 XPLM301)
    target_compile_options(cvfr-bridge PRIVATE
        -fvisibility=hidden -Wall -Wextra)
    target_link_libraries(cvfr-bridge PRIVATE pthread)

elseif(WIN32)
    target_compile_definitions(cvfr-bridge PRIVATE APL=0 IBM=1 LIN=0
                                                    XPLM200 XPLM210
                                                    XPLM300 XPLM301)
    target_link_libraries(cvfr-bridge PRIVATE
        "${SDK_XPLANE}/Libraries/Win/XPLM_64.lib"
        ws2_32)
endif()
