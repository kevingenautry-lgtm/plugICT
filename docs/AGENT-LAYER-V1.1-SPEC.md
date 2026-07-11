# Agent Layer v1.1 — Implementation Spec

**Status:** Ready to build  
**Date:** 2026-07-11  
**Source:** Product Improvement Assessment + Sanji critique + Silver Bullet A/B test  
**Also aligned with:** ChatGPT *Agent Layer Review — Revised After Hermes Feedback* (2026-07-11)  
**Vault under test:** `D:\PlugICT` (full vault, owner license)

---

## 0. Verdict (locked)

| Decision | Action |
|---|---|
| Core architecture | **Keep** — do not rebuild Agent Layer |
| Embedding model | **Stay BGE-small 384-dim** — no BGE-large until post-bench |
| Landing redesign | **Freeze** (bugfix / price / CTA only) |
| Main problem | **Coverage + source diversity + validation**, not architecture |
| Golden case | Silver Bullet multi-facet question is permanent |

```text
Buyer question
→ multi_search_ict (variants map to facets)
→ keyword + semantic + KG
→ fusion / rerank
→ video diversity + timestamp merge
→ opaque result_ref
→ expand_result (on demand)
→ external AI synthesis (skill rules)
```

---

## 1. Goals / non-goals

### Goals
1. Multi-facet questions get **broader video coverage** in one or two tool rounds.
2. Buyer (and skill) can see **what was covered / missing**.
3. Same video does not fill 3/5 slots with near-duplicate chunks.
4. Skill + tool description force **facet-aware planning** even if skill file not loaded.
5. Measurable full-vault eval (not demo-only 46.8% vanity).
6. Clean-machine + pay→first-search still release gates.

### Non-goals
- Vault re-encode / BGE-large rebuild
- Putting an LLM inside `mcp_server.py` for facet extraction
- Unlimited top_k (anti-extraction + rate limit)
- Landing redesign
- Changing license/encryption model

---

## 2. Split of responsibility

| Layer | Owns | Does not own |
|---|---|---|
| **Client agent + PLUGICT-AGENT-SKILL** | Facet list, variant planning, 1 follow-up search, answer synthesis, evidence vs interpretation | Ranking math |
| **Vault MCP server** | Retrieval, RRF/rerank, **video diversity**, merge adjacent ts, caps, rate limit, optional coverage **if facets passed**, result_ref | Inventing ICT claims |
| **Human / CI** | Golden benchmarks, clean install, payment path | — |

---

## 3. PR sequence (do not mega-PR)

Aligned with revised ChatGPT roadmap (skill before research_mode):

| PR | Name | Scope | Risk |
|---|---|---|---|
| **1.1a** | Source diversity | Post-rerank diversify by `video_id`; merge adjacent timestamps; max 2 chunks/video default | Low |
| **1.1b** | Skill + tool copy | Facet variants, one follow-up, independent-source rule, evidence vs synthesis | Low |
| **1.1c** | Golden SB + harness | Facet-tagged SB case + metrics on **full** vault (`D:\PlugICT`) | Low |
| **1.1d** | Research mode | Optional higher top_k + debug coverage — **only after 1.1a measured** | Medium |
| **1.1e** | Release ops | Clean Win10/11, doctor, MCP client, pay→first answer, low-disk | Process |

Ship **1.1a → 1.1b → 1.1c** before marketing claims. **1.1d** only after diversity metrics.

---

## 4. PR 1.1a — Source diversity (spec)

### 4.1 Algorithm (after normal rank, before finalize)

```text
1. Input: ranked candidates (scored), target_k (default 5)
2. Group by video_id
3. Within each video, merge candidates whose time ranges overlap or are within MERGE_GAP_SEC (e.g. 90s)
   - Keep best score; union matched_queries / retrieval_sources
   - Snippet = highest-score chunk (expand_result still available)
4. Greedy fill final list:
   - Sort groups by best score
   - Take best remaining chunk from a video if video_count[video] < MAX_PER_VIDEO (default 2)
   - Prefer a second chunk from same video only if facet_tag differs OR time gap > DISTINCT_GAP_SEC (e.g. 10 min)
5. If final list < target_k, fill from leftover by score (still respect MAX_PER_VIDEO)
6. Issue result_ref only on final list
```

### 4.2 Defaults

| Param | Default | Notes |
|---|---|---|
| `top_k` | 5 | Unchanged hard cap unless research_mode |
| `MAX_PER_VIDEO` | 2 | Was effectively unbounded after dedup-by-chunk |
| `MERGE_GAP_SEC` | 90 | Adjacent SB chunks in same lecture |
| `DISTINCT_GAP_SEC` | 600 | Second slot same video only if far apart |

