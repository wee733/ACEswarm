import json
from pathlib import Path
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor


class Fans(dict):
    def __init__(self):
        super().__init__(pwmfan={'profile': 'quiet', 'speed': [28], 'rpm': [1000]})
        self.calls = []

    def all_profiles(self, name):
        return ['quiet', 'manual']

    def set_profile(self, name, profile):
        self.calls.append(('profile', profile))
        self[name]['profile'] = profile

    def set_speed(self, name, speed, idx):
        self.calls.append(('speed', speed))
        self[name]['speed'][idx] = speed - 0.15


class Clocks:
    def __init__(self, enabled):
        self.enabled = enabled
        self.status = 'running' if enabled else 'inactive'

    def __bool__(self):
        return self.enabled


class Board:
    def __init__(self):
        self.cpu = {'total': {'idle': 60}}
        self.gpu = {'gpu': {'status': {'load': 15}}}
        self.memory = {'RAM': {'used': 2048, 'tot': 8192}}
        self.power = {'tot': {'power': 7500}}
        self.temperature = {'inactive': {'temp': 102, 'online': False}, 'tj': {'temp': 50},
                            'bad': {'temp': float('nan')}, 'nvme': {'temp': 65}}
        self.fan = Fans()
        self.nvpmodel = SimpleNamespace(name='MAXN')
        self.uptime = SimpleNamespace(total_seconds=lambda: 10)
        self._clocks = Clocks(False)
        self.clock_writes = []

    @property
    def jetson_clocks(self):
        return self._clocks

    @jetson_clocks.setter
    def jetson_clocks(self, enabled):
        self.clock_writes.append(enabled)
        self._clocks = Clocks(enabled)


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.board = Board()
        self.monitor = monitor.Monitor()
        self.sample()

    def sample(self):
        self.monitor._record(self.board, monitor.read_sample(self.board))

    def run_control(self, fn):
        # Model jtop reflecting asynchronous hardware updates on a later sample.
        stop = threading.Event()
        def refresh():
            while not stop.wait(0.01):
                self.sample()
        sampler = threading.Thread(target=refresh)
        sampler.start()
        try:
            fn()
        finally:
            stop.set()
            sampler.join()

    def test_only_verified_client_skips_library_inventory(self):
        class Client:
            def __init__(self, interval):
                self.interval = interval
                self._board = {'hardware': {'model': 'test board'}}
                self._load_jetson_libraries()

            def _load_jetson_libraries(self):
                self._board['libraries'] = {'probed': True}

        for version in ('7.2.2', '7.2.3', None):
            with self.subTest(version=version):
                module = SimpleNamespace(jtop=Client, __version__=version)
                with patch.dict(sys.modules, {'jtop': module}):
                    client = monitor.open_jtop(monitor.DEFAULT_INTERVAL)
                self.assertEqual(client.interval, 2.0)
                self.assertEqual(client._board['hardware'], {'model': 'test board'})
                if version == '7.2.2':
                    self.assertEqual(client._board['libraries'], {})
                    self.assertEqual(client._board['platform'], {})
                else:
                    self.assertIs(type(client), Client)
                    self.assertEqual(client._board['libraries'], {'probed': True})

    def test_fields_units_and_unavailable(self):
        d = self.monitor.snapshot()['data']
        self.assertEqual([d[k] for k in ('cpu_pct', 'gpu_pct', 'ram_pct', 'temp_max_c', 'temp_soc_c', 'power_w')],
                         [40, 15, 25, 65, 50, 7.5])
        self.assertEqual(d['ram_used_mib'], 2)
        self.assertNotIn('inactive', d['temperatures_c'])
        json.dumps(d, allow_nan=False)
        self.monitor._received = 0
        state = self.monitor.snapshot()
        self.assertTrue(state['stale'])
        self.assertIsNone(state['data']['temp_soc_c'])
        with self.assertRaises(RuntimeError):
            self.monitor.set_clocks(True)

    def test_control_is_explicit_and_independent(self):
        self.assertEqual(self.board.clock_writes, [])
        self.assertEqual(self.board.fan.calls, [])
        self.run_control(lambda: self.monitor.set_fan('pwmfan', 'manual', 74))
        self.assertEqual(self.board.fan.calls, [('profile', 'manual'), ('speed', 74)])
        self.assertEqual(self.board.clock_writes, [])
        self.run_control(lambda: self.monitor.set_clocks(True))
        self.assertEqual(self.board.clock_writes, [True])
        self.assertEqual(self.board.fan.calls, [('profile', 'manual'), ('speed', 74)])

    def test_invalid_requests_never_write(self):
        for args in [('pwmfan', 'manual', 101), ('pwmfan', 'manual', None), ('pwmfan', 'quiet', 40),
                     ('missing', 'quiet', None), ('pwmfan', 'bad', None), ('pwmfan', 'manual', False)]:
            with self.assertRaises(ValueError):
                self.monitor.set_fan(*args)
        with self.assertRaises(ValueError):
            self.monitor.set_clocks(1)
        self.assertEqual(self.board.fan.calls, [])
        self.assertEqual(self.board.clock_writes, [])

    def test_startup_keep_and_idempotent_values(self):
        self.monitor.apply_startup_settings('keep', 'keep', 100)
        self.monitor.apply_startup_settings('disable', 'quiet', 100)
        self.assertEqual(self.board.clock_writes, [])
        self.assertEqual(self.board.fan.calls, [])
        self.run_control(lambda: self.monitor.apply_startup_settings('enable', 'manual', 80))
        self.monitor.apply_startup_settings('enable', 'manual', 80)
        self.assertEqual(self.board.clock_writes, [True])
        self.assertEqual(self.board.fan.calls, [('profile', 'manual'), ('speed', 80)])
        self.run_control(lambda: self.monitor.apply_startup_settings('keep', 'quiet', 10))
        self.assertEqual(self.board.fan.calls[-1], ('profile', 'quiet'))
        self.assertEqual(self.board.clock_writes, [True])

    def test_startup_settings_independent_of_unsupported_hardware(self):
        self.board._clocks = None
        self.sample()
        with self.assertLogs(level='ERROR') as logs:
            self.run_control(lambda: self.monitor.apply_startup_settings('enable', 'manual', 90))
        self.assertIn('锁频设置', logs.output[0])
        self.assertEqual(self.board.fan.calls, [('profile', 'manual'), ('speed', 90)])
        self.board._clocks = Clocks(False)
        self.sample()
        with self.assertLogs(level='ERROR') as logs:
            self.run_control(lambda: self.monitor.apply_startup_settings('enable', 'cool', 90))
        self.assertIn('风扇设置', logs.output[0])
        self.assertEqual(self.board.clock_writes, [True])
        self.assertEqual(self.monitor.snapshot()['data']['status'], 'ok')

    def test_settings_wait_for_sample_and_run_once(self):
        self.monitor = monitor.Monitor()
        worker = threading.Thread(target=self.monitor.apply_startup_settings,
                                  args=('enable', 'keep', 100), daemon=True)
        worker.start()
        worker.join(.03)
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.board.clock_writes, [])
        self.run_control(lambda: worker.join(1))
        self.assertFalse(worker.is_alive())
        self.board._clocks = Clocks(False)
        self.sample()
        self.assertEqual(self.board.clock_writes, [True])

    def test_timeout_does_not_apply_later_fan_steps(self):
        with patch.object(monitor, 'CONTROL_TIMEOUT', .02), self.assertRaises(RuntimeError):
            self.monitor.set_fan('pwmfan', 'manual', 74)
        self.assertEqual(self.board.fan.calls, [('profile', 'manual')])

    def test_publication_uses_gateway_id_without_heartbeat(self):
        received = []
        def request(path, body=None):
            if path == '/status':
                return {'ok': True, 'local_agent_id': 'uav1'}
            self.assertEqual(path, '/zenoh/put')
            received.append(body)
            self.monitor.stop.set()
        with patch.object(monitor, 'get_json', request):
            self.monitor.publish_loop('')
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['key_expr'], 'uav1/telemetry/jetson')
        body = json.loads(received[0]['payload'])
        self.assertEqual(body['data']['status'], 'ok')
        self.assertFalse(body['stale'])
        self.assertNotIn('publish', body)
        self.assertTrue(self.monitor.snapshot()['publish']['ok'])


if __name__ == '__main__':
    unittest.main()
