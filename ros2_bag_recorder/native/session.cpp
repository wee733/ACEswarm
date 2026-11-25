#include "session.hpp"
#include "topic_name.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cctype>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <deque>
#include <exception>
#include <fstream>
#include <functional>
#include <iomanip>
#include <limits>
#include <map>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <vector>
#include <unistd.h>

#include <rcl_interfaces/msg/log.hpp>
#include <rclcpp/event_handler.hpp>
#include <rclcpp/generic_subscription.hpp>
#include <rclcpp/serialization.hpp>
#include <rmw/rmw.h>
#include <rosbag2_compression/sequential_compression_writer.hpp>
#include <rosbag2_cpp/message_definitions/local_message_definition_source.hpp>
#include <rosbag2_cpp/writer.hpp>
#include <rosbag2_storage/serialized_bag_message.hpp>

namespace {
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;
using Message = rclcpp::SerializedMessage;
using namespace std::chrono_literals;
namespace fs = std::filesystem;

int64_t steady_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count();
}

struct Rate {
  struct Bucket { int64_t first = 0; uint64_t count = 0; };
  static constexpr int64_t span = 250000000;
  std::array<Bucket, 16> buckets{};
  void written(int64_t stamp) {
    auto &bucket = buckets[(stamp / span) % buckets.size()];
    if (!bucket.count || bucket.first / span != stamp / span) {
      bucket = {stamp, 0};
    }
    ++bucket.count;
  }
  double hz(int64_t now) const {
    int64_t first = now;
    uint64_t count = 0;
    for (const auto &bucket : buckets) {
      if (bucket.count && bucket.first / span > now / span - static_cast<int64_t>(buckets.size())) {
        first = std::min(first, bucket.first);
        count += bucket.count;
      }
    }
    return count > 1 && now > first ? (count - 1) * 1e9 / static_cast<double>(now - first) : 0;
  }
};

struct Topic {
  rosbag2_storage::TopicMetadata metadata{};
  rosbag2_storage::MessageDefinition definition;
  double max_hz = 0;
  bool save_text = false;
  int64_t last_accepted = -1;
  uint64_t written = 0, bytes = 0, throttled = 0, queue_drops = 0, middleware_lost = 0;
  uint64_t failed = 0, delivered = 0;
  Rate rate;
};

struct Entry {
  Topic *topic;
  std::shared_ptr<Message> message;
  int64_t steady, stamp, source_stamp;
  size_t charge;
  bool write_bag;
  uint64_t sequence;
};

// rclcpp's shared-pointer callback adapter copies a SerializedMessage during
// dispatch. Retain the executor's original buffer at its virtual delivery hook.
class QueuedSubscription final : public rclcpp::GenericSubscription {
public:
  using Delivery = std::function<void(const std::shared_ptr<Message> &, const rclcpp::MessageInfo &)>;
  QueuedSubscription(rclcpp::Node &node, const Topic &topic, const rclcpp::QoS &qos,
                     const rclcpp::SubscriptionOptions &options, Delivery delivery)
    : GenericSubscription(
        node.get_node_base_interface().get(),
        rclcpp::get_typesupport_library(topic.metadata.type, "rosidl_typesupport_cpp"),
        topic.metadata.name, topic.metadata.type, qos,
        rclcpp::AnySubscriptionCallback<Message>().set([](const Message &) {}), options),
      delivery_(std::move(delivery)) {}

  void handle_serialized_message(const std::shared_ptr<Message> &message,
                                 const rclcpp::MessageInfo &info) override {
    delivery_(message, info);
  }

private:
  Delivery delivery_;
};

std::string type_hash(const rosidl_type_hash_t &hash) {
  if (hash.version != 1) return {};
  std::ostringstream result;
  result << "RIHS01_" << std::hex << std::setfill('0');
  for (auto byte : hash.value) result << std::setw(2) << static_cast<unsigned>(byte);
  return result.str();
}

rclcpp::QoS select_qos(const std::vector<rclcpp::TopicEndpointInfo> &endpoints, Topic &topic) {
  bool reliable = !endpoints.empty();
  bool latched = !endpoints.empty();
  for (const auto &endpoint : endpoints) {
    auto qos = endpoint.qos_profile();
    reliable &= qos.reliability() == rclcpp::ReliabilityPolicy::Reliable;
    latched &= qos.durability() == rclcpp::DurabilityPolicy::TransientLocal;
    topic.metadata.offered_qos_profiles.push_back(qos);
    auto hash = type_hash(endpoint.topic_type_hash());
    if (!hash.empty()) {
      auto &previous = topic.metadata.type_description_hash;
      if (!previous.empty() && previous != hash)
        throw std::invalid_argument("发布者消息类型哈希冲突：" + topic.metadata.name);
      previous = hash;
    }
  }
  return rclcpp::QoS(10)
    .reliability(reliable ? rclcpp::ReliabilityPolicy::Reliable : rclcpp::ReliabilityPolicy::BestEffort)
    .durability(latched ? rclcpp::DurabilityPolicy::TransientLocal : rclcpp::DurabilityPolicy::Volatile);
}

