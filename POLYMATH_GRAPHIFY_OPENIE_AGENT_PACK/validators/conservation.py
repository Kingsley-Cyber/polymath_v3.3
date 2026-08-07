from __future__ import annotations
import json


def conservation_check(total: int, terminal_counts: dict) -> dict:
    s = sum(int(v) for v in terminal_counts.values())
    return {"passed": total == s, "total": total, "terminal_sum": s, "terminal_counts": terminal_counts}


if __name__ == "__main__":
    import sys
    total = int(sys.argv[1])
    counts = json.loads(sys.argv[2])
    print(json.dumps(conservation_check(total, counts), indent=2))
