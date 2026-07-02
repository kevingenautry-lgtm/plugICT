"""Validate the benchmark spec is well-formed and every query is FTS-safe."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import vault_core as vc  # noqa: E402

BENCH = Path(__file__).resolve().parent / "benchmark_queries.json"


def test_benchmark_wellformed():
    spec = json.loads(BENCH.read_text())
    assert spec["queries"], "benchmark has no queries"
    assert len(spec["queries"]) >= 20
    seen = set()
    for case in spec["queries"]:
        assert case["q"].strip(), "empty query"
        assert case["q"] not in seen, f"duplicate query: {case['q']}"
        seen.add(case["q"])
        # Every query must survive FTS sanitisation without raising / going empty.
        assert vc.sanitize_fts(case["q"]) is not None
