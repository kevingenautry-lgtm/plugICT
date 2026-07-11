# Agent Layer golden cases

Full-vault evaluation cases for PlugICT Agent Layer (not the demo bench).

## Run (Windows example)

```bat
set TEMP=D:\tmp
set TMP=D:\tmp
set HF_HOME=D:\hf-cache
D:\PlugICT\.venv\Scripts\python.exe scripts\run_golden_eval.py --vault-dir D:\PlugICT --cases benchmarks\golden --out benchmarks\results\sb-golden-latest.json
```

Exit code 0 = all acceptance checks passed.

## Cases

| id | Purpose |
|---|---|
| `sb-001` | Silver Bullet multi-facet permanent golden |

Add more JSON files here over time (definitions, comparisons, unsupported).
