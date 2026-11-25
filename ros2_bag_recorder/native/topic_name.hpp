#pragma once

#include <stdexcept>
#include <string_view>
#include <rmw/validate_full_topic_name.h>

namespace recorder {

// A DDS graph may include transport channels that cannot be queried or
// subscribed to as ROS topics. Fast DDS uses <base>/_buf/<GID in hex> for
// per-subscriber descriptors; the separate _buf_cpu channel carries CDR.
inline bool recordable_topic_name(std::string_view name) {
  if (name.empty()) return false;
  int result;
  if (rmw_validate_full_topic_name_with_size(name.data(), name.size(), &result, nullptr) != RMW_RET_OK)
    throw std::runtime_error("无法验证 ROS 话题名称");
  if (result != RMW_TOPIC_VALID) return false;

  constexpr std::string_view prefix = "/_buf/";
  constexpr size_t gid_chars = 2 * RMW_GID_STORAGE_SIZE;
  const auto index = name.rfind(prefix);
  if (index == std::string_view::npos) return true;
  const auto suffix = name.substr(index + prefix.size());
  return suffix.size() != gid_chars ||
         suffix.find_first_not_of("0123456789abcdefABCDEF") != std::string_view::npos;
}

} // namespace recorder
