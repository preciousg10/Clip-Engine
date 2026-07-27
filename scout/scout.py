"""Scout — a personal Whop Content Rewards research tool.

Runs standalone from a terminal:  python scout.py
It drives *your own* logged-in, visible Chromium at human pace, once a day, and
degrades to "not today, browse manually" the moment anything looks like a block.
See README.md for the full flow. No Claude involvement at runtime.

Flags:
  --force     ignore the once-daily (20h) guard
  --refresh   full re-scrape of every campaign, not just new ones (delta is default)
  --probe     log in, screenshot + dump a card and a detail page to confirm selectors
"""
import argparse
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import extract
import footage as footage_mod
import report
import selectors as S
import social as social_mod
from browser import Session
from pacing import Pacer
from scoring import composite_score, pre_score
from state import State


# =============================================================================
# CONFIG — every tunable lives here. Edit these defaults to taste.
# (Selectors are their own concern; they live in selectors.py.)
# =============================================================================
@dataclass(frozen=True)
class Config:
    # session / browser
    profile_dir: str = "./whop_profile"       # persistent login profile
    viewport: tuple = (1366, 768)             # normal desktop window

    # once-daily + session caps
    min_hours_between_runs: float = 20.0      # refuse if last run < this (unless --force)
    max_campaigns: int = 200                  # per-run detail cap
    max_minutes: float = 90.0                 # per-run wall-clock cap

    # human pacing (seconds unless noted)
    base_delay: tuple = (1.5, 6.0)            # normal per-page think time
    fast_delay: tuple = (0.3, 0.8)            # fast click-through
    fast_chance: float = 0.20                 # 20% of delays are fast
    afk_every: tuple = (6, 15)                # campaigns between AFK breaks
    afk_break: tuple = (45, 150)              # AFK break length
    long_afk_chance: float = 0.10             # 10% of breaks are long
    long_afk_break: tuple = (180, 300)        # 3–5 min long break
    revisit_chance: float = 0.05              # 5% double-back to previous
    scroll_step: tuple = (300, 800)           # px per wheel tick
    scroll_pause: tuple = (0.5, 1.5)          # s between wheel ticks
    list_stable_rounds: int = 3               # scroll rounds w/ no new cards = "end"

    # pre-filter (applied only to values we actually parsed — nothing invisible)
    prefilter_min_pay_per_1k: float = 1.0     # $/1k floor; set 0 to disable
    prefilter_min_budget_remaining: float = 0.15   # must be strictly greater
    prefilter_required_platforms: tuple = ("tiktok", "shorts", "reels")

    # scoring / footage
    footage_top_n: int = 30                   # probe this many top campaigns
    social_top_n: int = 30                    # source-popularity lookup on this many

    # minimum-payout viability: a campaign whose first payout needs more than this
    # many views (min_payout / pay_per_1k * 1000) is flagged HIGH_MINIMUM and
    # deprioritized hard — a normal ~1k-view clip would earn nothing.
    min_payout_max_views: float = 1000.0

    # output paths
    state_path: str = "state.json"
    campaigns_path: str = "campaigns.json"
    summary_path: str = "campaigns_summary.md"
    errors_path: str = "errors.log"


CONFIG = Config()


# =============================================================================
class StopRun(Exception):
    """Raised to abort the whole run immediately (challenge / repeated failures)."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def log_error(path, url, exc):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{_now_iso()}\t{url}\t{type(exc).__name__}: {exc}\n")


def safe_goto(page, url, *, wait_until="domcontentloaded", timeout=30000):
    """Navigate without ever raising.

    Manual login triggers Whop's own OAuth redirect chain; a programmatic goto that
    collides with it raises "Navigation interrupted by another navigation" (or
    ERR_ABORTED). None of that should be able to kill a run. Returns True if the
    navigation settled, False otherwise.
    """
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout)
        return True
    except Exception as e:
        first_line = (str(e).splitlines() or [type(e).__name__])[0]
        print(f"    (navigation to {url} didn't settle: {first_line})")
        return False


# --- frame + selector helpers --------------------------------------------------
# The cards live inside a cross-origin app iframe (apps.whop.com). "scope" below is
# whatever we run selectors against — the app Frame in practice, the page as a
# fallback. Frame and Page share the .locator/.content/.url interface.
def pick_card_selector(scope):
    """The CARD candidate that currently matches the most elements in `scope`."""
    best, best_n = None, 0
    for sel in S.CARD:
        try:
            n = scope.locator(sel).count()
        except Exception:
            n = 0
        if n > best_n:
            best, best_n = sel, n
    return best, best_n


def get_app_frame_locator(page):
    """A FrameLocator for the Content Rewards app iframe. Used for ALL list
    queries/scrolling/clicks: it re-resolves the frame lazily on every call, so it
    survives the app re-rendering (unlike a captured Frame object). Returns None if
    the iframe element isn't present yet."""
    for sel in S.APP_IFRAME:
        try:
            if page.locator(sel).first.count() > 0:
                return page.frame_locator(sel)
        except Exception:
            continue
    return None


