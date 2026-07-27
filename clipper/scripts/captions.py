"""CAPTION GAUNTLET — 10 caption lines per clip, then attack them.

flzsh style: ONE top line, hook+context+joke in one, stakes+outcome+emotion, casual
lowercase energy, emoji as punctuation. Groq drafts; the gauntlet kills any candidate
containing a rules.json banned word BEFORE scoring, scores survivors against the
account style, and keeps the best (runner-up saved as a variant).

Also produces the rules-compliant per-platform text used in drafts/manifest.json.
Offline mode uses deterministic templates so the pipeline is testable without Groq.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⁩⁦]")

MAX_WORDS = 8
N_CANDIDATES = 12
# The caption's ONLY job is to make the payoff feel mandatory. Every kept caption must
# hit one of these five proven hook patterns; anything that just describes or gives the
# payoff away is killed.
HOOK_PATTERNS = {
    # open question
    "question": re.compile(
        r"(\?|^\s*(how|why|what|who|when|did|does|do|is|are|which)\b)", re.I),
    # stakes (money / high-stakes framing that still withholds the outcome)
    "stakes": re.compile(
        r"(\$\s*\d|\b(on the line|for the (win|title|lead)|to win|everything|last (place|second|lap)|"
        r"final (lap|round|race)|match point|sudden death|winner takes)\b)", re.I),
    # disbelief
    "disbelief": re.compile(
        r"\b(no way|no shot|nah|cant believe|can'?t believe|cant be real|can'?t be real|"
        r"not real|how is this real|unreal|i'?m done|im done)\b", re.I),
    # controversy bait
    "controversy": re.compile(
        r"\b(shouldn'?t|should not|didn'?t count|doesn'?t count|robbed|rigged|cheated|"
        r"illegal|not fair|how is this allowed|this ain'?t it)\b", re.I),
    # direct address / open loop
    "direct": re.compile(
        r"\b(wait for|wait till|watch(?: till| until)?|keep watching|you (have|need) to see|"
        r"tell me why|pov|the way|watch this|look what)\b", re.I),
}
HOOK_RE = re.compile("|".join(p.pattern for p in HOOK_PATTERNS.values()), re.I)

# Kill outright: descriptions and past-tense summaries that give the payoff away.
DESCRIPTIVE_RE = re.compile(
    r"\b(we (just|made)|here (is|are)|this is (a|the)|reacts?|reaction|highlights?|clip of|"
    r"footage|compilation|moment where|when (he|she|they) said)\b", re.I)
# Past-tense reveal: subject + past-tense outcome verb = the ending is spoiled. Only a
# giveaway when the caption isn't also withholding via a hook (checked in quality_kill).
GIVEAWAY_RE = re.compile(
    r"\b(he|she|they|it|the \w+)\s+(won|lost|crashed|died|scored|beat|smashed|flipped|"
    r"finished|ended|fell|missed|nailed|dropped|broke)\b", re.I)

OFFLINE_TEMPLATES = [
    "how did this even happen 😭",
    "no way this actually happened 💀",
    "wait for the last second 🔥",
    "this shouldn't have counted fr 😭",
    "why is this so unserious 💀",
    "who told him to do this 😭",
    "watch till the very end 👀",
    "how is this even real 😭",
    "tell me why he did this 😭",
    "nah this cant be real 💀",
    "the way this ends is diabolical 💀",
    "pov you witness peak chaos 🐹",
]


# --- banned-word gauntlet (the load-bearing rules gate) ------------------------
def banned_hit(text, banned):
    """Return the first banned word/phrase present in text, or None."""
    low = (text or "").lower()
    for term in banned:
        t = term.lower().strip()
        if not t:
            continue
        if " " in t:            # multi-word phrase: substring match
            if t in low:
                return term
        elif re.search(rf"\b{re.escape(t)}\b", low):
            return term
    return None


def gauntlet(candidates, banned):
    """Kill any candidate containing a banned word; return (survivors, killed)."""
    survivors, killed = [], []
    for c in candidates:
        hit = banned_hit(c, banned)
        if hit:
            killed.append({"caption": c, "reason": f"banned word: {hit}"})
        else:
            survivors.append(c)
    return survivors, killed


# --- quality gate + style scoring (flzsh) --------------------------------------
def _word_count(text):
    """Words for the length cap — emoji/punctuation tokens don't count (they're
    punctuation, not words), so an 8-word line + emoji isn't wrongly killed."""
    return sum(1 for w in text.split() if any(ch.isalnum() for ch in w))


