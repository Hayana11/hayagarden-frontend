"""Light capability modeling, partial status reads, and State Lean preservation."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from chat.context_budget import build_state_send_payload, normalize_state_dict
from chat.context_lean_state import (
    REANCHOR_TURN_INTERVAL,
    assemble_cc_state_context,
    build_state_lean_observation,
    compute_state_version,
)
from chat.system_builder import _cc_collect_state, _collect_lights_from_status_payload
from cc_resident import ResidentSession


def _resident_stub(**overrides):
    base = {
        'generation': 1,
        'last_state_snapshot': {},
        'last_state_send_snapshot': {},
        'last_successful_lean_state': True,
        'last_state_anchor_generation': 1,
        'last_state_schema_version': 1,
        'turns_since_state_anchor': 1,
        'state_delta_chars_since_anchor': 0,
        'last_state_anchor_version': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class PowerOnlyCapabilityTests(unittest.TestCase):
    def test_supported_query_props_defaults_to_power_only(self):
        import tools.light_control as lc

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            json.dump({
                'prop_map': {
                    'power': {'siid': 2, 'piid': 1},
                    'brightness': {'siid': 2, 'piid': 2},
                    'color_temp': {'siid': 2, 'piid': 3},
                },
                'zones': {
                    'main': {'supported_query_props': ['power']},
                    'bedside': {'supported_query_props': ['power']},
                },
            }, tmp)
            cfg_path = tmp.name

        with mock.patch.object(lc, 'CONFIG_PATH', cfg_path), \
             mock.patch('tools.light_control._api') as api_factory:
            api = api_factory.return_value
            api.get_devices_prop.return_value = [{'code': 0, 'value': False}]
            result = lc.light_status('did-main', supported_props=lc.supported_query_props('main'))

        query = api.get_devices_prop.call_args[0][0]
        self.assertEqual(len(query), 1)
        self.assertEqual(query[0]['piid'], 1)
        self.assertNotIn('brightness', result['values'])
        self.assertNotIn('color_temp', result['values'])
        os.unlink(cfg_path)

    def test_power_only_config_keeps_control_mappings(self):
        import tools.light_control as lc

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            json.dump({
                'prop_map': {'power': {'siid': 2, 'piid': 1}},
                'zones': {
                    'main': {'supported_query_props': ['power']},
                    'bedside': {'supported_query_props': ['power']},
                },
            }, tmp)
            cfg_path = tmp.name

        with mock.patch.object(lc, 'CONFIG_PATH', cfg_path), \
             mock.patch('tools.light_control._api') as api_factory:
            api = api_factory.return_value
            api.get_devices_prop.return_value = [{'code': 0, 'value': False}]
            lc.light_status('did-main', supported_props=lc.supported_query_props('main'))
            query = api.get_devices_prop.call_args[0][0]
            self.assertEqual(len(query), 1)
            self.assertEqual(query[0]['piid'], 1)

            api.reset_mock()
            lc.set_brightness('did-main', 50)
            lc.set_color_temp('did-main', 4000)
            calls = api.set_devices_prop.call_args_list
            self.assertEqual(calls[0][0][0]['piid'], 2)
            self.assertEqual(calls[1][0][0]['piid'], 3)
        os.unlink(cfg_path)


class MiotResponseValidationTests(unittest.TestCase):
    def _status_with_rows(self, rows):
        import tools.light_control as lc

        with mock.patch('tools.light_control._api') as api_factory:
            api = api_factory.return_value
            api.get_devices_prop.return_value = rows
            with self.assertRaises(RuntimeError):
                lc.light_status('did-main', supported_props=['power'])

    def test_empty_row_is_unavailable(self):
        self._status_with_rows([{}])

    def test_nonzero_code_is_unavailable(self):
        self._status_with_rows([{'code': 1, 'value': False}])

    def test_none_value_is_unavailable(self):
        self._status_with_rows([{'code': 0, 'value': None}])

    def test_row_count_mismatch_is_unavailable(self):
        self._status_with_rows([])

    def test_invalid_power_type_is_unavailable(self):
        self._status_with_rows([{'code': 0, 'value': 'on'}])

    def test_zone_status_marks_invalid_response_unavailable(self):
        import tools.light_control as lc

        with mock.patch('tools.light_control.light_status', side_effect=RuntimeError('bad row')):
            result = lc.zone_status('main', 'did-main')
        self.assertFalse(result['available'])


class WarmNeutralActionSeparationTests(unittest.TestCase):
    def test_bedside_warm_neutral_routes_exist_without_color_temp_query(self):
        import tools.mijia_daemon as md

        rules = {rule.rule for rule in md.app.url_map.iter_rules()}
        self.assertIn('/light/bedside/warm', rules)
        self.assertIn('/light/bedside/neutral', rules)

        import tools.light_control as lc
        props = lc.supported_query_props('bedside')
        self.assertEqual(props, ['power'])
        self.assertNotIn('color_temp', props)


class PartialSuccessStatusTests(unittest.TestCase):
    def test_collect_lights_keeps_main_when_bedside_unavailable(self):
        payload = {
            'main': {'available': True, 'values': {'power': True}},
            'bedside': {'available': False, 'error': 'timeout'},
        }
        text, meta = _collect_lights_from_status_payload(payload, lean=True)
        self.assertIn('main=开', text or '')
        self.assertNotIn('bedside=', text or '')
        self.assertEqual(meta['lights_source_status'], 'partial')
        self.assertTrue(meta['lights_main_available'])
        self.assertFalse(meta['lights_bedside_available'])

    def test_zone_status_isolates_failures(self):
        import tools.light_control as lc

        with mock.patch('tools.light_control.light_status') as status_mock:
            status_mock.side_effect = [
                {'available': True, 'values': {'power': False}},
                RuntimeError('bedside offline'),
            ]
            main = lc.zone_status('main', 'did-m')
            bedside = lc.zone_status('bedside', 'did-b')

        self.assertTrue(main['available'])
        self.assertEqual(main['values']['power'], False)
        self.assertFalse(bedside['available'])
        self.assertIn('error', bedside)


class PartialZoneHotTurnTests(unittest.TestCase):
    def test_partial_main_change_preserves_cumulative_bedside(self):
        cumulative = normalize_state_dict({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
            'drive': 'attachment=0.55',
            'time_bucket': 'bucket=上午',
        })
        raw_partial = {
            'lights': 'main=开',
            'emotion': 'valence=0.60',
            'drive': 'attachment=0.55',
            'time_bucket': 'bucket=上午',
            '_lights_source': json.dumps({
                'lights_source_status': 'partial',
                'lights_main_available': True,
                'lights_bedside_available': False,
            }),
        }
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_generation=1,
            last_state_schema_version=1,
            last_state_anchor_version=compute_state_version(cumulative),
        )
        result = assemble_cc_state_context(
            raw_state=raw_partial,
            is_cold=False,
            user_text='开灯',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'delta')
        self.assertEqual(result.send_payload.get('lights'), 'main=开 bedside=关')
        self.assertIn('changed=lights', result.state_text)

    def test_partial_main_unchanged_with_bedside_failure_is_omitted(self):
        cumulative = normalize_state_dict({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
            'drive': 'attachment=0.55',
            'time_bucket': 'bucket=上午',
        })
        raw_partial = {
            'lights': 'main=关',
            'emotion': 'valence=0.60',
            'drive': 'attachment=0.55',
            'time_bucket': 'bucket=上午',
            '_lights_source': json.dumps({
                'lights_source_status': 'partial',
                'lights_main_available': True,
                'lights_bedside_available': False,
            }),
        }
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_generation=1,
            last_state_schema_version=1,
            last_state_anchor_version=compute_state_version(cumulative),
        )
        result = assemble_cc_state_context(
            raw_state=raw_partial,
            is_cold=False,
            user_text='继续',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'omitted')
        self.assertNotIn('lights', result.send_payload)
        self.assertNotIn('changed=lights', result.state_text or '')

    def test_collector_partial_status_merges_on_hot_assemble(self):
        cumulative = normalize_state_dict({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
            'drive': 'attachment=0.55',
            'time_bucket': 'bucket=上午',
        })
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_generation=1,
            last_state_schema_version=1,
            last_state_anchor_version=compute_state_version(cumulative),
        )

        def get_db():
            raise AssertionError('db should not be queried')

        payload = {
            'ok': True,
            'result': {
                'main': {'available': True, 'values': {'power': True}},
                'bedside': {'available': False, 'error': 'timeout'},
            },
        }
        with mock.patch('chat.system_builder.build_time_bucket', return_value='上午'), \
             mock.patch('chat.system_builder._format_structured_emotion_snippet', return_value='valence=0.60'), \
             mock.patch('chat.system_builder._format_structured_drive_snippet', return_value='attachment=0.55'), \
             mock.patch('urllib.request.urlopen') as urlopen_mock, \
             mock.patch('config_store.get_bool', return_value=False):
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                json.dumps(payload).encode()
            )
            raw = _cc_collect_state(get_db, lean=True)

        self.assertEqual(raw.get('lights'), 'main=开')
        result = assemble_cc_state_context(
            raw_state=raw,
            is_cold=False,
            user_text='开灯',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(result.send_payload.get('lights'), 'main=开 bedside=关')


class PartialZoneReanchorTests(unittest.TestCase):
    _PARTIAL_META = {
        'lights_source_status': 'partial',
        'lights_main_available': True,
        'lights_bedside_available': False,
    }

    def _cumulative(self):
        return normalize_state_dict({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
        })

    def _raw_partial_main_on(self):
        return {
            'lights': 'main=开',
            'emotion': 'valence=0.60',
            '_lights_source': json.dumps(self._PARTIAL_META),
        }

    def _assert_reanchor_partial_lights(self, result, *, reason: str):
        self.assertEqual(result.state_context_mode, 'full_anchor')
        self.assertEqual(result.reanchor_reason, reason)
        self.assertEqual(result.send_payload.get('lights'), 'main=开 bedside=关')
        self.assertIn('lights: main=开 bedside=关', result.state_text)
        expected_version = compute_state_version({
            'lights': 'main=开 bedside=关',
            'emotion': 'valence=0.60',
        })
        self.assertEqual(result.observation['state_version'], expected_version)

    def test_partial_periodic_reanchor_preserves_unavailable_zone(self):
        cumulative = self._cumulative()
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_generation=1,
            generation=1,
            last_state_schema_version=1,
            turns_since_state_anchor=REANCHOR_TURN_INTERVAL,
        )
        result = assemble_cc_state_context(
            raw_state=self._raw_partial_main_on(),
            is_cold=False,
            user_text='开灯',
            resident=resident,
            lean_on=True,
        )
        self._assert_reanchor_partial_lights(result, reason='reanchor_turn_interval')

        sess = ResidentSession('/tmp', '', '')
        sess._last_state_send_snapshot = dict(cumulative)
        sess._last_successful_lean_state = True
        sess._last_state_anchor_generation = 1
        sess._generation = 1
        meta = {
            'state_snapshot': copy.deepcopy(result.raw_state),
            **result.commit_meta_extras,
        }
        sess._commit_sent_context(meta)
        self.assertEqual(sess.last_state_send_snapshot.get('lights'), 'main=开 bedside=关')

    def test_partial_generation_reanchor_preserves_unavailable_zone(self):
        cumulative = self._cumulative()
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_generation=1,
            generation=2,
            last_state_schema_version=1,
            turns_since_state_anchor=3,
        )
        result = assemble_cc_state_context(
            raw_state=self._raw_partial_main_on(),
            is_cold=False,
            user_text='开灯',
            resident=resident,
            lean_on=True,
        )
        self._assert_reanchor_partial_lights(result, reason='resident_generation_change')

        sess = ResidentSession('/tmp', '', '')
        sess._last_state_send_snapshot = dict(cumulative)
        sess._last_successful_lean_state = True
        sess._last_state_anchor_generation = 1
        sess._generation = 2
        meta = {
            'state_snapshot': copy.deepcopy(result.raw_state),
            **result.commit_meta_extras,
        }
        sess._commit_sent_context(meta)
        self.assertEqual(sess.last_state_send_snapshot.get('lights'), 'main=开 bedside=关')


class UnavailableZoneReanchorTests(unittest.TestCase):
    _UNAVAILABLE_META = {
        'lights_source_status': 'unavailable',
        'lights_main_available': False,
        'lights_bedside_available': False,
    }

    def _cumulative(self):
        return normalize_state_dict({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
        })

    def _raw_unavailable(self):
        return {
            'emotion': 'valence=0.60',
            '_lights_source': json.dumps(self._UNAVAILABLE_META),
        }

    def _assert_reanchor_preserves_known_lights(self, result, *, reason: str):
        self.assertEqual(result.state_context_mode, 'full_anchor')
        self.assertEqual(result.reanchor_reason, reason)
        self.assertEqual(result.send_payload.get('lights'), 'main=关 bedside=关')
        self.assertIn('lights: main=关 bedside=关', result.state_text)
        expected_version = compute_state_version({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
        })
        self.assertEqual(result.observation['state_version'], expected_version)

    def test_unavailable_periodic_reanchor_preserves_known_lights(self):
        cumulative = self._cumulative()
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_generation=1,
            generation=1,
            last_state_schema_version=1,
            turns_since_state_anchor=REANCHOR_TURN_INTERVAL,
        )
        result = assemble_cc_state_context(
            raw_state=self._raw_unavailable(),
            is_cold=False,
            user_text='继续',
            resident=resident,
            lean_on=True,
        )
        self._assert_reanchor_preserves_known_lights(result, reason='reanchor_turn_interval')

        sess = ResidentSession('/tmp', '', '')
        sess._last_state_send_snapshot = dict(cumulative)
        sess._last_successful_lean_state = True
        sess._last_state_anchor_generation = 1
        sess._generation = 1
        meta = {
            'state_snapshot': copy.deepcopy(result.raw_state),
            **result.commit_meta_extras,
        }
        sess._commit_sent_context(meta)
        self.assertEqual(sess.last_state_send_snapshot.get('lights'), 'main=关 bedside=关')

    def test_unavailable_generation_reanchor_preserves_known_lights(self):
        cumulative = self._cumulative()
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_generation=1,
            generation=2,
            last_state_schema_version=1,
            turns_since_state_anchor=2,
        )
        result = assemble_cc_state_context(
            raw_state=self._raw_unavailable(),
            is_cold=False,
            user_text='继续',
            resident=resident,
            lean_on=True,
        )
        self._assert_reanchor_preserves_known_lights(result, reason='resident_generation_change')

        sess = ResidentSession('/tmp', '', '')
        sess._last_state_send_snapshot = dict(cumulative)
        sess._last_successful_lean_state = True
        sess._last_state_anchor_generation = 1
        sess._generation = 2
        meta = {
            'state_snapshot': copy.deepcopy(result.raw_state),
            **result.commit_meta_extras,
        }
        sess._commit_sent_context(meta)
        self.assertEqual(sess.last_state_send_snapshot.get('lights'), 'main=关 bedside=关')


class DisconnectRecoverChainTests(unittest.TestCase):
    _OK_META = {
        'lights_source_status': 'ok',
        'lights_main_available': True,
        'lights_bedside_available': True,
    }
    _UNAVAILABLE_META = {
        'lights_source_status': 'unavailable',
        'lights_main_available': False,
        'lights_bedside_available': False,
    }

    def _base_state(self):
        return normalize_state_dict({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
            'drive': 'attachment=0.55',
            'time_bucket': 'bucket=上午',
        })

    def _raw_ok(self):
        base = self._base_state()
        return {
            **base,
            '_lights_source': json.dumps(self._OK_META),
        }

    def _raw_unavailable(self):
        base = self._base_state()
        raw = {k: v for k, v in base.items() if k != 'lights'}
        raw['_lights_source'] = json.dumps(self._UNAVAILABLE_META)
        return raw

    def test_success_disconnect_recover_unchanged_no_false_delta(self):
        cumulative = self._base_state()
        sess = ResidentSession('/tmp', '', '')
        sess._last_state_send_snapshot = dict(cumulative)
        sess._last_state_snapshot = dict(cumulative)
        sess._last_successful_lean_state = True
        sess._last_state_anchor_generation = 1
        sess._generation = 1
        sess._last_state_schema_version = 1
        sess._turns_since_state_anchor = 1

        r1 = assemble_cc_state_context(
            raw_state=self._raw_ok(),
            is_cold=False,
            user_text='继续',
            resident=sess,
            lean_on=True,
        )
        self.assertEqual(r1.state_context_mode, 'omitted')
        sess._commit_sent_context({
            'state_snapshot': copy.deepcopy(r1.raw_state),
            **r1.commit_meta_extras,
        })

        r2 = assemble_cc_state_context(
            raw_state=self._raw_unavailable(),
            is_cold=False,
            user_text='继续',
            resident=sess,
            lean_on=True,
        )
        self.assertEqual(r2.state_context_mode, 'omitted')
        self.assertNotIn('lights', r2.send_payload)
        sess._commit_sent_context({
            'state_snapshot': copy.deepcopy(r2.raw_state),
            **r2.commit_meta_extras,
        })
        self.assertEqual(sess.last_state_snapshot.get('lights'), 'main=关 bedside=关')
        self.assertEqual(sess.last_state_send_snapshot.get('lights'), 'main=关 bedside=关')

        version_before_recover = compute_state_version(sess.last_state_send_snapshot)
        r3 = assemble_cc_state_context(
            raw_state=self._raw_ok(),
            is_cold=False,
            user_text='继续',
            resident=sess,
            lean_on=True,
        )
        self.assertEqual(r3.state_context_mode, 'omitted')
        self.assertNotIn('lights', r3.send_payload)
        self.assertEqual(r3.observation['previous_version'], r3.observation['state_version'])
        self.assertEqual(r3.observation['state_version'], version_before_recover)
        self.assertEqual(sess.last_state_snapshot.get('lights'), 'main=关 bedside=关')
        self.assertEqual(sess.last_state_send_snapshot.get('lights'), 'main=关 bedside=关')


class HotRoundTransientFailureTests(unittest.TestCase):
    def test_light_source_failure_preserves_cumulative_lights(self):
        cumulative = normalize_state_dict({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
        })
        raw_after_failure = {
            'emotion': 'valence=0.60',
            '_lights_source': json.dumps({
                'lights_source_status': 'unavailable',
                'lights_main_available': False,
                'lights_bedside_available': False,
            }),
        }
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_version=compute_state_version(cumulative),
        )
        result = assemble_cc_state_context(
            raw_state=raw_after_failure,
            is_cold=False,
            user_text='继续',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'omitted')
        self.assertNotIn('lights', result.send_payload)
        self.assertNotIn('unknown', result.state_text.lower())
        self.assertNotIn('cleared', result.state_text.lower())
        self.assertEqual(
            compute_state_version(
                {'lights': 'main=关 bedside=关', 'emotion': 'valence=0.60'},
            ),
            compute_state_version(resident.last_state_send_snapshot),
        )
        self.assertEqual(result.observation.get('lights_source_status'), 'unavailable')

    def test_send_payload_skips_missing_lights_key(self):
        before = {'lights': 'main=关 bedside=关'}
        after = {'emotion': 'valence=0.61'}
        send = build_state_send_payload(before, after, user_text='嗯', is_cold=False)
        self.assertNotIn('lights', send)

    def test_collector_urlopen_failure_omits_lights_and_preserves_hot_state(self):
        cumulative = normalize_state_dict({
            'lights': 'main=关 bedside=关',
            'emotion': 'valence=0.60',
            'drive': 'attachment=0.55',
            'time_bucket': 'bucket=上午',
        })
        resident = _resident_stub(
            last_state_snapshot=cumulative,
            last_state_send_snapshot=cumulative,
            last_state_anchor_generation=1,
            last_state_schema_version=1,
            last_state_anchor_version=compute_state_version(cumulative),
        )

        def get_db():
            raise AssertionError('db should not be queried')

        with mock.patch('chat.system_builder.build_time_bucket', return_value='上午'), \
             mock.patch('chat.system_builder._format_structured_emotion_snippet', return_value='valence=0.60'), \
             mock.patch('chat.system_builder._format_structured_drive_snippet', return_value='attachment=0.55'), \
             mock.patch('urllib.request.urlopen', side_effect=OSError('light daemon down')), \
             mock.patch('config_store.get_bool', return_value=False):
            raw = _cc_collect_state(get_db, lean=True)

        self.assertNotIn('lights', raw)
        result = assemble_cc_state_context(
            raw_state=raw,
            is_cold=False,
            user_text='继续',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'omitted')
        self.assertNotIn('lights', result.send_payload)
        self.assertNotIn('cleared', result.state_text.lower())
        self.assertEqual(result.observation.get('lights_source_status'), 'unavailable')


class ColdStartNoLightsTests(unittest.TestCase):
    def test_cold_start_omits_lights_on_source_failure(self):
        raw = {
            'emotion': 'valence=0.60',
            '_lights_source': json.dumps({'lights_source_status': 'unavailable'}),
        }
        resident = _resident_stub(last_successful_lean_state=False)
        result = assemble_cc_state_context(
            raw_state=raw,
            is_cold=True,
            user_text='你好',
            resident=resident,
            lean_on=True,
        )
        self.assertNotIn('lights', result.send_payload)
        self.assertNotIn('unknown', result.state_text.lower())
        self.assertIn('emotion', result.state_text)
        self.assertNotEqual(result.state_context_mode, 'fallback')


class RealLightsChangeDeltaTests(unittest.TestCase):
    def test_successive_reads_emit_single_lights_delta(self):
        base = normalize_state_dict({
            'emotion': 'valence=0.60',
            'lights': 'main=关 bedside=关',
        })
        resident = _resident_stub(
            last_state_snapshot=base,
            last_state_send_snapshot=base,
            last_state_anchor_generation=1,
            last_state_anchor_version=compute_state_version(base),
        )
        anchor = assemble_cc_state_context(
            raw_state=base,
            is_cold=True,
            user_text='你好',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(anchor.state_context_mode, 'full_anchor')

        changed = dict(base)
        changed['lights'] = 'main=开 bedside=关'
        delta = assemble_cc_state_context(
            raw_state=changed,
            is_cold=False,
            user_text='开灯',
            resident=_resident_stub(
                generation=1,
                last_successful_lean_state=True,
                last_state_snapshot=base,
                last_state_send_snapshot=base,
                last_state_anchor_generation=1,
                last_state_schema_version=1,
                turns_since_state_anchor=1,
                state_delta_chars_since_anchor=0,
                last_state_anchor_version=anchor.observation['state_version'],
            ),
            lean_on=True,
        )
        self.assertEqual(delta.state_context_mode, 'delta')
        self.assertEqual(delta.send_payload.get('lights'), 'main=开 bedside=关')
        self.assertIn('changed=lights', delta.state_text)
        self.assertNotIn('unknown', delta.state_text.lower())


class ObservationMetaTests(unittest.TestCase):
    def test_lights_source_meta_not_in_provider_version(self):
        raw = {
            'lights': 'main=关',
            '_lights_source': json.dumps({
                'lights_source_status': 'ok',
                'lights_main_available': True,
                'lights_bedside_available': True,
            }),
        }
        version_with_meta = compute_state_version(raw)
        version_without = compute_state_version({'lights': 'main=关'})
        self.assertEqual(version_with_meta, version_without)

    def test_observation_includes_lights_health_fields(self):
        obs = build_state_lean_observation(
            enabled=True,
            state_context_mode='omitted',
            state_text='',
            state_version='abc',
            anchor_version='abc',
            previous_version='abc',
            changed_field_count=0,
            reanchor_reason=None,
            fallback_reason=None,
            resident_generation=1,
            lights_source_meta={
                'lights_source_status': 'partial',
                'lights_main_available': True,
                'lights_bedside_available': False,
                'lights_last_success_at': '2026-07-27T00:00:00Z',
            },
        )
        self.assertEqual(obs['lights_source_status'], 'partial')
        self.assertTrue(obs['lights_main_available'])
        self.assertFalse(obs['lights_bedside_available'])
        self.assertEqual(obs['lights_last_success_at'], '2026-07-27T00:00:00Z')

    def test_legacy_path_records_lights_source_observation(self):
        raw = {
            'emotion': 'valence=0.60',
            '_lights_source': json.dumps({
                'lights_source_status': 'partial',
                'lights_main_available': True,
                'lights_bedside_available': False,
            }),
        }
        resident = _resident_stub(last_successful_lean_state=False)
        result = assemble_cc_state_context(
            raw_state=raw,
            is_cold=True,
            user_text='你好',
            resident=resident,
            lean_on=False,
        )
        self.assertFalse(result.used_lean)
        self.assertEqual(result.observation.get('lights_source_status'), 'partial')
        self.assertTrue(result.observation.get('lights_main_available'))
        self.assertFalse(result.observation.get('lights_bedside_available'))


if __name__ == '__main__':
    unittest.main()
