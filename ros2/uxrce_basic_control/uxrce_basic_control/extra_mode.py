"""Own one PX4 ROS 2 Interface Library registration per running query server."""
import os
from pathlib import Path
import select
import subprocess
import threading


class ExtraMode:
    def __init__(self, px4_namespace):
        namespace = px4_namespace.strip('/')
        self._prefix = f'/{namespace}/' if namespace else '/'
        self._lock = threading.Lock()
        self._process = None
        self._id = None
        self._read_fd = None
        self._closed = False

    def _read_result(self, timeout):
        if self._read_fd is None:
            return
        ready, _, _ = select.select([self._read_fd], [], [], timeout)
        if ready:
            result = os.read(self._read_fd, 1)
            os.close(self._read_fd)
            self._read_fd = None
            if result and 23 <= result[0] <= 30:
                self._id = result[0]

    def registered_id(self):
        # Never block the ROS setpoint publisher on DDS discovery.
        if not self._lock.acquire(blocking=False):
            return None
        try:
            self._read_result(0)
            if self._process is not None and self._process.poll() is None:
                return self._id
            return None
        finally:
            self._lock.release()

    def register(self):
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError('Control service is stopping')
                if self._process is None or self._process.poll() is not None:
                    self._stop()
                    self._start()
                # The HTTP query is bounded to 10s. Keep an in-progress PX4
                # compatibility check alive across calls, sharing one registration.
                self._read_result(6)
                if self._process.poll() is not None or (self._id is None and self._read_fd is None):
                    raise RuntimeError('PX4 external mode registration failed; see app log')
                if self._id is None:
                    return dict(ok=False, command='register_extra', result_code=None, state='registering',
                                message='等待 PX4 消息兼容性检查和注册回执，请再次点击 Register Extra 查询')
                return dict(ok=True, command='register_extra', result_code=None, nav_state=self._id,
                            message=f'PX4 registered ACEswarm Hold as EXTERNAL{self._id - 22}')
        except (OSError, RuntimeError, LookupError) as error:
            return dict(ok=False, command='register_extra', result_code=None, message=str(error))

    def _start(self):
        helper = os.environ.get('UXRCE_EXTRA_HOLD')
        if not helper:
            from ament_index_python.packages import get_package_prefix
            helper = Path(get_package_prefix('uxrce_basic_control')) / 'lib/uxrce_basic_control/extra_hold'
        read_fd, write_fd = os.pipe()
        try:
            self._process = subprocess.Popen(
                [str(helper), self._prefix, str(write_fd), str(os.getpid())], pass_fds=(write_fd,))
        except OSError:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        self._read_fd = read_fd

    def _stop(self):
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        if self._read_fd is not None:
            os.close(self._read_fd)
        self._read_fd = None
        self._id = None
        self._process = None

    def close(self):
        with self._lock:
            self._closed = True
            self._stop()
