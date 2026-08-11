"""The wiki's drift audit as a suite gate (owner-ordered 2026-08-11).

Mechanizes vocabulary parity and line-pin freshness: if code drifts from
the claims layer (docs/wiki), this test fails and names the drifted claim.
"""
import pathlib
import subprocess
import sys

import pytest

VERIFIER = pathlib.Path(__file__).resolve().parents[2] / "docs" / "wiki" / "verify_claims.py"


@pytest.mark.skipif(not VERIFIER.exists(), reason="claims wiki not present in this checkout")
def test_wiki_claims_hold():
    proc = subprocess.run(
        [sys.executable, str(VERIFIER)],
        capture_output=True, text=True,
        cwd=str(VERIFIER.parents[2]),
    )
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        "wiki claims drifted from code:\n" + proc.stdout + proc.stderr
    )
