"""
Run the search-quality benchmark against the REAL vault (seller-side).

    ICT_VAULT_FILE=/path/ict-vault.kevin \
    ICT_VAULT_LICENSE=/path/license.key \
    python tests/run_benchmark.py

Reports, per query, whether at least `min_results` came back and whether any
expected term appeared in the top results. Use before shipping any change that
touches chunking, embeddings, reranking, or FTS — it catches silent quality
regressions. Requires chromadb + sentence-transformers installed.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import vault_core as vc  # noqa: E402
from query import VaultSession  # noqa: E402

BENCH = Path(__file__).resolve().parent / "benchmark_queries.json"


def main():
    spec = json.loads(BENCH.read_text())
    min_results = spec.get("min_results", 1)
    session = VaultSession().open()
    passed = failed = 0
    try:
        for case in spec["queries"]:
            q = case["q"]
            ranked, _, _ = session.search(q, top_k=5)
            blob = " ".join((r.get("title", "") + " " + r.get("text", "")) for r in ranked).lower()
            enough = len(ranked) >= min_results
            term_hit = any(t.lower() in blob for t in case.get("expect_terms", []))
            ok = enough and (term_hit or not case.get("expect_terms"))
            passed += ok
            failed += not ok
            mark = "PASS" if ok else "FAIL"
            note = "" if ok else ("  (no results)" if not enough else "  (no expected term in top 5)")
            print(f"  [{mark}] {q}{note}")
    finally:
        session.close()
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
