"""Run with the Foxglove app venv, passing the unpacked package directory."""
import dataclasses
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(sys.argv[1]).resolve() / "backend"))
from visualize_foxglove_server import visualizer_rotor as rotor
from visualize_foxglove_server.foxglove_sdk_server import FoxgloveSdkDisplayServer

for robot_type in ("mocap_rotor", "uxrce_mocap", "uxrce_fastlio2", "custom_mocap"):
    assert FoxgloveSdkDisplayServer._supports_metadata({"robot_type": robot_type})
    runtime = rotor.runtime_from_metadata({"robot_type": robot_type})
    assert rotor.metadata_from_runtime(rotor.merge_runtime_metadata(runtime, {}))["robot_type"] == robot_type
for robot_type in ("ground_station", "fixed_wing"):
    assert not FoxgloveSdkDisplayServer._supports_metadata({"robot_type": robot_type})

robot = rotor.RotorDroneVisualizer(rotor.rotor_robot_config_from_name("simulator"), rotor.RotorDisplayRuntime())
attitude = dict(x=0., y=0., z=0., w=1.)
good = dict(position=dict(x=1., y=2., z=3.), orientation=attitude)
with patch.object(rotor.foxglove, "log") as output, patch.object(rotor.time, "monotonic") as clock:
    clock.return_value = 10.
    for pos in (None, {}, dict(x=None, y=2., z=3.), dict(x=1., y=float("nan"), z=3.)):
        robot.update_stream("local_pos", {"data": dict(good, position=pos)})
        robot.publish(100., .05, 5.)
        assert not output.called and not robot.history
    for pose_health in ({"fresh": False}, {"stale": True}, {"received": False}):
        robot.update_stream("local_pos", {"data": good, "sources": {"pose": pose_health}})
        robot.publish(100., .05, 5.)
        assert not output.called
    robot.update_stream("local_pos", {"data": dict(good, orientation=None)})
    robot.publish(100., .05, 5.)
    assert not output.called

    robot.update_stream("local_pos", {"data": good, "timestamp_unix_ms": 500})
    robot.publish(100., .05, 5.)
    robot.publish(100.05, .05, 5.)
    assert len(robot.history) == 1 and output.called
    output.reset_mock()
    clock.return_value = 11.
    robot.update_stream("state", {"data": {"armed": False}})
    robot.publish(101., .05, 5.)
    assert not output.called and not robot.history

    robot.update_runtime(dataclasses.replace(robot.runtime, position_source="gps"))
    robot.update_stream("local_pos", {"data": good})
    robot.update_stream("gps", {"data": {"global": {"lat": 39., "lon": 116., "alt": None}}})
    robot.publish(101., .05, 5.)
    assert not output.called
    robot.update_stream("gps", {"data": {"global": {"lat": 39., "lon": 116., "alt": 60.}}})
    robot.publish(101., .05, 5.)
    assert output.called and robot.history[-1][1].z == 60. - robot.runtime.gps_origin.altitude

print("PASS: invalid, stale and missing poses suppressed; distinct fresh samples form the trail")
