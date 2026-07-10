"""
Retrieval-quality eval harness for the ICT Vault.

Compares:
  1. normal single search
  2. multi-search
  3. multi-search plus selective context expansion

Run seller-side against a real vault:

    ICT_VAULT_FILE=/path/ict-vault.kevin \
    ICT_VAULT_LICENSE=/path/license.key \
    python tests/run_benchmark.py --json benchmark.json
"""

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import vault_core as vc  # noqa: E402
from vault_core import VaultSession  # noqa: E402

BENCH = Path(__file__).resolve().parent / "benchmark_queries.json"


def result_text(r):
    return ((r.get("title", "") or "") + " " + (r.get("text") or r.get("snippet") or "")).lower()


def _relevance(case, result):
    terms = [t.lower() for t in case.get("expect_terms", [])]
    if not terms:
        return 0
    blob = result_text(result)
    return sum(1 for t in terms if t in blob)


def _dcg(rels):
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))


def evaluate(case, ranked, min_results=1):
    """Pure scoring for one query. Keeps old top1/top5 keys for unit tests."""
    terms = [t.lower() for t in case.get("expect_terms", [])]
    no_answer = bool(case.get("no_answer"))
    enough = len(ranked) >= min_results
    top1 = bool(ranked) and (not terms or _relevance(case, ranked[0]) > 0)
    top5 = (not terms) or any(_relevance(case, r) > 0 for r in ranked[:5])

    if no_answer:
        no_answer_accuracy = 1.0 if not ranked else 0.0
    else:
        no_answer_accuracy = None

    first_hit = None
    rels = []
    for i, r in enumerate(ranked[:5], 1):
        rel = _relevance(case, r)
        rels.append(rel)
        if rel > 0 and first_hit is None:
            first_hit = i
    mrr = (1.0 / first_hit) if first_hit else 0.0
    ideal = sorted(rels, reverse=True)
    ndcg5 = (_dcg(rels) / _dcg(ideal)) if _dcg(ideal) else 0.0

    expected_ts = case.get("expected_timestamp")
    if expected_ts:
        timestamp_accuracy = 1.0 if any(
            (r.get("timestamp") or r.get("start_ts")) == expected_ts for r in ranked[:5]
        ) else 0.0
    else:
        timestamp_accuracy = None

    ids = [(r.get("video_id"), r.get("timestamp") or r.get("start_ts"), r.get("title"))
           for r in ranked]
    duplicate_rate = 0.0
    if ids:
        duplicate_rate = 1.0 - (len(set(ids)) / len(ids))

    return {
        "enough": enough,
        "top1": top1 and enough,
        "top5": top5 and enough,
        "recall_at_1": 1.0 if (top1 and enough) else 0.0,
        "recall_at_5": 1.0 if (top5 and enough) else 0.0,
        "mrr": mrr,
        "ndcg_at_5": ndcg5,
        "timestamp_accuracy": timestamp_accuracy,
        "no_answer_accuracy": no_answer_accuracy,
        "duplicate_rate": duplicate_rate,
    }


def _pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return s[i]


def _variants(q):
    out = [q]
    expanded, changed = vc.expand_query(q)
    if changed and expanded.lower() != q.lower():
        out.append(expanded)
    compact = q.replace("what is ", "").replace("how does ict ", "").strip()
    if compact and compact.lower() not in {x.lower() for x in out}:
        out.append(compact)
    return out[:vc.MAX_QUERY_VARIANTS]


def _rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


def _run_strategy(session, case, strategy):
    q = case["q"]
    if strategy == "single":
        ranked, _, _ = session.search(q, top_k=5)
        return ranked
    if strategy == "multi":
        ranked, _ = session.multi_search(q, _variants(q), top_k=5)
        return ranked
    if strategy == "multi_context":
        internal, _ = vc.collect_multi_search_candidates(
            session.db, session._semantic_candidates, q, _variants(q), top_k=5)
        if internal:
            try:
                vc.expand_result_context(session.db, internal[0], before=1, after=1)
            except Exception:
                pass
        return vc.finalize_ranked_results(internal)
    raise ValueError(strategy)


def _summarize(rows, times):
    n = len(rows) or 1
    def avg(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return (sum(vals) / len(vals)) if vals else None
    return {
        "n": len(rows),
        "recall_at_1": avg("recall_at_1"),
        "recall_at_5": avg("recall_at_5"),
        "mrr": avg("mrr"),
        "ndcg_at_5": avg("ndcg_at_5"),
        "timestamp_accuracy": avg("timestamp_accuracy"),
        "no_answer_accuracy": avg("no_answer_accuracy"),
        "duplicate_rate": avg("duplicate_rate"),
        "avg_latency_ms": 1000 * sum(times) / n,
        "p95_latency_ms": 1000 * _pct(times, 95),
    }


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    json_out = None
    if "--json" in argv:
        json_out = argv[argv.index("--json") + 1]

    spec = json.loads(BENCH.read_text())
    min_results = spec.get("min_results", 1)

    peak_ram = _rss_mb()
    t0 = time.perf_counter()
    session = VaultSession().open()
    cold_start = time.perf_counter() - t0
    peak_ram = max(peak_ram, _rss_mb())
    print(f"Vault unlocked in {cold_start:.1f}s (one-time)\n")

    strategies = ["single", "multi", "multi_context"]
    data = {s: {"rows": [], "times": []} for s in strategies}

    try:
        for case in spec["queries"]:
            print(case["q"])
            for strategy in strategies:
                qt = time.perf_counter()
                ranked = _run_strategy(session, case, strategy)
                dt = time.perf_counter() - qt
                peak_ram = max(peak_ram, _rss_mb())
                metrics = evaluate(case, ranked, min_results)
                data[strategy]["rows"].append(metrics)
                data[strategy]["times"].append(dt)
                print(f"  {strategy:13s} R@1={metrics['recall_at_1']:.0f} "
                      f"R@5={metrics['recall_at_5']:.0f} {1000*dt:.0f}ms")
    finally:
        session.close()

    report = {
        "cold_start_seconds": cold_start,
        "peak_ram_mb": peak_ram or None,
        "strategies": {
            s: _summarize(data[s]["rows"], data[s]["times"]) for s in strategies
        },
        "limitations": {
            "timestamp_accuracy": "reported only for cases with expected_timestamp",
            "no_answer_accuracy": "reported only for cases with no_answer=true",
            "peak_ram_mb": "uses psutil RSS when available",
        },
    }

    print("\n" + "=" * 64)
    print(f"Cold start: {report['cold_start_seconds']:.2f}s")
    print(f"Peak RAM: {report['peak_ram_mb'] or 'unavailable'} MB")
    for name, summary in report["strategies"].items():
        print(f"\n{name}")
        for k, v in summary.items():
            if k == "n":
                continue
            print(f"  {k}: {'n/a' if v is None else round(v, 4)}")

    if json_out:
        Path(json_out).write_text(json.dumps(report, indent=2))
        print(f"\nWrote {json_out}")

    single_r5 = report["strategies"]["single"]["recall_at_5"] or 0
    return 0 if single_r5 >= 0.80 else 1


if __name__ == "__main__":
    sys.exit(main())
