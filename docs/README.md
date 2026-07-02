# ICT Knowledge Vault — Quick Start

## What You Got

A complete, AI-searchable library of **576 ICT (Inner Circle Trader) YouTube videos** — transcribed, indexed, and ready to query. **Hundreds of hours** of trading mentorship at your fingertips.

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

ICT Vault upgrades **your own AI agent**. Add the config from `examples/` to
your agent and restart it — it gains ICT tools (`search_ict`,
`explore_concept`, `glossary_lookup`, …) and can search all 576 videos,
answering with exact video timestamps.

- **Claude Desktop** → `examples/claude_desktop_config.json`
- **Cursor** → `examples/cursor_mcp.json`
- **Hermes Agent** → `examples/hermes_config.yaml`

Then just ask, in natural conversation:

> *"How does ICT teach the Silver Bullet entry in the New York session?"*

Your agent searches the vault and answers with cited sources and timestamps.
Full walkthrough: `docs/AI-AGENT-GUIDE.md`.

---

## What's Inside

| Component | What |
|---|---|
| 576 videos | 10 playlists, 2016-2026 |
| Hundreds of hours | Full transcriptions with timestamps |
| Tens of thousands of chunks | Split for precise search |
| Keyword search | Find exact terms instantly |
| Semantic search | Find concepts by meaning, not just words |
| Knowledge Graph | 17 ICT concepts with relationships |

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
