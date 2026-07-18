"""Strict launchctl state classification shared by deployment fail-safes."""

from __future__ import annotations

import subprocess
import time
from typing import Callable

LAUNCHCTL_SERVICE_NOT_FOUND_EXIT = 113
LAUNCHCTL_ABSENCE_ATTEMPTS = 50
LAUNCHCTL_ABSENCE_POLL_SECONDS = 0.1


def service_absence_proven(
    result: subprocess.CompletedProcess[str],
    service: str,
) -> bool:
    """Return true only for launchctl's canonical service-not-found result."""
    if result.returncode != LAUNCHCTL_SERVICE_NOT_FOUND_EXIT:
        return False
    parts = service.split("/", 2)
    if len(parts) != 3 or parts[0] != "gui" or not parts[1].isdigit() or not parts[2]:
        return False
    expected_command = ["launchctl", "print", service]
    if not isinstance(result.args, (list, tuple)):
        return False
    if list(result.args) != expected_command:
        return False
    if (result.stdout or "").strip():
        return False
    expected = (
        f'Could not find service "{parts[2]}" ' f"in domain for user gui: {parts[1]}"
    )
    observed = (result.stderr or "").strip().casefold()
    canonical = {
        expected.casefold(),
        f"Bad request.\n{expected}".casefold(),
    }
    return observed in canonical


def wait_for_service_absence(
    inspect: Callable[[], subprocess.CompletedProcess[str]],
    service: str,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    attempts: int = LAUNCHCTL_ABSENCE_ATTEMPTS,
    poll_seconds: float = LAUNCHCTL_ABSENCE_POLL_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Poll launchd's asynchronous bootout until exact absence or hard error."""
    if attempts < 1:
        raise ValueError("launchctl absence attempts must be positive")
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(attempts):
        last = inspect()
        if service_absence_proven(last, service):
            return last
        if last.returncode != 0:
            return last
        if attempt + 1 < attempts:
            sleep_fn(poll_seconds)
    assert last is not None
    return last