def get_app_frame(page, timeout=20):
    """Wait for and return the app iframe's Frame OBJECT (apps.whop.com). Used only
    where we need `.content()` (the probe dump) or `.url` (detail navigation
    tracking) — things a FrameLocator can't give. For querying elements, prefer
    get_app_frame_locator."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for fr in page.frames:
            try:
                if fr is not page.main_frame and S.APP_FRAME_URL_HINT in (fr.url or ""):
                    return fr
            except Exception:
                continue
        time.sleep(1.0)
    return None


def _iframe_box(page):
    for sel in S.APP_IFRAME:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                box = loc.bounding_box()
                if box:
                    return box
        except Exception:
            continue
    return None


def scroll_list(page, pacer, steps=None):
    """Human wheel-scroll INSIDE the app iframe (cursor parked over it), so the
    frame's own feed scrolls rather than the top page. Real wheel events, no JS."""
    steps = steps if steps is not None else random.randint(2, 5)
    box = _iframe_box(page)
    for _ in range(steps):
        try:
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.wheel(0, random.randint(*pacer.scroll_step))
        except Exception:
            pass
        time.sleep(random.uniform(*pacer.scroll_pause))


def wait_for_feed(page, pacer, timeout=40, min_anchors=30):
    """The list is a client-rendered app inside a cross-origin iframe. Wait for that
    iframe, then poll INSIDE it (via a FrameLocator) until its campaign feed renders.

    Returns the app FrameLocator (whether or not cards were detected, so callers can
    still query/dump), or None if the iframe element never appeared. Selector-
    agnostic readiness: a CARD candidate matching several elements, or the frame's
    anchor count growing past baseline and stabilizing.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    # Scroll the top page down so the app iframe mounts past the hero and is in view.
    pacer.human_scroll(page, steps=4)

    # Wait for the iframe element itself to exist, then build a FrameLocator.
    fl_deadline = time.monotonic() + 20
    fl = None
    while time.monotonic() < fl_deadline:
        fl = get_app_frame_locator(page)
        if fl is not None:
            break
        time.sleep(1.0)
    if fl is None:
        print("    app iframe (apps.whop.com) not found yet.")
        return None

    deadline = time.monotonic() + timeout
    last, stable = -1, 0
    while time.monotonic() < deadline:
        sel, n = pick_card_selector(fl)
        if sel and n >= 3:
            return fl
        try:
            anchors = fl.locator("a").count()
        except Exception:
            anchors = 0
        if anchors >= min_anchors and anchors == last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = anchors
        scroll_list(page, pacer, steps=2)
        time.sleep(1.2)
    return fl


def _app_id():
    m = re.search(r"(app_[A-Za-z0-9]+)", S.LIST_URL)
    return m.group(1) if m else ""


def _on_content_rewards(page):
    app_id = _app_id().lower()
    return bool(app_id and app_id in (page.url or "").lower())


def _find_banner(page):
    for sel in S.DISCOVER_BANNER:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def navigate_to_list(page, pacer):
    """Reach the Content Rewards app the way a user would: open /discover/, then
    CLICK the Clipping/Content Rewards banner. A direct goto to the app URL gets
    bounced back to /discover/ by Whop's client router, so clicking is the reliable
    path — the direct goto is only a fallback. Never raises. Returns True if we end
    up on the content-rewards app page.
    """
    if _on_content_rewards(page):
        return True

    # Preferred path: discover page, then click the banner.
    if safe_goto(page, S.DISCOVER_URL):
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        banner = _find_banner(page)
        if banner is not None:
            pacer.maybe_hover(banner)
            pacer.page_delay()
            try:
                banner.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                time.sleep(1.0)
            except Exception as e:
                first = (str(e).splitlines() or ["?"])[0]
                print(f"    (banner click didn't take: {first})")
        else:
            print("    (couldn't find the Content Rewards banner on /discover/)")

    # Fallback: a direct goto (may be bounced, but worth one try).
    if not _on_content_rewards(page):
        print("    falling back to direct navigation to the app URL...")
        safe_goto(page, S.LIST_URL)
    return _on_content_rewards(page)


# --- login ---------------------------------------------------------------------
# Nothing in this phase may ever crash the run. We never navigate programmatically
# while the user is mid-login (that's what collides with the OAuth redirect); we
# only navigate *after* their Enter, and even then via safe_goto with retries.
def _manual_login_prompt():
    print("\n" + "-" * 60)
    print("Log in manually in the browser window (email / Google / whatever).")
    print("Take all the time you need — Scout will NOT navigate while you do.")
    print("When you're fully logged in, come back here and press Enter.")
    print("(Scout never touches your credentials or automates the login form.)")
    print("-" * 60)
    input("Press Enter once logged in... ")


def _reach_list_or_wait(page, pacer):
    """Get to the Content Rewards list after login by clicking through /discover/.
    Retry a few times; if it still isn't reachable, show what we see and wait for
    another Enter — never exit."""
    while True:
        for attempt in range(1, 4):
            navigate_to_list(page, pacer)
            time.sleep(1.0)
            if not extract.is_login_wall(page) and _on_content_rewards(page):
                return
            print(f"    list not reachable yet (attempt {attempt}/3)...")
            time.sleep(2)
        print("\nCouldn't reach the Content Rewards list after 3 tries.")
        print(f"  Current URL : {page.url}")
        print(f"  Login wall? : {extract.is_login_wall(page)}")
        print("Finish logging in or navigate there manually, then press Enter to retry.")
        input("Press Enter to retry (or Ctrl+C to quit)... ")


def ensure_logged_in(session, first_run, pacer, always_prompt=False):
    page = session.page

    # always_prompt (used by --probe): never trust the login-wall heuristic — the
    # whole point of a probe is to dump the *logged-in* DOM, and the persistent
    # profile makes first_run False after the very first launch even when we're not
    # actually logged in. So force the manual flow every probe.
    if first_run or always_prompt:
        # Open a benign page and then get out of the way. No goto to the list until
        # the user has finished logging in and pressed Enter.
        safe_goto(page, "https://whop.com/")
        _manual_login_prompt()
        _reach_list_or_wait(page, pacer)
        return

    # Returning session: click through to the list once. If we're logged out, fall
    # back to the same manual flow — still never exiting.
    navigate_to_list(page, pacer)
    time.sleep(1.0)
    if not extract.is_login_wall(page) and _on_content_rewards(page):
        return
    _manual_login_prompt()
    _reach_list_or_wait(page, pacer)


# --- probe ---------------------------------------------------------------------
def run_probe(session, pacer):
    # The final "press Enter to close" pause must ALWAYS fire — including the
    # no-cards-matched early return and any mid-probe error — or a fast probe closes
    # the window instantly and can kill the logged-in session. Hence try/finally.
    try:
        _probe_body(session, pacer)
    finally:
        print("\nProbe done. probe_list.html / probe_detail.html + the PNGs are saved.")
        print("Send them to Claude to fix selectors.py, then run `python scout.py`.")
        input("\nPress Enter to close the browser... ")


def _probe_body(session, pacer):
    page = session.page
    print("\nProbe: opening Discover and clicking through to Content Rewards...")
    on_list = navigate_to_list(page, pacer)
    print(f"  On content-rewards app page: {on_list} ({page.url})")
    print("  Waiting for the app iframe + feed to render...")
    fl = wait_for_feed(page, pacer)              # FrameLocator for queries
    frame = get_app_frame(page)                  # Frame object for .content()/.url
    scope = fl or page
    print(f"  App frame: {'found — ' + (frame.url or '') if frame else 'NOT FOUND'}")

    # Dump the FRAME's document — that's where the cards live. page.content() misses
    # them entirely (cross-origin iframe).
    if frame is not None:
        try:
            Path("probe_list.html").write_text(frame.content(), encoding="utf-8")
            print("  Saved probe_list.html (app-frame document)")
        except Exception as e:
            print(f"  (frame content unavailable, dumping top page: {e})")
            Path("probe_list.html").write_text(page.content(), encoding="utf-8")
    else:
        Path("probe_list.html").write_text(page.content(), encoding="utf-8")
        print("  Saved probe_list.html (top page — frame not found)")

    sel, n = pick_card_selector(scope)
    print(f"  Best card selector: {sel!r} matched {n} element(s).")
    if not sel or not n:
        print("  No cards matched yet, but probe_list.html holds the app-frame DOM.")
        print("  Send it to Claude to fix selectors.CARD, then re-probe.")
        return

    card = scope.locator(sel).first
    try:
        card.screenshot(path="probe_card.png")
        print("  Saved probe_card.png")
    except Exception as e:
        print(f"  (could not screenshot card: {e})")
    print("  Sample card extraction:")
    for k, v in extract.extract_card(card).items():
        print(f"    {k}: {v}")

    _probe_detail(page, pacer, card, frame)


def _probe_detail(page, pacer, card, list_frame):
    """Click a card and figure out whether detail opens in the FRAME or the TOP page
    (handle both), then dump whichever document holds the detail and sample-extract."""
    pre_page = page.url
    pre_frame = list_frame.url if list_frame else None
    clicked = False
    try:
        pacer.maybe_hover(card)
        pacer.page_delay()
        try:
            card.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        card.click(timeout=8000)
        clicked = True
    except Exception as e:
        print(f"  (normal click failed: {(str(e).splitlines() or ['?'])[0]}; retrying with force)")
        try:
            card.click(timeout=5000, force=True)
            clicked = True
        except Exception as e2:
            print(f"  (force click failed too: {(str(e2).splitlines() or ['?'])[0]})")
    time.sleep(2.5)

    post_frame_obj = get_app_frame(page)
    post_page = page.url
    post_frame = post_frame_obj.url if post_frame_obj else None
    navigated = (post_page != pre_page) or (post_frame != pre_frame)
    print(f"  Clicked: {clicked}")
    print(f"  After click — page: {pre_page} -> {post_page}")
    print(f"               frame: {pre_frame} -> {post_frame}")

    # Dump the best available detail document REGARDLESS, so probe_detail.html always
    # exists for selector work. Warn loudly if nothing actually navigated.
    if post_frame_obj is not None:
        detail_doc, detail_scope = post_frame_obj, (get_app_frame_locator(page) or page)
    else:
        detail_doc, detail_scope = page, page
    try:
        Path("probe_detail.html").write_text(detail_doc.content(), encoding="utf-8")
        page.screenshot(path="probe_detail.png", full_page=True)
        tag = "looks like a detail page" if navigated else "WARNING: nothing navigated — may still be the list"
        print(f"  Saved probe_detail.html / probe_detail.png ({tag})")
        print("  Sample detail extraction:")
        for k, v in extract.extract_detail(detail_scope).items():
            print(f"    {k}: {v}")
    except Exception as e:
        print(f"  (detail dump skipped: {(str(e).splitlines() or ['?'])[0]})")


# --- phase 1: list pass --------------------------------------------------------
def collect_cards(page, pacer, cfg, deadline_ts):
    print("Phase 1 — scanning the Content Rewards list (human scroll)...")
    if extract.is_challenge(page):
        raise StopRun("challenge on the list page")
    # Cards render inside the app iframe; wait_for_feed returns that FrameLocator.
    fl = wait_for_feed(page, pacer)
    scope = fl or page
    sel, n = pick_card_selector(scope)
    if not sel:
        print("  No cards found with current selectors. Run `python scout.py --probe`.")
        return []

    cards_by_id = {}
    stable = 0
    while stable < cfg.list_stable_rounds:
        locs = scope.locator(sel)
        new_found = 0
        for i in range(locs.count()):
            try:
                data = extract.extract_card(locs.nth(i))
            except Exception:
                continue
            cid = data.get("id")
            if cid and cid not in cards_by_id:
                cards_by_id[cid] = data
                new_found += 1
        print(f"  {len(cards_by_id)} unique cards seen...", end="\r")

        stable = stable + 1 if new_found == 0 else 0
        if time.monotonic() > deadline_ts:
            print("\n  Time budget reached during the list scan.")
            break
        scroll_list(page, pacer)

    print(f"\n  Phase 1 done: {len(cards_by_id)} unique cards.")
    return list(cards_by_id.values())


# --- pre-filter ----------------------------------------------------------------
def prefilter(cards, cfg):
    survivors, skipped = [], []
    required = set(cfg.prefilter_required_platforms)
    for c in cards:
        reasons = []

        pay = c.get("pay_per_1k")
        if cfg.prefilter_min_pay_per_1k and pay is not None and pay < cfg.prefilter_min_pay_per_1k:
            reasons.append(f"pay ${pay:.2f}/1k < ${cfg.prefilter_min_pay_per_1k:.2f}")

        rem = c.get("budget_remaining_fraction")
        if rem is not None and rem <= cfg.prefilter_min_budget_remaining:
            reasons.append(f"budget {rem*100:.0f}% <= {cfg.prefilter_min_budget_remaining*100:.0f}%")

        plats = set(c.get("platforms") or [])
        if required and plats and not (plats & required):
            reasons.append("no tiktok/shorts/reels")

        if reasons:
            skipped.append((c, "; ".join(reasons)))
        else:
            survivors.append(c)
    return survivors, skipped


# --- records -------------------------------------------------------------------
def card_only_record(card, status, skip_reason=None):
    return {
        "id": card.get("id"),
        "url": card.get("url"),
        "status": status,
        "skip_reason": skip_reason,
        "scraped_at": None,
        "name": card.get("name"),
        "creator": None,
        "pay_value": card.get("pay_value"),
        "pay_unit": card.get("pay_unit"),
        "pay_per_1k": card.get("pay_per_1k"),
        "budget_total": card.get("budget_total"),
        "budget_remaining_fraction": card.get("budget_remaining_fraction"),
        "platforms": card.get("platforms") or [],
        "source_links": [],
        "rules_text": None,
        "participants": None,
        "deadline": None,
        "footage_stats": None,
        # source-popularity + minimum-payout dimensions (unknown for card-only records)
        "min_payout": None,
        "min_views_to_payout": None,
        "high_minimum": False,
        "source": {"name": None, "handles": [], "reach_estimate": None,
                   "recent_avg_views": None, "confidence": "UNKNOWN"},
    }


def build_record(card, detail, status):
    def pick(a, b):
        return a if a is not None else b
    return {
        "id": card.get("id"),
        "url": detail.get("url") or card.get("url"),
        "status": status,
        "skip_reason": None,
        "scraped_at": _now_iso(),
        "name": detail.get("name") or card.get("name"),
        "creator": detail.get("creator"),
        "pay_value": pick(detail.get("pay_value"), card.get("pay_value")),
        "pay_unit": detail.get("pay_unit") or card.get("pay_unit"),
        "pay_per_1k": pick(detail.get("pay_per_1k"), card.get("pay_per_1k")),
        "budget_total": pick(detail.get("budget_total"), card.get("budget_total")),
        "budget_remaining_fraction": pick(
            detail.get("budget_remaining_fraction"), card.get("budget_remaining_fraction")
        ),
        "platforms": detail.get("platforms") or card.get("platforms") or [],
        "source_links": detail.get("source_links") or [],
        "rules_text": detail.get("rules_text"),
        "participants": detail.get("participants"),
        "deadline": detail.get("deadline"),
        "footage_stats": None,
        # filled by enrich_active() once the record exists
        "min_payout": None,
        "min_views_to_payout": None,
        "high_minimum": False,
        "source": None,
    }


def enrich_active(rec, cfg):
    """Compute the minimum-payout viability + build the source scaffold for one active
    record. Pure/cheap (reads rules_text + source_links only) — the actual follower
    lookup happens later in the social pass. Idempotent; preserves an already-filled
    `source` block (so refreshed/carried records keep counts from a prior run)."""
    rules = rec.get("rules_text")
    pay = rec.get("pay_per_1k")

    min_payout = extract.parse_min_payout(rules)
    mv = extract.min_views_to_payout(min_payout, pay)
    rec["min_payout"] = min_payout
    rec["min_views_to_payout"] = mv
    rec["high_minimum"] = mv is not None and mv > cfg.min_payout_max_views

    existing = rec.get("source")
    if not existing or not existing.get("handles"):
        handles = extract.extract_handles(rules, rec.get("source_links"))
        name = rec.get("creator")
        rec["source"] = {
            "name": name,
            "handles": handles,
            "reach_estimate": (existing or {}).get("reach_estimate"),
            "recent_avg_views": (existing or {}).get("recent_avg_views"),
            "confidence": (existing or {}).get("confidence")
            or ("LOW" if (name or handles) else "UNKNOWN"),
        }


# --- phase 2: detail pass ------------------------------------------------------
# Clicking a card does NOT navigate — it opens an in-frame Radix dialog overlaid on
# the list (which stays mounted behind it). So we open by clicking the card, extract
# from the dialog, then press Escape to close and return to the exact same list. We
# find the card by its accessible name ("View <NAME> campaign"), so we never depend
# on scroll position surviving — the lookup re-resolves the card wherever it is.
def open_detail(page, fl, name, pacer):
    """Click the card for `name` and return its open dialog Locator. Raises on
    failure (caught per-campaign) or StopRun on a challenge."""
    btn = fl.get_by_role("button", name=f"View {name} campaign", exact=True).first
    try:
        btn.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    pacer.maybe_hover(btn)
    btn.click(timeout=8000)
    if extract.is_challenge(page) or extract.is_login_wall(page):
        raise StopRun("challenge / login wall after opening a campaign")
    dialog = fl.locator(S.DETAIL_DIALOG[0]).first
    dialog.wait_for(state="visible", timeout=8000)
    pacer.page_delay()
    return dialog


def close_detail(page, fl):
    """Escape closes the Radix dialog; the list is preserved behind it."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        fl.locator(S.DETAIL_DIALOG[0]).first.wait_for(state="detached", timeout=5000)
    except Exception:
        try:  # one more nudge if it didn't dismiss
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass


