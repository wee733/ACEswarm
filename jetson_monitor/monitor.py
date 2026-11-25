"""Jetson health samples and explicit hardware updates through a single jtop client."""
import argparse
import json
import logging
import math
import re
import signal
import threading
import time
import urllib.request
from contextlib import contextmanager


GATEWAY = 'http://127.0.0.1:17447'
CONTROL_TIMEOUT = 12
AGENT_ID = re.compile(r'[A-Za-z0-9_.-]+')
DEFAULT_INTERVAL = 2.0
# System proxy settings for external traffic must not route loopback telemetry.
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def number(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
        return round(value, 2)
    return None


def data_unavailable():
    return {
        'status': 'unavailable', 'cpu_pct': None, 'gpu_pct': None, 'ram_pct': None,
        'ram_used_mib': None, 'ram_total_mib': None, 'power_w': None,
        'temp_max_c': None, 'temp_soc_c': None, 'temperatures_c': {}, 'fans': {},
        'clocks': {'enabled': None, 'status': 'unavailable'}, 'nvpmodel': None,
        'uptime_s': None,
    }


def first_value(values):
    return number(values[0]) if values else None


def read_sample(board):
    sample = data_unavailable()
    idle = number(board.cpu.get('total', {}).get('idle'))
    # jtop reports RAM sizes in KiB and rail power in mW.
    ram = board.memory.get('RAM', {})
    used, total = number(ram.get('used')), number(ram.get('tot'))
    rail = board.power.get('tot', {})
    power = number(rail.get('power')) if rail.get('online', True) else None
    temperatures = {}
    for name, sensor in board.temperature.items():
        value = number(sensor.get('temp'))
        if sensor.get('online', True) and value is not None:
            temperatures[name] = value
    gpu_loads = [number(gpu.get('status', {}).get('load')) for gpu in board.gpu.values()]
    fans = {}
    for name, fan in board.fan.items():
        fans[name] = {
            'profile': fan.get('profile'), 'profiles': list(board.fan.all_profiles(name)),
            'speed_pct': first_value(fan.get('speed')), 'rpm': first_value(fan.get('rpm')),
        }
    clocks = board.jetson_clocks
    if clocks is not None:
        sample['clocks'] = {'enabled': bool(clocks), 'status': clocks.status}
    sample.update(
        status='ok', cpu_pct=number(100 - idle) if idle is not None else None,
        gpu_pct=max((load for load in gpu_loads if load is not None), default=None),
        ram_pct=number(100 * used / total) if used is not None and total and total > 0 else None,
        ram_used_mib=number(used / 1024) if used is not None else None,
        ram_total_mib=number(total / 1024) if total is not None else None,
        temp_max_c=max(temperatures.values(), default=None),
        temp_soc_c=temperatures.get('tj'), temperatures_c=temperatures,
        power_w=number(power / 1000) if power is not None else None,
        nvpmodel=board.nvpmodel.name if board.nvpmodel is not None else None,
        uptime_s=int(board.uptime.total_seconds()), fans=fans,
    )
    return sample


def get_json(path, body=None):
    request = urllib.request.Request(GATEWAY + path)
    if body is not None:
        request.add_header('Content-Type', 'application/json')
        request.data = json.dumps(body, allow_nan=False).encode()
    with LOCAL_HTTP.open(request, timeout=1.5) as response:
        result = json.load(response)
    if result.get('ok') is not True:
        raise RuntimeError(result.get('message') or 'Zenoh 网关未接受请求')
    return result


def open_jtop(interval):
    import jtop

    if getattr(jtop, '__version__', '') != '7.2.2':
        return jtop.jtop(interval=interval)

    class TelemetryClient(jtop.jtop):
        def _load_jetson_libraries(self):
            # 7.2.2 probes library versions with CDLL, including TensorRT.
            # No monitor reading or control uses the optional board inventory.
            self._board.update(platform={}, libraries={})

    return TelemetryClient(interval=interval)


class Monitor:
    def __init__(self, agent_id='', interval=DEFAULT_INTERVAL):
        self.agent_id = agent_id
        self.interval = interval
        self._max_age = max(4.0, interval * 3)
        self.stop = threading.Event()
        self._condition = threading.Condition()
        self._control = threading.Lock()
        self._board = None
        self._data = data_unavailable()
        self._received = 0
        self._timestamp = 0
        self._error = '等待 jtop 样本'
        self._publish = {'ok': None, 'error': None}

    def snapshot(self):
        with self._condition:
            valid = self._fresh()
            return {
                'agent_id': self.agent_id, 'timestamp_unix_ms': self._timestamp,
                'stale': not valid, 'data': self._data if valid else data_unavailable(),
                'error': None if valid else (self._error or 'jtop 样本已过期'),
                'publish': self._publish.copy(),
            }

    def _fresh(self):
        return self._board is not None and time.monotonic() - self._received < self._max_age

    def _record(self, board, data):
        with self._condition:
            self._board = board
            self._data = data
            self._received = time.monotonic()
            self._timestamp = time.time_ns() // 1_000_000
            self._error = None
            self._condition.notify_all()

    def sample_loop(self):
        delay = 1
        while not self.stop.is_set():
            try:
                with open_jtop(self.interval) as board:
                    while not self.stop.is_set() and board.ok():
                        started = time.monotonic()
                        self._record(board, read_sample(board))
                        delay = 1
                        # Other jtop clients may drive the shared daemon at a higher rate.
                        self.stop.wait(max(0, self.interval - (time.monotonic() - started)))
                    if not self.stop.is_set():
                        raise RuntimeError('jtop 服务已断开')
            except Exception as exc:
                with self._condition:
                    message = str(exc) or type(exc).__name__
                    if self._error != message:
                        logging.warning('jtop: %s', message)
                    self._error = message
            finally:
                with self._condition:
                    self._board = None
                    self._condition.notify_all()
            self.stop.wait(delay)
            delay = min(10, 2 * delay)

    def publish_loop(self, configured_id):
        refresh_id_at = 0
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                if not configured_id and start >= refresh_id_at:
                    current = get_json('/status').get('local_agent_id')
                    if not isinstance(current, str) or not AGENT_ID.fullmatch(current):
                        raise RuntimeError('Zenoh 网关未配置有效的 local_agent_id')
                    with self._condition:
                        self.agent_id = current
                    refresh_id_at = start + 30
                state = self.snapshot()
                body = {key: state[key] for key in ('agent_id', 'timestamp_unix_ms', 'stale', 'data')}
                body.update(schema_version=1, stream='jetson', source_variant='jtop')
                get_json('/zenoh/put', {'key_expr': body['agent_id'] + '/telemetry/jetson',
                                       'payload': json.dumps(body, allow_nan=False),
                                       'encoding': 'application/json'})
                result = {'ok': True, 'error': None}
            except Exception as exc:
                result = {'ok': False, 'error': str(exc)}
                refresh_id_at = 0
            with self._condition:
                if self._publish['ok'] != result['ok']:
                    logging.info('Zenoh: %s', result['error'] or '已连接')
                self._publish = result
            self.stop.wait(max(0.05, self.interval - (time.monotonic() - start)))

    @contextmanager
    def _change(self):
        if not self._control.acquire(blocking=False):
            raise RuntimeError('另一项设置正在等待设备确认')
        try:
            with self._condition:
                if not self._fresh() or self.stop.is_set():
                    raise RuntimeError('jtop 未就绪')
                yield self._board, time.monotonic() + CONTROL_TIMEOUT
        finally:
            self._control.release()

    def _confirm(self, board, deadline, matches):
        while True:
            if self._board is not board or not self._fresh() or self.stop.is_set():
                raise RuntimeError('jtop 样本已中断，请检查当前设置')
            if matches(self._data):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('设备尚未确认更改，请检查当前设置')
            self._condition.wait(remaining)

    def set_clocks(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('锁频开关必须为布尔值')
        with self._change() as (board, deadline):
            active = self._data['clocks']
            if active['status'] not in ('running', 'inactive'):
                raise RuntimeError('当前时钟状态无法更改：' + active['status'])
            if active['enabled'] is enabled:
                return
            board.jetson_clocks = enabled
            self._confirm(board, deadline, lambda data: data['clocks']['enabled'] is enabled
                          and data['clocks']['status'] == ('running' if enabled else 'inactive'))

    def set_fan(self, name, profile, speed=None):
        if not isinstance(name, str) or not isinstance(profile, str):
            raise ValueError('风扇名称与模式必须是字符串')
        if speed is not None and (type(speed) is not int or not 0 <= speed <= 100 or profile != 'manual'):
            raise ValueError('手动风扇速度必须为 0 到 100 的整数')
        with self._change() as (board, deadline):
            fan = self._data['fans'].get(name)
            if not fan or profile not in fan['profiles']:
                raise ValueError('该设备没有指定的风扇或模式')
            if profile == 'manual' and (speed is None or fan['speed_pct'] is None):
                raise ValueError('请明确指定手动风扇的可用 PWM 通道速度')
            if fan['profile'] != profile:
                board.fan.set_profile(name, profile)
                self._confirm(board, deadline, lambda data: data['fans'].get(name, {}).get('profile') == profile)
            if speed is not None:
                # PWM readback may be quantized to a 0..255 hardware step.
                def matches(data):
                    state = data['fans'].get(name, {})
                    measured = state.get('speed_pct')
                    return state.get('profile') == profile and measured is not None and abs(measured - speed) < 0.5
                if not matches(self._data):
                    board.fan.set_speed(name, speed, idx=0)
                    self._confirm(board, deadline, matches)

    def apply_startup_settings(self, clocks_mode, fan_profile, speed):
        if clocks_mode == 'keep' and fan_profile == 'keep':
            logging.info('锁频与风扇均保留当前状态')
            return
        with self._condition:
            self._condition.wait_for(lambda: self.stop.is_set() or self._fresh())
            if self.stop.is_set():
                return
        if clocks_mode != 'keep':
            try:
                self.set_clocks(clocks_mode == 'enable')
                logging.info('设备已确认锁频设置 %s', clocks_mode)
            except Exception as exc:
                logging.error('锁频设置 %s 未确认：%s', clocks_mode, exc)
        if fan_profile != 'keep' and not self.stop.is_set():
            try:
                fans = self.snapshot()['data']['fans']
                if 'pwmfan' in fans:
                    name = 'pwmfan'
                elif len(fans) == 1:
                    name = next(iter(fans))
                else:
                    raise RuntimeError('未找到可唯一确定的 PWM 风扇')
                self.set_fan(name, fan_profile, speed if fan_profile == 'manual' else None)
                logging.info('设备已确认风扇 %s = %s', name, self.snapshot()['data']['fans'][name])
            except Exception as exc:
                logging.error('风扇设置 %s 未确认：%s', fan_profile, exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--agent-id', default='')
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL)
    parser.add_argument('--clocks-mode', choices=['keep', 'enable', 'disable'], default='keep')
    parser.add_argument('--fan-profile', choices=['keep', 'quiet', 'cool', 'manual'], default='keep')
    parser.add_argument('--fan-speed-pct', type=int, default=100)
    args = parser.parse_args()
    if args.agent_id and not AGENT_ID.fullmatch(args.agent_id):
        parser.error('invalid agent ID')
    if not 0.5 <= args.interval <= 3.0:
        parser.error('interval must be 0.5 to 3 seconds')
    if not 0 <= args.fan_speed_pct <= 100:
        parser.error('fan speed must be 0 to 100 percent')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    monitor = Monitor(args.agent_id, args.interval)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: monitor.stop.set())
    sampler = threading.Thread(target=monitor.sample_loop, daemon=True)
    publisher = threading.Thread(target=monitor.publish_loop, args=(args.agent_id,), daemon=True)
    settings = threading.Thread(target=monitor.apply_startup_settings,
                                args=(args.clocks_mode, args.fan_profile, args.fan_speed_pct), daemon=True)
    sampler.start()
    publisher.start()
    settings.start()
    try:
        monitor.stop.wait()
    finally:
        with monitor._condition:
            monitor._condition.notify_all()
        settings.join(timeout=2)
        sampler.join(timeout=4)
        publisher.join(timeout=2)


if __name__ == '__main__':
    main()
