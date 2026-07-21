# PlugICT — Project Context

> Handoff document for Claude Code / Hermes Agent.  
> Last updated: 17 July 2026

---

## What is PlugICT?

A sellable, encrypted, AI-searchable knowledge vault of **581 ICT (Inner Circle Trader) YouTube videos** — transcribed, chunked into 14,757 semantic chunks, indexed with FTS5 + ChromaDB + Knowledge Graph, and sold as a local MCP server product.

Buyers get an encrypted vault + a unique license key. Their AI agent (Claude Desktop, Claude Code, Cursor, Codex CLI, Hermes) queries the vault locally via MCP tools.

---

## Architecture

```
Repo:  github.com/godzillacode0000/plugICT (main branch)
Prod:  D:\PlugICT\  (local production directory)
Code:  D:\plugict-repo\  (git repo working copy)
```

### Key files

| File | Purpose |
|---|---|
| `scripts/vault_core.py` | Vault open/decrypt, all retrieval logic (FTS5, Chroma, RRF, KG, citations) |
| `scripts/mcp_server.py` | MCP server — 10 tools: `search_ict`, `multi_search_ict`, `expand_result`, `explore_concept`, `glossary_lookup`, `list_playlists`, `vault_stats`, `vault_identity`, `build_research_bundle_plan`, `build_research_bundle` |
| `scripts/ict_ingest_v3.py` | V3 ingestion pipeline — transcripts → semantic chunks → FTS5 + ChromaDB |
| `scripts/build.py` | Encrypted vault builder (AES-256, streaming) |
| `scripts/generate_key.py` | Per-buyer envelope-encrypted license generator |
| `scripts/deliver.py` | Buyer package builder (verified snapshot, hash-gated) |
| `setup.py` | **Buyer-facing installer** — downloads latest vault from GitHub Releases (SHA-256 verified), writes license, installs deps, prints MCP config |
| `install.py` | Deprecated shim — forwards `--license` to `setup.py` so old purchase emails keep working |
| `release-manifest.json` | Frozen legacy file for pre-v3.3 `install.py` copies in the wild (they fetch it from `main` at runtime); not used by `setup.py` |
| `requirements.txt` | Buyer dependencies: `cryptography~=42.0`, `chromadb~=0.5.0`, `sentence-transformers~=3.0`, `mcp~=1.2` |
| `tests/` | 194 tests (pytest) |
| `store/issue_license.py` | Seller-side license issuance per purchase (manual or batch) |
| `docs/` | AI Agent Guide, FAQ, fulfillment email template |

### Vault data

| Metric | Value |
|---|---|
| Videos | 581 |
| Chunks | 14,757 (FTS5 = ChromaDB parity) |
| Vault size | 124,018,325 bytes |
| Vault SHA-256 | `0db15c1421a2da35b34b5fb5a355ad21e9c038e0035256098951b2bf3b723236` |
| Chunk schema | `chunk_id, chunk_index, title, video_id, playlist, start_ts, end_ts, start_seconds, end_seconds, source_file, content, entities, relations` |
| Schema version | `3` |
| KG entities | 29 |
| KG relations | 15 |

---

## Current retrieval flow (v3.2.0)

Every `search_ict()` call now runs **SQL-first** before falling back to hybrid:

```
Buyer question
    ↓
search_vault(query, top_k=15, kg=True, rerank=False)
    ↓
1. search_sql_first()
   ├── Entity detection (from entities table)
   ├── Shortform expansion (NWOG → new week opening gap)
   ├── Discriminative tokens (rare words ≤ 1000 corpus count)
   ├── SQL LIKE scan with AND combination
   ├── Adjacent context (before/after chunks auto-merged)
   ├── Coverage check: all facets present?
   │   ├── YES → return finalized results (~335ms)
   │   └── NO  → fallback
   └── Multi-facet: per-facet reservation + diversity guard (max 2/video)
    ↓
2. Hybrid fallback (when SQL weak)
   └── Cache check → FTS5 + ChromaDB + KG expansion → RRF fusion → dedup → hydrate → finalize
```

