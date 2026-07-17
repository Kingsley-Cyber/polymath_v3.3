#!/usr/bin/env python3
"""Transactionally replace and smoke-test the Apple ML LaunchAgent plist."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Callable, Sequence


class TransactionError(RuntimeError):
    """The new deployment or its mandatory rollback failed."""


@dataclass(frozen=True)
class PlistBackup:
    existed: bool
    content: bytes | None
    mode: int | None


Runner = Callable[..., subprocess.CompletedProcess[str]]


class LaunchAgentTransaction:
    def __init__(
        self,
        *,
        target_plist: Path,
        label: str,
        uid: int | None = None,
        runner: Runner = subprocess.run,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target_plist = target_plist
        self.label = label
        self.uid = os.getuid() if uid is None else uid
        self.runner = runner
        self.sleep_fn = sleep_fn

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def service(self) -> str:
        return f"{self.domain}/{self.label}"

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return self.runner(
            list(command),
            check=False,
            capture_output=True,
            text=True,
        )

    def _backup(self) -> PlistBackup:
        if not self.target_plist.exists():
            return PlistBackup(False, None, None)
        metadata = self.target_plist.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise TransactionError("existing LaunchAgent plist is not a regular file")
        return PlistBackup(
            True,
            self.target_plist.read_bytes(),
            stat.S_IMODE(metadata.st_mode),
        )

    def _atomic_install(self, content: bytes, mode: int) -> None:
        self.target_plist.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.target_plist.name}.",
            dir=self.target_plist.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, self.target_plist)
        finally:
            temporary.unlink(missing_ok=True)

    def _bootout(self) -> None:
        result = self._run(["launchctl", "bootout", self.service])
        verification = self._run(["launchctl", "print", self.service])
        if verification.returncode == 0:
            detail = (result.stderr or result.stdout or "")[-500:]
            raise TransactionError(
                f"launchctl bootout did not unload {self.service}: {detail}"
            )
        # A non-zero bootout is acceptable only because the independent print
        # verification above proves the service was already absent.
        if result.returncode != 0:
            return

    def _bootstrap(self) -> None:
        last_error = ""
        for attempt in range(5):
            result = self._run(
                ["launchctl", "bootstrap", self.domain, str(self.target_plist)]
            )
            if result.returncode == 0:
                return
            last_error = (result.stderr or result.stdout or "")[-500:]
            if attempt < 4:
                self.sleep_fn(1.0)
        raise TransactionError(f"launchctl bootstrap failed: {last_error}")

    def _kickstart(self) -> None:
        result = self._run(["launchctl", "kickstart", "-k", self.service])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "")[-500:]
            raise TransactionError(f"launchctl kickstart failed: {detail}")

    def _restore(self, backup: PlistBackup) -> None:
        self._bootout()
        if not backup.existed:
            self.target_plist.unlink(missing_ok=True)
            return
        assert backup.content is not None and backup.mode is not None
        self._atomic_install(backup.content, backup.mode)
        self._bootstrap()
        self._kickstart()

    def deploy(self, expected_plist: Path, smoke_command: Sequence[str]) -> None:
        if not smoke_command:
            raise TransactionError("a smoke command is required")
        expected = expected_plist.read_bytes()
        backup = self._backup()
        try:
            self._bootout()
            self._atomic_install(expected, 0o644)
            if self.target_plist.read_bytes() != expected:
                raise TransactionError("LaunchAgent plist drifted after install")
            self._bootstrap()
            self._kickstart()
            smoke = self._run(smoke_command)
            if smoke.returncode != 0:
                detail = (smoke.stderr or smoke.stdout or "")[-500:]
                raise TransactionError(f"Apple ML smoke failed: {detail}")
        except BaseException as deploy_error:
            try:
                self._restore(backup)
            except BaseException as rollback_error:
                try:
                    self._bootout()
                except BaseException as emergency_error:
                    raise TransactionError(
                        f"deployment failed ({deploy_error}); rollback failed "
                        f"({rollback_error}); emergency bootout failed "
                        f"({emergency_error}); service state is UNKNOWN"
                    ) from emergency_error
                raise TransactionError(
                    f"deployment failed ({deploy_error}); rollback failed "
                    f"({rollback_error}); prior plist was NOT restored; "
                    "service absence was verified"
                ) from rollback_error
            raise TransactionError(
                f"deployment failed and prior plist was restored: {deploy_error}"
            ) from deploy_error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-plist", required=True, type=Path)
    parser.add_argument("--target-plist", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("smoke_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    smoke_command = list(args.smoke_command)
    if smoke_command[:1] == ["--"]:
        smoke_command = smoke_command[1:]
    transaction = LaunchAgentTransaction(
        target_plist=args.target_plist,
        label=args.label,
    )
    transaction.deploy(args.expected_plist, smoke_command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
