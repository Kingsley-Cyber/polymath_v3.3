"""Strict launchctl state classification shared by deployment fail-safes."""

from __future__ import annotations

import subprocess

LAUNCHCTL_SERVICE_NOT_FOUND_EXIT = 113


def service_absence_proven(
    result: subprocess.CompletedProcess[str],
    service: str,
) -> bool:
    """Return true only for launchctl's canonical service-not-found result."""
    if result.returncode != LAUNCHCTL_SERVICE_NOT_FOUND_EXIT:
        return False
    parts = service.split("/", 2)
    if (
        len(parts) != 3
        or parts[0] != "gui"
        or not parts[1].isdigit()
        or not parts[2]
    ):
        return False
    expected_command = ["launchctl", "print", service]
    if not isinstance(result.args, (list, tuple)):
        return False
    if list(result.args) != expected_command:
        return False
    if (result.stdout or "").strip():
        return False
    expected = (
        f'Could not find service "{parts[2]}" '
        f"in domain for user gui: {parts[1]}"
    )
    return (result.stderr or "").strip().casefold() == expected.casefold()
