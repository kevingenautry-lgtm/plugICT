# PlugICT Release Gates (Agent Layer v1.1e)

Operational checklist before calling a buyer-facing release "done".
Not a substitute for code review.

## Gate 1 — Vault package health

On a machine with the product folder:

```bat
set TEMP=D:\tmp
set TMP=D:\tmp
set HF_HOME=D:\hf-cache
.venv\Scripts\python mcp_server.py --doctor
```

| Check | Pass |
|---|---|
| License loads | licensed to buyer/owner email |
| Vault decrypts | video count > 0 |
| Reranker / embeddings | ready or documented fallback |
| Exit | doctor exits 0 / prints All good |

## Gate 2 — Golden retrieval (full vault)

```bat
.venv\Scripts\python scripts\run_golden_eval.py --vault-dir . --cases benchmarks\golden --out benchmarks\results\release-sb.json
```

| Check | Pass |
|---|---|
| `sb-001` | `passed: true` |
| unique_videos | ≥ 3 |
| max_per_video | ≤ 2 |
| required_facet_coverage | 1.0 |

Demo file `benchmark-demo-agent-layer.json` is **DEMO_ONLY** — do not quote as production quality.

## Gate 3 — Clean Windows install

On a **fresh** Windows 10 or 11 profile (or VM):

1. Download release zip (not owner dev folder).
2. Unzip to a path without special permission issues (e.g. `D:\PlugICT`).
3. Create venv, install requirements / run setup as documented.
4. Place `license.key` from purchase email.
5. Run `--doctor`.
6. Configure **one** MCP client (Claude Desktop **or** Codex CLI).

| Check | Pass |
|---|---|
| No manual code edits required | ☐ |
| Doctor green | ☐ |
| MCP tools listed (`multi_search_ict`, `expand_result`) | ☐ |

## Gate 4 — Pay → first search

| Step | Pass |
|---|---|
| Landing loads, CTA works | ☐ |
| Payment completes (or sandbox) | ☐ |
| License / download delivered | ☐ |
| Buyer opens Claude/Codex + PlugICT | ☐ |
| Asks Silver Bullet golden question | ☐ |
| Answer includes **title + timestamp** citation | ☐ |
| Time-to-first-answer recorded (minutes) | ☐ |

### Golden buyer prompt

```text
Use multi_search_ict. What is the ICT Silver Bullet strategy and how do you trade it?
Include time windows (NY), entry (FVG), targets, and key rules. Cite title + timestamp.
```

## Gate 5 — Low disk / temp

| Check | Pass |
|---|---|
| Document that vault extract needs free disk (prefer TEMP on large drive) | ☐ |
| Failure message understandable if disk full | ☐ |

## Gate 6 — Landing freeze exceptions only

Allowed without full redesign freeze lift:

- Broken CTA / payment / price bugs
- Mobile usability blockers
- Accessibility / serious performance

Paused: new visual identity, hero experiments, animation thrash.

## Sign-off

| Role | Name | Date | Notes |
|---|---|---|---|
| Builder | | | |
| Owner | | | |

**Release version / git SHA:** _______________  
**Vault build id / hash:** _______________  
