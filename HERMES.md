# PlugICT — Project Grounding

## Agent Behavior Policy

**Never answer ICT concepts from model memory alone.** Always use PlugICT tools first. Model memory is not a substitute for vault evidence.

### Evidence Labeling (Answerability Gate)

After every vault search, tag evidence quality before composing an answer:

| Label | Meaning |
|-------|---------|
| **Supported** | Vault returned clear, on-topic results covering the facet |
| **Partial** | Results exist but are weak, tangentially related, or snippet-truncated |
| **Unsupported** | No relevant result was returned despite multiple query variants |
| **Conflicting** | Results from different sessions/years contradict each other |

- Only answer **confidently** when all core facets are **Supported**.
- For **Partial** facets, qualify: "The vault suggests… but this may be incomplete."
- For **Unsupported** or unresolvable **Conflicting** facets, say so directly.

### Auto-Escalation Policy

```
1. search_ict (fast) → evidence labelled
2. If any core facet is Partial: run multi_search_ict (deep) for that facet
3. If evidence remains Partial or Unsupported: admit uncertainty
```

- At most **one** automatic escalation per user turn.
- Do not fabricate results to fill gaps.

### Formatted Answer Structure

```
## Vault evidence
- Claim … (Title @ timestamp — URL)  ←  labelled Supported

## Gap
- Vault did not clearly establish: …  ←  labelled Unsupported / Partial

## Synthesis (if applicable)
- Practical takeaway — labelled as synthesis, not raw ICT quote
```

### Vault Tool Routing

| Question type | Tool | Escalation |
|---|---|---|
| Simple concept / term | `search_ict` | → `multi_search_ict` if weak |
| Multi-facet / comparison | `multi_search_ict` | → targeted follow-up for missing facets |
| Truncated snippet | `expand_result` (one shot only) | — |

Refer to **`PLUGICT-AGENT-SKILL.md`** for full facet-planning, coverage-check, and anti-pattern rules.

## Development Rules

- Run tests before pushing: `python -m pytest tests/ -q`
- All PRs merged to `main` via squash.
- Landing page = single `index.html` at repo root — inline CSS/JS only.
- Secrets (`VAULT_KEY`, `BUILD_KEY`, license keys) never committed.
- Render deployment reads `.vault_key` from Secret Files.

## Quick Reference

- `store/` — payment, license issuance, webhook server
- `scripts/` — ingestion, MCP server, vault build
- `tests/` — pytest suite (run all before push)
- `landing/` → redirects to `index.html` (single source of truth)
