#!/usr/bin/env python3
"""Small stdlib client for the warm Graphify E2E benchmark worker."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


async def main_async(args: argparse.Namespace) -> int:
    reader, writer = await asyncio.open_unix_connection(str(Path(args.socket).resolve()))
    request = {
        "action": "candidate",
        "input": str(Path(args.input).resolve()),
        "fixture_name": args.fixture_name,
        "namespace": args.namespace,
        "report_json": str(Path(args.report_json).resolve()),
        "proof": str(Path(args.proof).resolve()),
    }
    writer.write(json.dumps(request).encode("utf-8") + b"\n")
    await writer.drain()
    response = json.loads((await reader.readline()).decode("utf-8"))
    writer.close()
    await writer.wait_closed()
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0 if response.get("status") == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--fixture-name", choices=("quality", "throughput"), required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--proof", required=True)
    parser.add_argument("--socket", required=True)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
