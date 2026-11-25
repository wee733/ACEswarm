#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "session.hpp"
#include "topic_name.hpp"

#include <rcl_interfaces/msg/log.hpp>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp/typesupport_helpers.hpp>

#include <nlohmann/json.hpp>

#include <chrono>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <future>
#include <iomanip>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>

namespace {
using Json = nlohmann::json;
using Log = rcl_interfaces::msg::Log;
using Task = std::packaged_task<Json()>;

const char * reliability(const rclcpp::QoS & qos) {
  switch (qos.reliability()) {
    case rclcpp::ReliabilityPolicy::Reliable: return "RELIABLE";
    case rclcpp::ReliabilityPolicy::BestEffort: return "BEST_EFFORT";
    case rclcpp::ReliabilityPolicy::SystemDefault: return "SYSTEM_DEFAULT";
    default: return "UNKNOWN";
  }
}
const char * durability(const rclcpp::QoS & qos) {
  switch (qos.durability()) {
    case rclcpp::DurabilityPolicy::TransientLocal: return "TRANSIENT_LOCAL";
    case rclcpp::DurabilityPolicy::Volatile: return "VOLATILE";
    case rclcpp::DurabilityPolicy::SystemDefault: return "SYSTEM_DEFAULT";
    default: return "UNKNOWN";
  }
}
std::string log_level(uint8_t level) {
  switch (level) {
    case Log::DEBUG: return "DEBUG";
    case Log::INFO: return "INFO";
    case Log::WARN: return "WARN";
    case Log::ERROR: return "ERROR";
    case Log::FATAL: return "FATAL";
    default: return std::to_string(level);
  }
}
Json log_row(uint64_t id, const Log & msg) {
  std::ostringstream stamp;
  stamp << msg.stamp.sec << '.' << std::setw(9) << std::setfill('0') << msg.stamp.nanosec;
  return {{"id", id}, {"stamp", stamp.str()}, {"level", log_level(msg.level)},
    {"node", msg.name}, {"message", msg.msg}, {"file", msg.file},
    {"function", msg.function}, {"line", msg.line}};
}

class NativeRecorder {
public:
  explicit NativeRecorder(bool use_sim_time) : context_(std::make_shared<rclcpp::Context>()) {
    context_->init(0, nullptr);
    rclcpp::NodeOptions node_options;
    node_options.context(context_).enable_rosout(false).append_parameter_override("use_sim_time", use_sim_time);
    node_ = std::make_shared<rclcpp::Node>("ros2_bag_recorder", node_options);
    logs_sub_ = node_->create_subscription<Log>("/rosout", rclcpp::QoS(10).best_effort().durability_volatile(),
      [this](Log::ConstSharedPtr msg) {
        if (logs_.size() == 1000) logs_.pop_front();
        logs_.emplace_back(++log_id_, std::move(msg));
      });
    rclcpp::ExecutorOptions executor_options;
    executor_options.context = context_;
    executor_ = std::make_unique<rclcpp::executors::SingleThreadedExecutor>(executor_options);
    executor_->add_node(node_);
    worker_ = std::thread(&NativeRecorder::spin, this);
  }
  ~NativeRecorder() { try { close(); } catch (...) {} }

