#!/usr/bin/env python3
"""Scan tracked source files for accidentally committed secrets.

This is intentionally dependency-free so it can run in fresh clones, CI, and
install checks before the Python environment is fully provisioned. It scans only
`git ls-files`; local `.env`, Cloudflare credentials, and machine-specific
overrides stay ignored and are not read.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
)

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""
    (?P<name>[A-Z0-9_-]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|WEBHOOK|TUNNEL)[A-Z0-9_-]*)
    \s*[:=]\s*
    (?P<quote>['"]?)
    (?P<value>[^'"\s#]+)
    (?P=quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)

HIGH_ENTROPY_ASSIGNMENT_RE = re.compile(
    r"""
    (?P<name>[A-Z0-9_-]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_-]*)
    \s*[:=]\s*
    (?P<quote>['"]?)
    (?P<value>[A-Za-z0-9_./+=:-]{32,})
    (?P=quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)

BINARY_OR_GENERATED_RE = re.compile(
    r"""
    (
      \.png|\.jpe?g|\.gif|\.ico|\.pdf|\.zip|\.gz|\.tar|\.bin|\.gguf|
      \.safetensors|\.onnx|\.pt|\.pth|package-lock\.json|pnpm-lock\.yaml|
      yarn\.lock
    )$
    """,
    re.IGNORECASE | re.VERBOSE,
)

PLACEHOLDER_RE = re.compile(
    r"""
    ^$|CHANGE_ME|changeme|dummy|example|placeholder|your[_-]?|
    test|ci-dummy|localhost|host\.docker\.internal|null|none|false|true|
    local-dev-change-me|sk-corpus-secret|^http://n8n|^\$|
    os\.environ|process\.env|getenv|settings\.|body\.|request\.|resolved\.|
    current_|new_|plaintext|token_count|max_tokens|seed_limit_per_token|
    <.*>|\$\{.*\}
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _git_ls_files() -> list[str]:
    try:
        output = subprocess.check_output(["git", "ls-files"], text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"secret-scan: unable to list tracked files: {exc}", file=sys.stderr)
        return []
    return [line for line in output.splitlines() if line.strip()]


def _entropy(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _is_placeholder(value: str) -> bool:
    cleaned = value.strip().strip("'\"")
    return bool(PLACEHOLDER_RE.search(cleaned))


def _is_config_like(path: Path) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    return (
        name.startswith(".env")
        or suffix in {".env", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".json"}
    )


def _sensitive_name(name: str) -> bool:
    parts = [part for part in re.split(r"[_-]+", name.lower()) if part]
    joined = "_".join(parts)
    return (
        "api_key" in joined
        or "secret" in parts
        or "token" in parts
        or "password" in parts
        or "webhook" in parts
        or "tunnel" in parts
    )


def _literal_secret_candidate(path: Path, quote: str, value: str) -> bool:
    if _is_placeholder(value) or len(value) < 16:
        return False
    if not quote and not _is_config_like(path):
        return False
    if any(ch in value for ch in "()[]{}"):
        return False
    if value.startswith(("/", "./", "../")):
        return False
    return True


def _line_excerpt(line: str, match_start: int, match_end: int) -> str:
    start = max(0, match_start - 24)
    end = min(len(line), match_end + 24)
    excerpt = line[start:end].strip()
    return excerpt[:160]


def scan_file(path: Path) -> list[str]:
    if BINARY_OR_GENERATED_RE.search(path.as_posix()):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(line):
                findings.append(
                    f"{path}:{lineno}: {label}: {_line_excerpt(line, match.start(), match.end())}"
                )

        for match in SENSITIVE_ASSIGNMENT_RE.finditer(line):
            if not _sensitive_name(match.group("name")):
                continue
            value = match.group("value").strip().strip(",")
            if not _literal_secret_candidate(path, match.group("quote"), value):
                continue
            findings.append(
                f"{path}:{lineno}: sensitive assignment {match.group('name')}: "
                f"{_line_excerpt(line, match.start('value'), match.end('value'))}"
            )

        for match in HIGH_ENTROPY_ASSIGNMENT_RE.finditer(line):
            if not _sensitive_name(match.group("name")):
                continue
            value = match.group("value").strip().strip(",")
            if not _literal_secret_candidate(path, match.group("quote"), value):
                continue
            if _entropy(value) >= 4.5:
                findings.append(
                    f"{path}:{lineno}: high-entropy assignment {match.group('name')}: "
                    f"{_line_excerpt(line, match.start('value'), match.end('value'))}"
                )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan tracked files for committed secrets.")
    parser.add_argument("--quiet", action="store_true", help="Only print findings.")
    args = parser.parse_args()

    findings: list[str] = []
    for filename in _git_ls_files():
        findings.extend(scan_file(Path(filename)))

    if findings:
        print("Potential committed secrets found:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1

    if not args.quiet:
        print("[ OK ] No tracked API keys or secrets detected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
