"""Outputs: campaigns.json, campaigns_summary.md, and the terminal final report."""
import json
from datetime import datetime, timezone


def _fmt_pct(frac):
    return "n/a" if frac is None else f"{frac * 100:.0f}%"


def _fmt_money(v):
    return "n/a" if v is None else f"${v:,.0f}"


def _fmt_pay(c):
    if c.get("pay_per_1k") is None:
        return "n/a"
    unit = c.get("pay_unit") or "per_1k"
    return f"${c['pay_per_1k']:.2f}/1k (source: {unit})"


def _fmt_count(n):
    """Human follower/view count: 1_234_567 -> '1.2M'. 'unknown' when None."""
    if n is None:
        return "unknown"
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return f"{int(n):,}"


def _fmt_min_views(c):
    """Minimum views before a clip earns anything, flagged loudly when high."""
    mv = c.get("min_views_to_payout")
    if mv is None:
        mp = c.get("min_payout")
        return "no minimum found" if mp is None else f"${mp:g} min (rate unknown)"
    flag = "  ⚠ HIGH_MINIMUM" if c.get("high_minimum") else ""
    mp = c.get("min_payout")
    mp_txt = f"${mp:g} min → " if mp is not None else ""
    return f"{mp_txt}~{mv:,.0f} views to first payout{flag}"


def _fmt_source(c):
    """One-line source summary: name · per-platform reach · confidence · traction."""
    src = c.get("source") or {}
    conf = src.get("confidence") or "UNKNOWN"
    name = src.get("name") or "unidentified"
    handles = src.get("handles") or []
    parts = []
    for h in handles:
        who = h.get("handle") or h.get("platform")
        f = h.get("followers")
        tag = f"{h.get('platform')}:{who} {_fmt_count(f)}" if f is not None \
            else f"{h.get('platform')}:{who} (count unknown)"
        parts.append(tag)
    reach = src.get("reach_estimate")
    traction = src.get("recent_avg_views")
    line = f"**{name}** · confidence **{conf}**"
    line += f" · reach est. {_fmt_count(reach)}"
    if traction is not None:
        line += f" · recent traction {_fmt_count(traction)}/post"
    if parts:
        line += "\n    - " + "\n    - ".join(parts)
    return line


def _composite_of(c):
    return c.get("composite_score") or 0


def write_json(path, campaigns):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(campaigns),
        "campaigns": campaigns,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _breakdown_line(c):
    """Render the composite as a visible product so the ranking isn't a black box."""
    b = c.get("composite_breakdown")
    if not b:
        return f"- **Composite rank:** {_composite_of(c):.4f}"
    return (
        f"- **Composite rank:** {b.get('composite', 0):.4f} "
        f"= pay {b.get('pay_per_1k', 0):.2f} × budget {b.get('budget_remaining_fraction', 0):.2f} "
        f"× reach {b.get('reach_factor', 0):.2f} _({b.get('reach_basis')})_ "
        f"× clip {b.get('clippability', 0):.2f} "
        f"× conf {b.get('confidence_factor', 0):.2f} _({b.get('confidence')})_ "
        f"× min-penalty {b.get('minimum_penalty', 1)}"
        + ("  ⚠ HIGH_MINIMUM" if b.get("high_minimum") else "")
    )


