"""Unit tests for Agent Layer v1.1a diversify_by_video (no vault needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_core as vc


def _c(vid, ts, score, title=None, text=None):
    return {
        'video_id': vid,
        'start_ts': ts,
        'timestamp': ts,
        'title': title or f'{vid}@{ts}',
        'final_score': score,
        'rrf_score': score,
        '_full_text': text or f'body {vid} {ts}',
        'matched_queries': [f'q-{ts}'],
        'retrieval_sources': ['keyword'],
    }


def test_caps_same_video_at_two():
    cands = [
        _c('AAA', '10:00', 10),
        _c('AAA', '20:00', 9),   # 10 min later — allowed as 2nd
        _c('AAA', '30:00', 8),   # should drop (max 2)
        _c('BBB', '1:00', 7),
        _c('CCC', '2:00', 6),
    ]
    out, meta = vc.diversify_by_video(cands, top_k=5)
    aaa = [c for c in out if c['video_id'] == 'AAA']
    assert len(aaa) <= 2, aaa
    assert meta['max_per_video'] == 2
    vids = {c['video_id'] for c in out}
    assert 'BBB' in vids or 'CCC' in vids


def test_merges_adjacent_timestamps():
    cands = [
        _c('AAA', '10:00', 10, text='first chunk longer ' * 20),
        _c('AAA', '10:30', 9, text='near'),  # 30s later → merge
        _c('BBB', '5:00', 8),
    ]
    out, meta = vc.diversify_by_video(cands, top_k=5, merge_gap_sec=90)
    assert meta['merged_chunks'] >= 1
    aaa = [c for c in out if c['video_id'] == 'AAA']
    assert len(aaa) == 1


def test_prefers_other_videos_for_slots():
    cands = [
        _c('CORE', '11:20', 10),
        _c('CORE', '12:21', 9.5),  # ~1 min — should not take 2nd (distinct gap 600)
        _c('CORE', '0:00', 9.0),
        _c('WS', '288:16', 8.5),
        _c('MAY', '54:48', 8.0),
        _c('EVO', '4:47', 7.5),
    ]
    out, meta = vc.diversify_by_video(cands, top_k=5)
    assert meta['unique_videos'] >= 3
    core_n = sum(1 for c in out if c['video_id'] == 'CORE')
    assert core_n <= 2
    assert len(out) == 5 or len(out) >= 4


if __name__ == '__main__':
    test_caps_same_video_at_two()
    test_merges_adjacent_timestamps()
    test_prefers_other_videos_for_slots()
    print('ALL diversify_by_video tests passed')
