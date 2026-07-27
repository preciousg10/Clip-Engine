# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scout is a **personal, single-user research tool** that scrapes the user's *own*
logged-in Whop Content Rewards campaigns. It is intentionally low-volume, human-paced,
and once-daily. It is not a crawler and must never become one. The whole design
premise is "indistinguishable from me manually browsing my own account," so several
constraints below are hard requirements, not preferences.

## Commands

```powershell
# setup
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

# confirm selectors (login is manual in the opened window, then press Enter)
python scout.py --probe

# normal daily run
python scout.py

# flags
python scout.py --force     # ignore the 20h once-daily guard
python scout.py --refresh   # full re-scrape of every campaign (default is delta)

# syntax check all modules
python -m py_compile scout.py pacing.py browser.py state.py extract.py scoring.py footage.py social.py report.py selectors.py
```

There is no test framework wired up. The DOM-agnostic parsers in `extract.py`
(`parse_pay`, `parse_money`, `parse_remaining_fraction`, `parse_platforms`,
`parse_int`, `campaign_id_from_url`, `parse_min_payout`, `min_views_to_payout`,
`extract_handles`) are pure functions with no Playwright dependency — test them by
importing `extract` directly, no browser needed. `scoring.py` (`pre_score`,
`clippability`, `composite_score`) and `social.parse_count` are likewise pure.

## Architecture

Runtime is fully standalone (`python scout.py`), no Claude/LLM involvement. Flat
module layout; `scout.py` imports the others and none import back into it (no
circular deps). Data flows in one direction:

`browser.Session` (headful persistent Chromium)
→ **Phase 1** `collect_cards` scrolls the list, `extract.extract_card` per card
→ `prefilter` splits survivors vs. `skipped_prefilter` (recorded with reasons)
→ **Phase 2** `run_detail_pass` visits survivors, `extract.extract_detail` per page
→ `scoring.pre_score` → `footage.probe_campaigns` (top-N) → `report.*` writes outputs.

Key module responsibilities:
- **`scout.py`** — the `Config` dataclass at the top is the single source of truth for
  every tunable (pacing, caps, pre-filter thresholds, output paths). Orchestration,
  CLI, login flow, session caps, and the `StopRun` abort path all live here.
- **`selectors.py`** — every Whop DOM hook, each a list of candidate selectors tried
  in order. This is the *only* place selectors belong; extraction logic never inlines
  them. **Selectors are unconfirmed** in this build (see below).
- **`extract.py`** — two layers: pure text parsers (safe to change/test freely) and
  Playwright extraction helpers that feed those parsers text pulled via `selectors.py`.
  Extraction never raises on a missing field — it returns `None`.
- **`pacing.py`** — the `Pacer` owns all timing/human-behavior. Anything that adds a
  delay, break, scroll, or hover goes through it so the behavior stays centralized and
  auditable.
- **`state.py`** — `state.json`: the 20h guard (`hours_since_last_run`) and known
  campaign IDs that drive delta/resume. Campaign *data* lives in `campaigns.json`, not
  here.

## Delta vs. refresh (important)

Default mode is **delta**: only *new* campaign IDs get a full Phase-2 detail scrape;
already-known IDs just have their volatile card-level numbers (budget remaining, pay)
refreshed from the list view, reusing prior detail from `campaigns.json`. `--refresh`
forces a full re-scrape. `load_prev_campaigns` + `assemble` merge new results, skipped
records, and carried-forward `not_listed_this_run` campaigns. Changing this logic means
touching the `known` branch in `run_detail_pass` and `assemble` together.

## Selectors are unconfirmed — the `--probe` workflow

Whop's live DOM was not accessible at build time (login is manual, on the user's
machine), so the selectors in `selectors.py` are educated placeholders. The rule is
**never guess selectors blind inside logic** — instead run `--probe`, which logs in,
screenshots a card + detail page, dumps `probe_*.html`, and prints sample extraction.
Fix `selectors.py` against those artifacts before trusting a real run. When extraction
returns mostly `None`, the selectors are wrong, not the parsers.

**The cards live inside a cross-origin app iframe — the single most important fact
about scraping Whop here.** The list page is a client-rendered Vite SPA (assets under
`/_web/assets/*.js`) whose served HTML is just a shell, AND the Content Rewards
experience is a Whop "app" embedded via `<iframe title="Content Rewards"
src=".../core/app/launch/?redirect=…apps.whop.com…">`. The campaign grid renders
**inside that frame** (origin `apps.whop.com`), so `page.content()` on the top document
returns zero cards no matter how long you wait. Everything runs against the app
**Frame**, not the Page:
- **Queries/scrolling/clicks go through a `FrameLocator`** (`get_app_frame_locator`,
  i.e. `page.frame_locator(...)`), which re-resolves the frame lazily on each call and
  survives the app re-rendering — unlike a captured `Frame` object, which goes stale.
