"""Whole-prompt budget fence for CC cold bootstrap (P0 cold-storm fix).

Two fences protect a cold bootstrap from becoming an oversized prompt that
gets sent, immediately trips ``hard_context`` on the next turn, and gets
rebuilt identically forever:

  Fence A (history assembly budget) — ``chat.history_assembly`` /
  ``chat.history_boundary`` already trim history to a token budget when
  ``history_mode == 'cc_token_budget'``. ``gateway.build_messages`` now
  forces that mode for a cold bootstrap (``cold_safe=True``) regardless of
  the ``CONTEXT_LEAN_HISTORY_ENABLED`` rollout flag, because cold safety
  must not depend on an unrelated feature rollout.

  Fence B (whole-prompt preflight) — this module estimates the token count
  of what is actually about to be written to Claude stdin (full system text
  + assembled cold content) and refuses to send when it exceeds
  the resident rebuild packing target. At most one deterministic rebuild (smaller
  history budget, reusing the same token-budget history assembly) is
  attempted before failing closed with :class:`ColdBootstrapOverflow`. This
  fence never retries a model call and never truncates the final prompt
  string directly.
"""
from __future__ import annotations

from typing import Optional

from chat.context_budget import default_estimate_tokens

DEFAULT_SAFETY_MARGIN = 8000
# Fence C only applies when hard_context fires within this many completed
# resident turns after the preceding cold spawn — i.e. an immediate post-cold
# storm, not a long hot-growth path that legitimately shrinks back down.
COLD_STORM_MAX_TURNS_SINCE_RESPAWN = 1


def _cfg_int(key: str, default: int) -> int:
    try:
        import config_store
        return int(config_store.get_int(key, default))
    except Exception:
        return default


def cold_hard_limit() -> int:
    return _cfg_int('CC_CONTEXT_HARD_LIMIT', 180_000)


def cold_soft_limit() -> int:
    return _cfg_int('CC_CONTEXT_SOFT_LIMIT', 150_000)


def cold_safety_margin() -> int:
    return _cfg_int('CC_COLD_BOOTSTRAP_SAFETY_MARGIN', DEFAULT_SAFETY_MARGIN)


def capacity_swap_prompt_target() -> int:
    """Independent whole-prompt budget authority for Capacity Swap."""
    return max(1, _cfg_int('CC_CAPACITY_SWAP_PROMPT_TARGET', 90_000))


def resident_rebuild_prompt_target() -> int:
    """90k-based packing authority shared by cold, respawn, and capacity."""
    return capacity_swap_prompt_target()


def cold_rebuild_guard() -> int:
    """Reserved threshold for the future stale-cache pre-send policy.

    This legacy configuration must not affect resident rebuild packing,
    cold/respawn ContextPlan validity, cold whole-prompt admission, history
    trimming, or desired rebuild size.
    """
    return max(1, _cfg_int('CC_COLD_REBUILD_GUARD', 70_000))


def cold_prompt_target() -> int:
    """Whole-prompt token target for a cold bootstrap.

    Deliberately kept below ``hard`` (with a safety margin) so a freshly
    spawned cold resident still has headroom for the next hot user turn,
    model output, and tool rounds. A cold bootstrap that itself lands at or
    above ``hard`` is exactly what causes the historical cold-storm: the
    very next turn re-trips ``hard_context`` and rebuilds an equally large
    prompt.
    """
    hard = cold_hard_limit()
    soft = cold_soft_limit()
    margin = cold_safety_margin()
    return max(1, min(soft, hard - margin))


def estimate_text_tokens(text: Optional[str]) -> int:
    return int(default_estimate_tokens(text or ''))


def estimate_whole_prompt(full_system: str, content) -> int:
    """``estimate(full_system) + estimate(cold content actually sent)``."""
    from chat.history_assembly import flatten_message_content

    content_text = content if isinstance(content, str) else flatten_message_content(content)
    return estimate_text_tokens(full_system) + estimate_text_tokens(content_text)


def effective_history_budget(
    *,
    default_history_budget: int,
    non_history_estimate: int,
    cold_target: int,
) -> int:
    """Dynamic remaining-history budget for a deterministic rebuild.

    Never exceeds ``cold_target`` minus everything else already mandatory in
    the cold prompt (persona/state/cold_once/current user/...). When no
    history tokens remain, returns ``1`` — the minimum safe trim budget.
    ``history_assembly`` treats ``history_token_budget <= 0`` as *disable*
    token trimming, so zero must never be returned here.
    """
    remaining = max(0, int(cold_target) - int(non_history_estimate))
    capped = min(int(default_history_budget), remaining)
    if capped <= 0:
        return 1
    return capped


