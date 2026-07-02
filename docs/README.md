# ICT Knowledge Vault — Quick Start

## What You Got

A complete, AI-searchable library of **576 ICT (Inner Circle Trader) YouTube videos** — transcribed, indexed, and ready to query. **Hundreds of hours** of trading mentorship at your fingertips.

Not raw files. Not PDFs. **AI-searchable knowledge vault.**

---

## Setup (2 Minutes)

**Windows** — double-click `setup.bat`. It builds an isolated environment
(`.venv`) so it never disturbs any other Python on your machine, installs
everything, and runs a health check.

**macOS / Linux**
```bash
./setup.sh
```

### Test It
```bash
vault.bat "Fair Value Gap definition"     # Windows
./vault.sh "Fair Value Gap definition"    # macOS / Linux
```

You should see results with timestamps, playlists, and sources.
Something not working? Run `vault.bat --doctor` for a one-line-per-check report.

---

## Two Ways to Use

### A. Command Line Search
```bash
vault.bat "Silver Bullet London session"
vault.bat "Order Block vs Breaker"
vault.bat "how to trade FOMC"
vault.bat                       # interactive mode: unlock once, ask many
vault.bat --explain "FVG"       # show why each result matched
```

### B. Connect Your AI Agent
```bash
.venv\Scripts\python mcp_server.py
```

Then add the config from `examples/` to your AI agent:
- **Claude Desktop** → `examples/claude_desktop_config.json`
- **Cursor** → `examples/cursor_mcp.json`
- **Hermes Agent** → `examples/hermes_config.yaml`

Your AI agent can now query all 576 videos directly. See `docs/AI-AGENT-GUIDE.md`.

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
| `query.py` | CLI search tool |
| `mcp_server.py` | AI agent bridge |
| `docs/` | Full documentation |

---

## License

This product is licensed to a single user. Sharing is traceable. Support future updates by respecting the license.
