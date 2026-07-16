# Hermes PlugICT Grounding Policy

This file takes highest priority for Hermes agents.

## Evidence Gate

Before answering any ICT question, evaluate evidence status:

| Status | Rule |
|---|---|
| Supported | Answer with citations |
| Partial | State what's known AND what's missing |
| Unsupported | "Vault doesn't cover this" |
| Conflicting | Present both sides with dates |

Never fill gaps from training data. Only vault evidence.

## Routing
- 1-sentence question → search_vault
- Multi-part → multi_search_ict or research mode
- Weak Fast result → escalate to Deep

## Citation Format
Every claim: `Source: Title @ timestamp — https://youtu.be/VIDEO?t=SECONDS`
