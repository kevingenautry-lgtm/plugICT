#!/usr/bin/env python3
"""Local preflight for release gates 1-2 (doctor + golden). Does not do pay path."""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault-dir", type=Path, default=Path.cwd())
    ap.add_argument("--cases", type=Path, default=None)
    ap.add_argument("--skip-golden", action="store_true")
    args = ap.parse_args()
    vault = args.vault_dir.resolve()
    py = sys.executable
    doctor = subprocess.run(
        [py, str(vault / "mcp_server.py"), "--doctor"],
        cwd=str(vault),
    )
    print("doctor_exit", doctor.returncode)
    if doctor.returncode != 0:
        return doctor.returncode
    if args.skip_golden:
        return 0
    cases = args.cases
    if cases is None:
        # repo layout or vault-local
        for cand in (
            vault / "benchmarks" / "golden",
            vault.parent / "benchmarks" / "golden",
            Path(__file__).resolve().parents[1] / "benchmarks" / "golden",
        ):
            if cand.is_dir():
                cases = cand
                break
    if cases is None:
        print("No golden cases found; skip", file=sys.stderr)
        return 0
    harness = Path(__file__).resolve().parent / "run_golden_eval.py"
    if not harness.is_file():
        harness = vault / "scripts" / "run_golden_eval.py"
    out = vault / "benchmarks" / "results" / "release-local.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [py, str(harness), "--vault-dir", str(vault), "--cases", str(cases), "--out", str(out)],
    )
    print("golden_exit", r.returncode)
    if out.is_file():
        data = json.loads(out.read_text(encoding="utf-8"))
        print("golden_summary", data.get("n_passed"), "/", data.get("n_cases"))
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
