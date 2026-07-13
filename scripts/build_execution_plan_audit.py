#!/usr/bin/env python3
"""Build and verify the recovered 2026-07-13 execution-plan audit.

This is intentionally a mechanical renderer.  It does not call a model, infer
concept-to-claim mappings, repair prose, or update the implementation
checklist.  The durable artifact bundle contains the recovered claim,
registry, and critique JSON plus the exact frozen checklist text.

Initial recovery from Claude's surviving scratchpad:

    python3 scripts/build_execution_plan_audit.py \
      --import-scratchpad /private/tmp/claude-501/-Applications/\
25355cb5-c5b9-4d9e-92ab-e94544cd9481/scratchpad

Normal deterministic rebuild:

    python3 scripts/build_execution_plan_audit.py

Read-only verification:

    python3 scripts/build_execution_plan_audit.py --verify-only
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class Chunk:
    code: str
    label: str
    start: int
    end: int
    rows: int


CHUNKS: tuple[Chunk, ...] = (
    Chunk("HDR", "Header/Standing Rules/Legend/Invariants/Baseline", 1, 96, 41),
    Chunk("AUD", "Audit Delta 1 + Audit Delta 2", 97, 180, 28),
    Chunk("TMP", "Temporal RAG Program + Governing Librarian Constraint", 181, 247, 17),
    Chunk("LIB", "Deterministic-First Librarian Build Order (Phases 0-4)", 248, 346, 42),
    Chunk("P0S", "P0 Summary Integrity", 347, 401, 27),
    Chunk("P0R", "P0 Retrieval Correctness", 402, 474, 29),
    Chunk("P0L", "P0 Lifecycle And Storage Hygiene", 475, 545, 27),
    Chunk("P1Q", "P1 Librarian Query Understanding", 546, 696, 56),
    Chunk("P1L", "P1 Latency And Resource Isolation", 697, 812, 50),
    Chunk("P2V", "P2 Universal Concept And Vocabulary Layer", 813, 974, 54),
    Chunk("P2X", "P2 Extraction And RunPod Parity", 975, 1093, 40),
    Chunk("P3A", "P3 Thematic RAPTOR + Storage Experiments", 1094, 1184, 45),
    Chunk("GTE", "Quick Upload + Regression Matrix + Strict Ready + Completion Rule", 1185, 1248, 45),
    Chunk("LOG", "Implementation Log (historical receipts)", 1249, 1398, 32),
)

FAMILIES: tuple[tuple[str, str], ...] = (
    ("latent", "latent/underlying concepts"),
    ("alias", "aliases & vocabulary"),
    ("facet", "facets & lenses"),
    ("librarian", "librarian cards/shelves/seats"),
    ("temporal", "temporal metadata"),
    ("hierarchy", "hierarchy & headers"),
    ("extraction", "extraction & parity"),
    ("readiness", "readiness & enrichment states"),
    ("retrieval", "retrieval mechanics"),
    ("eval", "evaluation & gates"),
    ("leftover", "harvested terms not in any seeded family (leftover)"),
)

EXPECTED_REGISTRY_ROWS: Mapping[str, int] = {
    "latent": 11,
    "alias": 12,
    "facet": 10,
    "librarian": 15,
    "temporal": 10,
    "hierarchy": 12,
    "extraction": 14,
    "readiness": 8,
    "retrieval": 14,
    "eval": 10,
    "leftover": 60,
}

EXPECTED_DISPOSITIONS: Mapping[str, int] = {
    "build": 277,
    "done-unverified": 39,
    "done-verified": 105,
    "hold": 10,
    "informational": 24,
    "owner-decision": 14,
    "standing-invariant": 56,
    "superseded": 8,
}

EXPECTED_FEASIBILITY: Mapping[str, int] = {
    "already-done": 167,
    "blocked": 1,
    "feasible": 325,
    "infeasible-as-written": 1,
    "needs-owner-decision": 13,
    "risky": 26,
}

EXPECTED_WHEN: Mapping[str, int] = {
    "S0": 35,
    "S1": 3,
    "S2": 2,
    "S3": 4,
    "S4": 6,
    "S5": 38,
    "S6": 14,
    "S7": 2,
    "S8": 46,
    "S9": 19,
    "S10": 43,
    "S11": 30,
    "S12": 1,
    "S13": 40,
    "S14": 30,
    "CONT": 66,
    "LOG": 154,
}

EXPECTED_SCRIPT_HASHES: Mapping[str, str] = {
    "P1L": "f84f61315b55d79135fa6efbc7d6a0c8c69f14ed4ec817ae0f0e87b2c43de611",
    "P2X": "4c4f2f2510446df9cd33a8a3027d149567e41b62fa80945b395c16013a54abeb",
    "P3A": "0286f864e041ec3f62b00ffe0c68a75021c147898e078f6facd7ba609c2e4934",
}

EXPECTED_GENERATED_CRITIQUE_HASHES: Mapping[str, str] = {
    "P1L": "773ee9f83dd3a59af915ae91aaaac46fdf918dd7af5a0186973916bc1433b165",
    "P2X": "560df6f573009d90c3a524bd8b7011f5da58e065b930e1a2609c7d2d4ff60e26",
    "P3A": "0c163730158f5223ee3e66e3f34d3fa6dd9d8d2386f52d788637e1feaa9564b2",
}

FROZEN_COMMIT = "f049041"
FROZEN_SHA256 = "29081cfff772e8fb7faf01a3395f651b41c5165e7573c2c83af181b4e8400111"
FROZEN_LINES = 1398
EXPECTED_CLAIMS = 533
EXPECTED_CHECKBOXES = 409
EXPECTED_MULTI_COVERED_CHECKBOXES = {1148: 2, 1151: 2, 1165: 2}

CLAIM_FIELDS = (
    "id",
    "line_start",
    "line_end",
    "section",
    "kind",
    "ledger_status",
    "verbatim",
    "receipt",
    "terms",
)
CRITIQUE_FIELDS = (
    "action",
    "who",
    "where",
    "when_s",
    "why",
    "feasibility_verdict",
    "feasibility_note",
    "disposition",
    "required_receipt",
)
REGISTRY_FIELDS = (
    "plan_term",
    "code_symbols",
    "store_fields",
    "verdict",
    "canonical",
    "aliases",
    "collision_note",
    "evidence",
)

ALLOWED_KINDS = {
    "work",
    "acceptance",
    "milestone",
    "invariant",
    "standing-rule",
    "decision",
    "status-tag",
    "fact-claim",
    "log-entry",
}
ALLOWED_LEDGER_STATUS = {"x", "open", "in_code", "partial", "na"}
ALLOWED_REGISTRY_VERDICTS = {"SAME", "OVERLAPPING", "DISTINCT", "PLAN-ONLY"}
WHEN_ORDER = tuple(f"S{i}" for i in range(15)) + ("CONT", "LOG")

SCRIPT_RELATIVE_PATHS: Mapping[str, str] = {
    "P1L": "exec_plan/build_critique_P1L.py",
    "P2X": "build_critique_P2X.py",
    "P3A": "build_critique_P3A.py",
}

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"ak_[A-Za-z0-9_-]{12,}"),
    re.compile(r"rpa_[A-Za-z0-9_-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{12,}", re.IGNORECASE),
)


class AuditError(RuntimeError):
    """Raised when a recovered artifact violates a hard invariant."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def canonical_json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read JSON artifact {path}: {exc}") from exc


