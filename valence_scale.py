"""Normalize valence to the public bipolar scale [-1, 1]."""

from __future__ import annotations


def normalize_valence(raw: float, *, scale: str | None = None) -> float:
    """Convert stored valence to bipolar [-1, 1].

  Legacy ombre-brain frontmatter without ``valence_scale`` is treated as
  unipolar [0, 1]. Explicit ``bipolar`` values are clamped only.
    """
    value = float(raw)
    normalized_scale = (scale or 'unipolar').strip().lower()
    if normalized_scale == 'bipolar':
        return max(-1.0, min(1.0, value))
    if normalized_scale in {'unipolar', 'unit', '0_1', '01'}:
        return max(-1.0, min(1.0, value * 2.0 - 1.0))
    raise ValueError(f'unsupported valence_scale: {scale!r}')


def normalize_arousal(raw: float) -> float:
    return max(0.0, min(1.0, float(raw)))
