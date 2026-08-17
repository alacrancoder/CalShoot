"""Tests for TimeTree's paging semantics.

These lock in a subtlety that is easy to get backwards and fails silently:
`since` means "updated BEFORE this timestamp", and the feed is ordered
newest-first. Reading it as "changed after" makes the first sync look perfect
and every later sync return nothing at all.
"""

from __future__ import annotations

from calshoot.timetree.client import TimeTreeClient

PAGE_SIZE = 3


class FakeTimeTreeAPI(TimeTreeClient):
    """A TimeTreeClient wired to an in-memory feed with TimeTree's semantics."""

    def __init__(self, updated_ats):
        super().__init__("fake-session")
        # Newest first, the way TimeTree serves them.
        self.feed = [
            {"uuid": f"e{u}", "title": f"Event {u}", "updated_at": u, "start_at": u, "end_at": u}
            for u in sorted(updated_ats, reverse=True)
        ]
        self.requests: list[int] = []

    def _get(self, url, params=None):
        since = (params or {}).get("since", 0)
        self.requests.append(since)

        # since=0 means "no filter"; otherwise return records OLDER than it.
        pool = self.feed if since == 0 else [e for e in self.feed if e["updated_at"] < since]
        page = pool[:PAGE_SIZE]
        return {
            "events": page,
            "chunk": len(pool) > PAGE_SIZE,
            # The oldest record in this page, for paging further back.
            "since": min((e["updated_at"] for e in page), default=since),
        }


class TestFullScan:
    def test_walks_the_whole_history(self):
        api = FakeTimeTreeAPI(range(1, 11))  # 10 events
        events, high_water = api.changed_since(101, 0)

        assert len(events) == 10
        assert high_water == 10

    def test_pages_backwards_until_exhausted(self):
        api = FakeTimeTreeAPI(range(1, 11))
        api.changed_since(101, 0)

        assert len(api.requests) == 4  # 10 events at 3 per page
        assert api.requests[0] == 0  # first request is unfiltered
        # Each later request reaches strictly further back in time.
        later = api.requests[1:]
        assert later == sorted(later, reverse=True)
        assert len(set(later)) == len(later)


class TestIncrementalScan:
    def test_returns_only_events_newer_than_the_high_water_mark(self):
        api = FakeTimeTreeAPI(range(1, 11))
        events, high_water = api.changed_since(101, high_water=8)

        assert sorted(e.updated_at for e in events) == [9, 10]
        assert high_water == 10

    def test_stops_after_one_request_when_nothing_is_new(self):
        api = FakeTimeTreeAPI(range(1, 11))
        events, high_water = api.changed_since(101, high_water=10)

        assert events == []
        assert high_water == 10
        # The whole point: one cheap request, not a full re-walk.
        assert len(api.requests) == 1

    def test_a_returned_page_boundary_is_not_reused_as_a_high_water_mark(self):
        """The regression this suite exists for.

        Feeding TimeTree's returned `since` back as if it were a "changed
        after" cursor yields nothing forever, because it is the OLDEST record
        seen, not the newest.
        """
        api = FakeTimeTreeAPI(range(1, 11))
        _, high_water = api.changed_since(101, 0)

        # The high-water mark must be the newest record...
        assert high_water == 10
        # ...not the oldest, which is what the API hands back for paging.
        assert high_water != 1

        # And a second pass with it must still see a genuinely new event.
        api.feed.insert(0, {"uuid": "new", "title": "New", "updated_at": 99,
                            "start_at": 99, "end_at": 99})
        events, _ = api.changed_since(101, high_water)
        assert [e.uuid for e in events] == ["new"]


class TestDeduplication:
    def test_an_event_seen_on_two_pages_is_returned_once(self):
        api = FakeTimeTreeAPI([5, 5, 5, 4, 3, 2, 1])
        events, _ = api.changed_since(101, 0)
        assert len({e.uuid for e in events}) == len(events)
