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


def test_single_candidate_shortcircuits(monkeypatch):
    # <=1 candidate never needs the model at all
    monkeypatch.setattr(vc, "_reranker", None)
    assert vc.rerank("q", [{"snippet": "only one"}], top_k=5) == [{"snippet": "only one"}]