### Routing rules

| Query type | Route | Example |
|---|---|---|
| Exact ICT term | SQL-first | "Silver Bullet", "Unicorn model", "MSS vs CISD" |
| Multi-concept | SQL-first with facet pools | "Silver Bullet with NWOG" |
| Comparison | SQL-first both concepts | "MSS vs CISD", "FVG vs Order Block" |
| Vague / generic | Hybrid fallback | "how do I enter a trade", "when to take profit" |
| Troubleshooting + term | SQL-first anchored | "why Silver Bullet fail" |

### Benchmark (5 queries on V3)

| Metric | Hybrid | SQL-first |
|---|---|---|
| Avg latency | 373ms | **335ms** |
| Facet coverage | 70% | **100%** |
| Direct routes | — | **4/5** |
| Support coverage | 49% | **64%** |

---

## Buyer fulfillment workflow

### Seller does

```
1. Verify payment (DuitNow / FPX / USDT / Stripe)
2. Generate license:
   ICT_BUILD_DIR='D:/PlugICT' python store/issue_license.py buyer@email.com ORDER-123 --method duitnow
   → Outputs to store/issued/license_buyer_at_email_com.key
3. Attach license.key to email
4. Send using template in docs/fulfillment-email-template.md
   - Include: repo URL, install instructions, reminder not to share license.key
```

### Buyer does

```
1. Save license.key from email
2. Run: python setup.py  (inside a clone of the repo)
   OR paste to AI agent:
     "Install the ICT Knowledge Vault from godzillacode0000/plugICT
      My license.key is at C:\Users\...\Downloads\license.key
      Do not expose the license contents."
   (python install.py --license <path> still works — it forwards to setup.py)
3. Installer automatically:
   - Resolves the latest GitHub Release via the API
   - Downloads plugict.zip and verifies its SHA-256 asset digest
   - Writes license.key next to mcp_server.py
   - Pip installs requirements.txt
   - Runs mcp_server.py --doctor
   - Prints MCP config (Claude Desktop, Hermes, Cursor)
   - Done
```

### Key links

```
Repo:    https://github.com/godzillacode0000/plugICT
Release: https://github.com/godzillacode0000/plugICT/releases/tag/v3.2.0
Vault:   https://github.com/godzillacode0000/plugICT/releases/download/v3.2.0/ict-vault.kevin
```

### What NOT to do

- ❌ Never paste license.key contents into AI chat (contains decryption key)
- ❌ Never commit `.vault_key`, `.vault_sha256`, `store/issued/`, `store/issued_licenses.csv` 
- ❌ Never put `.vault_key` in buyer ZIP or public host
- ❌ Never auto-promote V3 to production without explicit approval
- ❌ Do not paste the raw vault contents in context (use `setup.py` as the approach)

---

## Production environment

```
Vault file:          D:\PlugICT\ict-vault.kevin (124MB, V3, SHA-256: 0db15c...b723236)
License file:        D:\PlugICT\license.key (owner license)
Vault key:           D:\PlugICT\.vault_key (32 bytes, secret — never commit/share)
Vault SHA-256 file:  D:\PlugICT\.vault_sha256 (seller-side hash reference)
Scripts:             D:\PlugICT\mcp_server.py, vault_core.py, metadata_enricher.py
                     (copied from D:\plugict-repo\scripts\, always keep in sync)
Owner generates:     ICT_BUILD_DIR='D:/PlugICT' python D:/plugict-repo/store/issue_license.py ...
```

### Environment variables

```
ICT_BUILD_DIR=D:/PlugICT        # For license generation (finds .vault_key / .vault_sha256)
TEMP=D:\tmp                     # Force temp dir to D: drive (C: has only 2GB free)
TMP=D:\tmp
```

---

## Development commands