def quality_kill(cap):
    """Hard-kill reasons (before scoring). Returns a reason or None."""
    if _word_count(cap) > MAX_WORDS:
        return f"too long (>{MAX_WORDS} words)"
    if "\n" in cap:
        return "multi-line"
    if DESCRIPTIVE_RE.search(cap):
        return "merely describes (no hook)"
    if not HOOK_RE.search(cap):
        return "no hook pattern (question/stakes/disbelief/controversy/direct address)"
    if GIVEAWAY_RE.search(cap) and not HOOK_PATTERNS["question"].search(cap):
        return "past-tense summary gives the payoff away"
    return None


def score_caption(text):
    """Higher = more scroll-stopping. Rewards the five hook patterns, punishes
    description and payoff-spoiling past-tense summaries."""
    s = 0.0
    if EMOJI_RE.search(text):
        s += 2
    hooks = [name for name, rx in HOOK_PATTERNS.items() if rx.search(text)]
    s += 4 * len(hooks)                           # each hook pattern present stops a scroll
    if "question" in hooks or "disbelief" in hooks:
        s += 2                                    # the strongest cold openers
    if DESCRIPTIVE_RE.search(text):
        s -= 6
    if GIVEAWAY_RE.search(text) and "question" not in hooks:
        s -= 5                                    # spoils the payoff
    wc = _word_count(text)
    if wc <= 6:
        s += 2
    if wc > MAX_WORDS:
        s -= 8
    letters = [ch for ch in text if ch.isalpha()]
    if letters and sum(ch.islower() for ch in letters) / len(letters) > 0.85:
        s += 1                                    # lowercase energy
    if text.strip().endswith("."):
        s -= 1
    return s


# --- candidate generation ------------------------------------------------------
def _offline_candidates(moment):
    # Rotate templates by moment id so different clips get different captions (offline
    # is for the self-test; real captions come from Groq).
    import hashlib
    rot = int(hashlib.md5((moment.get("id", "")).encode()).hexdigest(), 16) % len(OFFLINE_TEMPLATES)
    return OFFLINE_TEMPLATES[rot:] + OFFLINE_TEMPLATES[:rot]


def _groq_candidates(client, campaign, moment, style_notes):
    system = (
        "You write TOP captions for flzsh-style vertical clips. The caption's ONLY job "
        "is to make the payoff feel MANDATORY to watch. RULES: ONE line, MAX 8 words, "
        "all lowercase, casual grammar, emoji as punctuation. Every caption MUST use one "
        "of these five proven hook patterns:\n"
        "  1) open question — 'how did the yellow one win THIS'\n"
        "  2) stakes — '$10k on the line and he does THIS'\n"
        "  3) disbelief — 'no way this actually happened'\n"
        "  4) controversy bait — 'this shouldn't have counted'\n"
        "  5) direct address — 'wait for the finish'\n"
        "BANNED: descriptions, past-tense summaries, or anything that GIVES AWAY the "
        "payoff (never say who won / what happened). Withhold the ending; make them watch "
        "for it. Attack each line with 'why would someone scroll past this'. "
        "Respect the campaign's banned words/topics and style in the knowledge below. "
        f"Return ONLY a JSON array of {N_CANDIDATES} strings.")
    knowledge = C.load_knowledge()[:1500]
    user = (f"Audience: {C.AUDIENCE_CONTEXT}\n\n"
            + (f"Campaign knowledge (apply this):\n{knowledge}\n\n" if knowledge else "")
            + f"Campaign: {campaign}\nStyle notes: {style_notes}\n"
            f"Moment (type={moment['type']}, text={moment.get('text','')!r}). "
            f"Give {N_CANDIDATES} caption lines, each using one of the five hook patterns.")
    raw = C.groq_chat(client, system, user, temperature=0.9, max_tokens=800)
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            arr = [str(x).strip() for x in json.loads(m.group(0)) if str(x).strip()]
            if arr:
                return arr[:N_CANDIDATES]
        except Exception:
            pass
    C.warn("Groq caption output unparseable — using offline templates for this clip.")
    return _offline_candidates(moment)