def _scroll_dialog(page, pacer):
    """Gentle human scroll inside the centered detail dialog."""
    try:
        vp = page.viewport_size or {"width": 1366, "height": 768}
        page.mouse.move(vp["width"] / 2, vp["height"] / 2)
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(*pacer.scroll_step))
            time.sleep(random.uniform(*pacer.scroll_pause))
    except Exception:
        pass


def _frame_url(page):
    fr = get_app_frame(page)
    return fr.url if fr else None


def _looks_campaign_url(url, list_page_url):
    """A campaign-specific whop.com URL, not the list/discover/root base."""
    if not url:
        return False
    u = url.rstrip("/")
    bases = {(list_page_url or "").rstrip("/"), S.DISCOVER_URL.rstrip("/"), "https://whop.com"}
    return "whop.com" in u and u not in bases


def _return_to_list(page, fl, list_page_url, pacer):
    """After a full-page expand, go back to the list and confirm the feed is back.
    Cards aren't virtualized, so once the feed re-renders every card is findable."""
    try:
        page.go_back(timeout=10000)
    except Exception:
        pass
    time.sleep(1.0)
    if wait_for_feed(page, pacer) is not None:
        return True
    navigate_to_list(page, pacer)  # last resort: re-enter through discover
    return wait_for_feed(page, pacer) is not None


