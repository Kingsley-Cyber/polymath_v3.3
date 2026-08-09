# Practical Python Systems, Volume 2

**Authors:** Mara Voss, Daniel Okafor
**Edition:** 3rd edition, published March 2024 by Lantern Press
**ISBN:** 978-1-59327-000-0

## Preface

This volume covers the operational side of Python services: caching,
scheduled jobs, and observability. Every example was tested on Python 3.11.
The fictional company Northwind Analytics appears throughout the book as the
running case study. Northwind Analytics hired Priya Raman as platform lead
in January 2023.

## Chapter 5: Working with Caches

Caching is the cheapest latency win available to a small platform team. In
this chapter we build a bounded least-recently-used cache from first
principles, then compare it against the shared Redis deployment that
Northwind Analytics adopted in February 2023.

### 5.1 A bounded LRU cache with OrderedDict

The following example implements a bounded LRU cache. The constructor takes
a capacity, `get` marks entries as recently used, and `_evict` removes the
oldest entry whenever the map exceeds capacity.

```python
from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.entries: OrderedDict[str, object] = OrderedDict()

    def get(self, key: str):
        if key not in self.entries:
            return None
        self.entries.move_to_end(key)
        return self.entries[key]

    def put(self, key: str, value: object) -> None:
        self.entries[key] = value
        self.entries.move_to_end(key)
        self._evict()

    def _evict(self) -> None:
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)


cache = LRUCache(capacity=2)
cache.put("report", {"rows": 4200})
cache.put("dashboard", {"widgets": 12})
cache.put("session", {"user": "priya"})
print(cache.get("report"))
print(cache.get("session"))
```

Running the script shows the eviction in action: the oldest entry `report`
is dropped when `session` arrives, so only `session` survives.

```output
None
{'user': 'priya'}
```

Figure 5.1: Eviction order of the bounded LRU cache after three writes with
capacity two.

### 5.2 Capacity planning at Northwind

Priya Raman measured the hit ratio of the in-process cache across the
Northwind reporting fleet before approving the Redis migration.

| Cache layer | Entries | Hit ratio | p95 latency |
|-------------|---------|-----------|-------------|
| In-process LRU | 512 | 0.71 | 0.4 ms |
| Shared Redis | 20 000 | 0.93 | 2.1 ms |
| No cache | 0 | 0.00 | 48.0 ms |

Table 5.1: Cache performance measured by Priya Raman at Northwind Analytics
during the week of 13 February 2023.

The table shows why the team kept both layers: the in-process cache absorbs
hot keys at microsecond cost, while Redis carries the long tail. Daniel
Okafor presented these numbers to the infrastructure review board on
17 February 2023, and the board approved the Redis deployment the same day.

## Chapter 6: Scheduled Jobs

Batch work at Northwind runs on a nightly schedule. The ingestion job starts
at 02:00 UTC and must finish before the 06:00 dashboard refresh. Mara Voss
wrote the retry policy described in this chapter after the outage of
9 November 2022, when a wedged job blocked the dashboard for eleven hours.

Retries use exponential backoff with jitter. A job that fails three times is
moved to a dead-letter table and paged to the on-call engineer.
