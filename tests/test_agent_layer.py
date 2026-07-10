import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vault_core as vc  # noqa: E402
import mcp_server as mcp  # noqa: E402


class _ScoreByText:
    def __init__(self, scores=None):
        self.pairs = None
        self.scores = scores

    def predict(self, pairs):
        self.pairs = pairs
        if self.scores is not None:
            return self.scores
        out = []
        for query, text in pairs:
            q_words = set(query.lower().split())
            out.append(sum(w in text.lower() for w in q_words))
        return out


def _agent_db():
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE VIRTUAL TABLE transcripts_fts USING fts5(
        chunk_id, chunk_index, title, video_id, playlist, start_ts, end_ts, source_file, content,
        tokenize='porter unicode61')""")
    rows = [
        ("ck0", 0, "FVG Lesson", "v1", "P", "0:00", "0:10", "a.md",
         "0:00 before context " + ("b " * 400)),
        ("ck1", 1, "FVG Lesson", "v1", "P", "0:20", "0:35", "a.md",
         "0:20 fair value gap imbalance original answer " + ("x " * 700)),
        ("ck2", 2, "FVG Lesson", "v1", "P", "0:40", "0:55", "a.md",
         "0:40 after context " + ("a " * 400)),
        ("ck3", 0, "Silver Lesson", "v2", "P", "1:00", "1:20", "b.md",
         "1:00 silver bullet timing only"),
    ]
    db.executemany("INSERT INTO transcripts_fts VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.execute("CREATE TABLE entities(name,type,description,source_count)")
    db.execute("CREATE TABLE relations(from_entity,to_entity,relation_type,evidence)")
    db.commit()
    return db


def test_multi_search_fuses_variants_sources_and_dedups(monkeypatch):
    db = _agent_db()

    def semantic(query, limit, playlist, rrf_source, matched_query):
        return [{
            "source": "semantic", "method": "semantic", "chunk_id": "ck1",
            "title": "FVG Lesson", "video_id": "v1", "start_ts": "0:20",
            "timestamp": "0:20", "playlist": "P", "source_file": "a.md",
            "chunk_index": 1, "end_ts": "0:35",
            "_full_text": "semantic fair value gap original answer",
            "_rank_in_source": 0, "_rrf_source": rrf_source,
            "matched_queries": [matched_query], "retrieval_sources": ["semantic"],
        }]

    monkeypatch.setattr(vc, "_reranker", _ScoreByText())
    ranked, meta = vc.collect_multi_search_candidates(
        db, semantic, "what is the fair value gap original answer",
        ["fair value gap", "imbalance"], top_k=3)

    assert meta["queries"] == ["fair value gap", "imbalance"]
    assert ranked[0]["chunk_id"] == "ck1"
    assert set(ranked[0]["matched_queries"]) == {"fair value gap", "imbalance"}
    assert {"keyword", "semantic"} <= set(ranked[0]["retrieval_sources"])


def test_multi_search_reranks_against_original_question(monkeypatch):
    db = _agent_db()
    fake = _ScoreByText([1.0, 10.0])
    monkeypatch.setattr(vc, "_reranker", fake)
    vc.collect_multi_search_candidates(
        db, None, "original fair value gap", ["silver bullet", "fair value gap"], top_k=2)

    assert fake.pairs
    assert all(pair[0] == "original fair value gap" for pair in fake.pairs)


def test_stable_chunk_id_dedup_is_primary():
    cands = [
        {"chunk_id": "same", "video_id": "v1", "timestamp": "0:00", "_full_text": "short"},
        {"chunk_id": "same", "video_id": "v2", "timestamp": "9:00", "_full_text": "longer text"},
    ]
    out = vc.dedup_candidates(cands)
    assert len(out) == 1
    assert vc._cand_text(out[0]) == "longer text"


def test_result_refs_are_opaque_expiring_and_single_use():
    store = vc.ResultRefStore(ttl_seconds=10, max_uses=1)
    ref = store.issue({"chunk_id": "ck1", "source_file": "a.md", "chunk_index": 1}, now=100)
    assert "ck1" not in ref
    assert store.resolve(ref, now=101)["chunk_id"] == "ck1"
    with pytest.raises(vc.VaultError):
        store.resolve(ref, now=102)

    expired = store.issue({"chunk_id": "ck2"}, now=100)
    with pytest.raises(vc.VaultError):
        store.resolve(expired, now=111)


def test_expand_result_context_adjacency_timestamps_and_caps():
    db = _agent_db()
    payload = vc.expand_result_context(
        db, {"chunk_id": "ck1", "source_file": "a.md", "chunk_index": 1},
        before=1, after=1)

    assert [s["position"] for s in payload["sections"]] == ["before", "current", "after"]
    assert [s["timestamp"] for s in payload["sections"]] == ["0:00", "0:20", "0:40"]
    assert [s["end_ts"] for s in payload["sections"]] == ["0:10", "0:35", "0:55"]
    assert len(payload["sections"][0]["text"]) <= 500
    assert len(payload["sections"][1]["text"]) <= 1000
    assert len(payload["sections"][2]["text"]) <= 500
    assert payload["total_chars"] <= 2000


def test_finalize_caps_and_no_hidden_text_leakage():
    out = vc.finalize_ranked_results([{
        "chunk_id": "ck1",
        "_full_text": "secret " + ("x" * 1500),
        "_debug": "hide",
        "title": "T",
        "video_id": "v",
        "start_ts": "0:00",
        "result_ref": "opaque",
    }])
    assert len(out[0]["snippet"]) == 500
    assert "chunk_id" not in out[0]
    assert "_full_text" not in out[0]
    assert "_debug" not in out[0]
    assert out[0]["result_ref"] == "opaque"

    capped = vc.finalize_ranked_results([{"_full_text": "x" * 1500}], snippet_chars=5000)
    assert len(capped[0]["snippet"]) == 1000


def test_work_unit_rate_limit(monkeypatch):
    mcp._query_timestamps.clear()
    monkeypatch.setattr(mcp, "_RATE_LIMIT_WORK_UNITS_PER_MINUTE", 3)
    assert mcp._rate_limit_exceeded(2) is False
    assert mcp._rate_limit_exceeded(2) is True
    mcp._query_timestamps.clear()