def find_critique(scratchpad: Path, code: str) -> Path:
    candidates = (
        scratchpad / f"critique_{code}.json",
        scratchpad / "exec_plan" / f"critique_{code}.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AuditError(f"missing recovered critique for {code}: {candidates}")


def scan_for_secrets(value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False)
    matches = [pattern.pattern for pattern in SECRET_PATTERNS if pattern.search(text)]
    require(not matches, f"refusing to persist artifact bundle with credential-like tokens: {matches}")


def import_scratchpad(scratchpad: Path) -> dict[str, Any]:
    scratchpad = scratchpad.resolve()
    frozen_path = scratchpad / "exec_plan" / f"frozen_checklist_{FROZEN_COMMIT}.md"
    require(frozen_path.is_file(), f"missing frozen checklist: {frozen_path}")
    frozen_text = frozen_path.read_text(encoding="utf-8")

    claims: dict[str, Any] = {}
    critiques: dict[str, Any] = {}
    for chunk in CHUNKS:
        claim_path = scratchpad / "exec_plan" / f"claims_{chunk.code}.json"
        require(claim_path.is_file(), f"missing recovered claims: {claim_path}")
        claims[chunk.code] = load_json(claim_path)
        critiques[chunk.code] = load_json(find_critique(scratchpad, chunk.code))

    registries: dict[str, Any] = {}
    for family, _label in FAMILIES:
        path = scratchpad / f"registry_{family}.json"
        require(path.is_file(), f"missing recovered registry: {path}")
        registries[family] = load_json(path)

    recovered_scripts: dict[str, Any] = {}
    for code, relative_path in SCRIPT_RELATIVE_PATHS.items():
        script_path = scratchpad / relative_path
        require(script_path.is_file(), f"missing recovered critique script: {script_path}")
        source = script_path.read_text(encoding="utf-8")
        recovered_scripts[code] = {
            "scratchpad_relative_path": relative_path,
            "sha256": sha256_text(source),
            "source": source,
            "generated_critique_sha256": canonical_json_hash(critiques[code]),
        }

    bundle: dict[str, Any] = {
        "schema_version": "polymath.execution_plan_audit.v1",
        "provenance": {
            "frozen_commit": FROZEN_COMMIT,
            "frozen_sha256": FROZEN_SHA256,
            "frozen_lines": FROZEN_LINES,
            "claim_rows": EXPECTED_CLAIMS,
            "checkbox_lines": EXPECTED_CHECKBOXES,
            "registry_rows": sum(EXPECTED_REGISTRY_ROWS.values()),
            "primary_registry_rows": sum(
                count for family, count in EXPECTED_REGISTRY_ROWS.items() if family != "leftover"
            ),
            "recovery_method": (
                "P1L/P2X/P3A critique JSON was generated only by the recovered scripts "
                "whose exact source and hashes are embedded below; all other JSON was read "
                "verbatim from the surviving Claude scratchpad."
            ),
            "code_reconciliation_caveat": (
                "Registry and critique prose was produced on 2026-07-13 against a mutable "
                "working tree rather than a commit-pinned code snapshot. Required receipt "
                "cells are the authority for re-verification."
            ),
        },
        "chunks": [dataclasses.asdict(chunk) for chunk in CHUNKS],
        "families": [{"key": key, "label": label} for key, label in FAMILIES],
        "frozen_checklist": frozen_text,
        "claims": claims,
        "critiques": critiques,
        "registries": registries,
        "recovered_critique_scripts": recovered_scripts,
    }
    scan_for_secrets(bundle)
    validate_bundle(bundle)
    return bundle


def validate_frozen_source(bundle: Mapping[str, Any]) -> list[str]:
    frozen = bundle.get("frozen_checklist")
    require(isinstance(frozen, str), "artifact bundle lacks frozen checklist text")
    require(sha256_text(frozen) == FROZEN_SHA256, "frozen checklist SHA-256 mismatch")
    lines = frozen.splitlines()
    require(len(lines) == FROZEN_LINES, f"frozen checklist line count {len(lines)} != {FROZEN_LINES}")
    provenance = bundle.get("provenance", {})
    require(provenance.get("frozen_commit") == FROZEN_COMMIT, "frozen commit provenance mismatch")
    require(provenance.get("frozen_sha256") == FROZEN_SHA256, "frozen hash provenance mismatch")
    return lines


def validate_recovered_scripts(bundle: Mapping[str, Any]) -> None:
    scripts = bundle.get("recovered_critique_scripts")
    require(isinstance(scripts, dict), "missing recovered critique script sources")
    require(set(scripts) == set(EXPECTED_SCRIPT_HASHES), "recovered script key set mismatch")
    critiques = bundle["critiques"]
    for code, expected_hash in EXPECTED_SCRIPT_HASHES.items():
        row = scripts[code]
        source = row.get("source")
        require(isinstance(source, str) and source, f"missing recovered source for {code}")
        require(sha256_text(source) == expected_hash, f"{code} recovered script hash mismatch")
        require(row.get("sha256") == expected_hash, f"{code} recorded script hash mismatch")
        generated_hash = canonical_json_hash(critiques[code])
        require(
            generated_hash == EXPECTED_GENERATED_CRITIQUE_HASHES[code],
            f"{code} generated critique hash mismatch",
        )
        require(
            row.get("generated_critique_sha256") == generated_hash,
            f"{code} recorded generated critique hash mismatch",
        )


def claim_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str]:
    return int(row["line_start"]), int(row["line_end"]), str(row["id"])


