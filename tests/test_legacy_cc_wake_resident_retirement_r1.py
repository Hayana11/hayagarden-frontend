"""Focused retirement contract for the legacy CC Wake resident."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_cc_wake_resident_is_not_constructed():
    gateway = (ROOT / 'gateway.py').read_text(encoding='utf-8')
    runners = (ROOT / 'wake' / 'runners.py').read_text(encoding='utf-8')

    assert '_CC_WAKE_RESIDENT' not in gateway
    assert 'ClaudeCodeWakeRunner' not in gateway
    assert 'ClaudeCodeWakeRunner' not in runners
    assert 'SharedResidentWakeRunner' in runners
    assert 'SharedResidentWakeRunner' in gateway
    assert 'def _run_shared_claude_wake(request):' in gateway


def test_shared_wake_preserves_lease_and_transcript_boundaries():
    source = (ROOT / 'gateway.py').read_text(encoding='utf-8')

    for marker in (
        "turn_mode='wake'",
        'prepare_shared_transcript_watermark',
        'commit_shared_transcript_watermark',
        'begin_shared_wake_delivery_fence',
        "jsonl_finality_profile='unified_normal_wake'",
        'guard_cc_generation',
    ):
        assert marker in source


def test_wake_mode_partition_remains_explicit():
    source = (ROOT / 'wake' / 'runners.py').read_text(encoding='utf-8')
    for mode in ('normal', 'morning', 'nightwatch', 'ritual', 'self_trigger'):
        assert repr(mode) in source
    assert repr('summarize') in source
    assert repr('dream') in source


def test_mode_context_and_single_resident_invariants():
    gateway = (ROOT / 'gateway.py').read_text(encoding='utf-8')
    runners = (ROOT / 'wake' / 'runners.py').read_text(encoding='utf-8')

    # Production gateway has one persistent conversational Claude construction.
    assert gateway.count(
        'cc_resident.ResidentSession(CC_CWD, CC_ALLOWED_TOOLS'
    ) == 1
    assert gateway.count('_CC_WAKE_RESIDENT') == 0
    assert gateway.count('SharedResidentWakeRunner(') == 1

    # Wake mode identity and context remain owned by the existing Wake planner.
    for mode in ('normal', 'morning', 'nightwatch', 'ritual', 'self_trigger'):
        assert repr(mode) in runners
    assert "mode=mode," in gateway
    assert "_wake_trigger_message(mode, ritual_type)" in gateway
    assert "'self_trigger_note': data.get('self_trigger_note', '')" in gateway

    # Main Chat and Wake still use distinct lease identities.
    assert "turn_mode='chat'" in gateway
    assert "turn_mode='wake'" in gateway

    # The migrated control round retains the deterministic isolation/finality
    # chain used by canonical normal Wake.
    for marker in (
        'guard_cc_generation(guarded_events())',
        'prepare_shared_transcript_watermark',
        'commit_shared_transcript_watermark',
        'begin_shared_wake_delivery_fence',
        "jsonl_finality_profile='unified_normal_wake'",
    ):
        assert marker in gateway
