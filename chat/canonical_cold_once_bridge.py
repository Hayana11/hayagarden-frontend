"""Cold-start continuity selector for the canonical memory bridge.

This is the G4-2 policy seam.  It chooses exactly one opening-continuity
payload: canonical ``recent/current.md`` when available, otherwise the existing
legacy Ombre handoff.  It does not itself alter the resident lifecycle or call
any provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from chat.canonical_memory_context import (
    CanonicalMemoryContext,
    format_current_for_cold_once,
    load_canonical_memory_context,
)


@dataclass(frozen=True)
class ColdContinuitySelection:
    text: str
    source: str
    canonical_snapshot: CanonicalMemoryContext
    legacy_error: Optional[str] = None


def select_cold_continuity(
    legacy_loader: Callable[[], Optional[str]],
    *,
    snapshot: Optional[CanonicalMemoryContext] = None,
) -> ColdContinuitySelection:
    """Choose canonical current or legacy handoff, never both.

    Canonical archive failures/empty scaffolds are intentionally treated as
    absence so the established legacy handoff remains the fallback.  Once a
    non-empty canonical current exists, ``legacy_loader`` is not called at all;
    this prevents duplicate continuity from being injected into the same cold
    session.
    """
    resolved = snapshot or load_canonical_memory_context()
    canonical_text = format_current_for_cold_once(resolved)
    if canonical_text:
        return ColdContinuitySelection(
            text=canonical_text,
            source='canonical_current',
            canonical_snapshot=resolved,
        )

    legacy_error = None
    legacy_text = ''
    try:
        value = legacy_loader()
        if value and str(value).strip() and '无交接信息' not in str(value):
            legacy_text = '## 开窗交接\n' + str(value).strip()
    except Exception as exc:  # fail-open: chat must stay available
        legacy_error = type(exc).__name__

    return ColdContinuitySelection(
        text=legacy_text,
        source='legacy_handoff' if legacy_text else 'none',
        canonical_snapshot=resolved,
        legacy_error=legacy_error,
    )
