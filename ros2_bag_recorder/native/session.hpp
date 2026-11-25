#pragma once

#include <cstddef>
#include <filesystem>
#include <memory>
#include <nlohmann/json.hpp>
#include <rclcpp/node.hpp>

// All calls and subscription callbacks run on the host's single-threaded executor.
// The destination is the final bag directory. It must not already exist.
class RecordingSession {
public:
  RecordingSession(rclcpp::Node &node, const std::filesystem::path &destination,
                   const nlohmann::json &settings, std::size_t queue_bytes = 64u << 20);
  ~RecordingSession();
  RecordingSession(const RecordingSession &) = delete;
  RecordingSession &operator=(const RecordingSession &) = delete;

  nlohmann::json status();
  nlohmann::json stop();

private:
  struct Impl;
  std::shared_ptr<Impl> impl_;
};