```bash
# Run tests
python -m pytest                      # (from D:\plugict-repo)
python -m pytest tests/test_file.py   # Single file
python -m pytest -k "test_name"       # Single test

# Run doctor on production vault
python D:/PlugICT/mcp_server.py --doctor

# Generate a buyer license (seller side)
ICT_BUILD_DIR='D:/PlugICT' python store/issue_license.py "buyer@test.com" "TEST-001"

# Test vault opens
python -c "
import sys; sys.path.insert(0, r'D:\plugict-repo\scripts')
import vault_core as vc
db, _, who = vc.open_vault(vault_file=vc.Path('D:/PlugICT/ict-vault.kevin'), license_file=vc.Path('D:/PlugICT/license.key'))
n = db.execute('SELECT COUNT(*) FROM transcript_files').fetchone()[0]
c = db.execute('SELECT COUNT(*) FROM transcripts_fts').fetchone()[0]
print(f'{n} videos, {c} chunks, {who}')
"

# Test SQL-first search
python -c "
import sys; sys.path.insert(0, r'D:\plugict-repo\scripts')
import vault_core as vc
db, _, _ = vc.open_vault(vault_file=vc.Path('D:/PlugICT/ict-vault.kevin'), license_file=vc.Path('D:/PlugICT/license.key'))
r = vc.search_sql_first(db, 'Silver Bullet NWOG', top_k=3)
print('Direct SQL' if r else 'Hybrid fallback')
if r:
    for row in r: print(f\"  {row['title']} @ {row['timestamp']} | {row['video_url']}\")
"

# Full benchmark
python tests/run_benchmark.py --vault-dir D:/PlugICT --queries tests/benchmark_queries.json
```

---

## Git state

```text
Branch: main
Last commit: 6fa2b58 — feat: add requirements.txt at repo root for installer
Tags: v3.2.0 (current release), v1.0 (old release)
Commit history:
  main
  ├── 6fa2b58  requirements.txt at repo root
  ├── 24288bc  self-service installer + release packaging
  ├── 9e4d790  adaptive SQL-first retrieval as default buyer path
  ├── 1a26777  V3 semantic-chunks release candidate
  └── ... (earlier commits)
```

---

## Key principles

1. **Vault is source of truth** — answers grounded in transcript evidence, not model knowledge
2. **SQL-first is default** — exact ICT terms → direct SQL scan + adjacent context
3. **Hybrid is fallback** — vague questions still get FTS5 + ChromaDB + KG + RRF
4. **Manual fulfillment** — no auto-email, no webhook. Generate license per purchase, email manually
5. **License is the paywall** — vault is public on GitHub Release but useless without matching license.key
6. **Vault hash binds license to artifact** — changing vault requires new license
7. **No cross-encoder** — buyer's LLM handles relevance, RRF-only fusion
8. **KG + cache coupled** — KG must be enabled for cache to work (known design debt)
9. **No .vault_key in repo or buyer package**
10. **Seller keeps `D:\PlugICT\.vault_key` secret and never commits it**

---

## Recent changes (July 17)

| Change | What |
|---|---|
| V3 vault built & verified | 581 videos, 14,757 chunks, 124MB |
| V2 → V3 production | Copied V3 vault + updated `.vault_sha256` |
| SQL-first retrieval | `search_sql_first()` as default, hybrid fallback |
| `kg=True` on buyer path | Was `False` — enables entity expansion on fallback |
| `MIN_RERANK_SCORE = 0.0` | Was `-10.0` — now filters noise |
| GitHub Release v3.2.0 | Vault + SHA-256 + release-manifest.json |
| install.py + setup scripts | Self-service buyer installer |
| requirements.txt | At repo root for installer |
| 194 tests passing | All green |

---

## Known debt / future work

- [ ] Cache should be decoupled from KG (currently gated on `kg=True`)
- [ ] No `requirements.txt` in the old `D:\PlugICT` (installer uses repo's)
- [ ] Cross-encoder disabled (RRF-only, may be worth re-evaluating at buyer volume)
- [ ] Auto-email fulfillment deferred (manual is fine at low volume)
- [ ] Landing page needs V3 update (plugict.com)
- [ ] FAQ/docs may need V3 SQL-first updates
- [ ] Buyer ZIP in old v1.0 GitHub Release — stale but harmless
