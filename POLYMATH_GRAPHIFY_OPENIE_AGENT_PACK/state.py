from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StageReceipt:
    stage: str
    status: str
    started_at: str
    finished_at: str
    inputs_hash: str = ""
    outputs_hash: str = ""
    files_changed: List[str] = field(default_factory=list)
    tests: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class WorkflowState:
    repo_root: str
    pack_root: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    current_stage: Optional[str] = None
    completed: Dict[str, str] = field(default_factory=dict)
    receipts: List[str] = field(default_factory=list)
    flags: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "WorkflowState":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    def save(self, path: Path) -> None:
        self.updated_at = utc_now()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True), encoding="utf-8")


def write_receipt(pack_root: Path, receipt: StageReceipt) -> Path:
    receipts_dir = pack_root / "work" / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipts_dir / f"{receipt.stage}.receipt.json"
    path.write_text(json.dumps(asdict(receipt), indent=2, sort_keys=True), encoding="utf-8")
    return path
