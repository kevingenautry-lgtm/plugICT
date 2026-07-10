import builtins
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import vault_core as vc  # noqa: E402


class _CaptureCE:
    def __init__(self, scores=None):
        self.pairs = None
        self.scores = scores

    def predict(self, pairs):
        self.pairs = pairs
        return self.scores if self.scores is not None else [1.0 for _ in pairs]


def _meta_db(meta=None):
    db = sqlite3.connect(":memory:")
    vc.store_embedding_metadata(db, meta or vc.configured_embedding_metadata())
    db.commit()
    return db


def test_embedding_metadata_stored():
    db = _meta_db()
    rows = dict(db.execute("SELECT key, value FROM vault_metadata").fetchall())
    for key in (
        vc.EMBEDDING_MODEL_KEY,
        vc.EMBEDDING_DIM_KEY,
        vc.EMBEDDING_NORMALIZE_KEY,
        vc.EMBEDDING_REVISION_KEY,
        vc.QUERY_INSTRUCTION_KEY,
        vc.VECTOR_SCHEMA_KEY,
    ):
        assert rows[key]
    assert rows[vc.EMBEDDING_MODEL_KEY] == "BAAI/bge-large-en-v1.5"
    assert rows[vc.EMBEDDING_DIM_KEY] == "1024"


def test_embedding_validation_mismatch(monkeypatch):
    required = vc.configured_embedding_metadata()
    db = _meta_db(required)

    def fake_loader(required_metadata=None, return_metadata=False):
        actual = dict(required_metadata)
        actual[vc.EMBEDDING_MODEL_KEY] = "all-MiniLM-L6-v2"
        actual[vc.EMBEDDING_DIM_KEY] = "384"
        ef = lambda input: [[0.0] * 384 for _ in input]
        return (ef, actual) if return_metadata else ef

    monkeypatch.setattr(vc, "get_embedding_function", fake_loader)
    with pytest.raises(vc.VaultError) as e:
        vc.validate_embedding_compatibility(db, require_metadata=True)
    assert str(e.value) == (
        "This vault requires BAAI/bge-large-en-v1.5 (1024-dim). "
        "Loaded: all-MiniLM-L6-v2 (384-dim). Please install the correct model."
    )


def test_no_silent_fallback(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("BGE unavailable")
        if name.startswith("chromadb"):
            raise AssertionError("MiniLM fallback was attempted")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(vc.VaultError) as e:
        vc.get_embedding_function(vc.configured_embedding_metadata(), return_metadata=True)
    assert "This vault requires BAAI/bge-large-en-v1.5 (1024-dim)" in str(e.value)


def test_full_chunk_text_rerank(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE VIRTUAL TABLE transcripts_fts USING fts5(
        chunk_id, title, video_id, playlist, start_ts, source_file, content,
        tokenize='porter unicode61')""")
    full = "0:00 " + ("x " * 350) + "important tail"
    db.execute("INSERT INTO transcripts_fts VALUES (?,?,?,?,?,?,?)",
               ("chunk_1", "T", "v1", "P", "0:00", "a.md", full))
    cands = vc.fts_candidates(db, "tail", 5)
    fake = _CaptureCE([1.0])
    monkeypatch.setattr(vc, "_reranker", fake)
    vc.rerank("tail", cands, top_k=1)
    assert "important tail" in fake.pairs[0][1]
    assert len(fake.pairs[0][1]) > 500


def test_snippet_default_500_and_hard_capped_1000():
    out = vc.finalize_ranked_results([{"title": "T", "_full_text": "x" * 1500}])
    assert len(out[0]["snippet"]) == 500
    out = vc.finalize_ranked_results([{"title": "T", "_full_text": "x" * 1500}], snippet_chars=2000)
    assert len(out[0]["snippet"]) == 1000


def test_mmr_same_video_penalty():
    cands = [
        {"title": "A1", "video_id": "a", "timestamp": "1:00", "snippet": "alpha", "final_score": 1.0},
        {"title": "A2", "video_id": "a", "timestamp": "3:00", "snippet": "beta", "final_score": 0.95},
        {"title": "B1", "video_id": "b", "timestamp": "2:00", "snippet": "gamma", "final_score": 0.75},
    ]
    out = vc.apply_mmr(cands, top_k=2)
    assert [c["title"] for c in out] == ["A1", "B1"]


def test_mmr_nearby_timestamp_penalty():
    cands = [
        {"title": "A1", "video_id": "a", "timestamp": "1:00", "snippet": "alpha", "final_score": 1.0},
        {"title": "near", "video_id": "a", "timestamp": "1:20", "snippet": "beta", "final_score": 0.96},
        {"title": "far", "video_id": "a", "timestamp": "3:00", "snippet": "gamma", "final_score": 0.94},
    ]
    out = vc.apply_mmr(cands, top_k=2)
    assert [c["title"] for c in out] == ["A1", "far"]


def test_min_score_returns_empty(monkeypatch):
    monkeypatch.setattr(vc, "_reranker", _CaptureCE([
        vc.MIN_RERANK_SCORE - 1,
        vc.MIN_RERANK_SCORE - 2,
    ]))
    out = vc.rerank("fair value gap", [
        {"title": "low", "snippet": "unrelated"},
        {"title": "lower", "snippet": "also unrelated"},
    ], top_k=2)
    assert out == []


def test_chunk_overlap_preserves_timestamps():
    body = "\n".join([
        "0:00 " + ("a" * 80),
        "0:20 " + ("b" * 80),
        "0:40 " + ("c" * 80),
    ])
    chunks = vc.chunk_transcript_body(body, chunk_size=100, overlap_chars=90)
    assert len(chunks) == 2
    assert chunks[1]["start_ts"] == "0:20"
    assert chunks[1]["text"].splitlines()[0].startswith("0:20 ")
    assert not chunks[1]["text"].splitlines()[0].startswith("a")


def test_case_insensitive_acronym_expansion():
    expanded, changed = vc.expand_query("fvg Fvg BISI sibi")
    assert changed
    assert expanded.count("Fair Value Gap") == 2
    assert "Buy Side Imbalance Sell Side Inefficiency" in expanded
    assert "Sell Side Imbalance Buy Side Inefficiency" in expanded
    assert vc.expand_query("ms bs ce")[1] is False


def test_cache_invalidates_on_model_change():
    vc.clear_search_cache()
    vc._set_vault_embedding_cache_metadata({
        vc.EMBEDDING_MODEL_KEY: "model-a",
        vc.EMBEDDING_DIM_KEY: "10",
    })
    results = [{"title": "cached"}]
    vc.put_cached_results("query", 5, "playlist", results)
    assert vc.get_cached_results("query", 5, "playlist") == results
    vc._set_vault_embedding_cache_metadata({
        vc.EMBEDDING_MODEL_KEY: "model-b",
        vc.EMBEDDING_DIM_KEY: "10",
    })
    assert vc.get_cached_results("query", 5, "playlist") is None
