"""The buyer search path reranks its deduped candidate pool, works for both the
'text' (session) and 'snippet' (MCP) candidate shapes, and degrades gracefully
when the cross-encoder model isn't available. No model download in tests."""
import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import vault_core as vc


class _FakeCE:
    """Scores a (query, text) pair by how many query words appear in the text."""
    def __init__(self, *a, **k):
        pass
    def predict(self, pairs):
        q = pairs[0][0].lower().split()
        return [sum(w in text.lower() for w in q) for (_, text) in pairs]


class _CapturingCE:
    def __init__(self):
        self.pairs = None

    def predict(self, pairs):
        self.pairs = pairs
        return [0 for _ in pairs]


class _ScoreCE:
    def __init__(self, scores):
        self.scores = scores

    def predict(self, pairs):
        return self.scores


def test_cand_text_supports_both_shapes_and_strips_tags():
    assert vc._cand_text({"text": "<b>FVG</b> gap"}) == "FVG gap"
    assert vc._cand_text({"snippet": "order <b>block</b>"}) == "order block"
    assert vc._cand_text({}) == ""


def test_rerank_reorders_by_relevance(monkeypatch):
    monkeypatch.setattr(vc, "_reranker", _FakeCE())
    cands = [
        {"title": "A", "timestamp": "1:00", "snippet": "weather sunny"},
        {"title": "B", "timestamp": "2:00", "snippet": "a fair value gap is a candle imbalance gap"},
        {"title": "C", "timestamp": "3:00", "snippet": "coffee"},
    ]
    ranked = vc.rerank("fair value gap", cands, top_k=2)
    assert ranked[0]["title"] == "B"          # most relevant first
    assert len(ranked) == 2                    # respects top_k
    assert all("rerank_score" in c for c in ranked)


def test_rerank_handles_text_shape_too(monkeypatch):
    monkeypatch.setattr(vc, "_reranker", _FakeCE())
    cands = [{"text": "liquidity sweep runs the stops"}, {"text": "unrelated"}]
    ranked = vc.rerank("liquidity", cands, top_k=1)
    assert "liquidity" in ranked[0]["text"]


def test_rerank_boosts_dual_hits(monkeypatch):
    monkeypatch.setattr(vc, "_reranker", _FakeCE())
    cands = [
        {"title": "single", "snippet": "fair value gap"},
        {"title": "dual", "snippet": "fair value gap", "dual_hit": True},
    ]
    ranked = vc.rerank("fair value gap", cands, top_k=2)
    assert ranked[0]["title"] == "dual"


def test_rerank_uses_more_than_512_chars(monkeypatch):
    fake = _CapturingCE()
    monkeypatch.setattr(vc, "_reranker", fake)
    long_text = "a" * 512 + " important tail " + ("b" * 1200)
    vc.rerank("tail", [{"snippet": long_text}, {"snippet": "short"}], top_k=2)
    assert len(fake.pairs[0][1]) == 1500
    assert "important tail" in fake.pairs[0][1]


def test_rerank_degrades_gracefully_without_model(monkeypatch):
    monkeypatch.setattr(vc, "_reranker", None)
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "sentence_transformers":
            raise ImportError("simulated missing model")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    out = vc.rerank("x", [{"snippet": "1"}, {"snippet": "2"}, {"snippet": "3"}], top_k=2)
    assert len(out) == 2                        # never worse than pre-rerank: still returns top_k


def test_rerank_fallback_orders_by_rrf(monkeypatch):
    monkeypatch.setattr(vc, "_reranker", None)
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "sentence_transformers":
            raise ImportError("simulated missing model")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    out = vc.rerank("x", [
        {"title": "low", "snippet": "1", "rrf_score": 0.01},
        {"title": "high", "snippet": "2", "rrf_score": 0.03},
    ], top_k=1)
    assert out[0]["title"] == "high"


def test_cache_hit_and_miss(monkeypatch):
    vc.clear_search_cache()
    monkeypatch.setattr(vc, "vault_hash", "test-vault")
    assert vc.get_cached_results("FVG", 2) is None

    results = [{"title": "Fair Value Gap", "snippet": "same result"}]
    vc.put_cached_results("FVG", 2, None, results)
    cached = vc.get_cached_results("fvg", 2)

    assert cached == results
    assert cached is not results


def test_mmr_diversifies_results():
    cands = [
        {"title": "A1", "video_id": "a", "snippet": "fair value gap imbalance entry", "final_score": 1.0},
        {"title": "A2", "video_id": "a", "snippet": "fair value gap imbalance entry", "final_score": 0.99},
        {"title": "B1", "video_id": "b", "snippet": "london session liquidity sweep", "final_score": 0.6},
    ]
    out = vc.apply_mmr(cands, top_k=2)
    assert [c["video_id"] for c in out] == ["a", "b"]


def test_min_score_threshold(monkeypatch):
    monkeypatch.setattr(vc, "_reranker", _ScoreCE([1.0, vc.MIN_RERANK_SCORE - 1]))
    ranked = vc.rerank("fair value gap", [
        {"title": "keep", "snippet": "fair value gap"},
        {"title": "drop", "snippet": "unrelated"},
    ], top_k=2)
    assert [r["title"] for r in ranked] == ["keep"]


def test_single_candidate_shortcircuits(monkeypatch):
    # <=1 candidate never needs the model at all
    monkeypatch.setattr(vc, "_reranker", None)
    assert vc.rerank("q", [{"snippet": "only one"}], top_k=5) == [{"snippet": "only one"}]


def test_rank_by_rrf_orders_by_score_and_trims():
    # Fast path (rerank=False) must order by fused RRF score and cut to top_k,
    # without touching the cross-encoder.
    cands = [
        {"title": "low", "rrf_score": 0.1},
        {"title": "high", "rrf_score": 0.9},
        {"title": "mid", "rrf_score": 0.5},
    ]
    out = vc.rank_by_rrf(cands, top_k=2)
    assert [c["title"] for c in out] == ["high", "mid"]


def test_cache_variant_prevents_cross_contamination():
    # A fast-path (kg/rerank off) result must never be served to a full-pipeline
    # caller for the same query/top_k, and vice versa.
    vc.clear_search_cache()
    fast = [{"title": "fast-result"}]
    full = [{"title": "full-result"}]
    vc.put_cached_results("FVG", 3, None, fast, variant="kg0rr0")
    vc.put_cached_results("FVG", 3, None, full, variant="kg1rr1")
    assert vc.get_cached_results("fvg", 3, variant="kg0rr0") == fast
    assert vc.get_cached_results("fvg", 3, variant="kg1rr1") == full
    # A variant with no stored entry is a clean miss, not a wrong-shape hit.
    assert vc.get_cached_results("fvg", 3, variant="kg1rr0") is None


def test_cache_default_variant_backward_compatible():
    vc.clear_search_cache()
    results = [{"title": "x"}]
    vc.put_cached_results("q", 2, None, results)          # no variant arg
    assert vc.get_cached_results("q", 2) == results        # still hits