// Explicitly disable MCAP's default chunk codec. The plugin reopens this file at each split.
struct McapConfig {
  fs::path path;
  void create() {
    std::string filename = (fs::temp_directory_path() / "ros2-recorder-mcap-XXXXXX").string();
    int fd = ::mkstemp(filename.data());
    if (fd < 0) throw std::system_error(errno, std::generic_category(), "MCAP config");
    path = filename;
    constexpr char contents[] = "compression: \"None\"\n";
    bool complete = ::write(fd, contents, sizeof(contents) - 1) == sizeof(contents) - 1;
    if (::close(fd) != 0 || !complete) throw std::runtime_error("MCAP 配置写入失败");
  }
  ~McapConfig() {
    std::error_code ignored;
    if (!path.empty()) fs::remove(path, ignored);
  }
};

bool interframe_video(const std::string &type, std::string name) {
  if (type == "foxglove_msgs/msg/CompressedVideo" || type == "ffmpeg_image_transport_msgs/msg/FFMPEGPacket")
    return true;
  if (type != "sensor_msgs/msg/CompressedImage") return false;
  std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
  return name.find("h264") != std::string::npos || name.find("h265") != std::string::npos ||
         name.find("hevc") != std::string::npos;
}

const char *log_level(uint8_t level) {
  switch (level) {
    case 10: return "DEBUG";
    case 20: return "INFO";
    case 30: return "WARN";
    case 40: return "ERROR";
    case 50: return "FATAL";
    default: return nullptr;
  }
}
} // namespace

struct RecordingSession::Impl : public std::enable_shared_from_this<RecordingSession::Impl> {
  rclcpp::Node &node;
  const size_t limit;
  std::map<std::string, Topic> topics;
  std::vector<rclcpp::GenericSubscription::SharedPtr> subscriptions;
  McapConfig mcap_config;
  std::unique_ptr<rosbag2_cpp::Writer> writer;
  std::ofstream log, jsonl;
  rclcpp::Serialization<rcl_interfaces::msg::Log> log_decoder;
  std::mutex mutex;
  std::condition_variable ready;
  std::deque<Entry> queue;
  size_t charged = 0, peak = 0;
  bool closing = false, stopped = false;
  std::string error;
  std::thread worker;

  Impl(rclcpp::Node &n, size_t budget) : node(n), limit(budget) {
    if (!limit) throw std::invalid_argument("录制缓冲必须大于零");
  }

