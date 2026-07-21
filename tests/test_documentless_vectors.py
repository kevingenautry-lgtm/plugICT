"""
Documentless vector store (schema v3) — regression + security tests.

The vault's Chroma store must not carry a second plaintext copy of the
transcript text. These tests prove that:

  1. The runtime semantic path still recovers full text from the in-memory
     SQLite/FTS even when Chroma returns NO documents (the v3 shape).
  2. hydrate_candidate_text restores text/provenance purely from SQLite.
  3. (chromadb-gated) A freshly built collection with documents omitted keeps
     no readable transcript text anywhere in its on-disk files, yet still
     returns the right ids from a semantic query.

Tests 1-2 run everywhere. Test 3 skips automatically when chromadb / the
embedding model are not installed (e.g. CI without the heavy ML deps); run it
on the build machine before shipping a vault.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import vault_core as vc  # noqa: E402


SENTINEL = "quantum_liquidity_purge_sentinel_9f3a"  # unique, unlikely to appear anywhere else


def _memory_db_with_chunk(chunk_id, text, *, title="Lesson", video_id="vid123",
                          playlist="2022 Mentorship", source_file="lesson.md",
                          start_ts="00:12:30", chunk_index=4):
    """Build an in-memory DB shaped like a decrypted vault: an FTS table with the
    chunk text, plus the vault_metadata rows the reader expects."""
    db = sqlite3.connect(":memory:")
    db.execute("""
        CREATE VIRTUAL TABLE transcripts_fts USING fts5(
            chunk_id, chunk_index, title, video_id, playlist,
            start_ts, end_ts, source_file, content,
            start_seconds UNINDEXED, end_seconds UNINDEXED,
            timing_precision UNINDEXED, chunker_version UNINDEXED,
            content_hash UNINDEXED, tokenize='porter unicode61')
    """)
    db.execute(
        "INSERT INTO transcripts_fts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (chunk_id, chunk_index, title, video_id, playlist, start_ts, "00:13:10",
         source_file, text, 750, 790, "exact", "v3", "hash"),
    )
    db.commit()
    return db


def test_hydrate_restores_text_and_provenance_from_sqlite():
    """A documentless semantic candidate (chunk_id only, empty text) must come
    back fully hydrated from the in-memory FTS."""
    cid = "abc123chunk"
    db = _memory_db_with_chunk(cid, f"Displacement precedes the {SENTINEL} move.")

    # This is exactly the shape the v3 semantic path emits: id present, no text.
    candidate = {"source": "semantic", "method": "semantic", "chunk_id": cid,
                 "_full_text": ""}
    hydrated = vc.hydrate_candidate_text(db, candidate)

    assert SENTINEL in hydrated["_full_text"]
    assert hydrated["title"] == "Lesson"
    assert hydrated["video_id"] == "vid123"
    assert hydrated["start_ts"] == "00:12:30"
    assert hydrated["playlist"] == "2022 Mentorship"


def test_vault_session_semantic_path_is_documentless(monkeypatch):
    """VaultSession._semantic_candidates must iterate over ids (not documents)
    and survive a Chroma result that carries no documents at all — then the
    candidate hydrates its text from SQLite."""
    cid = "chunk-xyz-1"
    db = _memory_db_with_chunk(cid, f"Order block context around the {SENTINEL}.")

    class _FakeCollection:
        def query(self, query_texts, n_results, where=None, include=None):
            # Real Chroma with include=['metadatas','distances'] returns documents=None.
            assert include is not None and "documents" not in include
            return {
                "ids": [[cid]],
                "documents": None,
                "metadatas": [[{"title": "Lesson", "video_id": "vid123",
                                "start_ts": "00:12:30", "playlist": "2022 Mentorship",
                                "chunk_index": 4, "source_file": "lesson.md"}]],
                "distances": [[0.12]],
            }

    session = vc.VaultSession()
    session.db = db
    session.chroma_dir = "/nonexistent"
    monkeypatch.setattr(vc, "chroma_store_usable", lambda _dir: True)
    monkeypatch.setattr(session, "_get_collection", lambda: _FakeCollection())

    cands = session._semantic_candidates("what is a mitigation block", 5)
    assert len(cands) == 1
    assert cands[0]["chunk_id"] == cid
    assert cands[0]["_full_text"] == ""  # documentless: no text from the vector store

    hydrated = vc.hydrate_candidate_text(db, cands[0])
    assert SENTINEL in hydrated["_full_text"]


def test_vector_schema_version_is_documentless():
    """The schema marker must advertise v3 so tooling can tell documentless
    vaults apart from legacy with-text ones."""
    assert vc.VECTOR_SCHEMA_VERSION == "3"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("chromadb") is None
    or __import__("importlib").util.find_spec("sentence_transformers") is None,
    reason="chromadb / sentence-transformers not installed (run on the build machine)",
)
def test_built_collection_holds_no_transcript_text(tmp_path):
    """End-to-end: a collection built the documentless way must keep NO readable
    transcript text in any of its on-disk files, yet still return ids for a
    semantic query."""
    import chromadb
    from chromadb.config import Settings

    ef, _ = vc.get_embedding_function(return_metadata=True)
    text = f"Fair value gap and the {SENTINEL} displacement leg."
    client = chromadb.PersistentClient(path=str(tmp_path),
                                       settings=Settings(anonymized_telemetry=False))
    col = client.get_or_create_collection("ict_vault", embedding_function=ef)
    # Documentless upsert: embeddings + metadata, NO documents.
    col.upsert(ids=["c1"], embeddings=ef([text]),
               metadatas=[{"playlist": "2022 Mentorship", "title": "Lesson"}])

    # 1) The sentinel must appear in NONE of the vector store's files.
    for root, _dirs, files in os.walk(tmp_path):
        for fn in files:
            blob = open(os.path.join(root, fn), "rb").read()
            assert SENTINEL.encode() not in blob, f"transcript text leaked into {fn}"

    # 2) Chroma still returns the id for a semantic query.
    res = col.query(query_texts=["displacement leg"], n_results=1,
                    include=["metadatas", "distances"])
    assert res["ids"][0] == ["c1"]
