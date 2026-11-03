cmake_minimum_required(VERSION 3.24)

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" PREMARKET_VERSION_RAW)
string(STRIP "${PREMARKET_VERSION_RAW}" PREMARKET_VERSION)

project(premarket VERSION ${PREMARKET_VERSION} LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  message(FATAL_ERROR "premarket builds with clang + libc++ (got ${CMAKE_CXX_COMPILER_ID}); use securities-build:22.04")
endif()

set(PREMARKET_BUILD_SHA "" CACHE STRING "Commit the binary is built from; stamped into every manifest")
if(PREMARKET_BUILD_SHA STREQUAL "")
  execute_process(COMMAND git -C "${CMAKE_CURRENT_SOURCE_DIR}" rev-parse HEAD
                  OUTPUT_VARIABLE PREMARKET_GIT_SHA OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET
                  RESULT_VARIABLE PREMARKET_GIT_RC)
  if(PREMARKET_GIT_RC EQUAL 0)
    set(PREMARKET_BUILD_SHA "${PREMARKET_GIT_SHA}")
  else()
    set(PREMARKET_BUILD_SHA "unknown")
  endif()
endif()
message(STATUS "premarket ${PREMARKET_VERSION}, build ${PREMARKET_BUILD_SHA}")

option(PREMARKET_BUILD_TESTS "Build the test suite" ON)
option(PREMARKET_WERROR "Treat warnings as errors" ON)
set(PREMARKET_SANITIZE "" CACHE STRING "Sanitizers for a checking build, e.g. address,undefined (./build.sh sanitize)")

find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(ZLIB REQUIRED)
find_package(nlohmann_json 3.12 REQUIRED)
find_package(httplib REQUIRED)
find_package(date REQUIRED)
find_package(databento REQUIRED)
set(PREMARKET_PQ_ROOT "/usr/local/pgsql" CACHE PATH "Static libpq (built in securities-build:22.04)")
add_library(premarket_pq INTERFACE)
target_include_directories(premarket_pq SYSTEM INTERFACE ${PREMARKET_PQ_ROOT}/include)
target_link_libraries(premarket_pq INTERFACE
  ${PREMARKET_PQ_ROOT}/lib/libpq.a ${PREMARKET_PQ_ROOT}/lib/libpgcommon_shlib.a ${PREMARKET_PQ_ROOT}/lib/libpgport_shlib.a
  OpenSSL::SSL OpenSSL::Crypto)
find_library(DUCKDB_LIBRARY NAMES duckdb REQUIRED)
find_path(DUCKDB_INCLUDE_DIR NAMES duckdb.h REQUIRED)

add_library(premarket_flags INTERFACE)
target_compile_options(premarket_flags INTERFACE -Wall -Wextra -Wpedantic -Wshadow -Wnon-virtual-dtor)
if(PREMARKET_WERROR)
  target_compile_options(premarket_flags INTERFACE -Werror)
endif()
set(PREMARKET_SANITIZED OFF)
if(PREMARKET_SANITIZE)
  set(PREMARKET_SANITIZED ON)
  target_compile_options(premarket_flags INTERFACE -fsanitize=${PREMARKET_SANITIZE} -fno-sanitize-recover=all -fno-omit-frame-pointer)
  target_link_options(premarket_flags INTERFACE -fsanitize=${PREMARKET_SANITIZE})
endif()

file(GLOB_RECURSE PREMARKET_SOURCES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)
add_library(premarket_core STATIC ${PREMARKET_SOURCES})
target_include_directories(premarket_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_include_directories(premarket_core SYSTEM PUBLIC ${DUCKDB_INCLUDE_DIR})
target_compile_definitions(premarket_core PUBLIC
  PREMARKET_VERSION="${PREMARKET_VERSION}"
  PREMARKET_BUILD_SHA="${PREMARKET_BUILD_SHA}")
target_link_libraries(premarket_core
  PUBLIC
    databento::databento
    nlohmann_json::nlohmann_json
    httplib::httplib
    date::date
    date::date-tz
    premarket_pq
    OpenSSL::SSL
    OpenSSL::Crypto
    ZLIB::ZLIB
    ${DUCKDB_LIBRARY}
    Threads::Threads
  PRIVATE
    premarket_flags)

add_executable(premarket apps/premarket.cpp)
target_link_libraries(premarket PRIVATE premarket_core premarket_flags)
target_link_options(premarket PRIVATE -static-libstdc++ -fuse-ld=lld "-Wl,-rpath,\$ORIGIN/../lib")
set_target_properties(premarket PROPERTIES SKIP_BUILD_RPATH TRUE)

if(PREMARKET_BUILD_TESTS)
  enable_testing()
  file(GLOB PREMARKET_TESTS CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_*.cpp)
  add_executable(premarket_tests tests/main.cpp ${PREMARKET_TESTS})
  target_include_directories(premarket_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/tests)
  target_link_libraries(premarket_tests PRIVATE premarket_core premarket_flags)
  target_link_options(premarket_tests PRIVATE -static-libstdc++ -fuse-ld=lld)
  target_compile_definitions(premarket_tests PRIVATE
    PREMARKET_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}"
    PREMARKET_SANITIZED=$<IF:$<BOOL:${PREMARKET_SANITIZED}>,true,false>)
  add_test(NAME premarket_tests COMMAND premarket_tests)
endif()