### 4.3 API

No breaking change. Internal post-process in `collect_multi_search_candidates` or `finalize_ranked_results`.

Optional response field (always safe):

```json
"diversity": {
  "unique_videos": 4,
  "max_per_video": 2,
  "merged_chunks": 2
}
```

### 4.4 Acceptance (SB regression)

Question (fixed golden):

```text
What is the ICT Silver Bullet strategy and how do you trade it?
Include time windows (NY), entry (FVG), targets, and key rules.
```

Variants (fixed):

```text
1. ICT Silver Bullet time based trading model
2. Silver Bullet 10am to 11am New York
3. Silver Bullet fair value gap entry
4. Silver Bullet rules handles pips killzone
```

**Pass if:**

- `unique_videos@5` ≥ 3 (today often 2–3 with triple core video)
- No more than 2 results share the same `video_id`
- Core video `tRq1hyGGtl4` still appears ≥ 1
- May 23 criteria and/or Jun 15 workshop still eligible under diversity (not hard-required every run, but measured)

---

## 5. PR 1.1d — Research mode (spec)

> Numbering: research mode ships **after** skill + golden (see §3). Kept detailed here; implement as **1.1d**.

### 5.1 Tool params (additive)

```json
{
  "research_mode": false,
  "top_k": 5
}
```

| Mode | top_k clamp | MAX_PER_VIDEO | work_units |
|---|---|---|---|
| default | 1–5 | 2 | existing formula |
| `research_mode=true` | 1–10 | 2 (or 3) | scale with k + variants |

### 5.2 Caps (anti-extraction)

- Never return full transcripts
- expand_result still one-shot per ref / existing expiry rules
- Rate limit must increase work_units when research_mode

### 5.3 Optional facets input (no server LLM)

```json
{
  "question": "...",
  "queries": ["...", "..."],
  "facets": ["definition", "time_windows", "entry", "targets", "rules", "forex"]
}
```

If `facets` provided and `queries` aligned (same length or query carries facet prefix):

Server may return:

```json
{
  "coverage": {
    "definition": "covered",
    "time_windows": "partial",
    "entry": "covered",
    "targets": "missing",
    "rules": "covered",
    "forex": "missing"
  },
  "suggested_followup_queries": [
    "Silver Bullet 2pm 3pm",
    "Silver Bullet Forex killzone",
    "Silver Bullet 5 handles 10 pips"
  ]
}
```

**Rules:**

- `covered` = ≥1 final result tagged or matched to facet  
- `missing` = none  
- `partial` = only weak/single weak match (optional heuristic; else binary)  
- `not_in_vault` only if explicit unsupported list later  
- **Never** inject low-score garbage just to mark covered  

Default buyer path: omit coverage unless facets passed or `debug=true`.

---

## 6. PR 1.1b — Skill + tool description

### 6.1 `multi_search_ict` description (add)

```text
For complex questions, map buyer asks to FACETS (definition, times, entry,
targets, rules, market/session variants). Send 1-4 query variants that cover
DIFFERENT facets—not 4 synonyms of the same phrase.

After results: check each facet has evidence. If a requested facet is missing,
call multi_search_ict ONCE more with targeted variants only for missing facets.
Prefer diverse video_ids; multiple hits from one video are not independent confirmation.
Use expand_result only when snippet lacks needed context.
```

### 6.2 PLUGICT-AGENT-SKILL.md additions

**Before search**

- List explicit components the buyer asked for  
- Variants cover components, not synonyms only  
- Prefer playlist filters only when buyer names a series  

**After search**

- Facet checklist mentally or in notes  
- One follow-up multi_search max per user turn for missing facets  
- Prefer diversity across videos and teaching dates  
- Do not treat repeated same-video chunks as broader confirmation  

**When answering**

- Separate **Vault evidence** vs **Practical synthesis**  
- Every major factual claim → title + timestamp (+ link if available)  
- Optional ICT comments ≠ mandatory rules  
- State clearly when vault did not establish something  

### 6.3 Acceptance

- Skill file present in product zip / repo `PLUGICT-AGENT-SKILL.md`  
- Tool description in mcp_server list_tools updated  
- No vault rebuild required  

---

## 7. PR 1.1c — Full-vault evaluation (+ SB golden)

### 7.0 Demo bench caveat (revised review)

Existing demo Agent Layer ~**46.8% recall@5** and identical single/multi scores are a **hypothesis signal**, not proven architectural failure.

Before redesigning around that number:

