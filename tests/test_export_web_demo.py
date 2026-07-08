"""export_web_demo must export demo chunks faithfully and refuse paid vaults."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "store"))

import export_web_demo  # noqa: E402


def _make_db(path, demo=True):
    db = sqlite3.connect(path)
    db.execute("""CREATE VIRTUAL TABLE transcripts_fts USING fts5(
        title, video_id, playlist, start_ts, source_file, content,
        tokenize='porter unicode61')""")
    db.execute("INSERT INTO transcripts_fts VALUES (?,?,?,?,?,?)",
               ("2022 Mentorship — Ep 4", "abc123", "2022 Mentorship", "15:23",
                "ep4.md", "A fair value gap forms when price leaves imbalance."))
    db.execute("INSERT INTO transcripts_fts VALUES (?,?,?,?,?,?)",
               ("2022 Mentorship — Ep 31", "def456", "2022 Mentorship", "41:07",
                "ep31.md", "The silver bullet window is a specific hour of the day."))
    db.execute("CREATE TABLE vault_metadata (key TEXT, value TEXT)")
    if demo:
        db.executemany("INSERT INTO vault_metadata VALUES (?,?)",
                       [("demo", "1"), ("demo_count", "5"), ("demo_total", "576"),
                        ("demo_cta", "https://plugict.com/#pricing")])
    db.commit()
    return db


def test_exports_chunks_videos_and_shortforms(tmp_path):
    db = _make_db(tmp_path / "demo.db")
    out = tmp_path / "demo-index.json"
    export_web_demo.export_from_db(db, out)

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["demo"] == {"count": "5", "total": "576"}
    assert len(data["chunks"]) == 2
    assert data["chunks"][0]["ts"] == "15:23"
    assert data["chunks"][0]["v"] == "abc123"
    assert "fair value gap" in data["chunks"][0]["x"]
    assert len(data["videos"]) == 2
    assert "FVG" in data["shortforms"]  # acronym map ships for client-side expansion


def test_refuses_non_demo_vault(tmp_path):
    db = _make_db(tmp_path / "paid.db", demo=False)
    with pytest.raises(SystemExit) as e:
        export_web_demo.export_from_db(db, tmp_path / "out.json")
    assert "not a demo" in str(e.value)
