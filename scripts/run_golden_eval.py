#!/usr/bin/env python3
"""Run Agent Layer golden cases against a live PlugICT vault (full product path).

Usage (from vault folder or with --vault-dir):
  set TEMP=D:\\tmp
  set HF_HOME=D:\\hf-cache
  .venv\\Scripts\\python path\\to\\run_golden_eval.py --vault-dir D:\\PlugICT \\
      --cases path\\to\\benchmarks\\golden --out results.json

Exit 0 if all required acceptance checks pass; 1 otherwise.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path


def _load_mcp(vault_dir: Path):
    mcp_path = vault_dir / "mcp_server.py"
    if not mcp_path.is_file():
        raise FileNotFoundError(f"mcp_server.py not found in {vault_dir}")
    # Prefer vault dir on path so vault_core imports resolve
    sys.path.insert(0, str(vault_dir))
    # Force temp onto TEMP if set (Windows vault extract)
    for key in ("TEMP", "TMP", "TMPDIR"):
        if os.environ.get(key):
            tempfile.tempdir = os.environ[key]
            break
    spec = importlib.util.spec_from_file_location("plugict_mcp_golden", str(mcp_path))
    mod = importlib.util.module_from_spec(spec)
    with redirect_stdout(sys.stderr):
        spec.loader.exec_module(mod)
    return mod


def _facet_hit(facet_spec: dict, results: list) -> bool:
    matchers = facet_spec.get("match_any") or []
    if not matchers and facet_spec.get("accept_video_ids"):
        matchers = [{"video_id": v} for v in facet_spec["accept_video_ids"]]
    for r in results:
        vid = (r.get("video_id") or "").strip()
        snip = r.get("snippet") or ""
        title = r.get("title") or ""
        blob = f"{title}\n{snip}"
        for m in matchers:
            if m.get("video_id") and vid == m["video_id"]:
                return True
            rx = m.get("snippet_regex")
            if rx and re.search(rx, blob):
                return True
    return False


def _eval_case(mod, case: dict) -> dict:
    t0 = time.perf_counter()
    payload = mod.multi_search_vault(
        case["question"],
        case.get("queries") or [case["question"]],
        top_k=int(case.get("top_k") or 5),
        playlist=case.get("playlist"),
    )
    latency_ms = (time.perf_counter() - t0) * 1000.0
    results = payload.get("results") or []
    vids = [(r.get("video_id") or "").strip() for r in results]
    vid_counts = Counter(v for v in vids if v)
    unique_videos = len(vid_counts)
    k = max(len(results), 1)
    dup_video_rate = 1.0 - (unique_videos / k) if results else 0.0
    # fraction of result slots that are "extra" copies of a video beyond first
    extras = sum(max(0, c - 1) for c in vid_counts.values())
    dup_slot_rate = extras / k if results else 0.0
    max_per_video = max(vid_counts.values()) if vid_counts else 0
    ts_ok = sum(1 for r in results if (r.get("timestamp") or r.get("start_ts")))
    ts_presence = ts_ok / k if results else 0.0

    facets = case.get("facets") or {}
    facet_status = {}
    req_total = req_hit = 0
    for name, spec in facets.items():
        hit = _facet_hit(spec, results)
        required = bool(spec.get("required"))
        status = "covered" if hit else "missing"
        facet_status[name] = {"required": required, "status": status}
        if required:
            req_total += 1
            if hit:
                req_hit += 1
    required_facet_coverage = (req_hit / req_total) if req_total else 1.0

    acc = case.get("acceptance") or {}
    checks = {}
    if "min_unique_videos" in acc:
        checks["unique_videos"] = unique_videos >= int(acc["min_unique_videos"])
    if "max_per_video" in acc:
        checks["max_per_video"] = max_per_video <= int(acc["max_per_video"])
    if acc.get("require_core_video_id"):
        core = acc["require_core_video_id"]
        checks["core_video"] = core in vid_counts
    if "min_required_facet_coverage" in acc:
        checks["required_facet_coverage"] = (
            required_facet_coverage + 1e-9 >= float(acc["min_required_facet_coverage"])
        )
    if "min_timestamp_presence" in acc:
        checks["timestamp_presence"] = (
            ts_presence + 1e-9 >= float(acc["min_timestamp_presence"])
        )

    passed = all(checks.values()) if checks else True
    return {
        "id": case.get("id"),
        "passed": passed,
        "latency_ms": round(latency_ms, 1),
        "n_results": len(results),
        "video_ids": vids,
        "video_counts": dict(vid_counts),
        "unique_videos": unique_videos,
        "max_per_video": max_per_video,
        "dup_slot_rate": round(dup_slot_rate, 4),
        "timestamp_presence": round(ts_presence, 4),
        "required_facet_coverage": round(required_facet_coverage, 4),
        "facets": facet_status,
        "diversity_meta": payload.get("diversity"),
        "work_units": payload.get("work_units"),
        "checks": checks,
        "results_brief": [
            {
                "video_id": r.get("video_id"),
                "timestamp": r.get("timestamp") or r.get("start_ts"),
                "title": (r.get("title") or "")[:80],
                "sources": r.get("retrieval_sources"),
            }
            for r in results
        ],
    }


def main():
    ap = argparse.ArgumentParser(description="PlugICT Agent Layer golden eval")
    ap.add_argument("--vault-dir", type=Path, default=Path.cwd(),
                    help="Folder with mcp_server.py + vault + license")
    ap.add_argument("--cases", type=Path, required=True,
                    help="Golden JSON file or directory of *.json")
    ap.add_argument("--out", type=Path, default=None, help="Write full report JSON")
    ap.add_argument("--case-id", default=None, help="Run only this case id")
    args = ap.parse_args()

    vault_dir = args.vault_dir.resolve()
    cases_path = args.cases.resolve()
    if cases_path.is_dir():
        files = sorted(cases_path.glob("*.json"))
    else:
        files = [cases_path]
    cases = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, list):
            cases.extend(data)
        else:
            cases.append(data)
    if args.case_id:
        cases = [c for c in cases if c.get("id") == args.case_id]
    if not cases:
        print("No cases to run", file=sys.stderr)
        return 2

    print(f"Loading vault from {vault_dir} …", file=sys.stderr)
    mod = _load_mcp(vault_dir)
    with redirect_stdout(sys.stderr):
        mod.ensure_vault()

    reports = []
    for case in cases:
        print(f"Running {case.get('id')} …", file=sys.stderr)
        rep = _eval_case(mod, case)
        reports.append(rep)
        status = "PASS" if rep["passed"] else "FAIL"
        print(
            f"  {status} unique_videos={rep['unique_videos']} "
            f"facet_cov={rep['required_facet_coverage']} "
            f"max_per_video={rep['max_per_video']} "
            f"latency_ms={rep['latency_ms']}",
            file=sys.stderr,
        )
        for name, fs in (rep.get("facets") or {}).items():
            print(f"    facet {name}: {fs['status']}"
                  f"{' (required)' if fs['required'] else ''}", file=sys.stderr)

    summary = {
        "vault_dir": str(vault_dir),
        "n_cases": len(reports),
        "n_passed": sum(1 for r in reports if r["passed"]),
        "n_failed": sum(1 for r in reports if not r["passed"]),
        "cases": reports,
        "note": "Demo benchmark-demo-agent-layer.json is demo-only; use this harness on full vault.",
    }
    text = json.dumps(summary, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)

    return 0 if summary["n_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