1. Confirm variants truly differ  
2. Confirm multi_search receives them  
3. Confirm expected docs match **rebuilt full vault**  
4. Confirm cache not masking differences  
5. Run multiple times on production vault  

Label demo bench as **demo only**.

### 7.1 Retire demo bench as “production proof”

`benchmark-demo-agent-layer.json` → label **demo only**.  
Do not quote 46.8% as product quality.

### 7.2 Golden set targets (~120)

| Bucket | N |
|---|---|
| Simple definitions | 30 |
| Multi-facet strategy (incl. **Silver Bullet**) | 30 |
| Comparisons | 20 |
| Timing / session | 20 |
| Unsupported / ambiguous | 20 |

Each multi-facet case includes:

```json
{
  "id": "sb-001",
  "question": "...",
  "queries": ["..."],
  "facets": {
    "definition": {"required": true, "accept_video_ids": ["tRq1hyGGtl4"]},
    "time_am": {"required": true, "accept_any": true},
    "entry_fvg": {"required": true},
    "targets": {"required": false},
    "rules_invalidation": {"required": true},
    "forex_or_pm": {"required": false}
  }
}
```

### 7.3 Metrics

**Retrieval**

- Recall@1, Recall@5 (where labeled docs exist)  
- **facet_coverage** (required facets hit / required facets)  
- **unique_videos@k**  
- **dup_video_rate** (results sharing video_id / k)  
- Timestamp presence rate  
- Unsupported accuracy (should not fabricate)  
- Latency p50/p95, peak RAM  

**Answer-level (manual or LLM-judge later)**

- Claim accuracy  
- Citation support  
- Requested-facet completeness  
- Evidence/interpretation separation  
- Hallucination rate  

### 7.4 Silver Bullet permanent case

Use fixed question + 4 variants from §4.4.  
Track before/after 1.1a diversity metrics.

### 7.5 Run environment

- Full vault on **D:\PlugICT** (or clean unzip + license)  
- TEMP/HF on D:  
- Record: git commit of vault_core/mcp_server, model revs  

---

## 8. PR 1.1e — Release gates (checklist)

| # | Gate | Pass criteria |
|---|---|---|
| 1 | Full vault build current | doctor green on buyer-shaped package |
| 2 | Full-vault benchmark | Report committed; SB golden pass thresholds TBD after baseline |
| 3 | Clean Windows machine | Fresh venv, setup, doctor, MCP connect |
| 4 | Pay → first search | Landing → pay → license → install → Claude/Codex → SB question → cited answer |

**Buyer path script (manual):**

```text
Visit landing
→ pay
→ download
→ install on normal Windows
→ connect Claude Desktop or Codex
→ ask Silver Bullet golden question
→ receive answer with title + timestamp citations
```

Log every confusing step / workaround.

---

## 9. Explicit non-actions

| Idea | Decision |
|---|---|
| BGE-large rebuild (~long) | **No** until diversity+facet bench baseline |
| Server-side LLM facet extractor | **No** |
| Hard guarantee “every facet has a chunk” | **No** — coverage + retry only |
| top_k unlimited | **No** |
| Landing redesign | **Frozen** |

---

## 10. Implementation map (files)

| Area | Likely files |
|---|---|
| Diversity | `vault_core.py` (`finalize_ranked_results` / new `diversify_by_video`) |
| multi_search wiring | `mcp_server.py` (`multi_search_vault`, tool schema) |
| Skill | `PLUGICT-AGENT-SKILL.md` |
| Eval | `tests/` or `benchmarks/agent_layer_v1_1/` |
| Docs | this file; `docs/AI-AGENT-GUIDE.md` short note |

---

## 11. Definition of done (v1.1)

- [ ] 1.1a merged: max 2/video default; SB unique_videos@5 improves vs baseline  
- [ ] 1.1b skill + tool description live in package  
- [ ] 1.1c SB golden case runnable on full vault with metrics JSON  
- [ ] Baseline metrics recorded **before** research_mode / embedding experiments  
- [ ] Landing freeze held (only CTA/price/pay/a11y bugs)  
- [ ] Clean-machine path documented with pass/fail  
- [ ] Snippet vs retrieval: skill uses expand_result when facet “missing” may be context-outside-snippet  

---

## 12. First code task (start here)

**Implement only 1.1a** `diversify_by_video` + unit tests with synthetic candidates (3 chunks same video_id different ts → ≤2 kept; third slot other video).

Then re-run SB golden on `D:\PlugICT` and attach metrics to PR.

---

*Educational product engineering spec. Not financial advice.*