def write_summary_md(path, campaigns):
    active = [c for c in campaigns if c.get("status") in ("scraped", "refreshed")]
    active.sort(key=lambda c: (_composite_of(c), c.get("pre_score", 0)), reverse=True)
    skipped = [c for c in campaigns if c.get("status") == "skipped_prefilter"]

    lines = []
    lines.append("# Scout — Content Rewards summary")
    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()}_")
    lines.append("")
    lines.append(
        f"{len(active)} campaigns passed the pre-filter · {len(skipped)} skipped. "
        "Sorted by **composite rank** (pay × budget-left × source-reach/traction × "
        "clippability, minus penalties for high minimum payout & low source "
        "confidence). The old **pre-score** is kept below it as a crude reference. "
        "Source popularity is best-effort: numbers are real or marked unknown, never "
        "guessed."
    )
    lines.append("")

    for c in active:
        name = c.get("name") or "(unnamed campaign)"
        url = c.get("url") or ""
        lines.append(f"## [{name}]({url})")
        lines.append("")
        lines.append(_breakdown_line(c))
        lines.append(f"- **Pre-score:** {c.get('pre_score', 0):.4f} _(old crude sort hint)_")
        lines.append(f"- **Pay:** {_fmt_pay(c)}")
        lines.append(f"- **Budget remaining:** {_fmt_pct(c.get('budget_remaining_fraction'))}"
                     f" of {_fmt_money(c.get('budget_total'))}")
        lines.append(f"- **Min payout viability:** {_fmt_min_views(c)}")
        lines.append(f"- **Source popularity:** {_fmt_source(c)}")
        lines.append(f"- **Platforms:** {', '.join(c.get('platforms') or []) or 'n/a'}")
        lines.append(f"- **Creator/brand:** {c.get('creator') or 'n/a'}")
        lines.append(f"- **Participants/clippers:** {c.get('participants') if c.get('participants') is not None else 'n/a'}")
        lines.append(f"- **Deadline:** {c.get('deadline') or 'n/a'}")
        lines.append(f"- **Scraped:** {c.get('scraped_at') or 'n/a'} · **Status:** {c.get('status')}")

        source_links = c.get("source_links") or []
        if source_links:
            lines.append("- **Source / footage:**")
            for link in source_links:
                lines.append(f"    - [{link}]({link}) · [open]({link})")

        footage = c.get("footage_stats") or []
        if footage:
            lines.append("- **Footage probe:**")
            for f in footage:
                dur = f.get("total_duration_sec")
                dur_txt = f"{dur / 3600:.1f}h" if dur else "n/a"
                lines.append(
                    f"    - {f.get('url')}: {f.get('video_count')} videos, "
                    f"{dur_txt} total, recent: {', '.join(f.get('recent_upload_dates') or []) or 'n/a'}"
                )

        lines.append("")
        lines.append("**Rules / requirements:**")
        lines.append("")
        rules = c.get("rules_text") or "_(none captured)_"
        lines.append(rules)
        lines.append("")
        lines.append("---")
        lines.append("")

    if skipped:
        lines.append("## Skipped by pre-filter")
        lines.append("")
        for c in skipped:
            name = c.get("name") or "(unnamed)"
            url = c.get("url") or ""
            lines.append(f"- [{name}]({url}) — {c.get('skip_reason') or 'filtered'}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def terminal_report(campaigns, *, db_total, new_count, failures):
    active = [c for c in campaigns if c.get("status") in ("scraped", "refreshed")]
    active.sort(key=lambda c: (_composite_of(c), c.get("pre_score", 0)), reverse=True)
    high_min = sum(1 for c in active if c.get("high_minimum"))

    print("")
    print("=" * 60)
    print("SCOUT — run complete")
    print("=" * 60)
    print(f"  Campaigns scraped/refreshed this run : {len(active)}")
    print(f"  Total campaigns in DB                : {db_total}")
    print(f"  New since last run                   : {new_count}")
    print(f"  Flagged HIGH_MINIMUM                 : {high_min}")
    print(f"  Failures (see errors.log)            : {failures}")
    print("")
    print("  Top 10 by composite rank (comp | conf | source reach):")
    if not active:
        print("    (none)")
    for c in active[:10]:
        name = (c.get("name") or "(unnamed)")[:34]
        src = c.get("source") or {}
        conf = (src.get("confidence") or "UNKNOWN")[:4]
        reach = _fmt_count(src.get("recent_avg_views") or src.get("reach_estimate"))
        flag = " !MIN" if c.get("high_minimum") else ""
        print(f"    {_composite_of(c):.4f}  {name:<34}  {conf:<4}  {reach:>7}{flag}")
    print("=" * 60)
