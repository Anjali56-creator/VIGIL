"""Signal fusion via grouped noisy-OR, with exact leave-one-out attribution.

Why noisy-OR instead of a weighted sum:
  * bounded in [0,1] without clipping
  * monotone in every signal
  * diminishing returns, so several *correlated* detectors firing together
    do not spuriously saturate the score
  * exact attribution falls out for free (see leave_one_out)
"""
from __future__ import annotations

from .signals import Signal

# Correlated detectors share a group; the group is noisy-OR'd first, then groups
# are noisy-OR'd together. This stops e.g. a new device in a new city from being
# counted as two fully independent pieces of evidence.
GROUPS: dict[str, tuple[str, ...]] = {
    "amount": ("AMT_DEV",),
    "velocity": ("VEL_1H", "AUTH_FAIL"),
    "access": ("DEV_NEW", "GEO_DIST"),
    "temporal": ("TIME_ODD",),
}


def _contribs(signals: list[Signal]) -> dict[str, float]:
    return {s.code: s.weight * s.normalized for s in signals}


def _fuse(contribs: dict[str, float], exclude: str | None = None) -> float:
    p_keep = 1.0
    for _group, codes in GROUPS.items():
        g_keep = 1.0
        for code in codes:
            if code == exclude:
                continue
            c = contribs.get(code, 0.0)
            g_keep *= (1.0 - c)
        group_val = 1.0 - g_keep
        p_keep *= (1.0 - group_val)
    return 1.0 - p_keep


def fuse_score(signals: list[Signal]) -> float:
    """Fused probability in [0,1]. Multiply by 100 for the score."""
    return _fuse(_contribs(signals))


def leave_one_out(signals: list[Signal]) -> dict[str, float]:
    """Return each signal's contribution share (percentages summing to ~100).

    delta_i = P(all) - P(all without i)  >= 0 by monotonicity.
    share_i = delta_i / sum(delta).
    """
    contribs = _contribs(signals)
    full = _fuse(contribs)
    deltas = {s.code: max(0.0, full - _fuse(contribs, exclude=s.code)) for s in signals}
    total = sum(deltas.values())
    if total <= 0:
        return {code: 0.0 for code in deltas}
    return {code: round(100.0 * d / total, 1) for code, d in deltas.items()}
