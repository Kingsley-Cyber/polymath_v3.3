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
    diagnostic = "\n".join((result.stdout or "", result.stderr or "")).strip()
    label = service.rsplit("/", 1)[-1]
    expected = f'could not find service "{label}" in domain'
    return expected.casefold() in diagnostic.casefold()
