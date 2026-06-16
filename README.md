# Betway esports odds scraper

Public Betway esports odds. No login required.

## Pages scraped

- https://betway.com/g/en/sports/cat/esports/popular
- https://betway.com/g/en/sports/cat/esports/live
- https://betway.com/g/en/sports/cat/esports/upcoming
- https://betway.com/g/en/sports/cat/esports/all

## Output schema

Each event pushed to the Apify dataset:

```json
{
  "event_id": "...",
  "brand": "betway",
  "sport": "Esports",
  "game": "CS2",
  "league": "IEM Cologne Major",
  "team_a": "Aurora",
  "team_b": "BB Team",
  "start_time": "2026-06-18T13:45:00.000Z",
  "is_live": false,
  "markets": [
    {"market_id": "match_winner", "outcome_id": "H", "team": "Aurora", "odds": 1.70},
    {"market_id": "match_winner", "outcome_id": "A", "team": "BB Team", "odds": 2.10}
  ],
  "scraped_at": "..."
}
```

## Supported input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tabs` | array<string> | `["live", "upcoming"]` | Tabs to scrape: `popular`, `live`, `upcoming`, `all` |
| `headful` | boolean | `false` | Run browser visibly for debugging |
| `screenshotOnError` | boolean | `true` | Save screenshot on failure |

## Notes

- Uses residential proxies if available in the Apify account; falls back to datacenter/no proxy.
- Extracts from rendered DOM + JSON-LD. Designed to avoid depending on Betway internal JS bundles.
