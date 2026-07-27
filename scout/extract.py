"""DOM-agnostic parsers + Playwright extraction.

The parsers (parse_pay, parse_money, ...) work on plain text, so they stay
correct regardless of how rough the selectors currently are. The extraction
helpers pull that text out of the page via the candidate selectors in
selectors.py and never raise on a missing field — they return None.
"""
import re
from urllib.parse import urljoin, urlparse

import selectors as S

BASE = "https://whop.com"

# Platform words we recognize in free text (used for the prefilter).
PLATFORM_WORDS = [
    "tiktok", "shorts", "reels", "youtube", "instagram",
    "twitch", "kick", "twitter", "facebook", "snapchat",
]

# Hosts we're willing to run a footage probe against.
CHANNEL_HOSTS = ("youtube.com", "youtu.be", "twitch.tv", "kick.com")

# Social platforms we recognize by URL host, for the source-popularity lookup.
# Order matters only for reporting; classification is host-substring based.
SOCIAL_HOSTS = {
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "instagram": ("instagram.com",),
    "kick": ("kick.com",),
    "x": ("twitter.com", "x.com"),
}


# --- pure parsers --------------------------------------------------------------
def parse_pay(text):
    """'$1.50 / 1k', '$2 per 1M', '$0.80/1K' -> normalized pay_per_1k."""
    out = {"pay_value": None, "pay_unit": None, "pay_per_1k": None}
    if not text:
        return out
    t = text.replace(",", "")
    m = re.search(
        r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|per)?\s*(1?\s*[kKmM]|1000000|1000)?", t
    )
    if not m:
        return out
    value = float(m.group(1))
    unit_raw = (m.group(2) or "").lower().replace(" ", "")
    if unit_raw in ("1m", "m", "1000000"):
        out.update(pay_value=value, pay_unit="per_1M", pay_per_1k=round(value / 1000.0, 4))
    else:  # default assumption is per-1k, the common Content Rewards unit
        out.update(pay_value=value, pay_unit="per_1k", pay_per_1k=round(value, 4))
    return out


def parse_money(text):
    if not text:
        return None
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def parse_remaining_fraction(text, total=None):
    """Return the fraction of budget still available, 0..1, or None.

    Handles '38% remaining', '62% paid out', and '$1,234 remaining' (when total
    is known).
    """
    if not text:
        return None
    pct = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if pct:
        val = float(pct.group(1)) / 100.0
        if re.search(r"paid|used|spent|out", text, re.I):
            return max(0.0, 1.0 - val)
        return max(0.0, min(1.0, val))
    money = parse_money(text)
    if money is not None and total:
        return max(0.0, min(1.0, money / total))
    return None


def parse_platforms(text):
    if not text:
        return []
    t = text.lower()
    return [p for p in PLATFORM_WORDS if p in t]


def parse_int(text):
    if not text:
        return None
    m = re.search(r"([0-9][0-9,]*)", text)
    return int(m.group(1).replace(",", "")) if m else None


def campaign_id_from_url(url):
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    seg = path.split("/")[-1] if path else ""
    return seg or url


def absolute(href):
    if not href:
        return None
    return urljoin(BASE, href)


def slugify(text):
    """Stable id from a campaign name (cards have no href/id in the list DOM)."""
    if not text:
        return None
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or None


def parse_budget_pair(text):
    """'$136,668/$250,000' -> (paid, total, remaining_fraction).

    On the card the first (bold) number is the amount paid out so far and tracks the
    progress bar; the second (faded) number is the total budget cap.
    """
    if not text:
        return (None, None, None)
    nums = [float(a.replace(",", "")) for a in re.findall(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)]
    if len(nums) >= 2:
        paid, total = nums[0], nums[1]
        rem = max(0.0, min(1.0, 1.0 - paid / total)) if total > 0 else None
        return (paid, total, rem)
    if len(nums) == 1:
        return (None, nums[0], None)
    return (None, None, None)


