"""Ranking scores. Two live side by side, on purpose:

`pre_score` — the original CRUDE sort hint, kept unchanged and labeled as such:
    pay_per_1k * budget_remaining_fraction / max(participants, 1)
It ignores everything that actually matters and is only "look at these first".

`composite_score` — the richer rank the report sorts by. It multiplies the levers
that make a campaign worth a clip and then applies honest penalties:

    base       = pay_per_1k * budget_remaining * reach_factor * clippability
    composite  = base * confidence_factor * minimum_penalty

  - reach_factor: log10(1 + reach), where reach is RECENT TRACTION (avg recent
    views) when we have it, else follower/subscriber reach, else a neutral 1.0
    baseline (NOT a fabricated follower number — the confidence penalty covers it).
  - clippability: crude 0..1 proxy — short-form platforms + provided source footage.
  - confidence_factor: HIGH 1.0 / LOW 0.7 / UNKNOWN 0.5 — we trust reach less when
    we couldn't verify it.
  - minimum_penalty: 0.3 when a single ~1k-view clip earns nothing (HIGH_MINIMUM),
    else 1.0. This is what "deprioritize high-minimum hard" means numerically.

`composite_score(c)` returns (score, breakdown) so the report can show WHY a
campaign ranks where it does rather than a black-box number.
"""
import math

CONFIDENCE_FACTORS = {"HIGH": 1.0, "LOW": 0.7, "UNKNOWN": 0.5}
HIGH_MINIMUM_PENALTY = 0.3


def pre_score(c):
    """The original crude hint. Unchanged. Not a recommendation."""
    pay = c.get("pay_per_1k") or 0.0
    rem = c.get("budget_remaining_fraction")
    rem = rem if rem is not None else 0.0
    participants = c.get("participants") or 0
    return round(pay * rem / max(participants, 1), 6)


def clippability(c):
    """Rough 0..1 proxy for how easy this campaign is to make clips for.

    Short-form platforms (tiktok/shorts/reels) are the easy-clip case; provided
    source footage (drive/youtube links) means you don't have to source raw material.
    With no signal at all we return a neutral 0.5 rather than penalising the unknown.
    """
    plats = set(c.get("platforms") or [])
    has_source = bool(c.get("source_links"))
    if not plats and not has_source:
        return 0.5
    score = 0.0
    if plats & {"tiktok", "shorts", "reels"}:
        score += 0.6
    if has_source:
        score += 0.4
    return round(min(score, 1.0), 4) if score else 0.3


def _reach_input(c):
    """Reach value feeding the score, preferring recent traction over raw followers.

    Returns (value_or_None, basis) where basis is 'recent_traction', 'followers', or
    'unknown'. A creator with big followings but weak recent views ranks on the weak
    recent views — recent traction beats raw follower count.
    """
    src = c.get("source") or {}
    traction = src.get("recent_avg_views")
    if traction:
        return traction, "recent_traction"
    reach = src.get("reach_estimate")
    if reach:
        return reach, "followers"
    return None, "unknown"


def composite_score(c):
    """Return (composite_score, breakdown_dict). Never raises."""
    pay = c.get("pay_per_1k") or 0.0
    rem = c.get("budget_remaining_fraction")
    rem = rem if rem is not None else 0.0
    clip = clippability(c)

    reach_val, reach_basis = _reach_input(c)
    if reach_val:
        reach_factor = round(math.log10(1 + reach_val), 4)  # 1k->~3, 1M->~6
    else:
        reach_factor = 1.0  # neutral baseline — NOT a follower claim

    conf = (c.get("source") or {}).get("confidence") or "UNKNOWN"
    confidence_factor = CONFIDENCE_FACTORS.get(conf, 0.5)

    high_min = bool(c.get("high_minimum"))
    minimum_penalty = HIGH_MINIMUM_PENALTY if high_min else 1.0

    base = pay * rem * reach_factor * clip
    composite = round(base * confidence_factor * minimum_penalty, 6)

    breakdown = {
        "pay_per_1k": pay,
        "budget_remaining_fraction": round(rem, 4),
        "reach_value": reach_val,
        "reach_basis": reach_basis,
        "reach_factor": reach_factor,
        "clippability": clip,
        "confidence": conf,
        "confidence_factor": confidence_factor,
        "high_minimum": high_min,
        "minimum_penalty": minimum_penalty,
        "min_views_to_payout": c.get("min_views_to_payout"),
        "composite": composite,
    }
    return composite, breakdown