def validate_claims(bundle: Mapping[str, Any], frozen_lines: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    claims = bundle.get("claims")
    require(isinstance(claims, dict), "claims must be an object keyed by chunk")
    require(set(claims) == {chunk.code for chunk in CHUNKS}, "claim chunk set mismatch")

    by_id: dict[str, Mapping[str, Any]] = {}
    for chunk in CHUNKS:
        rows = claims[chunk.code]
        require(isinstance(rows, list), f"claims {chunk.code} must be an array")
        require(len(rows) == chunk.rows, f"claims {chunk.code}: {len(rows)} != {chunk.rows}")
        require(rows == sorted(rows, key=claim_sort_key), f"claims {chunk.code} are not line-sorted")
        for row in rows:
            require(isinstance(row, dict), f"claim row in {chunk.code} is not an object")
            missing = [field for field in CLAIM_FIELDS if field not in row]
            require(not missing, f"claim {row.get('id')} lacks fields {missing}")
            identifier = row["id"]
            require(isinstance(identifier, str), f"claim id is not a string: {identifier!r}")
            match = re.fullmatch(rf"{re.escape(chunk.code)}:L(\d+)[A-Za-z]?", identifier)
            require(match is not None, f"claim id does not match chunk: {identifier}")
            require(int(match.group(1)) == row["line_start"], f"claim id/line mismatch: {identifier}")
            require(chunk.start <= row["line_start"] <= row["line_end"] <= chunk.end, f"bad span: {identifier}")
            require(row["kind"] in ALLOWED_KINDS, f"bad claim kind: {identifier} {row['kind']}")
            require(
                row["ledger_status"] in ALLOWED_LEDGER_STATUS,
                f"bad ledger status: {identifier} {row['ledger_status']}",
            )
            require(isinstance(row["verbatim"], str) and row["verbatim"], f"empty claim: {identifier}")
            require(isinstance(row["terms"], list) and row["terms"], f"empty claim terms: {identifier}")
            require(identifier not in by_id, f"duplicate claim id: {identifier}")
            by_id[identifier] = row

    require(len(by_id) == EXPECTED_CLAIMS, f"claim total {len(by_id)} != {EXPECTED_CLAIMS}")

    checkbox_lines = [
        index
        for index, line in enumerate(frozen_lines, start=1)
        if re.match(r"^\s*[-*]?\s*\[[ xX]\]", line)
    ]
    require(
        len(checkbox_lines) == EXPECTED_CHECKBOXES,
        f"checkbox total {len(checkbox_lines)} != {EXPECTED_CHECKBOXES}",
    )
    coverage: dict[int, int] = {}
    claim_rows = list(by_id.values())
    for line_number in checkbox_lines:
        count = sum(
            1
            for row in claim_rows
            if row["line_start"] <= line_number <= row["line_end"]
        )
        require(count > 0, f"uncovered checkbox at frozen line {line_number}")
        coverage[line_number] = count
    multi = {line: count for line, count in coverage.items() if count > 1}
    require(
        multi == EXPECTED_MULTI_COVERED_CHECKBOXES,
        f"unexpected multiply-covered checkbox lines: {multi}",
    )
    return by_id


def validate_critiques(
    bundle: Mapping[str, Any],
    claims_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    critiques = bundle.get("critiques")
    require(isinstance(critiques, dict), "critiques must be an object keyed by chunk")
    require(set(critiques) == {chunk.code for chunk in CHUNKS}, "critique chunk set mismatch")

    by_id: dict[str, Mapping[str, Any]] = {}
    for chunk in CHUNKS:
        rows = critiques[chunk.code]
        require(isinstance(rows, list), f"critiques {chunk.code} must be an array")
        require(len(rows) == chunk.rows, f"critiques {chunk.code}: {len(rows)} != {chunk.rows}")
        for row in rows:
            require(isinstance(row, dict), f"critique row in {chunk.code} is not an object")
            identifier = row.get("id")
            require(identifier in claims_by_id, f"critique has unknown id: {identifier}")
            require(identifier not in by_id, f"duplicate critique id: {identifier}")
            for field in CLAIM_FIELDS:
                require(
                    row.get(field) == claims_by_id[identifier].get(field),
                    f"critique inherited field drift: {identifier}.{field}",
                )
            for field in CRITIQUE_FIELDS:
                require(
                    isinstance(row.get(field), str) and row[field].strip(),
                    f"critique field is empty: {identifier}.{field}",
                )
            by_id[identifier] = row

    require(set(by_id) == set(claims_by_id), "claim/critique id sets differ")
    require(len(by_id) == EXPECTED_CLAIMS, f"critique total {len(by_id)} != {EXPECTED_CLAIMS}")

    dispositions = collections.Counter(row["disposition"] for row in by_id.values())
    feasibility = collections.Counter(row["feasibility_verdict"] for row in by_id.values())
    schedule = collections.Counter(row["when_s"] for row in by_id.values())
    require(dict(sorted(dispositions.items())) == dict(sorted(EXPECTED_DISPOSITIONS.items())), "disposition totals differ")
    require(dict(sorted(feasibility.items())) == dict(sorted(EXPECTED_FEASIBILITY.items())), "feasibility totals differ")
    require(dict(sorted(schedule.items())) == dict(sorted(EXPECTED_WHEN.items())), "when_s totals differ")
    return by_id


def validate_registries(bundle: Mapping[str, Any]) -> None:
    registries = bundle.get("registries")
    require(isinstance(registries, dict), "registries must be an object keyed by family")
    require(set(registries) == set(EXPECTED_REGISTRY_ROWS), "registry family set mismatch")
    total = 0
    primary = 0
    for family, _label in FAMILIES:
        registry = registries[family]
        require(isinstance(registry, dict), f"registry {family} is not an object")
        rows = registry.get("rows")
        require(isinstance(rows, list), f"registry {family}.rows is not an array")
        expected = EXPECTED_REGISTRY_ROWS[family]
        require(len(rows) == expected, f"registry {family}: {len(rows)} != {expected}")
        terms: set[str] = set()
        for row in rows:
            missing = [field for field in REGISTRY_FIELDS if field not in row]
            require(not missing, f"registry {family} row lacks fields {missing}")
            term = row["plan_term"]
            require(isinstance(term, str) and term, f"registry {family} has empty plan_term")
            require(term not in terms, f"registry {family} duplicate plan_term: {term}")
            require(row["verdict"] in ALLOWED_REGISTRY_VERDICTS, f"registry {family} bad verdict")
            require(isinstance(row["canonical"], str) and row["canonical"], f"registry {family} empty canonical")
            require(row["evidence"] not in (None, "", [], {}), f"registry {family} empty evidence: {term}")
            terms.add(term)
        total += len(rows)
        if family != "leftover":
            primary += len(rows)
    require(total == 176, f"registry total {total} != 176")
    require(primary == 116, f"primary registry total {primary} != 116")


def validate_bundle(bundle: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    require(bundle.get("schema_version") == "polymath.execution_plan_audit.v1", "bundle schema mismatch")
    frozen_lines = validate_frozen_source(bundle)
    claims_by_id = validate_claims(bundle, frozen_lines)
    critiques_by_id = validate_critiques(bundle, claims_by_id)
    validate_registries(bundle)
    validate_recovered_scripts(bundle)
    return claims_by_id, critiques_by_id


def verify_git_frozen_source(repo: Path) -> None:
    result = subprocess.run(
        ["git", "show", f"{FROZEN_COMMIT}:docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md"],
        cwd=repo,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    require(result.returncode == 0, f"cannot resolve frozen checklist from git: {result.stderr.decode()}")
    require(sha256_bytes(result.stdout) == FROZEN_SHA256, "git frozen checklist hash mismatch")


def plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(plain(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def format_code_symbols(value: Any) -> str:
    if not isinstance(value, list):
        return plain(value)
    rendered: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            rendered.append(plain(item))
            continue
        symbol = plain(item.get("symbol"))
        file_line = plain(item.get("file_line"))
        role = plain(item.get("role"))
        entry = symbol
        if file_line:
            entry += f" @ {file_line}" if entry else file_line
        if role:
            entry += f" ({role})"
        rendered.append(entry)
    return "; ".join(rendered)


def markdown_cell(value: Any) -> str:
    text = plain(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"(?<!\\)\|", r"\\|", text)


def trim(value: str, limit: int = 160) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def table_row(values: Iterable[Any]) -> str:
    return "| " + " | ".join(markdown_cell(value) for value in values) + " |"


def ordered_counter_text(counter: Mapping[str, int], order: Sequence[str] | None = None) -> str:
    keys = order if order is not None else sorted(counter)
    return ", ".join(f"{key}={counter[key]}" for key in keys if key in counter)


def render_registry(bundle: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Concept & Terminology Registry",
        "",
        (
            "This section is a mechanical render of the 10 recovered terminology families "
            "plus the leftover-term sweep. No names or mappings were added during salvage."
        ),
        "",
    ]
    registries = bundle["registries"]
    collisions: dict[tuple[str, str], tuple[str, str, str]] = {}
    for family, expected_label in FAMILIES:
        registry = registries[family]
        label = plain(registry.get("family")) or expected_label
        lines.extend(
            [
                f"### {label}",
                "",
                "| Plan term | Code truth | Store fields | Verdict | Canonical name | Aliases | Collision note | Evidence |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for row in sorted(registry["rows"], key=lambda item: item["plan_term"].casefold()):
            lines.append(
                table_row(
                    (
                        row["plan_term"],
                        format_code_symbols(row["code_symbols"]),
                        row["store_fields"],
                        row["verdict"],
                        row["canonical"],
                        row["aliases"],
                        row["collision_note"],
                        row["evidence"],
                    )
                )
            )
            note = plain(row["collision_note"]).strip()
            evidence = plain(row["evidence"]).strip()
            if note:
                collisions.setdefault((note, evidence), (label, row["plan_term"], evidence))
        lines.append("")

    lines.extend(["### Collisions and duplicate-implementation risks", ""])
    for (note, evidence), (label, term, _same_evidence) in sorted(
        collisions.items(), key=lambda item: (item[1][0].casefold(), item[1][1].casefold(), item[0][0])
    ):
        lines.append(
            f"- **{markdown_cell(label)} — {markdown_cell(term)}:** "
            f"{markdown_cell(note)} **Evidence:** {markdown_cell(evidence)}"
        )
    lines.append("")
    return lines


def render_schedule_index(critiques_by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    chunk_rank = {chunk.code: index for index, chunk in enumerate(CHUNKS)}

    def row_order(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
        code = str(row["id"]).split(":", 1)[0]
        return chunk_rank[code], int(row["line_start"]), int(row["line_end"]), str(row["id"])

    grouped: dict[str, list[Mapping[str, Any]]] = {slot: [] for slot in WHEN_ORDER}
    for row in critiques_by_id.values():
        require(row["when_s"] in grouped, f"unknown when_s during render: {row['when_s']}")
        grouped[row["when_s"]].append(row)

    lines = [
        "## Execution Schedule Index — mechanical `when_s` grouping",
        "",
        (
            "The original concept-flow writer produced no artifact. To avoid inventing flow "
            "prose, this index groups rows only by their recovered `when_s` value. It makes "
            "no inferred family-to-claim assignments."
        ),
        "",
        "| Slot | Rows | Dispositions | Claim IDs |",
        "|---|---:|---|---|",
    ]
    indexed_ids: list[str] = []
    for slot in WHEN_ORDER:
        rows = sorted(grouped[slot], key=row_order)
        indexed_ids.extend(str(row["id"]) for row in rows)
        dispositions = collections.Counter(str(row["disposition"]) for row in rows)
        identifiers = "<br>".join(f"`{row['id']}`" for row in rows)
        lines.append(
            table_row(
                (
                    slot,
                    len(rows),
                    ordered_counter_text(dispositions),
                    identifiers,
                )
            )
        )
    require(len(indexed_ids) == EXPECTED_CLAIMS, "schedule index row count mismatch")
    require(len(set(indexed_ids)) == EXPECTED_CLAIMS, "schedule index duplicates claim IDs")
    lines.append("")
    return lines


def render_ledger(bundle: Mapping[str, Any]) -> list[str]:
    lines = ["## Audit Ledger — every claim, every disposition", ""]
    for chunk in CHUNKS:
        rows = sorted(bundle["critiques"][chunk.code], key=claim_sort_key)
        lines.extend(
            [
                f"### {chunk.label} (frozen lines {chunk.start}-{chunk.end}, chunk {chunk.code})",
                "",
                "| ID | Claim (verbatim) | Kind / status | Disposition | Auditable action | Who | Where | When | Why | Feasibility | Required receipt |",
                "|---|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for row in rows:
            lines.append(
                table_row(
                    (
                        row["id"],
                        trim(row["verbatim"]),
                        f"{row['kind']} ({row['ledger_status']})",
                        row["disposition"],
                        row["action"],
                        row["who"],
                        row["where"],
                        row["when_s"],
                        row["why"],
                        f"{row['feasibility_verdict']} — {row['feasibility_note']}",
                        row["required_receipt"],
                    )
                )
            )
        lines.append("")
    return lines


def summary_table(title: str, counts: Mapping[str, int], order: Sequence[str] | None = None) -> list[str]:
    keys = order if order is not None else sorted(counts)
    lines = [f"### {title}", "", "| Value | Rows |", "|---|---:|"]
    for key in keys:
        if key in counts:
            lines.append(table_row((key, counts[key])))
    lines.extend([table_row(("Total", sum(counts.values()))), ""])
    return lines


def render_summaries(critiques_by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    dispositions = collections.Counter(row["disposition"] for row in critiques_by_id.values())
    feasibility = collections.Counter(row["feasibility_verdict"] for row in critiques_by_id.values())
    schedule = collections.Counter(row["when_s"] for row in critiques_by_id.values())
    lines = ["## Summaries", ""]
    lines.extend(summary_table("Disposition Summary", dispositions))
    lines.extend(summary_table("Feasibility Summary", feasibility))
    lines.extend(summary_table("Schedule Summary", schedule, WHEN_ORDER))
    return lines


def render_attestation(bundle: Mapping[str, Any]) -> list[str]:
    scripts = bundle["recovered_critique_scripts"]
    lines = [
        "## Parity Attestation",
        "",
        f"- Frozen checklist commit: `{FROZEN_COMMIT}`.",
        f"- Frozen checklist SHA-256: `{FROZEN_SHA256}`; lines: {FROZEN_LINES}.",
        f"- Extracted claims: {EXPECTED_CLAIMS}; unique IDs: {EXPECTED_CLAIMS}.",
        f"- Critiqued claims: {EXPECTED_CLAIMS}; claim/critique missing IDs: **NONE**; extras: **NONE**.",
        f"- Rendered ledger rows: {EXPECTED_CLAIMS}; duplicate rendered IDs: **NONE**.",
        (
            f"- Frozen checkbox lines: {EXPECTED_CHECKBOXES}; covered: {EXPECTED_CHECKBOXES}; "
            "multiply-covered only at 1148, 1151, and 1165 (checkbox plus distinct fact-claim)."
        ),
        "- Registry rows: 176 total (116 seeded-family rows + 60 leftover-term rows).",
        "- Recovered flow prose: **NONE**. The schedule index is a direct `when_s` grouping only.",
        "- Historical `part_00_endstate.md` was not imported: it was not wired into the original assembly and was superseded in part by the committed S3 disposition evidence.",
        "",
        "### Recovered critique-script provenance",
        "",
        "| Chunk | Recovered script SHA-256 | Generated critique canonical SHA-256 | Rows |",
        "|---|---|---|---:|",
    ]
    chunk_rows = {chunk.code: chunk.rows for chunk in CHUNKS}
    for code in ("P1L", "P2X", "P3A"):
        row = scripts[code]
        lines.append(
            table_row(
                (
                    code,
                    row["sha256"],
                    row["generated_critique_sha256"],
                    chunk_rows[code],
                )
            )
        )
    lines.append("")
    return lines


def render_document(
    bundle: Mapping[str, Any],
    critiques_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    lines = [
        "# Execution Plan — Auditable Claim Ledger (2026-07-13)",
        "",
        (
            "Derived from `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md` frozen at commit "
            f"`{FROZEN_COMMIT}`. Claim IDs use `<chunk>:L<line>` in that frozen copy."
        ),
        "",
        (
            f"Frozen source SHA-256: `{FROZEN_SHA256}`. Durable source bundle: "
            "[`docs/audits/execution_plan_2026-07-13/source_artifacts.json`](audits/execution_plan_2026-07-13/source_artifacts.json). "
            "Deterministic builder/verifier: "
            "[`scripts/build_execution_plan_audit.py`](../scripts/build_execution_plan_audit.py)."
        ),
        "",
        "## Provenance and scope caveat",
        "",
        (
            "The checklist claim source is commit-pinned and hash-verified. The recovered "
            "registry and critique prose was produced on 2026-07-13 against a concurrently "
            "changing local worktree, not a commit-pinned code snapshot. Treat code-state "
            "statements as audit findings whose **Required receipt** must be rerun before a "
            "checkbox or production behavior changes."
        ),
        "",
        (
            "The P1L, P2X, and P3A critique JSON files were generated only by three surviving, "
            "pre-authored scripts. Their exact source, source hashes, output hashes, and output "
            "rows are preserved in the durable artifact bundle. No missing critique was newly "
            "written during salvage."
        ),
        "",
        (
            "The original concept-flow writer failed before producing an artifact. This document "
            "therefore uses an honest mechanical `when_s` index and does not infer replacement "
            "family-to-claim flow prose. `docs/PLAN_CRITIQUE_2026-07-13.md` remains the companion "
            "S0-S14 sequencing rationale."
        ),
        "",
        "How to audit a ledger row: open its frozen line, run or inspect its Required receipt, and confirm its Disposition.",
        "",
    ]
    lines.extend(render_registry(bundle))
    lines.extend(render_schedule_index(critiques_by_id))
    lines.extend(render_ledger(bundle))
    lines.extend(render_summaries(critiques_by_id))
    lines.extend(render_attestation(bundle))
    return "\n".join(lines).rstrip() + "\n"


def unescaped_pipe_count(line: str) -> int:
    count = 0
    for index, char in enumerate(line):
        if char != "|":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and line[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            count += 1
    return count


def validate_rendered_document(document: str, expected_ids: set[str]) -> None:
    ledger_ids: list[str] = []
    ledger_pattern = re.compile(r"^\|\s*([A-Z0-9]+:L\d+[A-Za-z]?)\s*\|")
    for line_number, line in enumerate(document.splitlines(), start=1):
        match = ledger_pattern.match(line)
        if not match:
            continue
        ledger_ids.append(match.group(1))
        require(
            unescaped_pipe_count(line) == 12,
            f"ledger row has malformed Markdown columns at document line {line_number}",
        )
    require(len(ledger_ids) == EXPECTED_CLAIMS, f"rendered ledger rows {len(ledger_ids)} != {EXPECTED_CLAIMS}")
    require(len(set(ledger_ids)) == EXPECTED_CLAIMS, "rendered ledger contains duplicate IDs")
    require(set(ledger_ids) == expected_ids, "rendered ledger ID set differs from critique IDs")


def write_bundle(path: Path, bundle: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")


def parse_args(repo: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--import-scratchpad",
        type=Path,
        help="Import Claude's recovered scratchpad and replace the durable artifact bundle.",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=repo / "docs" / "audits" / "execution_plan_2026-07-13" / "source_artifacts.json",
        help="Durable recovered artifact bundle.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo / "docs" / "EXECUTION_PLAN_2026-07-13.md",
        help="Rendered execution-plan Markdown.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Do not write; require the existing output to equal a fresh deterministic render.",
    )
    return parser.parse_args()


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    args = parse_args(repo)
    artifact_path = args.artifacts.resolve()
    output_path = args.output.resolve()

    require(
        not (args.import_scratchpad and args.verify_only),
        "--import-scratchpad and --verify-only are mutually exclusive",
    )
    if args.import_scratchpad:
        bundle = import_scratchpad(args.import_scratchpad)
        write_bundle(artifact_path, bundle)
    else:
        require(artifact_path.is_file(), f"artifact bundle does not exist: {artifact_path}")
        bundle = load_json(artifact_path)

    verify_git_frozen_source(repo)
    claims_by_id, critiques_by_id = validate_bundle(bundle)
    document = render_document(bundle, critiques_by_id)
    validate_rendered_document(document, set(claims_by_id))

    if args.verify_only:
        require(output_path.is_file(), f"rendered document does not exist: {output_path}")
        existing = output_path.read_text(encoding="utf-8")
        require(existing == document, "rendered document differs from deterministic reconstruction")
        action = "verified"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
        action = "wrote"

    artifact_hash = sha256_bytes(artifact_path.read_bytes())
    document_hash = sha256_text(document)
    print(f"{action}: {output_path}")
    print(f"artifacts: {artifact_path} sha256={artifact_hash}")
    print(f"document_sha256={document_hash}")
    print("claims=533 critiques=533 rendered=533 checkboxes=409/409 registries=176 parity=OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