- **`.content()` dumps and `.url` tracking use the `Frame` object** (`get_app_frame`,
  from `page.frames` matching `APP_FRAME_URL_HINT` = `apps.whop.com`) — a FrameLocator
  can't give those.
- `wait_for_feed` returns the **FrameLocator** (settles network, scrolls the top page
  so the iframe mounts past the hero, then polls *inside the frame* for cards). Returns
  it even when no cards were detected, so the probe can still query/dump.
- `pick_card_selector`, `extract.extract_card/extract_detail`, and card iteration take a
  `scope` that is the FrameLocator (Page only as fallback).
- `scroll_list` wheel-scrolls with the cursor over the iframe's bounding box so the
  frame's own feed scrolls. `is_challenge`/`is_login_wall` stay on the Page.

Navigation: `selectors.LIST_URL` (`/discover/app/app_...`) gets **bounced to
`/discover/` by the client router** on a direct `goto`, so `navigate_to_list` opens
`DISCOVER_URL` and *clicks* the banner (`selectors.DISCOVER_BANNER`); `goto` is fallback
only. The grid also sits below a full-height hero and is lazy-mounted.

The probe dumps the **frame's** HTML to `probe_list.html`/`probe_detail.html` and reaches
detail by *clicking* a card and reading the resulting frame (a top-page `goto` would
break the embed). No cards in a dump ⇒ suspect the frame wasn't found / render timing
before suspecting card selectors.

**Confirmed card markup (from the app-frame dump):** cards are `<button class="card-wrapper">`
elements (NOT anchors) with `aria-label="View <NAME> campaign"`. They have **no href and
no id**, so `extract_card` derives identity by slugifying the name (`extract.slugify`)
and `url` is `None` at list time. Fields on the card: name (aria-label / `h3.line-clamp-1`
/ `h2.line-clamp-2`), pay pill `.verified-pill-blue` (e.g. `$1/1K`; featured cards add
`views`), budget as a `$paid/$total` pair → `extract.parse_budget_pair` (first = paid /
progress, second = total; `remaining = 1 - paid/total`). Platforms are bare SVG icons
with no text, so platform info must come from the detail page. Names/cards repeat across
carousels (Featured / All Campaigns / category rows) — dedupe by slug.

**Detail is an in-frame Radix dialog, NOT a navigation.** Clicking a card opens
`div[role="dialog"].campaign-details-modal-bg` overlaid on the list (which stays
mounted behind it). So Phase 2 (`open_detail` → `extract_detail(dialog)` →
`close_detail`) works like this:
- `open_detail` clicks the card via `fl.get_by_role("button", name=f"View {name}
  campaign", exact=True)` — lookup is by **accessible name**, so it never depends on
  list scroll position surviving (the card re-resolves wherever it is).
- `extract_detail` is scoped to the **dialog Locator**; `DETAIL_*` selectors are
  relative to it. Rules come from `span.break-all` bullets (joined), budget is the same
  `$paid/$total` pair, pay is `$1/1K views`. Source links (`drive.google`/`youtube`)
  appear only on campaigns that have them.
- `capture_campaign_url` records a clickable campaign URL: cheap path checks whether
  opening the dialog already shallow-routed the top-page URL; otherwise it clicks the
  dialog's **"Expand to full page"** button, reads whichever of the top-page/frame URL
  changed, then `_return_to_list` (`page.go_back` → `wait_for_feed`; full re-nav as last
  resort). All non-fatal — on any failure the record keeps `url = None` and the pass
  continues. URLs persist in `campaigns.json`, so delta runs don't re-capture them.
- `close_detail` presses **Escape** (Radix closes on it) and waits for the dialog to
  detach; the list is preserved behind, so no rescroll is needed.
- Cards aren't virtualized (~500 stay mounted), so `get_by_role` finds any card after a
  back-nav without needing to restore scroll position.

## Hard constraints (do not violate)

- **Always headful.** Never add headless mode.
- No stealth/evasion plugins, no proxy rotation, no concurrency (one page, one tab,
  strictly sequential), no direct private-API calls.
- Never automate the login form or touch credentials; login is always manual.
- **Never navigate programmatically during manual login** — Whop's OAuth redirect chain
  will collide with a `page.goto` and raise "Navigation interrupted by another
  navigation". All navigation goes through `safe_goto` (never raises), and the login
  phase only navigates *after* the user's Enter, with retry-or-wait. Nothing in
  `ensure_logged_in` may exit or crash the run.
- Never retry through a captcha/challenge/login wall. On any challenge, 3 consecutive
  failures, or a login wall mid-run, raise `StopRun` — save progress and exit. The tool
  degrades to "not today, browse manually"; it never escalates.
- Keep the once-daily (20h) guard and the 200-campaign / 90-minute session caps intact.
- All human-pacing changes go through `pacing.Pacer`; keep intervals randomized (never
  periodic).

## Files never to commit

`whop_profile/` holds the live logged-in session — treat it like a credential. Runtime
outputs (`campaigns.json`, `campaigns_summary.md`, `state.json`, `errors.log`,
`probe_*`) are gitignored.
