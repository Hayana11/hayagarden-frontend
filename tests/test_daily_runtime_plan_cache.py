from __future__ import annotations

import types
from unittest import mock


def _hot_cache_key_inputs():
    return {
        'ready_surface_fingerprint': (('chunk:a', 'rev-a'),),
        'canonical_hot_source_scope': (('message:1', 'hash-a'),),
        'source_rows_identity': ((1, 'row-hash-a'),),
        'budget_policy_version': 'budget-v1',
        'policy_input': {
            'mode': 'hot', 'static_system': 'system-a',
            'pending_user_text': 'question-a', 'dynamic_state_text': 'state-a',
        },
        'policy_identity': (('token_budget', 100),),
        'fixed_sections_identity': (('invariant_system', 'system:a'),),
        'static_system_hash': 'static-hash-a',
        'mode': 'hot',
        'receipt_identity': (('message:1', 'raw', 'raw:1'),),
    }


def test_hot_plan_key_changes_when_any_input_identity_changes():
    from chat import daily_runtime as dr

    base = _hot_cache_key_inputs()
    original = dr._hot_context_plan_input_key(**base)
    assert original
    for name, value in base.items():
        changed = dict(base)
        changed[name] = ('changed', value)
        assert dr._hot_context_plan_input_key(**changed) != original, name


def test_hot_plan_cache_hit_reuses_plan_but_still_reconciles(monkeypatch):
    from chat import daily_runtime as dr
    from continuity import store
    from continuity.context_plan import ContextBudgetPolicy

    dr._clear_hot_context_plan_cache_for_tests()
    plan = types.SimpleNamespace(
        resident_key='resident-1', db_path='cache-test.db', user_message_id=9,
        user_content='question', assembly={'state_snapshot': {}}, manifest={},
        cold_start_diagnostic={}, hot_receipt_frozen=None,
    )
    frozen = {
        'receipt': object(), 'members': (), 'receipt_members_complete': True,
        'membership_valid': True, 'receipt_read_error': None,
    }
    context = types.SimpleNamespace(
        plan_id='plan:one', plan_hash='a' * 64, source_hash='b' * 64,
        valid=True, budget_status='fits',
    )
    budget = ContextBudgetPolicy(token_budget=100)
    source_rows = ((), {'id': 9, 'author': 'hayana', 'content': 'question'})
    source_conn = mock.Mock()
    monkeypatch.setattr(dr, '_freeze_hot_receipt', lambda *a, **k: frozen)
    monkeypatch.setattr(
        dr, '_hot_canonical_source_scope',
        lambda *a, **k: (('message:1', 'hash-a'),),
    )
    monkeypatch.setattr(dr.config_store, 'get_bool', lambda *a, **k: False)
    monkeypatch.setattr(store, 'strict_revalidate_enabled', lambda: False)
    monkeypatch.setattr(
        store, 'read_ready_surface',
        lambda *a, **k: types.SimpleNamespace(status='ready', artifacts=()),
    )
    monkeypatch.setattr(
        dr, '_context_plan_policy', lambda **kwargs: (budget, 'budget-v1'),
    )
    monkeypatch.setattr(
        dr, '_build_continuity_shadow_fixed_sections', lambda **kwargs: (),
    )
    shadow = __import__('chat.daily_continuity_shadow', fromlist=['_source_rows'])
    monkeypatch.setattr(shadow, '_read_only_connection', lambda *a, **k: source_conn)
    monkeypatch.setattr(shadow, '_check_source_schema', lambda *a, **k: None)
    monkeypatch.setattr(shadow, '_source_rows', lambda *a, **k: source_rows)
    build = mock.Mock(return_value=(context, {'chunk:a': 'body'}))
    reconcile = mock.Mock(return_value='NO_OP')
    monkeypatch.setattr(dr, '_build_production_context_plan', build)
    monkeypatch.setattr(dr, '_reconcile_hot_context_plan', reconcile)

    assert dr._prepare_hot_context_plan(
        plan, resident=object(), static_system='SYSTEM',
    ) == 'NO_OP'
    assert plan.cold_start_diagnostic['hot_plan_cache'] == 'miss'
    assert dr._prepare_hot_context_plan(
        plan, resident=object(), static_system='SYSTEM',
    ) == 'NO_OP'

    assert plan.cold_start_diagnostic['hot_plan_cache'] == 'hit'
    assert build.call_count == 1
    assert reconcile.call_count == 2
    assert plan.hot_desired_plan is context
    assert plan.hot_desired_chunk_bodies == {'chunk:a': 'body'}
    assert source_conn.close.call_count == 2
    dr._clear_hot_context_plan_cache_for_tests()


def test_hot_plan_reuse_birth_detail_is_visible():
    from chat import daily_runtime as dr

    diagnostic = {'hot_context_plan_start_at': 1.0}
    plan = types.SimpleNamespace(cold_start_diagnostic=diagnostic)
    dr._turn_timing_mark(
        plan,
        'hot_context_plan_end_at',
        decision='NO_OP',
        detail='通过 · 复用',
    )
    assert diagnostic['phases'][-1]['detail'] == '通过 · 复用'
