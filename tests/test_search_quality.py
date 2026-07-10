"""Eval-harness scoring, candidate dedup, and KG auto-expansion — all pure /
in-memory, no vault or model download."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_core as vc
import run_benchmark as bench


# ── eval harness scoring ─────────────────────────────────────────────────────

def test_evaluate_top1_and_top5():
    case = {"q": "fvg", "expect_terms": ["fair value"]}
    ranked = [{"title": "Ep 4", "snippet": "a fair value gap forms"},
              {"title": "Ep 9", "snippet": "unrelated"}]
    m = bench.evaluate(case, ranked)
    assert m["top1"] and m["top5"] and m["enough"]


def test_evaluate_term_only_in_top5_not_top1():
    case = {"q": "fvg", "expect_terms": ["fair value"]}
    ranked = [{"title": "Ep 1", "snippet": "coffee"},
              {"title": "Ep 4", "snippet": "the fair value gap"}]
    m = bench.evaluate(case, ranked)
    assert not m["top1"] and m["top5"]


def test_evaluate_miss():
    case = {"q": "fvg", "expect_terms": ["fair value"]}
    ranked = [{"title": "Ep 1", "snippet": "coffee"}]
    m = bench.evaluate(case, ranked)
    assert not m["top1"] and not m["top5"]


def test_benchmark_queries_file_valid():
    spec = json.loads((Path(__file__).resolve().parent / "benchmark_queries.json").read_text())
    assert len(spec["queries"]) >= 40
    assert all("q" in q and "expect_terms" in q for q in spec["queries"])


# ── dedup ────────────────────────────────────────────────────────────────────

def test_dedup_collapses_same_chunk_keeps_longer_text():
    cands = [
        {"method": "keyword", "video_id": "v1", "timestamp": "1:00", "snippet": "short"},
        {"method": "semantic", "video_id": "v1", "start_ts": "1:00", "text": "a much longer version"},
        {"method": "keyword", "video_id": "v2", "timestamp": "2:00", "snippet": "distinct"},
    ]
    out = vc.dedup_candidates(cands)
    assert len(out) == 2
    v1 = next(c for c in out if c.get("video_id") == "v1")
    assert "longer version" in vc._cand_text(v1)
    assert v1["dual_hit"]


# ── KG auto-expansion ────────────────────────────────────────────────────────

def _kg_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE entities(name,type,description,source_count)")
    db.executemany("INSERT INTO entities VALUES(?,?,?,?)", [
        ("Silver Bullet", "model", "", 5), ("FVG", "concept", "", 9),
        ("Killzone", "time", "", 4), ("Order Block", "concept", "", 7), ("MS", "x", "", 1)])
    db.execute("CREATE TABLE relations(from_entity,to_entity,relation_type,evidence)")
    db.executemany("INSERT INTO relations VALUES(?,?,?,?)", [
        ("Silver Bullet", "FVG", "uses", ""), ("Silver Bullet", "Killzone", "timed_by", ""),
        ("MS", "FVG", "x", "")])
    db.commit()
    return db


def test_kg_expand_returns_related_entities():
    assert set(vc.kg_expand(_kg_db(), "what is the silver bullet setup")) == {"FVG", "Killzone"}


def test_kg_expand_word_boundary_no_false_match():
    # 'terms' must NOT trigger the 'MS' entity
    assert vc.kg_expand(_kg_db(), "these terms matter") == []


def test_kg_expand_degrades_without_tables():
    assert vc.kg_expand(sqlite3.connect(":memory:"), "silver bullet") == []


def test_kg_expand_caps_results():
    db = _kg_db()
    assert len(vc.kg_expand(db, "silver bullet", max_related=1)) == 1