# --- minimum-payout viability --------------------------------------------------
# A campaign often gates the FIRST payout behind a minimum ("$10 minimum payout",
# "you must reach $5 to withdraw"). Combined with pay_per_1k that tells us how many
# views a clip needs before it earns a single cent. A high minimum means a normal
# ~1k-view clip earns nothing, which is a trap worth flagging loudly.
_MIN_PAYOUT_PATTERNS = (
    # keyword ... $amount    e.g. "minimum payout of $10", "payout threshold: $5"
    r"(?:min(?:imum)?(?:\s+payout)?|payout\s+(?:minimum|threshold)|threshold|"
    r"cash\s*out|withdraw(?:al)?)[^$\n]{0,40}\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    # $amount ... keyword    e.g. "$10 minimum", "$5 to withdraw", "$25 payout minimum"
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)[^$\n]{0,30}?"
    r"(?:min(?:imum)?|to\s+(?:cash\s*out|withdraw|be\s+paid|get\s+paid|payout)|payout\s+min)",
)


def parse_min_payout(text):
    """Smallest explicit minimum-payout dollar figure in free text, or None.

    Fires ONLY on explicit minimum/threshold/withdraw language so it never mistakes
    a pay rate ("$1/1K") or budget ("$250,000") for a minimum. If nothing matches we
    return None (unknown) rather than guessing.
    """
    if not text:
        return None
    found = []
    for pat in _MIN_PAYOUT_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            try:
                found.append(float(m.group(1).replace(",", "")))
            except (TypeError, ValueError):
                continue
    return min(found) if found else None


def min_views_to_payout(min_payout, pay_per_1k):
    """Views a single clip needs before it earns anything: min_payout / rate * 1000.

    None if either input is unknown/zero — we never invent a number.
    """
    if not min_payout or not pay_per_1k:
        return None
    return (min_payout / pay_per_1k) * 1000.0


# --- source handles ------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s)>\]\"'}]+", re.I)
# Bare "@handle on tiktok"-style mentions where a platform word sits nearby.
_MENTION_RE = re.compile(r"@([A-Za-z0-9_.]{2,30})", re.I)


def _classify_host(url):
    u = (url or "").lower()
    for platform, hosts in SOCIAL_HOSTS.items():
        if any(h in u for h in hosts):
            return platform
    return None


def _handle_from_url(platform, url):
    """Best-effort readable handle from a profile URL ('.../@name' or '.../name')."""
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return None
    if not path:
        return None
    seg = path.split("/")[0]
    if platform == "youtube" and not seg.startswith("@") and seg in ("channel", "c", "user"):
        parts = path.split("/")
        seg = parts[1] if len(parts) > 1 else seg
    return seg or None


def extract_handles(text, source_links=None):
    """Social profile handles referenced by a campaign brief.

    Pulls full URLs out of `text` and `source_links`, classifies each by host into a
    known platform (TikTok/YouTube/Instagram/Kick/X), and de-dupes. Returns a list of
    dicts: {platform, url, handle, followers: None, recent_avg_views: None}. The count
    fields start None on purpose — the social lookup fills them, and anything it can't
    retrieve stays None (unknown), never fabricated.
    """
    seen = {}
    urls = list(source_links or [])
    if text:
        urls += _URL_RE.findall(text)
    for raw in urls:
        url = (raw or "").rstrip('.,);]}"\'')
        platform = _classify_host(url)
        if not platform:
            continue
        key = (platform, url.lower())
        if key in seen:
            continue
        seen[key] = {
            "platform": platform,
            "url": url,
            "handle": _handle_from_url(platform, url),
            "followers": None,
            "recent_avg_views": None,
        }
    return list(seen.values())


def name_from_aria(aria):
    """'View <NAME> campaign' -> '<NAME>'."""
    if not aria:
        return None
    m = re.match(r"\s*View\s+(.*?)\s+campaign\s*$", aria)
    return (m.group(1) if m else aria).strip() or None


# --- locator helpers -----------------------------------------------------------
def _text(scope, candidates):
    """First non-empty inner_text among candidate selectors, else None."""
    for sel in candidates:
        try:
            loc = scope.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=1500).strip()
                if txt:
                    return txt
        except Exception:
            continue
    return None


def _safe_inner_text(scope):
    try:
        return scope.inner_text(timeout=1500)
    except Exception:
        return ""


