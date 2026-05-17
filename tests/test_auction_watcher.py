import datetime as dt
import tempfile
import unittest

from auction_watcher import (
    AuctionItem,
    SiteAdapter,
    filter_targets,
    init_db,
    mark_alerted,
    parse_auctions,
    parse_end_time,
    parse_money,
    was_alerted,
)


class AuctionWatcherTests(unittest.TestCase):
    def test_parse_money(self):
        self.assertEqual(parse_money("$1,234.50"), 1234.50)
        self.assertEqual(parse_money("No bids"), 0.0)

    def test_parse_end_time_with_timezone(self):
        result = parse_end_time("05/17/2026 14:32 PT")
        self.assertEqual(result.tzinfo, dt.timezone.utc)
        self.assertEqual(result.hour, 21)
        self.assertEqual(result.minute, 32)

    def test_parse_auctions(self):
        adapter = SiteAdapter(
            name="govplanet",
            base_url="https://www.govplanet.com",
            listing_url="https://www.govplanet.com/Government+Surplus",
        )
        html = """
        <table>
          <tr class=\"auction-row\">
            <td class=\"title\"><a href=\"/item/abc\">Truck</a></td>
            <td class=\"current-bid\">$150</td>
            <td class=\"bids\">0</td>
            <td class=\"end-time\">05/17/2026 14:32 PT</td>
          </tr>
        </table>
        """
        parsed = parse_auctions(html, adapter)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].title, "Truck")
        self.assertEqual(parsed[0].url, "https://www.govplanet.com/item/abc")
        self.assertEqual(parsed[0].bids, 0)

    def test_filter_targets(self):
        now = dt.datetime(2026, 5, 17, 21, 0, tzinfo=dt.timezone.utc)
        items = [
            AuctionItem("site", "A", "https://a", 100, 0, now + dt.timedelta(minutes=10)),
            AuctionItem("site", "B", "https://b", 700, 0, now + dt.timedelta(minutes=10)),
            AuctionItem("site", "C", "https://c", 100, 2, now + dt.timedelta(minutes=10)),
            AuctionItem("site", "D", "https://d", 100, 0, now + dt.timedelta(minutes=90)),
        ]
        filtered = filter_targets(items, now=now, alert_window_minutes=30, max_bids=0, max_current_bid=500)
        self.assertEqual([item.title for item, _ in filtered], ["A"])

    def test_sqlite_cache(self):
        now = dt.datetime(2026, 5, 17, 21, 10, tzinfo=dt.timezone.utc)
        item = AuctionItem("site", "A", "https://a", 100, 0, now)
        with tempfile.NamedTemporaryFile() as f:
            conn = init_db(f.name)
            self.assertFalse(was_alerted(conn, item))
            mark_alerted(conn, item)
            self.assertTrue(was_alerted(conn, item))


if __name__ == "__main__":
    unittest.main()
