cmake_minimum_required(VERSION 3.20)
project(hft_sim LANGUAGES CXX)

add_executable(hft_replay
    src/main.cpp
    src/pcap.cpp
    src/moldudp.cpp
    src/itch.cpp
    src/order_book.cpp
)
target_include_directories(hft_replay PRIVATE include)
target_compile_features(hft_replay PRIVATE cxx_std_20)
set_target_properties(hft_replay PROPERTIES CXX_EXTENSIONS OFF)

if(MSVC)
    target_compile_options(hft_replay PRIVATE /W4 /permissive-)
else()
    target_compile_options(hft_replay PRIVATE -Wall -Wextra -Wpedantic)
endif()

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 COMPONENTS Interpreter QUIET)
    if(Python3_Interpreter_FOUND)
        add_test(NAME replay_integration COMMAND
            "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/replay_test.py"
            "$<TARGET_FILE:hft_replay>")
    else()
        message(STATUS "Python 3 unavailable. Skipping the replay integration test.")
    endif()
endif()
