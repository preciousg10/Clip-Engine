"""SELECT stage — Groq first-pass moment scoring, then pick the top clips.

(Named selectclips.py, not select.py: a module named `select` would shadow Python's
stdlib `select`, which asyncio/httpx/subprocess import on Linux — that would break
real runs. Everything else matches the spec's stage name "select".)

Groq does the volume work (scan the moment index, score each). Final taste is the
user's when they run the agent in Claude Code. Dedup against already-posted moments
(memory/posted_moments.json) so we never re-clip something we've shipped.

Offline mode scores by spike intensity + transcript richness (deterministic), so the
pipeline is testable without Groq.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

POSTED = C.MEMORY / "posted_moments.json"
MAX_CANDIDATES = 120     # cap what we hand Groq, to fit context

# Filler categories to kill outright — "if nothing has happened yet, it's not a moment."
FILLER_RE = re.compile(
    r"\b(we'?re live|we are live|going live|stream(ing)?( is)?( starting| soon)|"
    r"starting soon|start(ing)? in|be right back|brb|countdown|count down|"
    r"waiting (for|on)|hold on|one sec|give it a (sec|second|minute|moment)|"
    r"welcome (to|back)|mic check|sound check|can you (hear|guys hear)|test test|"
    r"chat check|is the (stream|mic|audio|sound)|almost ready|setting up|"
    r"getting started|about to start|two seconds|gimme a)\b", re.I)


def _overlaps(a, b):
    return a["source"] == b["source"] and not (a["end"] <= b["start"] or a["start"] >= b["end"])


def _is_filler(m):
    return bool(m.get("text") and FILLER_RE.search(m["text"]))


def merge_close(moments, gap):
    """Merge moments in the same source whose gap < `gap` seconds into one moment
    (union bounds, max intensity, joined text). Kills same-event duplication."""
    from collections import defaultdict
    by_src = defaultdict(list)
    for m in moments:
        by_src[m["source"]].append(m)
    out = []
    for ms in by_src.values():
        ms.sort(key=lambda x: x["start"])
        cur = None
        for m in ms:
            if cur and m["start"] - cur["end"] <= gap:
                cur["end"] = max(cur["end"], m["end"])
                if m.get("intensity", 0) > cur.get("intensity", 0):
                    cur["intensity"] = m["intensity"]
                    cur["type"] = m["type"]
                    cur["peak"] = m.get("peak")   # follow the peak to the loudest sub-moment
                if m.get("text"):
                    cur["text"] = ((cur.get("text", "") + " " + m["text"]).strip())[:400]
            else:
                if cur:
                    out.append(cur)
                cur = dict(m)
        if cur:
            out.append(cur)
    return out


def dedup(moments):
    posted = C.load_json(POSTED, default=[]) or []
    if not posted:
        return moments
    kept = [m for m in moments if not any(_overlaps(m, p) for p in posted)]
    C.log(f"dedup: dropped {len(moments) - len(kept)} already-posted moment(s).")
    return kept


def _spread_ok(m, used, min_sep):
    """True unless a pick from the same source is within min_sep seconds."""
    return all(m["source"] != p["source"] or abs(m["start"] - p["start"]) >= min_sep
               for p in used)


def _heuristic_scores(moments, n, min_sep):
    """Offline: intensity-led, favor moments that have transcript text, spread out."""
    scored = []
    for m in moments:
        s = m.get("intensity", 0) * (10 if m["type"] == "audio_spike" else 6)
        if m.get("text"):
            s += min(len(m["text"]), 120) / 12.0
        scored.append((s, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked, used = [], []
    for s, m in scored:
        if not _spread_ok(m, used, min_sep):
            continue
        picked.append({**m, "score": round(float(s), 2),
                       "reason": f"{m['type']} intensity {m.get('intensity')}"})
        used.append(m)
        if len(picked) >= n:
            break
    return picked


def _parse_json_array(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _groq_scores(client, campaign, moments, n, min_sep):
    cand = sorted(moments, key=lambda m: m.get("intensity", 0), reverse=True)[:MAX_CANDIDATES]
    lines = [{"id": m["id"], "type": m["type"], "intensity": m.get("intensity"),
              "t": round(m["start"], 1), "peak": m.get("peak"),
              "text": (m.get("text") or "")[:160]} for m in cand]
    knowledge = C.load_knowledge()[:2000]
    system = ("You are an elite short-form clipper doing FIRST-PASS moment scoring for a "
              "vertical clip campaign. PRIMARY criterion, above everything else: WOULD THE "
              "FIRST 2 SECONDS STOP A SCROLL? A moment can be opened on its peak (field "
              "'peak' = the loudest/chaos second); score how hard that opening beat hits. "
              "A merely-good moment with a killer openable beat BEATS a great moment with a "
              "weak open. Secondary: self-contained payoff, chaos/emotion, and fit with the "
              "campaign audience + rules below. "
              "Return ONLY a JSON array of objects "
              '{"id","score","reason"} with score 0-100. No prose.')
    user = (f"Campaign: {campaign}\n"
            f"Audience: {C.AUDIENCE_CONTEXT}\n\n"
            + (f"Campaign knowledge (apply this):\n{knowledge}\n\n" if knowledge else "")
            + f"Pick and score the strongest {n}+ moments from this index. Score the "
            f"first-2-seconds scroll-stop first, moment quality second "
            f"(id, type, intensity, start seconds t, peak second, transcript text):\n"
            f"{json.dumps(lines, ensure_ascii=False)}")
    raw = C.groq_chat(client, system, user, temperature=0.4, max_tokens=1500)
    arr = _parse_json_array(raw)
    if not arr:
        C.warn("Groq returned unparseable scores — falling back to heuristic.")
        return _heuristic_scores(moments, n, min_sep)
    by_id = {m["id"]: m for m in moments}
    scored = []
    for item in arr:
        m = by_id.get(item.get("id"))
        if not m:
            continue
        scored.append({**m, "score": float(item.get("score", 0)),
                       "reason": str(item.get("reason", ""))[:200]})
    scored.sort(key=lambda x: x["score"], reverse=True)
    picked, used = [], []
    for m in scored:
        if not _spread_ok(m, used, min_sep):
            continue
        picked.append(m)
        used.append(m)
        if len(picked) >= n:
            break
    return picked or _heuristic_scores(moments, n, min_sep)


def run(state):
    data = C.load_json(C.MOMENTS_JSON)
    if not data:
        C.fail("campaign/moments.json missing — run the index stage first.")
    cfg = state.get("config", {})
    n = int(cfg.get("clips_per_batch", 10))
    min_sep = float(cfg.get("min_separation_seconds", 60))
    merge_gap = float(cfg.get("merge_gap_seconds", 15))

    moments = data.get("moments", [])
    raw_count = len(moments)
    moments = [m for m in moments if not _is_filler(m)]                 # kill filler
    moments = merge_close(moments, merge_gap)                           # merge same-event
    moments = dedup(moments)                                            # drop already-posted
    C.log(f"moments: {raw_count} raw -> {len(moments)} after filler-kill + merge + dedup.")
    if not moments:
        C.fail("no candidate moments left after filtering — nothing to select.")

    campaign = (C.load_json(C.RULES_JSON) or {}).get("campaign", state.get("campaign") or "campaign")

    client = C.groq_client()
    if client is None:
        C.warn("offline mode — selecting via heuristic (no Groq).")
        selected = _heuristic_scores(moments, n, min_sep)
    else:
        selected = _groq_scores(client, campaign, moments, n, min_sep)

    C.save_json(C.SELECTED_JSON, {"campaign": campaign, "selected": selected})
    C.mark_stage(state, "select", selected=len(selected))
    C.log(f"select done: {len(selected)} moment(s) chosen.")


if __name__ == "__main__":
    run(C.load_state())
