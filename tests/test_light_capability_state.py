"""Light capability modeling, partial status reads, and State Lean preservation."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from chat.context_budget import build_state_send_payload, normalize_state_dict
from chat.context_lean_state import (
    assemble_cc_state_context,
    build_state_lean_observation,
    compute_state_version,
)
from chat.system_builder import _collect_lights_from_status_payload


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
             mock.patch.object(lc, 'AUTH_PATH', '/tmp/fake-auth'), \
             mock.patch('tools.light_control.os.path.exists', return_value=True), \
             mock.patch('tools.light_control.mijiaAPI') as api_cls:
            api = api_cls.return_value
            api.get_devices_prop.return_value = [{'value': False}]
            result = lc.light_status('did-main', supported_props=lc.supported_query_props('main'))

        query = api.get_devices_prop.call_args[0][0]
        self.assertEqual(len(query), 1)
        self.assertEqual(query[0]['piid'], 1)
        self.assertNotIn('brightness', result['values'])
        self.assertNotIn('color_temp', result['values'])
        os.unlink(cfg_path)


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


if __name__ == '__main__':
    unittest.main()