  Json topics() {
    return submit([this]() {
      Json rows = Json::array();
      for (const auto & [name, types] : node_->get_topic_names_and_types()) {
        if (!recorder::recordable_topic_name(name)) continue;
        std::vector<rclcpp::TopicEndpointInfo> endpoints;
        size_t subscribers;
        try {
          endpoints = node_->get_publishers_info_by_topic(name);
          subscribers = node_->count_subscribers(name);
        } catch (const rclcpp::exceptions::InvalidTopicNameError &) {
          continue;
        }
        Json publishers = Json::array();
        bool all_reliable = !endpoints.empty(), all_latched = !endpoints.empty();
        for (const auto & endpoint : endpoints) {
          const auto & qos = endpoint.qos_profile();
          all_reliable &= qos.reliability() == rclcpp::ReliabilityPolicy::Reliable;
          all_latched &= qos.durability() == rclcpp::DurabilityPolicy::TransientLocal;
          publishers.push_back({{"node", endpoint.node_name()}, {"namespace", endpoint.node_namespace()},
            {"reliability", reliability(qos)}, {"durability", durability(qos)}});
        }
        std::string label;
        for (const auto & type : types) {
          if (!label.empty()) label += ", ";
          label += type;
        }
        rows.push_back({{"name", name}, {"type", label}, {"types", types},
          {"publishers", endpoints.size()}, {"subscribers", subscribers},
          {"reliability", all_reliable ? "RELIABLE" : "BEST_EFFORT"},
          {"durability", all_latched ? "TRANSIENT_LOCAL" : "VOLATILE"},
          {"publisher_qos", std::move(publishers)},
          {"type_available", types.size() == 1 && type_available(types.front())}});
      }
      return rows;
    });
  }
  Json logs(uint64_t since) {
    return submit([this, since]() {
      Json result = Json::array();
      for (const auto & [id, msg] : logs_) if (id > since) result.push_back(log_row(id, *msg));
      return result;
    });
  }
  Json start(const std::string & path, const Json & settings) {
    return submit([this, &path, &settings]() {
      if (session_ && session_stats(false).at("recording").get<bool>()) {
        throw std::runtime_error("已有录制正在进行");
      }
      const auto mib = settings.at("buffer_mib").get<uint64_t>();
      if (mib == 0 || mib > SIZE_MAX / (1024 * 1024)) throw std::invalid_argument("录制缓冲区大小无效");
      session_ = std::make_unique<RecordingSession>(*node_, std::filesystem::path(path), settings,
        static_cast<size_t>(mib * 1024 * 1024));
      return session_stats(false);
    });
  }
  Json stats() { return submit([this]() { return session_stats(false); }); }
  Json stop() { return submit([this]() { return session_stats(true); }); }
  void close() {
    // Serialize concurrent close()/destruction; other callers wait only on their own native futures.
    std::lock_guard<std::mutex> shutdown_lock(close_mutex_);
    if (!worker_.joinable()) return;
    std::future<Json> drained;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      accepting_ = false;
      if (!exited_) {
        Task task([this]() { exiting_ = true; return session_stats(true); });
        drained = task.get_future(); tasks_.push_back(std::move(task));
        wake();
      }
    }
    worker_.join();
    session_.reset(); logs_sub_.reset(); executor_.reset(); node_.reset();
    if (context_->is_valid()) context_->shutdown("recorder closed");
    if (drained.valid()) drained.get();
  }

private:
  bool type_available(const std::string & type) {
    if (auto cached = type_cache_.find(type); cached != type_cache_.end()) return cached->second;
    bool available;
    try {
      const std::string id = "rosidl_typesupport_cpp";
      auto library = rclcpp::get_typesupport_library(type, id);
      available = rclcpp::get_message_typesupport_handle(type, id, *library) != nullptr;
    } catch (const std::exception &) { available = false; }
    type_cache_.emplace(type, available);
    return available;
  }
  Json session_stats(bool stopping) {
    if (session_) {
      auto result = stopping ? session_->stop() : session_->status();
      if (result.at("recording").get<bool>()) return result;
      last_ = std::move(result); session_.reset();
    }
    return last_;
  }
  void wake() noexcept {
    // Wake the ROS wait set for control operations while leaving message delivery event-driven.
    // A bounded spin timeout also covers contexts that are in the process of shutting down.
    try { node_->get_node_base_interface()->trigger_notify_guard_condition(); } catch (...) {}
  }
  template<class F> Json submit(F && action) {
    Task task(std::forward<F>(action));
    auto result = task.get_future();
    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (!accepting_) throw std::runtime_error("ROS 录制器已关闭");
      tasks_.push_back(std::move(task)); wake();
    }
    return result.get();
  }
  void spin() noexcept {
    try {
      std::deque<Task> ready;
      while (!exiting_ && context_->is_valid()) {
        {
          std::lock_guard<std::mutex> lock(mutex_);
          if (!tasks_.empty()) ready.swap(tasks_);
        }
        for (auto & task : ready) task();
        ready.clear();
        if (!exiting_) executor_->spin_once(std::chrono::milliseconds(500));
      }
    } catch (const std::exception & exc) {
      RCLCPP_ERROR(node_->get_logger(), "Recorder executor failed: %s", exc.what());
    }
    std::lock_guard<std::mutex> lock(mutex_);
    exited_ = true; accepting_ = false;
    tasks_.clear();  // Wake pending callers with broken-promise if the executor failed.
  }

  rclcpp::Context::SharedPtr context_;
  rclcpp::Node::SharedPtr node_;
  rclcpp::Subscription<Log>::SharedPtr logs_sub_;
  std::unique_ptr<rclcpp::executors::SingleThreadedExecutor> executor_;
  std::unique_ptr<RecordingSession> session_;
  Json last_ = {{"recording", false}, {"error", ""}, {"topics", Json::object()}};
  std::map<std::string, bool> type_cache_;
  std::deque<std::pair<uint64_t, Log::ConstSharedPtr>> logs_;
  uint64_t log_id_ = 0;
  std::thread worker_;
  std::mutex mutex_, close_mutex_;
  std::deque<Task> tasks_;
  bool accepting_ = true, exited_ = false, exiting_ = false;
};