def capture_campaign_url(page, fl, list_page_url, pacer):
    """Record a human-clickable campaign URL for the currently-open dialog.

    Cheap path: opening the dialog may have shallow-routed the top-page URL already.
    Otherwise click the dialog's 'Expand to full page' button, read whichever of the
    top-page / frame URL changed, then return to the list. Fully non-fatal — returns
    (url_or_None, list_ok).
    """
    if _looks_campaign_url(page.url, list_page_url):
        return page.url, True

    dialog = fl.locator(S.DETAIL_DIALOG[0]).first
    expand = dialog.get_by_role("button", name="Expand to full page").first
    try:
        if expand.count() == 0:
            return None, True
    except Exception:
        return None, True

    pre_page, pre_frame = page.url, _frame_url(page)
    try:
        pacer.maybe_hover(expand)
        pacer.page_delay()
        expand.click(timeout=6000)
        time.sleep(1.5)
    except Exception as e:
        print(f"    (expand failed: {(str(e).splitlines() or ['?'])[0]})")
        return None, True

    post_page, post_frame = page.url, _frame_url(page)
    url = None
    if post_page != pre_page and _looks_campaign_url(post_page, list_page_url):
        url = post_page                       # preferred: clickable whop.com URL
    elif post_frame and post_frame != pre_frame:
        url = post_frame                      # fallback: frame-level (apps.whop.com)

    list_ok = _return_to_list(page, fl, list_page_url, pacer)
    return url, list_ok


