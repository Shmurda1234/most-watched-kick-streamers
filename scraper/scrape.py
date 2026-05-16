#!/usr/bin/env python3
"""
StreamerPlus — Most Watched Kick Streamers Scraper
===================================================
 
Scrapes the top 20 most-watched Kick streamers (last 7 days) from
streamscharts.com/channels?platform=kick
 
Output: data/most-watched-kick-streamers.json
 
Notes
-----
- Streams Charts soft-locks rows 4-20 via CSS overlays, but the data is in the
  rendered DOM. We extract it directly.
- Uses Playwright (Vue.js SPA, needs JS execution)
- Anonymous access — top 20 is all we can see (rows 21-50 are empty for unauth users)
- Validation gate refuses to overwrite good data with bad
- Diagnostic HTML dump on failure
"""
 
from __future__ import annotations
 
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
 
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
 
# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
 
URL = "https://streamscharts.com/channels?platform=kick"
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "most-watched-kick-streamers.json"
DIAGNOSTIC_HTML_PATH = Path(__file__).parent.parent / "data" / "last-debug.html"
 
TOP_N = 20
 
# Validation gates
MIN_ROWS = 15
MIN_TOP_HOURS_WATCHED = 200_000  # Top channel should have ≥200k hours in 7 days
 
# Realistic user agent
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15"
)
 
PAGE_LOAD_TIMEOUT_MS = 30_000
TABLE_WAIT_TIMEOUT_MS = 15_000
 
# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------
 
@dataclass
class KickChannel:
    rank: int
    name: str
    slug: str
    url: str
    avatar: Optional[str]
    country: Optional[str]
    country_code: Optional[str]
    language: Optional[str]
    primary_game: Optional[str]
    primary_game_slug: Optional[str]
    followers: Optional[int]           # exact count from tooltip
    followers_compact: Optional[str]   # "665K" display version
    hours_watched: Optional[int]       # 7-day hours watched
    peak_viewers: Optional[int]
    average_viewers: Optional[int]
    airtime_minutes: Optional[int]     # in minutes
    airtime_display: Optional[str]     # "73h 20m"
    followers_gained: Optional[int]    # 7-day gain
 
 
@dataclass
class ScrapeResult:
    scraped_at: str
    source: str
    period: str
    channels: list[KickChannel] = field(default_factory=list)
 
 
# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
 
def parse_int_with_spaces(raw: str) -> Optional[int]:
    """
    Streams Charts uses spaces/non-breaking-spaces as thousand separators:
    '2 257 099' -> 2257099, '7 338' -> 7338
    """
    if not raw or raw.strip() in ("", "--", "—", "-"):
        return None
    cleaned = re.sub(r"[\s,]+", "", raw.strip())
    if not cleaned.isdigit():
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None
 
 
def parse_followers_tooltip(html: str) -> tuple[Optional[int], Optional[str]]:
    """
    Extract exact follower count from tooltip + compact display.
 
    HTML structure:
      <div data-tippy-content="664 853 followers at the end of the selected period" ...>
        ...
        <span>665K</span>
      </div>
    """
    exact = None
    display = None
 
    # Try tooltip for exact count
    tip_match = re.search(r'data-tippy-content="([\d\s\xa0,]+)\s+followers', html)
    if tip_match:
        exact = parse_int_with_spaces(tip_match.group(1))
 
    # Display value (e.g., "665K")
    # Strip all HTML tags then take the visible text
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    if text:
        display = text
 
    return (exact, display)
 
 
def parse_airtime(raw: str) -> tuple[Optional[int], Optional[str]]:
    """
    Streams Charts airtime format: '73h 20m', '42h 10m', '72h' (no minutes).
    Returns (total_minutes, display_string)
    """
    if not raw or raw.strip() in ("", "--", "—", "-"):
        return (None, None)
 
    display = raw.strip()
    # Match patterns like '73h 20m', '42h', '95h 10m', '120m'
    total_minutes = 0
    match_h = re.search(r"(\d+)\s*h", display)
    match_m = re.search(r"(\d+)\s*m", display)
 
    if match_h:
        total_minutes += int(match_h.group(1)) * 60
    if match_m:
        total_minutes += int(match_m.group(1))
 
    if total_minutes == 0:
        return (None, display)
 
    return (total_minutes, display)
 
 
def is_locked_cell(cell_html: str) -> bool:
    """Detect 'PRO users only' lock icons on this cell."""
    return "lock-fill" in cell_html
 
 
