# ICT Knowledge Vault — Quick Start

## What You Got

A complete, AI-searchable library of **775 ICT (Inner Circle Trader) YouTube videos** — transcribed, indexed, and ready to query. **Hundreds of hours** of trading mentorship at your fingertips.

Not raw files. Not PDFs. **AI-searchable knowledge vault.**

---

## Setup (2 Minutes)

**Windows** — double-click `setup.bat`. It builds an isolated environment
(`.venv`) so it never disturbs any other Python on your machine, installs
everything, and verifies your vault.

**macOS / Linux**
```bash
./setup.sh
```

Something not working? Run a health check:
```bash
.venv\Scripts\python mcp_server.py --doctor      # Windows
.venv/bin/python mcp_server.py --doctor          # macOS / Linux
```

---

## Connect Your AI Agent

ICT Vault upgrades **your own AI agent**. Add the MCP config to your agent and
restart it. The best default tool is `multi_search_ict`; it returns cited,
capped snippets plus safe `result_ref` values for `expand_result` when more
context is needed. Legacy `search_ict`, `explore_concept`, `glossary_lookup`,
`list_playlists`, and `vault_stats` are also available.

- **Claude Desktop** → `examples/claude_desktop_config.json`
- **Cursor** → `examples/cursor_mcp.json`
- **Hermes Agent** → `examples/hermes_config.yaml`

Then just ask, in natural conversation:

> *"How does ICT teach the Silver Bullet entry in the New York session?"*

Your agent searches the vault and answers with cited sources and timestamps.
Full walkthrough: `docs/AI-AGENT-GUIDE.md`. Evidence rules for agents are in
`PLUGICT-AGENT-SKILL.md`; clients do not load that file automatically unless
your agent setup explicitly supports it.

---

## What's Inside

| Component | What |
|---|---|
| 775 videos | 10 playlists, 2016-2026 |
| Hundreds of hours | Full transcriptions with timestamps |
| 21,985 semantic chunks | Timestamp-preserving search units |
| Keyword search | Find exact terms instantly |
| Semantic search | Find concepts by meaning, not just words |
| Knowledge Graph | 29 ICT concepts with 15 relationships |

---

## System Requirements

| Component | Minimum |
|---|---|
| Python | 3.10+ |
| RAM | 4GB |
| Disk | 500MB free |
| OS | Windows 10+, macOS 12+, Linux |

---

## Files

| File | Purpose |
|---|---|
| `ict-vault.kevin` | Encrypted vault (don't share) |
| `license.key` | Your unique license (don't share) |
| `mcp_server.py` | AI agent bridge (the app) |
| `docs/` | Full documentation |

---

## License

This product is licensed to a single user. Sharing is traceable. Support future updates by respecting the license.
