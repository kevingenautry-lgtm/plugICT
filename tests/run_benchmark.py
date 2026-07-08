"""
Retrieval-quality eval harness for the ICT Vault (seller-side).

    ICT_VAULT_FILE=/path/ict-vault.kevin \
    ICT_VAULT_LICENSE=/path/license.key \
    python tests/run_benchmark.py [--json out.json]

Reads tests/benchmark_queries.json and reports, over the whole set and per
category:
  * top-1 hit rate  — an expected term is in the #1 result
  * top-5 recall    — an expected term is anywhere in the top 5
  * timing          — avg / p50 / p95 per-query (after the one-time unlock)

Run before shipping ANY change to chunking, embeddings, reranking, FTS, or KG
expansion — it catches silent quality regressions. Requires chromadb +
sentence-transformers installed and a real (or demo) vault.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from vault_core import VaultSession  # noqa: E402

BENCH = Path(__file__).resolve().parent / "benchmark_queries.json"


def result_text(r):
    """The searchable text of a ranked result — works for both the session
    ('text') and MCP ('snippet') shapes."""
    return ((r.get("title", "") or "") + " " + (r.get("text") or r.get("snippet") or "")).lower()


def evaluate(case, ranked, min_results=1):
    """Pure scoring for one query. Returns a dict of booleans/metrics so it can
    be unit-tested without a vault."""
    terms = [t.lower() for t in case.get("expect_terms", [])]
    enough = len(ranked) >= min_results
    top1 = bool(ranked) and (not terms or any(t in result_text(ranked[0]) for t in terms))
    top5_blob = " ".join(result_text(r) for r in ranked[:5])
    top5 = (not terms) or any(t in top5_blob for t in terms)
    return {"enough": enough, "top1": top1 and enough, "top5": top5 and enough}


def _pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return s[i]


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    json_out = None
    if "--json" in argv:
        json_out = argv[argv.index("--json") + 1]

    spec = json.loads(BENCH.read_text())
    min_results = spec.get("min_results", 1)

    t0 = time.perf_counter()
    session = VaultSession().open()
    print(f"Vault unlocked in {time.perf_counter() - t0:.1f}s (one-time)\n")

    rows, times = [], []
    by_cat = {}
    try:
        for case in spec["queries"]:
            qt = time.perf_counter()
            ranked, _, _ = session.search(case["q"], top_k=5)
            dt = time.perf_counter() - qt
            times.append(dt)
            m = evaluate(case, ranked, min_results)
            cat = case.get("category", "uncategorized")
            by_cat.setdefault(cat, []).append(m)
            rows.append((case["q"], m, dt))
            mark = "✓" if m["top5"] else "✗"
            r1 = "1" if m["top1"] else ("5" if m["top5"] else "-")
            print(f"  [{mark}] rank≤{r1:>1}  {case['q']}")
    finally:
        session.close()

    n = len(rows)
    top1 = sum(r[1]["top1"] for r in rows)
    top5 = sum(r[1]["top5"] for r in rows)
    print(f"\n{'='*56}")
    print(f"Top-1 hit rate : {top1}/{n} ({100*top1/n:.0f}%)")
    print(f"Top-5 recall   : {top5}/{n} ({100*top5/n:.0f}%)")
    print(f"Timing (ms)    : avg {1000*sum(times)/n:.0f} · p50 {1000*_pct(times,50):.0f} · p95 {1000*_pct(times,95):.0f}")
    print("\nBy category (top-1 / top-5):")
    for cat, ms in sorted(by_cat.items()):
        c = len(ms)
        print(f"  {cat:12s} {sum(x['top1'] for x in ms)}/{c}  ·  {sum(x['top5'] for x in ms)}/{c}")

    if json_out:
        Path(json_out).write_text(json.dumps({
            "n": n, "top1": top1, "top5": top5,
            "avg_ms": 1000*sum(times)/n, "p95_ms": 1000*_pct(times, 95),
            "by_category": {c: {"n": len(ms), "top1": sum(x["top1"] for x in ms),
                                "top5": sum(x["top5"] for x in ms)} for c, ms in by_cat.items()},
            "failures": [q for q, m, _ in rows if not m["top5"]],
        }, indent=2))
        print(f"\nWrote {json_out}")

    # Fail the run if top-5 recall drops below 80% — tune as the vault matures.
    return 0 if (top5 / n) >= 0.80 else 1


if __name__ == "__main__":
    sys.exit(main())