def scrape_detail(page, fl, name, list_page_url, pacer):
    """Open the dialog for `name`, extract it, capture its URL, always close it."""
    dialog = open_detail(page, fl, name, pacer)
    detail, url = {}, None
    try:
        _scroll_dialog(page, pacer)
        detail = extract.extract_detail(dialog)
        url, _list_ok = capture_campaign_url(page, fl, list_page_url, pacer)
    finally:
        # If capture navigated away and back, the dialog is already gone; if it stayed
        # open (cheap/no-expand path), Escape closes it. Either way this is safe.
        close_detail(page, fl)
    detail["url"] = url
    return detail


def run_detail_pass(page, fl, survivors, prev_by_id, state, pacer, cfg, deadline_ts, refresh):
    print(f"Phase 2 — detail pass on {len(survivors)} survivors "
          f"({'full refresh' if refresh else 'delta: new campaigns only'})...")
    if fl is None:
        print("  App frame not available — cannot open detail dialogs.")
        return [], []
    results, new_ids = [], []
    consecutive_failures = 0
    processed = 0
    prev_name = None
    total = len(survivors)
    list_page_url = page.url  # baseline for detecting campaign URLs on expand

    for c in survivors:
        if processed >= cfg.max_campaigns:
            print("\n  Session cap: max campaigns reached — stopping cleanly.")
            break
        if time.monotonic() > deadline_ts:
            print("\n  Session cap: time budget reached — stopping cleanly.")
            break

        cid = c["id"]
        known = state.is_known(cid) and cid in prev_by_id and not refresh
        try:
            if known:
                rec = dict(prev_by_id[cid])
                # Refresh only the volatile card-level numbers from the list view.
                if c.get("budget_remaining_fraction") is not None:
                    rec["budget_remaining_fraction"] = c["budget_remaining_fraction"]
                if c.get("pay_per_1k") is not None:
                    rec["pay_per_1k"] = c["pay_per_1k"]
                    rec["pay_unit"] = c["pay_unit"]
                    rec["pay_value"] = c["pay_value"]
                rec["status"] = "refreshed"
                rec["refreshed_at"] = _now_iso()
                results.append(rec)
            else:
                detail = scrape_detail(page, fl, c["name"], list_page_url, pacer)
                rec = build_record(c, detail, status="scraped")
                results.append(rec)
                if not state.is_known(cid):
                    new_ids.append(cid)
                state.mark_scraped(cid, rec.get("url"))
            consecutive_failures = 0
        except StopRun:
            raise
        except Exception as e:
            consecutive_failures += 1
            log_error(cfg.errors_path, c.get("name"), e)
            if consecutive_failures >= 3:
                raise StopRun("3 consecutive failures")
            processed += 1
            continue

        processed += 1
        print(f"  {processed}/{total} scraped · {len(new_ids)} new · "
              f"next break ~{pacer.campaigns_until_break}      ", end="\r")

        # rare human double-back: briefly re-open the previous campaign's dialog
        if prev_name and pacer.should_revisit():
            try:
                open_detail(page, fl, prev_name, pacer)
                pacer.page_delay()
            except StopRun:
                raise
            except Exception:
                pass
            else:
                close_detail(page, fl)
        prev_name = c["name"]

        pacer.tick()
        pacer.page_delay()

    print("")
    return results, new_ids


