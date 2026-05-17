# deal

Simple auction watcher for government surplus sites. It polls listing pages, filters auctions ending soon with low/no bidding activity, de-duplicates alerts with SQLite, and notifies via Discord webhook (or stdout fallback).

## Quick start

```bash
cp .env.example .env
python3 -m pip install requests beautifulsoup4
python3 auction_watcher.py
```

## Configure

Edit `.env`:

- `ALERT_WINDOW_MIN` — only include auctions ending within this many minutes.
- `MAX_BIDS` — maximum bids allowed for alerts.
- `MAX_CURRENT_BID` — maximum current bid allowed (`none` to disable cap).
- `ENABLED_SITES` — comma-separated: `govplanet,publicsurplus,govdeals`.
- `*_LISTING_URL` — point each site to your preferred “ending soon” listing query.
- `CACHE_DB_PATH` — SQLite cache DB path.
- `DISCORD_WEBHOOK_URL` — if set, notifications are posted to Discord.

## Scheduling

Example cron (every 5 minutes):

```bash
*/5 * * * * /usr/bin/python3 /path/to/repo/auction_watcher.py
```

## Notes

- Auction end times are normalized to UTC before filtering.
- Alert de-duplication key is `site + url + end_time`, so updated end times can re-alert.
- Respect each platform's Terms of Service and throttle polling accordingly.
