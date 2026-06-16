"""Apify actor: Betway esports odds via public DOM text extraction.

Implementation notes
--------------------
* Betway shows esports odds publicly; no login is required.
* The actor navigates each requested esports tab, accepts cookies, clicks the
  tab to ensure hydration, scrolls to load lazy content, then extracts
  structured event rows straight from the rendered DOM using
  ``data-eventid`` wrappers and ``data-testid="outcome-price-value"`` spans.
* JSON-LD provides ``startDate`` / ``league`` metadata where present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from apify import Actor
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from src.normalise import normalise_game

logger = logging.getLogger("betway-scraper")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

BETWAY_BASE_URL = "https://betway.com/g/en/sports/cat/esports/{tab}"
VALID_TABS = {"popular", "live", "upcoming", "all"}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button:has-text('Allow All')",
    "button:has-text('Accept All')",
]

EXTRACT_ROWS_JS = r"""
() => {
    const rows = [];
    const sections = document.querySelectorAll('section[data-testid="event-table-section"]');
    for (const sec of sections) {
        const titleEl = sec.querySelector('[data-testid="table-header-title"]');
        const title = titleEl ? titleEl.innerText.trim() : '';
        for (const row of sec.querySelectorAll('[data-eventid]')) {
            const eventId = row.getAttribute('data-eventid') || '';
            // 1. Odds values
            const odds = Array.from(row.querySelectorAll('[data-testid="outcome-price-value"]'))
                .map(x => x.innerText.trim()).filter(Boolean);
            // 2. Collect candidate text nodes, excluding scores/odds/UI tokens
            const skipRe = /in-play|awaiting\s*start|more\s*bets|more\s*markets|watch|cash\s*out|edit\s*bet|no\s*bets\s*available/i;
            const seen = new Set();
            const candidates = [];
            for (const el of row.querySelectorAll('span, div')) {
                const t = el.innerText.trim();
                if (!t) continue;
                if (seen.has(t)) continue;
                if (/^\d+(\.\d+)?$/.test(t)) continue;          // decimal number (odds/score)
                if (/^\d{1,2}:\d{2}$/.test(t)) continue;          // time
                if (skipRe.test(t)) continue;
                seen.add(t);
                candidates.push(t);
            }
            const teamA = candidates[0] || '';
            const teamB = candidates[1] || '';
            const timeMatch = row.innerText.match(/(\d{1,2}:\d{2})/);
            const startTime = timeMatch ? timeMatch[1] : '';
            if (teamA && teamB) {
                rows.push({title, eventId, teamA, teamB, odds, startTime});
            }
        }
    }
    return rows;
}
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_header(title: str) -> tuple[str, str]:
    """Return (game_raw, league) from the section header text."""
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return "Esports", ""
    if re.fullmatch(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Today|Tomorrow)", title, re.I):
        return "Esports", ""
    if "," in title:
        parts = [p.strip() for p in title.split(",", 1)]
        return parts[0], parts[1]
    return "Esports", title


def stable_event_id(team_a: str, team_b: str, start_time: str | None) -> str:
    import hashlib
    payload = "|".join(sorted([team_a.lower(), team_b.lower()]) + [str(start_time or "")]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


async def save_screenshot(page: Page, store: Any, name: str) -> None:
    try:
        data = await page.screenshot(type="png", full_page=True)
        await store.set_value(name, data, content_type="image/png")
        logger.warning(f"Screenshot saved: {name}")
    except Exception as exc:
        logger.warning(f"Could not save screenshot: {exc}")


async def create_browser_context(actor: Actor, headful: bool):
    proxy = None
    try:
        proxy_cfg = await actor.create_proxy_configuration(groups=["RESIDENTIAL"])
        proxy_url = await proxy_cfg.new_url()
        parsed = urlparse(proxy_url)
        server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        proxy = {"server": server, "username": parsed.username or "", "password": parsed.password or ""}
        logger.info("Using Apify residential proxy")
    except Exception as exc:
        logger.warning(f"Could not configure proxy: {exc}; running without proxy")

    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1280,900",
    ]
    launch_kwargs: dict[str, Any] = {"headless": not headful, "args": args}
    if proxy:
        launch_kwargs["proxy"] = proxy

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(**launch_kwargs)
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )
    page = await context.new_page()
    return playwright, browser, context, page


async def accept_cookies(page: Page) -> None:
    for selector in COOKIE_SELECTORS:
        try:
            button = await page.query_selector(selector)
            if button and await button.is_visible():
                await button.click(timeout=3000)
                logger.info("Cookie banner accepted")
                await asyncio.sleep(0.5)
                return
        except Exception:
            continue


async def scroll_page(page: Page, steps: int = 6) -> None:
    try:
        for _ in range(steps):
            await page.evaluate("window.scrollBy(0, 700)")
            await asyncio.sleep(0.5)
    except Exception as exc:
        logger.debug(f"Scroll failed: {exc}")


