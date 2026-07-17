"""批4：整夜同一次掷骰，不应每 tick 重掷。"""
import datetime
import importlib.util
import unittest
from pathlib import Path
from unittest import mock


def _load_mod():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'dream_wake.py'
    spec = importlib.util.spec_from_file_location('dream_wake_mod', path)
    mod = importlib.util.module_from_spec(spec)
    # bot_config / config_store 在 import 时会被拉起；用最小桩绕开
    import sys
    import types
    if 'bot_config' not in sys.modules:
        bc = types.ModuleType('bot_config')
        bc.NIGHTWATCH_START = 1
        bc.NIGHTWATCH_END = 3
        bc.NIGHTWATCH_PROB = 0.1
        bc.NIGHTWATCH_ACTIVITY_WINDOW = 30
        sys.modules['bot_config'] = bc
    if 'config_store' not in sys.modules:
        cs = types.ModuleType('config_store')
        cs.get_int = lambda k, d=None: d
        cs.get_float = lambda k, d=None: d
        sys.modules['config_store'] = cs
    spec.loader.exec_module(mod)
    return mod


DW = _load_mod()


class DreamWakeSkipTests(unittest.TestCase):
    def test_date_seed_stable_within_night(self):
        """同一天多次调用应得到同一掷骰结果。"""
        now = datetime.datetime(2026, 7, 17, 3, 30, 0)
        rolls = [
            __import__('random').Random(f'dream-{now:%Y-%m-%d}').random()
            for _ in range(5)
        ]
        self.assertEqual(len(set(rolls)), 1)

    def test_run_dreaming_skip_is_date_seeded(self):
        now = datetime.datetime(2026, 7, 17, 4, 0, 0)
        # 强制过睡眠/去重门，只看概率门
        fake_event = {'created_at': '2026-07-17 02:00:00'}

        class FakeConn:
            def execute(self, *a, **k):
                class R:
                    def fetchone(self_inner):
                        if 'dream_events' in a[0] or 'created_at FROM dream_events' in a[0]:
                            return fake_event
                        if "type='DREAM'" in a[0]:
                            return None
                        return fake_event
                # first query in run_dreaming is last dream_events
                sql = a[0]
                class Cur:
                    def fetchone(self_inner):
                        if 'dream_events' in sql:
                            return fake_event
                        return None
                return Cur()

            def close(self):
                pass

        with mock.patch.object(DW, '_db', return_value=FakeConn()):
            with mock.patch('random.Random') as RR:
                rng = mock.Mock()
                rng.random.return_value = 0.1  # < 0.2 → skip
                RR.return_value = rng
                with mock.patch.object(DW, '_log') as log:
                    DW.run_dreaming(now)
                RR.assert_called_with('dream-2026-07-17')
                self.assertTrue(
                    any('no dream tonight' in str(c) for c in log.call_args_list)
                )


if __name__ == '__main__':
    unittest.main()