  void start(const fs::path &path, const Json &settings) {
    if (fs::symlink_status(path).type() != fs::file_type::not_found)
      throw std::invalid_argument("同名录包已存在");
    const auto format = settings.value("format", std::string("mcap"));
    const auto compression = settings.value("compression", std::string("none"));
    const bool save_logs = settings.value("save_rosout", true);
    if ((format != "mcap" && format != "sqlite3") ||
        (compression != "none" && compression != "zstd_fast" && compression != "zstd_small"))
      throw std::invalid_argument("不支持该存储或压缩格式");
    const auto graph = node.get_topic_names_and_types();
    for (const auto &item : settings.value("topics", Json::array())) {
      const auto name = item.at("name").get<std::string>();
      if (!recorder::recordable_topic_name(name))
        throw std::invalid_argument("无法录制该话题，名称无效或属于内部缓冲通道：" + name);
      auto [it, inserted] = topics.try_emplace(name);
      if (!inserted) throw std::invalid_argument("话题重复：" + name);
      it->second.max_hz = std::max(0.0, item.value("max_hz", 0.0));
    }
    if (save_logs) topics["/rosout"].save_text = true;
    if (topics.empty()) throw std::invalid_argument("请至少选择一个话题或开启日志记录");

    rosbag2_cpp::LocalMessageDefinitionSource definitions;
    for (auto &[name, topic] : topics) {
      const auto it = graph.find(name);
      if (it == graph.end() || it->second.size() != 1)
        throw std::invalid_argument("话题离线或类型冲突：" + name);
      topic.metadata.name = name;
      topic.metadata.type = it->second.front();
      if (topic.max_hz > 0 && interframe_video(topic.metadata.type, name))
        throw std::invalid_argument("编码视频帧之间存在依赖，请将话题 " + name + " 的录制频率设为 0（全速）");
      topic.metadata.serialization_format = rmw_get_serialization_format();
      auto qos = select_qos(node.get_publishers_info_by_topic(name), topic);
      try {
        topic.definition = definitions.get_full_text(topic.metadata.type);
        if (topic.definition.encoded_message_definition.empty())
          throw std::runtime_error("消息定义为空");
        topic.definition.type_hash = topic.metadata.type_description_hash;
        subscribe(topic, qos);
      } catch (const std::exception &e) {
        throw std::invalid_argument("无法加载话题 " + name + " 的消息定义或订阅；请检查 overlay：" + e.what());
      }
    }

    rosbag2_storage::StorageOptions options{};
    options.uri = path.string();
    options.storage_id = format;
    options.max_cache_size = 0;  // The session queue is the only dropping write buffer.
    options.max_bagfile_size = settings.value("split_mb", uint64_t(0)) * 1024 * 1024;
    options.max_bagfile_duration = settings.value("split_seconds", uint64_t(0));
    if (format == "mcap" && compression == "none") {
      mcap_config.create();
      options.storage_config_uri = mcap_config.path.string();
    } else if (format == "mcap") {
      options.storage_preset_profile = compression;
    }
    if (format == "sqlite3" && compression != "none") {
      rosbag2_compression::CompressionOptions c{};
      c.compression_format = "zstd";
      c.compression_mode = compression == "zstd_fast" ? rosbag2_compression::CompressionMode::MESSAGE
                                                     : rosbag2_compression::CompressionMode::FILE;
      c.compression_threads = 1;
      c.compression_queue_size = 0; // rosbag2 applies backpressure to its message compressor.
      writer = std::make_unique<rosbag2_cpp::Writer>(
        std::make_unique<rosbag2_compression::SequentialCompressionWriter>(c));
    } else {
      writer = std::make_unique<rosbag2_cpp::Writer>();
    }
    writer->open(options);
    for (const auto &[name, topic] : topics) writer->create_topic(topic.metadata, topic.definition);
    if (save_logs) {
      log.exceptions(std::ios::badbit | std::ios::failbit);
      jsonl.exceptions(std::ios::badbit | std::ios::failbit);
      log.open(path / "rosout.log");
      jsonl.open(path / "rosout.jsonl");
    }
    worker = std::thread([this] { consume(); });
  }

  void subscribe(Topic &topic, const rclcpp::QoS &qos) {
    auto weak = weak_from_this();
    auto receive = [weak, &topic](const std::shared_ptr<Message> &message, const rclcpp::MessageInfo &info) {
      if (auto self = weak.lock()) self->enqueue(topic, message, info.get_rmw_message_info().source_timestamp);
    };
    rclcpp::SubscriptionOptions options;
    options.event_callbacks.message_lost_callback = [weak, &topic](rclcpp::QOSMessageLostInfo &event) {
      if (auto self = weak.lock()) {
        std::lock_guard<std::mutex> lock(self->mutex);
        if (event.total_count_change > 0) topic.middleware_lost += event.total_count_change;
      }
    };
    auto create = [&] {
      auto subscription = std::make_shared<QueuedSubscription>(node, topic, qos, options, receive);
      node.get_node_topics_interface()->add_subscription(subscription, options.callback_group);
      subscriptions.push_back(std::move(subscription));
    };
    try {
      create();
    } catch (const rclcpp::UnsupportedEventTypeException &) {
      // Some RMWs cannot report message_lost. Recording can still proceed.
      options.event_callbacks.message_lost_callback = {};
      create();
    }
  }

  void enqueue(Topic &topic, const std::shared_ptr<Message> &message, int64_t source_stamp) {
    const auto now = steady_ns();
    std::unique_lock<std::mutex> lock(mutex);
    if (closing) return;
    ++topic.delivered;
    const bool keep = topic.max_hz <= 0 || topic.last_accepted < 0 ||
                      (now - topic.last_accepted) * topic.max_hz >= 1e9;
    if (!keep) {
      ++topic.throttled;
      if (!topic.save_text) return;
    }
    // Account allocated payload capacity and the queue entry, including the active write.
    const auto available = limit - charged;
    constexpr size_t overhead = sizeof(Entry) + sizeof(Message);
    if (available < overhead || message->capacity() > available - overhead) {
      ++topic.queue_drops;
      return;
    }
    const size_t charge = message->capacity() + overhead;
    const auto receipt = node.now().nanoseconds();
    queue.push_back({&topic, message, now, receipt, source_stamp > 0 ? source_stamp : receipt,
                     charge, keep, topic.delivered});
    if (keep) topic.last_accepted = now;
    charged += charge;
    peak = std::max(peak, charged);
    lock.unlock();
    ready.notify_one();
  }

