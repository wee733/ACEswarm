#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <optional>
#include <sys/prctl.h>
#include <unistd.h>

#include <px4_ros2/components/mode.hpp>
#include <px4_ros2/control/setpoint_types/multicopter/goto.hpp>
#include <px4_ros2/odometry/local_position.hpp>
#include <rclcpp/rclcpp.hpp>

class ExtraHold final : public px4_ros2::ModeBase {
 public:
  ExtraHold(rclcpp::Node &node, const std::string &topic_prefix)
      : ModeBase(node, Settings{"ACEswarm Hold"}.preventArming(true), topic_prefix),
        position_(std::make_shared<px4_ros2::OdometryLocalPosition>(*this)),
        hold_(std::make_shared<px4_ros2::MulticopterGotoSetpointType>(*this)) {}

  void checkArmingAndRunConditions(px4_ros2::HealthAndArmingCheckReporter &reporter) override {
    if (!positionValid()) {
      reporter.armingCheckFailureExt(px4_ros2::events::ID("aceswarm_hold_local_position"),
                                     px4_ros2::events::Log::Error, "Local position unavailable");
    }
  }

  void onActivate() override { target_.reset(); }

  void updateSetpoint(float) override {
    if (!positionValid()) {
      target_.reset();
      return;
    }
    const auto &p = position_->last();
    if (!target_ || xy_reset_ != p.xy_reset_counter || z_reset_ != p.z_reset_counter) {
      target_ = position_->positionNed();
      xy_reset_ = p.xy_reset_counter;
      z_reset_ = p.z_reset_counter;
    }
    hold_->update(*target_);
  }

 private:
  bool positionValid() const {
    return position_->positionXYValid() && position_->positionZValid() &&
           position_->positionNed().allFinite();
  }

  std::shared_ptr<px4_ros2::OdometryLocalPosition> position_;
  std::shared_ptr<px4_ros2::MulticopterGotoSetpointType> hold_;
  std::optional<Eigen::Vector3f> target_;
  uint8_t xy_reset_{}, z_reset_{};
};

int main(int argc, char **argv) {
  if (argc != 4) return 2;
  const std::string prefix = argv[1];
  const int result_fd = std::stoi(argv[2]);
  // An ungraceful exit of the query server must also release its external mode.
  if (prctl(PR_SET_PDEATHSIG, SIGTERM) != 0 || getppid() != std::stoi(argv[3])) return 2;
  try {
    rclcpp::init(0, nullptr);
    auto node = std::make_shared<rclcpp::Node>("uxrce_extra_hold");
    auto mode = std::make_shared<ExtraHold>(*node, prefix);
    if (!mode->doRegister()) return 1;
    const uint8_t mode_id = mode->id();
    if (write(result_fd, &mode_id, sizeof(mode_id)) != sizeof(mode_id)) return 1;
    close(result_fd);
    rclcpp::spin(node);
    mode.reset();
    if (rclcpp::ok()) rclcpp::shutdown();
  } catch (const std::exception &err) {
    std::fprintf(stderr, "PX4 external mode: %s\n", err.what());
    return 1;
  }
  return 0;
}