# --- per-platform text ---------------------------------------------------------
def _required_hashtags(rules):
    tags = []
    for r in rules.get("required_elements", []):
        if r["type"] == "hashtag":
            tag = r["detail"].replace("Include", "").strip()
            if tag:
                tags.append(tag)
    return tags


def platform_text(caption, rules, banned):
    req = _required_hashtags(rules)
    base_tags = req + ["#fyp", "#clips", "#viral"]
    # filter tags through banned words too
    tags = [t for t in dict.fromkeys(base_tags) if not banned_hit(t, banned)]
    shorts = caption.rstrip("😭✌️🔥💀🙏✋ ").strip() or caption
    out = {
        "tiktok_caption": (caption + "  " + " ".join(req)).strip(),
        "shorts_title": shorts[:90],
        "reels_hashtags": tags,
        "suggested_post_window": "evening 6–9pm local (peak engagement)",
    }
    # final safety: none of the emitted text may carry a banned word
    for k in ("tiktok_caption", "shorts_title"):
        if banned_hit(out[k], banned):
            out[k] = caption
    return out


def run(state):
    selected = C.load_json(C.SELECTED_JSON)
    if not selected:
        C.fail("campaign/selected.json missing — run the select stage first.")
    rules = C.load_json(C.RULES_JSON) or {}
    # Gauntlet screens both banned words AND banned topics from intake's analysis.
    banned = list(rules.get("banned_words", C.DEFAULT_BANNED_WORDS)) + list(rules.get("banned_topics", []))
    campaign = rules.get("campaign", state.get("campaign") or "campaign")
    client = C.groq_client()
    if client is None:
        C.warn("offline mode — generating captions from templates (no Groq).")

    clips = []
    for m in selected["selected"]:
        cands = _offline_candidates(m) if client is None else _groq_candidates(
            client, campaign, m, "stakes+outcome+emotion, lowercase, emoji punctuation")
        # normalize: one line, lowercase energy (flzsh)
        cands = [" ".join(c.split()).lower() for c in cands if c and c.strip()]
        # gauntlet 1: banned words
        survivors, killed = gauntlet(cands, banned)
        # gauntlet 2: quality (max 8 words, no describing, single line)
        kept = []
        for c in survivors:
            reason = quality_kill(c)
            if reason:
                killed.append({"caption": c, "reason": reason})
            else:
                kept.append(c)
        survivors = kept
        if not survivors:
            C.warn(f"all captions killed for moment {m['id']} — using a safe curiosity fallback.")
            survivors = ["what just happened here 😭"]
        ranked = sorted(survivors, key=score_caption, reverse=True)
        best = ranked[0]
        variant = ranked[1] if len(ranked) > 1 else None
        clips.append({
            "moment_id": m["id"], "source": m["source"],
            "start": m["start"], "end": m["end"], "type": m["type"],
            "peak": m.get("peak"),                # cold-open anchor for the cut stage
            "score": m.get("score"), "reason": m.get("reason"),
            "candidates": cands, "killed": killed,
            "caption": best, "variant": variant,
            **platform_text(best, rules, banned),
        })

    C.save_json(C.CAPTIONS_JSON, {"campaign": campaign, "clips": clips})
    C.mark_stage(state, "captions", clips=len(clips), killed=sum(len(c["killed"]) for c in clips))
    C.log(f"captions done: {len(clips)} clip(s); "
          f"{sum(len(c['killed']) for c in clips)} candidate(s) killed by rules.")


if __name__ == "__main__":
    run(C.load_state())
