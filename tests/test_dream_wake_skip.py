"""批4：整夜同一次掷骰；概率走 config_store，默认 0。"""
import datetime
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


def _load_mod():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'dream_wake.py'
    spec = importlib.util.spec_from_file_location('dream_wake_mod', path)
    mod = importlib.util.module_from_spec(spec)
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


def _fake_conn(event_at='2026-07-17 02:00:00'):
    fake_event = {'created_at': event_at}

    class FakeConn:
        def execute(self, *a, **k):
            sql = a[0]

            class Cur:
                def fetchone(self_inner):
                    if 'dream_events' in sql:
                        return fake_event
                    return None

            return Cur()

        def close(self):
            pass

    return FakeConn()


class DreamWakeSkipTests(unittest.TestCase):
    def test_date_seed_stable_within_night(self):
        now = datetime.datetime(2026, 7, 17, 3, 30, 0)
        rolls = [
            __import__('random').Random(f'dream-{now:%Y-%m-%d}').random()
            for _ in range(5)
        ]
        self.assertEqual(len(set(rolls)), 1)

    def test_run_dreaming_skip_is_date_seeded_when_prob_set(self):
        now = datetime.datetime(2026, 7, 17, 4, 0, 0)

        def _cfg_float(key, default=None):
            if key == 'dream_skip_prob':
                return 0.2
            return default

        with mock.patch.object(DW, '_db', return_value=_fake_conn()):
            with mock.patch.object(DW._wcfg, 'get_float', side_effect=_cfg_float):
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

    def test_default_skip_prob_zero_never_rolls(self):
        now = datetime.datetime(2026, 7, 17, 4, 0, 0)
        fake_mod = types.ModuleType('dream_generator')
        fake_mod.generate_dream = mock.Mock()

        with mock.patch.object(DW, '_db', return_value=_fake_conn()):
            with mock.patch.object(DW._wcfg, 'get_float', return_value=0.0):
                with mock.patch('random.Random') as RR:
                    with mock.patch.object(DW, '_log'):
                        with mock.patch.dict(sys.modules, {'dream_generator': fake_mod}):
                            DW.run_dreaming(now)
                    RR.assert_not_called()
        fake_mod.generate_dream.assert_called_once()


if __name__ == '__main__':
    unittest.main()