  void write_bag(const Entry &item) {
    auto msg = std::make_shared<rosbag2_storage::SerializedBagMessage>();
    msg->topic_name = item.topic->metadata.name;
    msg->recv_timestamp = item.stamp;
    msg->send_timestamp = item.source_stamp;
    // Alias ownership: the original rclcpp message and its CDR allocation live
    // until the storage/compression plugin releases its final reference.
    msg->serialized_data = std::shared_ptr<rcutils_uint8_array_t>(
      item.message, &item.message->get_rcl_serialized_message());
    writer->write(std::move(msg));
  }

  void write_text(const Entry &item) {
    rcl_interfaces::msg::Log msg;
    log_decoder.deserialize_message(item.message.get(), &msg);
    std::ostringstream stamp;
    stamp << msg.stamp.sec << '.' << std::setfill('0') << std::setw(9) << msg.stamp.nanosec;
    const char *known_level = log_level(msg.level);
    const std::string level = known_level ? known_level : std::to_string(msg.level);
    const auto timestamp = stamp.str();
    log << '[' << timestamp << "] [" << level << "] [" << msg.name << "] " << msg.msg << '\n';
    const Json row = {{"id",item.sequence},{"stamp",timestamp},{"level",level},{"node",msg.name},
                      {"message",msg.msg},{"file",msg.file},{"function",msg.function},{"line",msg.line}};
    jsonl << row.dump(-1, ' ', false, Json::error_handler_t::replace) << '\n';
  }

  void written(const Entry &item) {
    ++item.topic->written;
    item.topic->bytes += item.message->size();
    item.topic->rate.written(item.steady);
  }

  void consume() {
    bool dirty = false;
    auto flush_time = Clock::now();
    try {
      while (true) {
        std::unique_lock<std::mutex> lock(mutex);
        if (dirty && Clock::now() >= flush_time) {
          lock.unlock();
          log.flush();
          jsonl.flush();
          dirty = false;
          continue;
        }
        if (queue.empty()) {
          if (closing) break;
          if (dirty) ready.wait_until(lock, flush_time);
          else ready.wait(lock);
          continue;
        }
        auto item = std::move(queue.front());
        queue.pop_front();
        lock.unlock();
        bool saved = false;
        try {
          if (item.write_bag) {
            write_bag(item);
            saved = true;
          }
          if (item.topic->save_text) {
            write_text(item);
            if (!dirty) flush_time = Clock::now() + 250ms;
            dirty = true;
          }
        } catch (...) {
          lock.lock();
          if (saved) written(item);
          ++item.topic->failed;
          charged -= item.charge;
          throw;
        }
        lock.lock();
        if (saved) written(item);
        charged -= item.charge;
      }
    } catch (const std::exception &e) {
      std::lock_guard<std::mutex> lock(mutex);
      closing = true;
      error = e.what();
      for (auto &item : queue) ++item.topic->failed;
      queue.clear();
      charged = 0;
    }
  }

  void finish() {
    if (stopped) return;
    subscriptions.clear();
    {
      std::lock_guard<std::mutex> lock(mutex);
      closing = true;
    }
    ready.notify_one();
    if (worker.joinable()) worker.join();
    auto close_with_error = [this](auto &resource) {
      try { resource.close(); }
      catch (const std::exception &e) {
        if (!error.empty()) error += "; ";
        error += e.what();
      }
    };
    if (log.is_open()) close_with_error(log);
    if (jsonl.is_open()) close_with_error(jsonl);
    if (writer) {
      close_with_error(*writer);
      writer.reset();
    }
    stopped = true;
  }

  Json status() {
    bool needs_stop;
    {
      std::lock_guard<std::mutex> lock(mutex);
      needs_stop = closing && !stopped;
    }
    if (needs_stop) finish();
    std::lock_guard<std::mutex> lock(mutex);
    Json counters = Json::object();
    const auto now = steady_ns();
    for (const auto &[name, t] : topics) {
      counters[name] = {{"max_hz",t.max_hz},{"written_count",t.written},{"written_bytes",t.bytes},
                        {"dropped_count",t.throttled},{"queue_dropped_count",t.queue_drops},
                        {"middleware_lost_count",t.middleware_lost},{"write_error_count",t.failed},
                        {"actual_hz",t.rate.hz(now)}};
    }
    return {{"recording",!stopped},{"topics",std::move(counters)},{"error",error},
            {"queue_bytes",charged},{"peak_queue_bytes",peak},{"buffer_bytes",limit}};
  }
};

RecordingSession::RecordingSession(rclcpp::Node &node, const fs::path &path, const Json &settings,
                                   size_t budget) : impl_(std::make_shared<Impl>(node, budget)) {
  try { impl_->start(path, settings); }
  catch (...) { impl_->finish(); throw; }
}

RecordingSession::~RecordingSession() { impl_->finish(); }
Json RecordingSession::status() { return impl_->status(); }
Json RecordingSession::stop() { impl_->finish(); return impl_->status(); }
