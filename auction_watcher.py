#!/usr/bin/env python3
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import sqlite3
import sys
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


@dataclasses.dataclass(frozen=True)
class SiteAdapter:
    name: str
    base_url: str
    listing_url: str
    row_selector: str = "tr.auction-row"
    title_selector: str = ".title a"
    current_bid_selector: str = ".current-bid"
    bids_selector: str = ".bids"
    end_time_selector: str = ".end-time"


@dataclasses.dataclass(frozen=True)
class AuctionItem:
    site: str
    title: str
    url: str
    current_bid: float
    bids: int
    end_time: dt.datetime


def parse_money(text: str) -> float:
    cleaned = re.sub(r"[^0-9.\-]", "", text or "")
    if not cleaned:
        return 0.0
    return float(cleaned)


def parse_end_time(text: str) -> dt.datetime:
    text = (text or "").strip()
    if not text:
        raise ValueError("end time is empty")

    zone_map = {
        "PT": "America/Los_Angeles",
        "PDT": "America/Los_Angeles",
        "PST": "America/Los_Angeles",
        "MT": "America/Denver",
        "MDT": "America/Denver",
        "MST": "America/Denver",
        "CT": "America/Chicago",
        "CDT": "America/Chicago",
        "CST": "America/Chicago",
        "ET": "America/New_York",
        "EDT": "America/New_York",
        "EST": "America/New_York",
        "UTC": "UTC",
    }

    parts = text.rsplit(" ", 1)
    tz_abbrev = None
    if len(parts) == 2 and parts[1].upper() in zone_map:
        text, tz_abbrev = parts[0], parts[1].upper()

    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = dt.datetime.strptime(text, fmt)
            if tz_abbrev and ZoneInfo:
                local = naive.replace(tzinfo=ZoneInfo(zone_map[tz_abbrev]))
                return local.astimezone(dt.timezone.utc)
            return naive.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue

    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"unsupported end time format: {text}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def fetch_page(url: str, timeout: int = 15) -> str:
    user_agent = os.getenv("AUCTION_WATCHER_USER_AGENT", "deal-auction-watcher/1.0")
    if requests is not None:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        response.raise_for_status()
        return response.text

    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


