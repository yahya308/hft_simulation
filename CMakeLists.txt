cmake_minimum_required(VERSION 3.20)
project(hft_sim LANGUAGES CXX)

add_library(hft_feed STATIC
    src/pcap.cpp
    src/moldudp.cpp
    src/itch.cpp
    src/order_book.cpp
)
target_include_directories(hft_feed PUBLIC include)
target_compile_features(hft_feed PUBLIC cxx_std_20)

add_executable(hft_replay src/main.cpp)
add_executable(hft_asels_feed src/asels_feed.cpp)
target_link_libraries(hft_replay PRIVATE hft_feed)
target_link_libraries(hft_asels_feed PRIVATE hft_feed)

foreach(target hft_feed hft_replay hft_asels_feed)
    set_target_properties(${target} PROPERTIES CXX_EXTENSIONS OFF)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /permissive-)
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
    endif()
endforeach()

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 COMPONENTS Interpreter QUIET)
    if(Python3_Interpreter_FOUND)
        add_test(NAME replay_integration COMMAND
            "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/replay_test.py"
            "$<TARGET_FILE:hft_replay>")
        add_test(NAME asels_feed_integration COMMAND
            "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/feed_test.py"
            "$<TARGET_FILE:hft_asels_feed>")
        add_test(NAME asels_ouch_simulation COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_simulation.py)
        set_tests_properties(asels_ouch_simulation PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME asels_dashboard_integration COMMAND
            "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/dashboard_test.py"
            "$<TARGET_FILE:hft_asels_feed>")
        add_test(NAME strategy_api COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_strategy_api.py)
        set_tests_properties(strategy_api PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME strategy_dashboard_integration COMMAND
            "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/strategy_dashboard_test.py"
            "$<TARGET_FILE:hft_asels_feed>")
        add_test(NAME multi_simulation COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_multi_simulation.py)
        set_tests_properties(multi_simulation PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME portfolio_risk COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_portfolio_risk.py)
        set_tests_properties(portfolio_risk PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME learning_quote COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_learning_quote.py)
        set_tests_properties(learning_quote PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME ouch_gateway COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_ouch_gateway.py)
        set_tests_properties(ouch_gateway PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME enterprise_runtime COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_enterprise_runtime.py)
        set_tests_properties(enterprise_runtime PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME strategy_isolation COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_isolation.py)
        set_tests_properties(strategy_isolation PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME external_ouch_priority COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_external_priority.py)
        set_tests_properties(external_ouch_priority PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME audit_recovery COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_run_audit.py)
        set_tests_properties(audit_recovery PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
        add_test(NAME broker_comparison COMMAND
            "${Python3_EXECUTABLE}" -B -m unittest discover -s tests -p test_broker_comparison.py)
        set_tests_properties(broker_comparison PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    else()
        message(STATUS "Python 3 unavailable. Skipping the replay integration test.")
    endif()
endif()