# --- assembly ------------------------------------------------------------------
def load_prev_campaigns(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
        return {c["id"]: c for c in data.get("campaigns", []) if c.get("id")}
    except Exception:
        return {}


def assemble(results, skipped_records, prev_by_id, seen_ids):
    all_records = list(results)
    all_records.extend(skipped_records)

    # Carry forward campaigns we knew about but didn't see listed this run.
    seen = set(seen_ids)
    for cid, rec in prev_by_id.items():
        if cid not in seen:
            carried = dict(rec)
            carried["status"] = "not_listed_this_run"
            all_records.append(carried)

    for rec in all_records:
        if rec.get("status") in ("scraped", "refreshed"):
            rec["pre_score"] = pre_score(rec)
            comp, breakdown = composite_score(rec)
            rec["composite_score"] = comp
            rec["composite_breakdown"] = breakdown
        else:
            rec.setdefault("pre_score", 0)
            rec.setdefault("composite_score", 0)
            rec.setdefault("composite_breakdown", None)
    return all_records


# --- main ----------------------------------------------------------------------
def main():
    cfg = CONFIG
    parser = argparse.ArgumentParser(description="Scout — personal Whop Content Rewards scraper.")
    parser.add_argument("--force", action="store_true", help="ignore the 20h once-daily guard")
    parser.add_argument("--refresh", action="store_true", help="full re-scrape of every campaign")
    parser.add_argument("--probe", action="store_true", help="confirm selectors: screenshot + dump DOM")
    args = parser.parse_args()

    state = State(cfg.state_path)

    # Once-daily guard (skipped for probe).
    if not args.probe:
        hrs = state.hours_since_last_run()
        if hrs is not None and hrs < cfg.min_hours_between_runs and not args.force:
            wait = cfg.min_hours_between_runs - hrs
            print(f"Last run was {hrs:.1f}h ago. Once-daily guard: wait ~{wait:.1f}h "
                  f"or pass --force. Not today — browse manually if you like.")
            return

    profile = Path(cfg.profile_dir)
    first_run = not profile.exists() or not any(profile.iterdir()) if profile.exists() else True

    pacer = Pacer(
        base_delay=cfg.base_delay, fast_delay=cfg.fast_delay, fast_chance=cfg.fast_chance,
        afk_every=cfg.afk_every, afk_break=cfg.afk_break,
        long_afk_chance=cfg.long_afk_chance, long_afk_break=cfg.long_afk_break,
        revisit_chance=cfg.revisit_chance, scroll_step=cfg.scroll_step,
        scroll_pause=cfg.scroll_pause,
    )

    with Session(profile_dir=cfg.profile_dir, viewport=cfg.viewport, headful=True) as session:
        ensure_logged_in(session, first_run, pacer, always_prompt=args.probe)

        if args.probe:
            run_probe(session, pacer)
            return

        page = session.page
        start = time.monotonic()
        deadline_ts = start + cfg.max_minutes * 60
        errors_before = _count_errors(cfg.errors_path)

        stopped_reason = None
        results, new_ids, skipped = [], [], []
        cards = []
        try:
            cards = collect_cards(page, pacer, cfg, deadline_ts)
            survivors, skipped_pairs = prefilter(cards, cfg)
            skipped = [card_only_record(c, "skipped_prefilter", r) for c, r in skipped_pairs]
            print(f"  Pre-filter: {len(survivors)} survivors, {len(skipped)} skipped.")

            prev_by_id = load_prev_campaigns(cfg.campaigns_path)
            fl = get_app_frame_locator(page)  # still on the list; cards clicked here
            results, new_ids = run_detail_pass(
                page, fl, survivors, prev_by_id, state, pacer, cfg, deadline_ts, args.refresh
            )

            # Derive minimum-payout viability + source handles, then rank the top-N
            # by the crude pre_score so the popularity/footage probes hit those first.
            for rec in results:
                enrich_active(rec, cfg)
                rec["pre_score"] = pre_score(rec)
            social_mod.probe_sources(results, cfg.social_top_n, pacer)
            footage_mod.probe_campaigns(results, cfg.footage_top_n, pacer)
        except StopRun as e:
            stopped_reason = str(e)
            print(f"\n!! STOP: {e}. Saving progress and exiting. Not today — browse manually.")
            prev_by_id = load_prev_campaigns(cfg.campaigns_path)

        seen_ids = [c["id"] for c in cards if c.get("id")]
        all_records = assemble(results, skipped, prev_by_id, seen_ids)

        report.write_json(cfg.campaigns_path, all_records)
        report.write_summary_md(cfg.summary_path, all_records)
        state.finish_run()

        report.terminal_report(
            all_records,
            db_total=state.known_count,
            new_count=len(new_ids),
            failures=_count_errors(cfg.errors_path) - errors_before,
        )
        if stopped_reason:
            print(f"\n(Run ended early: {stopped_reason})")


def _count_errors(path):
    p = Path(path)
    if not p.exists():
        return 0
    try:
        return sum(1 for _ in p.open(encoding="utf-8"))
    except Exception:
        return 0


if __name__ == "__main__":
    main()