def _first_href(scope, candidates):
    for sel in candidates:
        try:
            loc = scope.locator(sel).first
            if loc.count() > 0:
                href = loc.get_attribute("href")
                if href:
                    return href
        except Exception:
            continue
    return None


def _all_hrefs(scope, candidates):
    out = []
    for sel in candidates:
        try:
            locs = scope.locator(sel)
            for i in range(locs.count()):
                href = locs.nth(i).get_attribute("href")
                if href and href not in out:
                    out.append(href)
        except Exception:
            continue
    return out


# --- blocker detection ---------------------------------------------------------
def is_challenge(page):
    for sel in S.CHALLENGE_MARKERS:
        try:
            if page.locator(sel).first.count() > 0:
                return True
        except Exception:
            continue
    return False


def is_login_wall(page):
    url = (page.url or "").lower()
    if any(k in url for k in ("/login", "sign-in", "signin", "/auth")):
        return True
    try:
        if page.locator('input[type="password"]').first.count() > 0:
            return True
    except Exception:
        pass
    return False


# --- extraction ----------------------------------------------------------------
def extract_card(card):
    """Card-level fields from the list view. Never raises.

    Cards are <button> elements with no href/id: the name comes from the button's
    aria-label ("View <NAME> campaign"), the id is a slug of that name, and there is
    no per-card URL (the detail URL is only obtainable by clicking, done in Phase 2).
    """
    try:
        aria = card.get_attribute("aria-label")
    except Exception:
        aria = None
    name = name_from_aria(aria) or _text(card, S.CARD_NAME)

    pay = parse_pay(_text(card, S.CARD_PAY))
    paid, total, rem = parse_budget_pair(_text(card, S.CARD_BUDGET))
    platforms_text = _text(card, S.CARD_PLATFORMS) if S.CARD_PLATFORMS else None

    return {
        "id": slugify(name),
        "name": name,
        "url": None,
        "pay_value": pay["pay_value"],
        "pay_unit": pay["pay_unit"],
        "pay_per_1k": pay["pay_per_1k"],
        "budget_paid": paid,
        "budget_total": total,
        "budget_remaining_fraction": rem,
        "platforms": parse_platforms(platforms_text),
    }


def _join_texts(scope, candidates, sep="\n"):
    """Join the text of ALL matches of the first candidate that matches anything."""
    for sel in candidates:
        out = []
        try:
            locs = scope.locator(sel)
            for k in range(locs.count()):
                t = locs.nth(k).inner_text(timeout=1500).strip()
                if t and t not in out:
                    out.append(t)
        except Exception:
            continue
        if out:
            return sep.join(out)
    return None


def extract_detail(scope):
    """Detail fields from the campaign dialog. `scope` is the dialog Locator (or a
    frame/page). Missing -> None; never raises."""
    pay = parse_pay(_text(scope, S.DETAIL_PAY))
    # Budget shows as "$paid/$total" (same as the card); parse the pair.
    _paid, budget_total, budget_rem = parse_budget_pair(_text(scope, S.DETAIL_BUDGET_TOTAL))
    platforms_text = _text(scope, S.DETAIL_PLATFORMS) if S.DETAIL_PLATFORMS else None
    source_links = [absolute(h) for h in _all_hrefs(scope, S.SOURCE_LINK_SELECTORS)]

    # Requirement bullets (span.break-all); fall back to the whole dialog text.
    rules = _join_texts(scope, S.DETAIL_RULES)
    if not rules:
        rules = _safe_inner_text(scope) or None

    return {
        "name": _text(scope, S.DETAIL_NAME),
        "creator": _text(scope, S.DETAIL_CREATOR),
        "pay_value": pay["pay_value"],
        "pay_unit": pay["pay_unit"],
        "pay_per_1k": pay["pay_per_1k"],
        "budget_total": budget_total,
        "budget_remaining_fraction": budget_rem,
        "platforms": parse_platforms(platforms_text),
        "source_links": source_links,
        "rules_text": rules,
        "participants": parse_int(_text(scope, S.DETAIL_PARTICIPANTS)) if S.DETAIL_PARTICIPANTS else None,
        "deadline": _text(scope, S.DETAIL_DEADLINE),
    }