async def extract_jsonld(page: Page) -> list[dict[str, Any]]:
    events = []
    script = await page.query_selector('script[data-testid="ldjson-events"]')
    if not script:
        return events
    try:
        raw = await script.inner_text()
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return events
    graph = data.get("@graph", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    for item in graph:
        if not isinstance(item, dict):
            continue
        types = item.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if "SportsEvent" not in types:
            continue
        team_a = str(item.get("homeTeam") or "")
        team_b = str(item.get("awayTeam") or "")
        competitors = item.get("competitor", [])
        if (not team_a or not team_b) and isinstance(competitors, list) and len(competitors) >= 2:
            team_a = competitors[0].get("name", "") if isinstance(competitors[0], dict) else str(competitors[0])
            team_b = competitors[1].get("name", "") if isinstance(competitors[1], dict) else str(competitors[1])
        if not team_a or not team_b:
            continue
        loc = item.get("location") or {}
        league = loc.get("name", "") if isinstance(loc, dict) else ""
        events.append({
            "team_a": team_a,
            "team_b": team_b,
            "league": str(league),
            "start_time": item.get("startDate"),
        })
    return events


async def click_tab(page: Page, tab: str) -> None:
    """Click the requested tab link if it's not already active."""
    try:
        # Try exact aria/tab role first.
        locator = page.locator(f'[role="tab"]:has-text("{tab.capitalize()}")')
        await locator.first.click(timeout=3000)
        return
    except Exception:
        pass
    try:
        locator = page.locator(f'a:has-text("{tab.capitalize()}")')
        await locator.first.click(timeout=3000)
    except Exception:
        pass


def build_markets(team_a: str, team_b: str, odds: list[str]) -> list[dict[str, Any]]:
    markets = []
    if len(odds) == 2:
        mapping = [("H", team_a), ("A", team_b)]
    elif len(odds) >= 3:
        mapping = [("H", team_a), ("D", None), ("A", team_b)]
    else:
        mapping = [(str(i), None) for i in range(len(odds))]
    for i, odd in enumerate(odds):
        try:
            decimal = float(odd)
        except ValueError:
            continue
        outcome_id, team = mapping[i] if i < len(mapping) else (str(i), None)
        markets.append({"market_id": "match_winner", "outcome_id": outcome_id, "team": team, "odds": decimal})
    return markets


async def scrape_tab(page: Page, tab: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url = BETWAY_BASE_URL.format(tab=tab)
    logger.info(f"Scraping tab '{tab}': {url}")
    try:
        await page.goto(url, wait_until="networkidle", timeout=90000)
    except Exception as exc:
        logger.warning(f"Navigation to {url} ended with {exc}; continuing anyway")

    await accept_cookies(page)
    await click_tab(page, tab)
    await scroll_page(page)
    await asyncio.sleep(1.0)

    jsonld_events = await extract_jsonld(page)
    jsonld_index = {}
    for ev in jsonld_events:
        key = "|".join(sorted([ev["team_a"].lower(), ev["team_b"].lower()]))
        jsonld_index[key] = ev

    dom_rows = await page.evaluate(EXTRACT_ROWS_JS)
    logger.info(f"Tab '{tab}': {len(dom_rows)} DOM rows, {len(jsonld_index)} JSON-LD events")

    items = []
    for row in dom_rows:
        team_a = row.get("teamA", "")
        team_b = row.get("teamB", "")
        if not team_a or not team_b:
            continue

        key = "|".join(sorted([team_a.lower(), team_b.lower()]))
        meta = jsonld_index.get(key, {})

        game_raw, league_from_header = parse_header(row.get("title", ""))
        league = meta.get("league") or league_from_header or ""
        game = normalise_game(game_raw) if game_raw.lower() != "esports" else normalise_game(league) if league else "Esports"
        start_time = meta.get("start_time")
        event_id = row.get("eventId") or stable_event_id(team_a, team_b, start_time)

        items.append({
            "event_id": event_id,
            "brand": "betway",
            "sport": "Esports",
            "game": game,
            "league": league,
            "team_a": team_a,
            "team_b": team_b,
            "start_time": start_time,
            "is_live": tab == "live",
            "markets": build_markets(team_a, team_b, row.get("odds", [])),
            "scraped_at": now_iso(),
        })

    return items, jsonld_events


async def main() -> None:
    async with Actor() as actor:
        actor_input = await actor.get_input() or {}

        tabs = actor_input.get("tabs", ["live", "upcoming"])
        if isinstance(tabs, str):
            tabs = [tabs]
        tabs = [str(t).lower().strip() for t in tabs if str(t).lower().strip() in VALID_TABS]

        headful = bool(actor_input.get("headful"))
        screenshot_on_error = bool(actor_input.get("screenshotOnError", True))
        store = await actor.open_key_value_store()

        playwright: Playwright | None = None
        browser: Browser | None = None

        try:
            playwright, browser, _context, page = await create_browser_context(actor, headful)
            total = 0
            for tab in tabs:
                items, _jsonld = await scrape_tab(page, tab)
                for item in items:
                    await Actor.push_data(item)
                total += len(items)
            logger.info(f"Finished; pushed {total} events total")
        except Exception:
            logger.exception("Betway scraper failed")
            if browser and screenshot_on_error:
                with suppress(Exception):
                    page = browser.contexts[0].pages[0]
                    await save_screenshot(page, store, "ERROR.png")
            raise
        finally:
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()


if __name__ == "__main__":
    asyncio.run(main())