def extract_cell_text(cell_html: str) -> str:
    """Strip HTML tags and normalize whitespace from a cell."""
    text = re.sub(r"<[^>]+>", " ", cell_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text
 
 
def extract_attribute(html: str, attr: str) -> Optional[str]:
    """Extract the value of an HTML attribute (e.g. 'href')."""
    m = re.search(rf'{attr}="([^"]+)"', html)
    return m.group(1) if m else None
 
 
# -----------------------------------------------------------------------------
# Row parser
# -----------------------------------------------------------------------------
 
def parse_row(rank: int, cells: list[dict]) -> Optional[KickChannel]:
    """
    Parse one row into a KickChannel.
 
    Cell layout (16 cells):
      0: rank number
      1: avatar (img) + link to /channels/{slug}?platform=kick
      2: channel name + language tag (<span class="t_c_s_l">AR</span>)
      3: partner badge (icon)
      4: gender icon
      5: country flag (alt has country name)
      6: language code (e.g. "ar")
      7: followers (tooltip has exact count, display is compact "665K")
      8: primary game (href has slug, name in JS string)
      9: favorite button (skip)
     10: hours watched (7d)
     11: peak viewers (7d)
     12: average viewers (7d)
     13: airtime (7d) — format "73h 20m"
     14: followers gained (7d)
     15: business email (skip)
    """
    if len(cells) < 15:
        return None
 
    # Quick validity check: row needs a channel link in cell 1 or 2
    if "/channels/" not in cells[1]["html"] and "/channels/" not in cells[2]["html"]:
        return None
 
    # --- Avatar (cell 1) ---
    # Live URL will be on a real CDN domain; saved-HTML had local paths.
    # We accept either by looking for src="" attribute
    avatar = None
    avatar_match = re.search(
        r'<img[^>]+src="(https?://[^"]+)"',
        cells[1]["html"],
    )
    if avatar_match:
        # Decode HTML entities (&amp; -> &) so the URL is usable in <img src>
        from html import unescape
        avatar = unescape(avatar_match.group(1))
 
    # --- Channel slug + name (cell 2) ---
    slug_match = re.search(
        r'<a href="(?:https?://streamscharts\.com)?/channels/([^?"]+)\?platform=kick"',
        cells[2]["html"],
    )
    if not slug_match:
        return None
    slug = slug_match.group(1)
 
    # Name from inner span
    name_match = re.search(
        r'<span class="[^"]*t_c_t_c--channel-name[^"]*"[^>]*>\s*([^<]+?)\s*</span>',
        cells[2]["html"],
    )
    name = name_match.group(1).strip() if name_match else slug
 
    # --- Country (cell 5) ---
    country = None
    country_code = None
    if not is_locked_cell(cells[5]["html"]):
        country_alt = re.search(
            r'<img[^>]+alt="([^"]+)"[^>]*src="[^"]*flags?/[a-z]+\.svg',
            cells[5]["html"],
        )
        if not country_alt:
            # Try alt-first ordering
            country_alt = re.search(
                r'<img[^>]+alt="([^"]+)"',
                cells[5]["html"],
            )
        if country_alt:
            country = country_alt.group(1)
 
        # Country code from flag src — match both live and saved-file path variants
        # Live: src="/img/flags/jo.svg" → 'jo'
        # Saved: src="..._files/jo.svg" → 'jo'
        # Generic: any 2-letter .svg at end of path
        code_match = re.search(r'/([a-z]{2,3})\.svg(?:\?|")', cells[5]["html"])
        if code_match:
            country_code = code_match.group(1).upper()
 
    # --- Language (cell 6) ---
    language_text = extract_cell_text(cells[6]["html"]).upper()
    language = language_text if language_text else None
 
    # --- Followers (cell 7) — exact from tooltip + compact display ---
    followers, followers_compact = parse_followers_tooltip(cells[7]["html"])
 
    # --- Primary game (cell 8) ---
    primary_game = None
    primary_game_slug = None
    # Game name lives inside a JS string: `name: \`EA Sports FC 26\``
    game_name_match = re.search(r"name:\s*`([^`]+)`", cells[8]["html"])
    if game_name_match:
        primary_game = game_name_match.group(1).strip()
    # Game slug from href
    game_slug_match = re.search(r"\?platform=kick(?:&amp;|&)game=([a-z0-9-]+)", cells[8]["html"])
    if game_slug_match:
        primary_game_slug = game_slug_match.group(1)
 
    # --- Hours watched (cell 10) ---
    # The visible number is inside <span>...</span> within the t_v div
    hours_text = extract_cell_text(cells[10]["html"])
    hours_watched = parse_int_with_spaces(hours_text)
 
    # --- Peak viewers (cell 11) ---
    peak_text = extract_cell_text(cells[11]["html"])
    peak_viewers = parse_int_with_spaces(peak_text)
 
    # --- Average viewers (cell 12) ---
    avg_text = extract_cell_text(cells[12]["html"])
    average_viewers = parse_int_with_spaces(avg_text)
 
    # --- Airtime (cell 13) ---
    airtime_text = extract_cell_text(cells[13]["html"])
    airtime_minutes, airtime_display = parse_airtime(airtime_text)
 
    # --- Followers gained (cell 14) ---
    gained_text = extract_cell_text(cells[14]["html"])
    followers_gained = parse_int_with_spaces(gained_text)
 
    return KickChannel(
        rank=rank,
        name=name,
        slug=slug,
        url=f"https://kick.com/{slug}",
        avatar=avatar,
        country=country,
        country_code=country_code,
        language=language,
        primary_game=primary_game,
        primary_game_slug=primary_game_slug,
        followers=followers,
        followers_compact=followers_compact,
        hours_watched=hours_watched,
        peak_viewers=peak_viewers,
        average_viewers=average_viewers,
        airtime_minutes=airtime_minutes,
        airtime_display=airtime_display,
        followers_gained=followers_gained,
    )
 
 
# -----------------------------------------------------------------------------
# Scraper
# -----------------------------------------------------------------------------
 
def scrape() -> ScrapeResult:
    result = ScrapeResult(
        scraped_at=datetime.now(timezone.utc).isoformat(),
        source="streamscharts.com",
        period="last_7_days",
    )
 
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
 
        # Hide webdriver flag
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
 
        print(f"[scrape] Navigating to {URL}", flush=True)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print(f"[scrape] WARN: page load timeout, continuing", flush=True)
 
        print("[scrape] Waiting for table rows...", flush=True)
        try:
            page.wait_for_selector("table tbody tr", timeout=TABLE_WAIT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print("[scrape] WARN: table didn't render, dumping HTML", flush=True)
            DIAGNOSTIC_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
            DIAGNOSTIC_HTML_PATH.write_text(page.content(), encoding="utf-8")
            browser.close()
            return result
 
        # Let Vue.js finish populating cells
        time.sleep(3)
 
        # Dump full HTML for debugging
        DIAGNOSTIC_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
        DIAGNOSTIC_HTML_PATH.write_text(page.content(), encoding="utf-8")
 
        # Extract rows using JS evaluation
        print("[scrape] Extracting rows from DOM...", flush=True)
        rows_data = page.evaluate("""
            () => {
                const rows = Array.from(document.querySelectorAll('table tbody tr'));
                return rows.map(row => {
                    const cells = Array.from(row.querySelectorAll('td'));
                    return cells.map(cell => ({
                        text: cell.innerText.trim(),
                        html: cell.innerHTML
                    }));
                });
            }
        """)
 
        print(f"[scrape] Got {len(rows_data)} raw rows from DOM", flush=True)
 
        # Walk all rows, parsing each. Ad rows (colspan="100%") return None and
        # get skipped. We re-rank sequentially so ad rows don't leave gaps.
        seq_rank = 0
        for idx, cells in enumerate(rows_data, start=1):
            # Skip rows with too few cells (ad rows have colspan and only 1 cell)
            if len(cells) < 15:
                continue
            try:
                # Pass the next sequential rank, not the DOM position
                channel = parse_row(seq_rank + 1, cells)
                if channel:
                    result.channels.append(channel)
                    seq_rank += 1
                    if seq_rank >= TOP_N:
                        break
            except Exception as exc:
                print(f"[scrape] Failed to parse row {idx}: {exc}", flush=True)
                continue
 
        browser.close()
 
    return result
 
 
# -----------------------------------------------------------------------------
# Validation + write
# -----------------------------------------------------------------------------
 
def validate(result: ScrapeResult) -> tuple[bool, str]:
    if len(result.channels) < MIN_ROWS:
        return (False, f"only {len(result.channels)} channels (min {MIN_ROWS})")
 
    top = result.channels[0]
    if top.hours_watched is None or top.hours_watched < MIN_TOP_HOURS_WATCHED:
        return (
            False,
            f"top channel {top.name} has {top.hours_watched} hours "
            f"(expected ≥{MIN_TOP_HOURS_WATCHED:,})",
        )
 
    expected_ranks = list(range(1, len(result.channels) + 1))
    actual_ranks = [c.rank for c in result.channels]
    if actual_ranks != expected_ranks:
        return (False, f"ranks aren't sequential: {actual_ranks}")
 
    return (True, "ok")
 
 
def write_json(result: ScrapeResult) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scraped_at": result.scraped_at,
        "source": result.source,
        "period": result.period,
        "channels": [asdict(c) for c in result.channels],
    }
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[scrape] Wrote {len(result.channels)} channels to {OUTPUT_PATH}", flush=True)
 
 
def main() -> int:
    print(f"[scrape] Starting most-watched-kick scrape at {datetime.now(timezone.utc).isoformat()}", flush=True)
 
    result = scrape()
    print(f"[scrape] Captured {len(result.channels)} channels", flush=True)
 
    ok, reason = validate(result)
    if not ok:
        print(f"[scrape] VALIDATION FAILED: {reason}", flush=True)
        return 1
 
    write_json(result)
    print(
        f"[scrape] Done. Top channel: {result.channels[0].name} "
        f"({result.channels[0].hours_watched:,} hours watched)",
        flush=True,
    )
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
