"""
Standalone embedding benchmark for a built ICT vault.

Example:
    ICT_VAULT_FILE=/path/ict-vault.kevin ICT_VAULT_LICENSE=/path/license.key \
    python tests/benchmark_embeddings.py --json embedding-results.json

This script intentionally lives under tests/ and is not imported by production.
It decrypts the vault, reads tests/benchmark_queries.json, embeds all chunks per
model, then reports retrieval and runtime/storage metrics.
"""

import argparse
import json
import math
import os
import sys
import time
import tracemalloc
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import vault_core as vc  # noqa: E402

BENCH = Path(__file__).resolve().parent / "benchmark_queries.json"

MODELS = [
    {
        "label": "all-MiniLM-L6-v2",
        "model_id": "sentence-transformers/all-MiniLM-L6-v2",
        "revision": None,
        "trust_remote_code": False,
    },
    {
        "label": "BAAI/bge-large-en-v1.5",
        "model_id": "BAAI/bge-large-en-v1.5",
        "revision": vc.DEFAULT_EMBEDDING_REVISION,
        "trust_remote_code": False,
    },
    {
        "label": "Qwen3-Embedding-0.6B",
        "model_id": "Qwen/Qwen3-Embedding-0.6B",
        "revision": None,
        "trust_remote_code": True,
    },
]


def _result_text(chunk):
    return f"{chunk['title']} {chunk['text']}".lower()


def _is_relevant(case, chunk):
    terms = [t.lower() for t in case.get("expect_terms", [])]
    if case.get("no_answer"):
        return False
    if not terms:
        return False
    blob = _result_text(chunk)
    return any(t in blob for t in terms)


def _dcg(rels):
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))


def _score_case(case, ranked):
    rels = [1 if _is_relevant(case, c) else 0 for c in ranked[:5]]
    first = next((i + 1 for i, r in enumerate(rels) if r), None)
    ideal = sorted(rels, reverse=True)
    return {
        "recall_at_1": 1.0 if rels[:1] and rels[0] else 0.0,
        "recall_at_5": 1.0 if any(rels) else 0.0,
        "mrr": (1.0 / first) if first else 0.0,
        "ndcg_at_5": (_dcg(rels) / _dcg(ideal)) if any(ideal) else 0.0,
        "diversity": len({c.get("video_id", "") for c in ranked[:5]}) / max(1, min(5, len(ranked))),
    }


def _mean(rows, key):
    vals = [r[key] for r in rows if r[key] is not None]
    return sum(vals) / len(vals) if vals else None


def _model_cache_size(model_id):
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    repo = "models--" + model_id.replace("/", "--")
    path = home / "hub" / repo
    if not path.exists():
        return None
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _fts_columns(db):
    return [r[1] for r in db.execute("PRAGMA table_info(transcripts_fts)").fetchall()]


def load_chunks(db):
    cols = _fts_columns(db)
    has_chunk_id = "chunk_id" in cols
    if has_chunk_id:
        sql = "SELECT chunk_id, title, video_id, playlist, start_ts, content FROM transcripts_fts"
    else:
        sql = "SELECT rowid, title, video_id, playlist, start_ts, content FROM transcripts_fts"
    chunks = []
    for r in db.execute(sql):
        chunks.append({
            "chunk_id": str(r[0]),
            "title": r[1],
            "video_id": r[2],
            "playlist": r[3],
            "start_ts": r[4],
            "text": r[5],
        })
    return chunks


def run_model(model_spec, chunks, queries):
    from sentence_transformers import SentenceTransformer

    tracemalloc.start()
    t0 = time.perf_counter()
    kwargs = {"trust_remote_code": model_spec["trust_remote_code"]}
    if model_spec["revision"]:
        kwargs["revision"] = model_spec["revision"]
    model = SentenceTransformer(model_spec["model_id"], **kwargs)
    cold_start = time.perf_counter() - t0

    texts = [c["text"] for c in chunks]
    doc_embeddings = model.encode(
        texts,
        batch_size=64,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    doc_embeddings = np.asarray(doc_embeddings)

    rows = []
    latencies = []
    for case in queries:
        qt = time.perf_counter()
        q_emb = model.encode([case["q"]], normalize_embeddings=True)
        scores = np.dot(doc_embeddings, np.asarray(q_emb[0]))
        top_idx = np.argsort(scores)[::-1][:5]
        ranked = [chunks[int(i)] for i in top_idx]
        latencies.append(time.perf_counter() - qt)
        rows.append(_score_case(case, ranked))

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    no_answer_cases = [q for q in queries if q.get("no_answer")]
    no_answer_accuracy = None
    if no_answer_cases:
        no_answer_accuracy = 0.0

    return {
        "model": model_spec["label"],
        "recall_at_1": _mean(rows, "recall_at_1"),
        "recall_at_5": _mean(rows, "recall_at_5"),
        "mrr": _mean(rows, "mrr"),
        "ndcg_at_5": _mean(rows, "ndcg_at_5"),
        "no_answer_accuracy": no_answer_accuracy,
        "result_diversity": _mean(rows, "diversity"),
        "cold_start_seconds": cold_start,
        "warm_latency_ms": 1000 * (sum(latencies) / len(latencies)),
        "peak_ram_bytes": peak,
        "model_storage_bytes": _model_cache_size(model_spec["model_id"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", help="Optional output JSON path")
    args = parser.parse_args(argv)

    spec = json.loads(BENCH.read_text())
    queries = spec["queries"]

    try:
        db, _, _ = vc.open_vault()
    except Exception as e:
        print("Benchmark requires a built vault and license.")
        print(f"Set ICT_VAULT_FILE and ICT_VAULT_LICENSE. Detail: {e}")
        return 2

    try:
        chunks = load_chunks(db)
    finally:
        db.close()

    if not chunks:
        print("No chunks found in transcripts_fts.")
        return 2

    results = []
    for model_spec in MODELS:
        print(f"\n== {model_spec['label']} ==")
        result = run_model(model_spec, chunks, queries)
        results.append(result)
        print(json.dumps(result, indent=2))

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
