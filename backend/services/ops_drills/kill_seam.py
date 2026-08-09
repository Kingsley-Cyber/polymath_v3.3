"""Deterministic crash injection for the O2 recovery battery.

A kill point is a named process boundary (e.g. ``ENTITY_CENSUS_COMPLETE:
after_compute``, ``store:qdrant:after_write``). When the environment
variable ``GRAPHIFY_OPS_KILL`` names that point, the process hard-exits
(``os._exit``) the FIRST time the point is crossed; a marker file makes
every later crossing a no-op so the supervised restart can run through
cleanly. With the variable unset (production) the seam is pure no-op.

The marker directory must survive the crash+restart cycle, so drills set
``GRAPHIFY_OPS_KILL_MARKER`` to a mounted volume path inside the worker
container.
"""

from __future__ import annotations

import os

KILL_ENV = "GRAPHIFY_OPS_KILL"
MARKER_ENV = "GRAPHIFY_OPS_KILL_MARKER"
EXIT_CODE = 137

# Seam for tests; production always hard-exits so no cleanup/flush code
# can turn the drill into a graceful shutdown.
_exit = os._exit


def ops_kill_point(point: str) -> None:
    target = os.environ.get(KILL_ENV)
    if not target or target != point:
        return
    marker_dir = os.environ.get(MARKER_ENV) or "/tmp/graphify_ops_kill"
    marker = os.path.join(marker_dir, point.replace(":", "_").replace("/", "_"))
    try:
        os.makedirs(marker_dir, exist_ok=True)
        if os.path.exists(marker):
            return
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("fired")
    except OSError:
        # An unwritable marker dir must not fire an unbounded kill loop.
        return
    _exit(EXIT_CODE)