def is_immediate_post_cold_hard_context(pre_spawn_turns) -> bool:
    """True when ``hard_context`` fired right after a cold bootstrap."""
    try:
        turns = int(pre_spawn_turns)
    except (TypeError, ValueError):
        return False
    return 0 <= turns <= COLD_STORM_MAX_TURNS_SINCE_RESPAWN


def should_refuse_no_benefit_hard_context_respawn(
    *,
    pre_spawn_turns,
    cold_prompt_estimate: int,
    last_cold_bootstrap_estimate: int,
    last_cold_bootstrap_generation: int,
    current_generation: int,
) -> bool:
    """Return True when an immediate post-cold hard_context would repeat an
    equal-or-larger cold bootstrap with no shrink evidence.

    Genuine hot growth (many turns since the last cold) is never refused here.
    A stale ``last_cold_bootstrap_estimate`` from an older generation (e.g.
    native-fork hot trial skipped cold on the authoritative resident) is
    ignored via the generation pairing check.
    """
    if not is_immediate_post_cold_hard_context(pre_spawn_turns):
        return False
    last_gen = int(last_cold_bootstrap_generation or 0)
    cur_gen = int(current_generation or 0)
    if last_gen <= 0 or cur_gen - last_gen != 1:
        return False
    baseline = int(last_cold_bootstrap_estimate or 0)
    if baseline <= 0:
        return False
    return int(cold_prompt_estimate or 0) >= baseline


class ColdBootstrapOverflow(RuntimeError):
    """Cold bootstrap cannot fit under the whole-prompt target.

    Raised after at most one deterministic history-budget rebuild attempt.
    Callers must fail closed: no stdin write, no provider call, no repeated
    respawn, active transcript unchanged.
    """

    respawn_reason = 'cold_bootstrap_overflow'

    def __init__(
        self,
        *,
        estimate: int,
        target: int,
        history_budget: int,
        usage=None,
        cold_history_trimmed=None,
        error_code='cold_bootstrap_overflow',
        guard_target=None,
    ):
        super().__init__(
            'cold_bootstrap_overflow: estimate=%d target=%d history_budget=%d'
            % (int(estimate), int(target), int(history_budget))
        )
        self.estimate = int(estimate)
        self.target = int(target)
        self.history_budget = int(history_budget)
        self.error_code = str(error_code or 'cold_bootstrap_overflow')
        from cc_resident import empty_usage

        if usage is None:
            usage = empty_usage(respawn_reason=self.respawn_reason)
        usage = dict(usage)
        usage['cold_budget_overflow'] = True
        usage['cold_prompt_estimate'] = int(estimate)
        usage['cold_prompt_target'] = int(target)
        usage['cold_history_budget'] = int(history_budget)
        usage['cold_budget_mode'] = 'token_budget'
        if guard_target is not None:
            usage['cold_rebuild_guard_triggered'] = True
            usage['cold_rebuild_guard_overflow'] = True
            usage['cold_rebuild_guard'] = int(guard_target)
        if cold_history_trimmed is not None:
            usage['cold_history_trimmed'] = bool(cold_history_trimmed)
        self.usage = usage


class NoBenefitRespawnError(RuntimeError):
    """A ``hard_context`` respawn would rebuild an equal-or-larger cold
    bootstrap than the one that just tripped ``hard_context``.

    Without evidence that a fresh cold bootstrap would actually be smaller,
    repeating the respawn only burns another full-context model call for no
    benefit. Fail closed instead of looping.
    """

    respawn_reason = 'hard_context_no_shrink'

    def __init__(self, *, new_estimate: int, baseline: int, usage=None):
        super().__init__(
            'hard_context_no_shrink: new_estimate=%d baseline=%d'
            % (int(new_estimate), int(baseline))
        )
        self.new_estimate = int(new_estimate)
        self.baseline = int(baseline)
        from cc_resident import empty_usage

        self.usage = usage if usage is not None else empty_usage(
            respawn_reason=self.respawn_reason,
        )