class _AuctionHTMLParser(HTMLParser):
    def __init__(self, adapter: SiteAdapter):
        super().__init__(convert_charrefs=True)
        self.adapter = adapter
        self.rows: list[dict[str, str]] = []
        self._row_tag, self._row_class = _split_simple_selector(adapter.row_selector)
        self._field_selectors = {
            "title": _split_descendant_selector(adapter.title_selector),
            "current_bid": _split_descendant_selector(adapter.current_bid_selector),
            "bids": _split_descendant_selector(adapter.bids_selector),
            "end_time": _split_descendant_selector(adapter.end_time_selector),
        }
        self._row_depth = 0
        self._current_row: Optional[dict[str, str]] = None
        self._current_field: Optional[str] = None
        self._current_field_depth: Optional[int] = None
        self._current_href: Optional[str] = None
        self._open_elements: list[tuple[str, set[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        classes = set(attr_map.get("class", "").split())

        if self._current_row is None:
            if _matches_simple_selector(tag, classes, self._row_tag, self._row_class):
                self._current_row = {}
                self._row_depth = 1
                self._open_elements = [(tag, classes)]
            return

        for field_name, selector in self._field_selectors.items():
            if self._current_field and field_name != self._current_field:
                continue
            if _matches_selector_chain(tag, classes, selector, self._open_elements):
                self._current_field = field_name
                self._current_field_depth = len(self._open_elements) + 1
                if field_name == "title":
                    href = attr_map.get("href", "").strip()
                    if href:
                        self._current_href = href
                break
        self._open_elements.append((tag, classes))
        self._row_depth += 1

    def handle_data(self, data: str) -> None:
        if self._current_row is None or self._current_field is None:
            return
        self._current_row[self._current_field] = self._current_row.get(self._current_field, "") + data

    def handle_endtag(self, tag: str) -> None:
        if self._current_row is None:
            return

        if self._open_elements:
            self._open_elements.pop()
        if self._current_field_depth is not None and len(self._open_elements) < self._current_field_depth:
            self._current_field = None
            self._current_field_depth = None
        self._row_depth -= 1
        if self._row_depth == 0:
            if self._current_href:
                self._current_row["href"] = self._current_href
            self.rows.append({key: value.strip() for key, value in self._current_row.items() if value.strip()})
            self._current_row = None
            self._current_field = None
            self._current_field_depth = None
            self._current_href = None
            self._open_elements = []


def _split_simple_selector(selector: str) -> tuple[str, Optional[str]]:
    selector = selector.strip()
    if not selector:
        raise ValueError("selector is empty or whitespace")
    if selector.startswith("."):
        return "*", selector[1:]
    if "." in selector:
        tag, class_name = selector.split(".", 1)
        return tag, class_name
    return selector, None


def _split_descendant_selector(selector: str) -> list[tuple[str, Optional[str]]]:
    return [_split_simple_selector(part) for part in selector.split() if part.strip()]


def _matches_simple_selector(tag: str, classes: set[str], expected_tag: str, expected_class: Optional[str]) -> bool:
    if expected_tag != "*" and tag != expected_tag:
        return False
    if expected_class and expected_class not in classes:
        return False
    return True


def _matches_selector_chain(
    tag: str,
    classes: set[str],
    selector: list[tuple[str, Optional[str]]],
    ancestors: list[tuple[str, set[str]]],
) -> bool:
    expected_tag, expected_class = selector[-1]
    if not _matches_simple_selector(tag, classes, expected_tag, expected_class):
        return False
    if len(selector) == 1:
        return True

    ancestor_index = len(ancestors) - 1
    for expected_tag, expected_class in reversed(selector[:-1]):
        matched = False
        while ancestor_index >= 0:
            ancestor_tag, ancestor_classes = ancestors[ancestor_index]
            ancestor_index -= 1
            if _matches_simple_selector(ancestor_tag, ancestor_classes, expected_tag, expected_class):
                matched = True
                break
        if not matched:
            return False
    return True


def parse_auctions_without_bs4(html: str, adapter: SiteAdapter) -> list[AuctionItem]:
    parser = _AuctionHTMLParser(adapter)
    parser.feed(html)
    parsed: list[AuctionItem] = []

    for row in parser.rows:
        href = row.get("href")
        if not href:
            continue

        bids_text = row.get("bids", "")
        bids_number_match = re.search(r"\d+", bids_text)
        bids = int(bids_number_match.group(0)) if bids_number_match else 0

        try:
            item = AuctionItem(
                site=adapter.name,
                title=row.get("title", ""),
                url=urljoin(adapter.base_url, href),
                current_bid=parse_money(row.get("current_bid", "")),
                bids=bids,
                end_time=parse_end_time(row.get("end_time", "")),
            )
        except (TypeError, ValueError):
            continue

        parsed.append(item)

    return parsed


def parse_auctions(html: str, adapter: SiteAdapter) -> list[AuctionItem]:
    if BeautifulSoup is None:
        return parse_auctions_without_bs4(html, adapter)

    soup = BeautifulSoup(html, "html.parser")
    parsed: list[AuctionItem] = []

    for row in soup.select(adapter.row_selector):
        title_el = row.select_one(adapter.title_selector)
        bid_el = row.select_one(adapter.current_bid_selector)
        bids_el = row.select_one(adapter.bids_selector)
        end_el = row.select_one(adapter.end_time_selector)

        if not (title_el and bid_el and bids_el and end_el):
            continue

        href = title_el.get("href")
        if not href:
            continue

        bids_text = bids_el.get_text(strip=True)
        bids_number_match = re.search(r"\d+", bids_text or "")
        bids = int(bids_number_match.group(0)) if bids_number_match else 0

        try:
            item = AuctionItem(
                site=adapter.name,
                title=title_el.get_text(strip=True),
                url=urljoin(adapter.base_url, href),
                current_bid=parse_money(bid_el.get_text(strip=True)),
                bids=bids,
                end_time=parse_end_time(end_el.get_text(strip=True)),
            )
        except (TypeError, ValueError):
            continue

        parsed.append(item)

    return parsed


def filter_targets(
    items: Iterable[AuctionItem],
    now: Optional[dt.datetime] = None,
    alert_window_minutes: int = 30,
    max_bids: int = 0,
    max_current_bid: Optional[float] = 500.0,
) -> list[tuple[AuctionItem, float]]:
    now = now or dt.datetime.now(dt.timezone.utc)
    hits: list[tuple[AuctionItem, float]] = []

    for item in items:
        remaining = item.end_time - now
        remaining_minutes = remaining.total_seconds() / 60
        if remaining_minutes < 0 or remaining_minutes > alert_window_minutes:
            continue
        if item.bids > max_bids:
            continue
        if max_current_bid is not None and item.current_bid > max_current_bid:
            continue
        hits.append((item, remaining_minutes))

    hits.sort(key=lambda item: item[1])
    return hits


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            site TEXT NOT NULL,
            url TEXT NOT NULL,
            end_time_utc TEXT NOT NULL,
            alerted_at_utc TEXT NOT NULL,
            PRIMARY KEY (site, url, end_time_utc)
        )
        """
    )
    conn.commit()
    return conn


def was_alerted(conn: sqlite3.Connection, item: AuctionItem) -> bool:
    cursor = conn.execute(
        "SELECT 1 FROM alerts WHERE site = ? AND url = ? AND end_time_utc = ? LIMIT 1",
        (item.site, item.url, item.end_time.astimezone(dt.timezone.utc).isoformat()),
    )
    return cursor.fetchone() is not None


def mark_alerted(conn: sqlite3.Connection, item: AuctionItem) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO alerts(site, url, end_time_utc, alerted_at_utc)
        VALUES (?, ?, ?, ?)
        """,
        (
            item.site,
            item.url,
            item.end_time.astimezone(dt.timezone.utc).isoformat(),
            dt.datetime.now(dt.timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def build_default_adapters() -> list[SiteAdapter]:
    adapters = [
        SiteAdapter(
            name="govplanet",
            base_url="https://www.govplanet.com",
            listing_url=os.getenv("GOVPLANET_LISTING_URL", "https://www.govplanet.com/Government+Surplus"),
        ),
        SiteAdapter(
            name="publicsurplus",
            base_url="https://www.publicsurplus.com",
            listing_url=os.getenv("PUBLICSURPLUS_LISTING_URL", "https://www.publicsurplus.com/sms/browse/home"),
        ),
        SiteAdapter(
            name="govdeals",
            base_url="https://www.govdeals.com",
            listing_url=os.getenv("GOVDEALS_LISTING_URL", "https://www.govdeals.com/en"),
        ),
    ]

    enabled = {site.strip().lower() for site in os.getenv("ENABLED_SITES", "govplanet,publicsurplus,govdeals").split(",") if site.strip()}
    return [adapter for adapter in adapters if adapter.name in enabled]


def send_discord(webhook_url: str, lines: list[str]) -> None:
    if not webhook_url or not lines:
        return

    payload = {"content": "Auctions ending soon with low/no bids:\n\n" + "\n\n".join(lines[:20])}

    if requests is None:
        raise RuntimeError("requests is required for Discord webhook notifications")

    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()


def notify_stdout(lines: list[str]) -> None:
    if not lines:
        print("No matching auctions found.")
        return
    print("Auctions ending soon with low/no bids:\n")
    print("\n\n".join(lines))


def run() -> int:
    load_env_file()

    alert_window_min = int(os.getenv("ALERT_WINDOW_MIN", "30"))
    max_bids = int(os.getenv("MAX_BIDS", "0"))
    max_current_bid = os.getenv("MAX_CURRENT_BID", "500")
    max_current_bid_value = None if max_current_bid.lower() in {"", "none", "null"} else float(max_current_bid)
    db_path = os.getenv("CACHE_DB_PATH", "auction_watcher.db")
    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")

    adapters = build_default_adapters()
    all_items: list[AuctionItem] = []

    for adapter in adapters:
        try:
            html = fetch_page(adapter.listing_url)
            all_items.extend(parse_auctions(html, adapter))
        except Exception as exc:
            print(f"[{adapter.name}] fetch/parse failed: {exc}", file=sys.stderr)

    if not all_items:
        print("No auctions parsed from configured sites.")
        return 0

    conn = init_db(db_path)

    pending: list[tuple[AuctionItem, float]] = []
    for item, minutes_left in filter_targets(
        all_items,
        alert_window_minutes=alert_window_min,
        max_bids=max_bids,
        max_current_bid=max_current_bid_value,
    ):
        if was_alerted(conn, item):
            continue
        pending.append((item, minutes_left))

    if not pending:
        print("No new alert candidates.")
        return 0

    lines: list[str] = []
    for item, minutes_left in pending:
        lines.append(
            f"[{item.site}] {item.title} — ${item.current_bid:,.0f}, {item.bids} bids, ~{minutes_left:.0f} min left\n{item.url}"
        )

    if discord_webhook:
        send_discord(discord_webhook, lines)
    else:
        notify_stdout(lines)

    for item, _ in pending:
        mark_alerted(conn, item)

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
