cmake_minimum_required(VERSION 3.18)
project(ros2_bag_recorder_native LANGUAGES C CXX)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

find_package(Python3 REQUIRED COMPONENTS Interpreter Development.Module)
find_package(Threads REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(rcl_interfaces REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rmw REQUIRED)
find_package(rosbag2_cpp REQUIRED)
find_package(rosbag2_storage REQUIRED)
find_package(rosbag2_compression REQUIRED)

Python3_add_library(_recorder_native MODULE WITH_SOABI module.cpp session.cpp)
target_compile_features(_recorder_native PRIVATE cxx_std_17)
target_link_libraries(_recorder_native PRIVATE
  Threads::Threads nlohmann_json::nlohmann_json rclcpp::rclcpp rmw::rmw
  rcl_interfaces::rcl_interfaces__rosidl_typesupport_cpp
  rosbag2_cpp::rosbag2_cpp rosbag2_storage::rosbag2_storage
  rosbag2_compression::rosbag2_compression)
set_target_properties(_recorder_native PROPERTIES CXX_VISIBILITY_PRESET hidden
  VISIBILITY_INLINES_HIDDEN ON)