// Python only initializes sessions and exchanges control/status structures. The ROS executor,
// serialized subscriptions and disk writer never acquire the GIL or construct Python objects.
class ReleaseGil {
public:
  ReleaseGil() : state_(PyEval_SaveThread()) {}
  ~ReleaseGil() { PyEval_RestoreThread(state_); }
  ReleaseGil(const ReleaseGil &) = delete;
  ReleaseGil & operator=(const ReleaseGil &) = delete;
private:
  PyThreadState * state_;
};
struct Decref { void operator()(PyObject * object) const { Py_XDECREF(object); } };
using PyRef = std::unique_ptr<PyObject, Decref>;

PyObject * to_python(const Json & value) {
  if (value.is_object()) {
    PyRef dict(PyDict_New());
    if (!dict) return nullptr;
    for (const auto & [key, element] : value.items()) {
      PyRef item(to_python(element));
      if (!item || PyDict_SetItemString(dict.get(), key.c_str(), item.get()) < 0) return nullptr;
    }
    return dict.release();
  }
  if (value.is_array()) {
    PyRef list(PyList_New(static_cast<Py_ssize_t>(value.size())));
    if (!list) return nullptr;
    Py_ssize_t i = 0;
    for (const auto & element : value) {
      PyObject * item = to_python(element);
      if (!item) return nullptr;
      PyList_SET_ITEM(list.get(), i++, item);  // steals reference
    }
    return list.release();
  }
  if (value.is_string()) {
    const auto & data = value.get_ref<const std::string &>();
    return PyUnicode_DecodeUTF8(data.data(), static_cast<Py_ssize_t>(data.size()), "replace");
  }
  if (value.is_boolean()) return PyBool_FromLong(value.get<bool>());
  if (value.is_number_unsigned()) return PyLong_FromUnsignedLongLong(value.get<uint64_t>());
  if (value.is_number_integer()) return PyLong_FromLongLong(value.get<int64_t>());
  if (value.is_number_float()) return PyFloat_FromDouble(value.get<double>());
  Py_RETURN_NONE;
}
void translate_exception() {
  try { throw; }
  catch (const std::bad_alloc &) { PyErr_NoMemory(); }
  catch (const std::invalid_argument & exc) { PyErr_SetString(PyExc_ValueError, exc.what()); }
  catch (const Json::exception & exc) { PyErr_SetString(PyExc_ValueError, exc.what()); }
  catch (const std::exception & exc) { PyErr_SetString(PyExc_RuntimeError, exc.what()); }
  catch (...) { PyErr_SetString(PyExc_RuntimeError, "Native recorder failed"); }
}
struct RecorderObject { PyObject_HEAD NativeRecorder * host; };
int init(RecorderObject * self, PyObject * args, PyObject * kwargs) {
  static char keyword[] = "use_sim_time";
  static char * names[] = {keyword, nullptr};
  int sim = 0;
  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|p:NativeRecorder", names, &sim)) return -1;
  if (self->host) { PyErr_SetString(PyExc_RuntimeError, "Recorder already initialized"); return -1; }
  try {
    NativeRecorder * host;
    { ReleaseGil release; host = new NativeRecorder(sim != 0); }
    self->host = host;
    return 0;
  } catch (...) { translate_exception(); return -1; }
}
void dealloc(RecorderObject * self) {
  { ReleaseGil release; delete self->host; }
  PyTypeObject * type = Py_TYPE(self);
  type->tp_free(reinterpret_cast<PyObject *>(self));
  Py_DECREF(type);  // heap type from PyType_FromSpec
}
template<class F> PyObject * invoke(RecorderObject * self, F && operation) {
  if (!self->host) { PyErr_SetString(PyExc_RuntimeError, "Recorder is uninitialized"); return nullptr; }
  try {
    Json result;
    { ReleaseGil release; result = operation(*self->host); }
    return to_python(result);
  } catch (...) { translate_exception(); return nullptr; }
}
PyObject * topics(RecorderObject * self, PyObject *) {
  return invoke(self, [](auto & host) { return host.topics(); });
}
PyObject * stats(RecorderObject * self, PyObject *) {
  return invoke(self, [](auto & host) { return host.stats(); });
}
PyObject * stop(RecorderObject * self, PyObject *) {
  return invoke(self, [](auto & host) { return host.stop(); });
}
PyObject * close(RecorderObject * self, PyObject *) {
  return invoke(self, [](auto & host) { host.close(); return Json(nullptr); });
}
PyObject * logs(RecorderObject * self, PyObject * args) {
  unsigned long long since = 0;
  if (!PyArg_ParseTuple(args, "|K:logs", &since)) return nullptr;
  return invoke(self, [since](auto & host) { return host.logs(since); });
}
PyObject * start(RecorderObject * self, PyObject * args) {
  const char * path;
  PyObject * settings;
  if (!PyArg_ParseTuple(args, "sO!:start", &path, &PyDict_Type, &settings)) return nullptr;
  // Settings are small and cross this boundary just once per session.
  PyRef json(PyImport_ImportModule("json"));
  if (!json) return nullptr;
  PyRef encoded(PyObject_CallMethod(json.get(), "dumps", "O", settings));
  if (!encoded) return nullptr;
  const char * utf8 = PyUnicode_AsUTF8(encoded.get());
  if (!utf8) return nullptr;
  return invoke(self, [path, utf8](auto & host) { return host.start(path, Json::parse(utf8)); });
}
PyMethodDef methods[] = {
  {"topics", reinterpret_cast<PyCFunction>(topics), METH_NOARGS, "Discover topics and publisher QoS."},
  {"stats", reinterpret_cast<PyCFunction>(stats), METH_NOARGS, "Get the current session statistics."},
  {"start", reinterpret_cast<PyCFunction>(start), METH_VARARGS, "Start a recording at a new path."},
  {"stop", reinterpret_cast<PyCFunction>(stop), METH_NOARGS, "Drain and finalize the current bag."},
  {"close", reinterpret_cast<PyCFunction>(close), METH_NOARGS, "Stop and shut down the private ROS context."},
  {"logs", reinterpret_cast<PyCFunction>(logs), METH_VARARGS, "Get live ROS logs after a sequence number."},
  {nullptr, nullptr, 0, nullptr}
};
PyType_Slot slots[] = {
  {Py_tp_new, reinterpret_cast<void *>(PyType_GenericNew)},
  {Py_tp_init, reinterpret_cast<void *>(init)},
  {Py_tp_dealloc, reinterpret_cast<void *>(dealloc)},
  {Py_tp_methods, methods}, {0, nullptr}
};
PyType_Spec type_spec = {"_recorder_native.NativeRecorder", sizeof(RecorderObject), 0,
  Py_TPFLAGS_DEFAULT, slots};
PyModuleDef module = {PyModuleDef_HEAD_INIT, "_recorder_native", nullptr, -1, nullptr};
}  // namespace

PyMODINIT_FUNC PyInit__recorder_native() {
  PyRef mod(PyModule_Create(&module));
  if (!mod) return nullptr;
  PyRef type(PyType_FromSpec(&type_spec));
  if (!type || PyModule_AddObjectRef(mod.get(), "NativeRecorder", type.get()) < 0) return nullptr;
  return mod.release();
}
