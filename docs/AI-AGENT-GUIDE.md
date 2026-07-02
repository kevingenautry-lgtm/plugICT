---
title: ICT Knowledge Vault — AI Agent Integration Guide
created: 2026-06-30
updated: 2026-06-30
type: concept
profile: sanji
tags: [ict, ai, guide, integration, mcp, claude, cursor, hermes, chatgpt]
confidence: high
---

# AI Agent Integration Guide

> How to connect the ICT Knowledge Vault to your AI agent.
> All methods. Zero API cost for the vault. Use your own AI agent.

---

## 0. FREE & CHEAP AI Models (No $20/month needed!)

You DON'T need ChatGPT Pro or Claude Pro. Here are free and cheap options:

### Completely FREE

| Model | Provider | Daily Limit | How to Get API Key |
|---|---|---|---|
| **Gemini 2.0 Flash** | Google | 1,500 requests/day | [aistudio.google.com](https://aistudio.google.com) → Get API Key |
| **Gemini 2.5 Flash** | Google | 100 requests/day | Same — Google AI Studio |
| **Llama 3 70B** | Groq | Rate limited | [console.groq.com](https://console.groq.com) |
| **Mistral** | Mistral | Free tier | [console.mistral.ai](https://console.mistral.ai) |

### Very Cheap (cents per month)

| Model | Cost per 1M tokens | ~Monthly cost |
|---|---|---|
| **DeepSeek V3** | $0.27 | $1-3 |
| **GPT-4o-mini** | $0.15 | $0.50-2 |
| **Claude Haiku** | $0.25 | $1-3 |
| **Gemini 1.5 Flash** | $0.075 | $0.30-1 |

### How to use free/cheap models with this vault

**Best for beginners: Hermes Agent + Gemini 2.0 Flash (both FREE)**
1. Install [Hermes Agent](https://hermes-agent.nousresearch.com) — free, open source
2. Get free Gemini API key from [Google AI Studio](https://aistudio.google.com)
3. Run: `hermes config set model.default "google/gemini-2.0-flash"`
4. Add ICT vault MCP config
5. Total cost: **$0**

Once the MCP config is added, just talk to your agent normally — ask *"What
does ICT say about the Silver Bullet?"* and it calls the vault's `search_ict`
tool and answers with cited timestamps. No copy-pasting.

> 💡 **Start free, upgrade later.** Gemini 2.0 Flash via Hermes = 1,500 free
> queries/day. Most users won't exceed this.

---

## Quick Start — Verify Before Connecting

Before wiring up your AI agent, confirm the vault is healthy:

```bash
# Windows
setup.bat

# Or manual
pip install -r requirements.txt

# Verify vault + environment
python mcp_server.py --doctor
```

Output:
```
ICT Vault — environment check

  ✓ Python 3.11 (need 3.10+)
  ✓ chromadb installed
  ✓ vault opens & decrypts (576 videos, licensed to you@email)

✅ All good — add the MCP config to your AI agent and start asking questions.

📍 2024 Mentorship Lecture #7 — 22:10
"FVG occurs when price moves too quickly in one direction..."

📍 2023 Mentorship Ep.01 — 05:00
"ICT uses Fair Value Gaps to identify institutional order flow..."
```

---

## 1. Claude Desktop (MCP Server)

### Setup (30 seconds)

**Step 1:** Find your Claude Desktop config file:
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`

**Step 2:** Add this to the `mcpServers` section:

```json
{
  "mcpServers": {
    "ict-knowledge-vault": {
      "command": "python",
      "args": ["C:/ict-knowledge-vault/mcp_server.py"],
      "env": {
        "VAULT_PATH": "C:/ict-knowledge-vault",
        "LICENSE_KEY": "auto"
      }
    }
  }
}
```

**Step 3:** Restart Claude Desktop.

**Step 4:** Ask Claude anything about ICT:

```
You: "How does ICT define a Fair Value Gap?"

Claude: [Auto-searches vault]
Based on ICT's 2022 Mentorship Episode 12 (15:23):

"A Fair Value Gap is a 3-candle pattern where the middle candle
leaves a gap in price delivery..."

ICT also explains in 2024 Mentorship Lecture #7 that "FVG acts
as a magnet for price to return and fill the imbalance."

Sources:
📍 2022 Mentorship Ep.12 (timestamp 15:23)
📍 2024 Mentorship Lecture #7 (22:10)
📍 2023 Mentorship Ep.01 (05:00)
```

### Available MCP Tools

| Tool | Description |
|---|---|
| `search_ict(query, top_k=5)` | Search the vault |
| `get_video(video_id)` | Get full transcript for a video |
| `explore_concept(concept)` | Get KG connections + definition |
| `list_playlists()` | List all playlists with video counts |

---

## 2. Cursor IDE

Same as Claude Desktop — uses MCP protocol.

**Step 1:** Open Cursor Settings → Features → MCP

**Step 2:** Add new MCP server:

```json
{
  "mcpServers": {
    "ict-knowledge-vault": {
      "command": "python",
      "args": ["C:/ict-knowledge-vault/mcp_server.py"],
      "env": {
        "VAULT_PATH": "C:/ict-knowledge-vault"
      }
    }
  }
}
```

**Step 3:** In Cursor's AI chat, ask ICT questions. Agent auto-searches vault.

---

## 3. Hermes Agent

### Option A: MCP Server (Recommended)

Add to Hermes config:

```yaml
# ~/.hermes/profiles/<profile>/config.yaml
mcp_servers:
  ict-knowledge-vault:
    command: python
    args: ["C:/ict-knowledge-vault/mcp_server.py"]
    env:
      VAULT_PATH: "C:/ict-knowledge-vault"
```

Hermes agent auto-discovers the ICT vault tool.

### Option B: Direct Python Plugin

```python
# Save as ~/.hermes/profiles/<profile>/plugins/ict_vault.py
import sys
sys.path.insert(0, r"C:\ict-knowledge-vault")
from ict_search import ICTVault

vault = ICTVault()

# Hermes can now call:
# vault.search("FVG")
# vault.get_video("abc123")
# vault.explore_concept("Silver Bullet")
```

---

## 4. ChatGPT (Paid / Plus)

ChatGPT cannot run MCP servers directly. Use these methods:

### Method A: Use an MCP-native desktop agent (Recommended)

ChatGPT on the web can't reach a local vault. The smoothest path today is a
desktop agent that speaks MCP — Claude Desktop, Cursor or Hermes — which
connects to the vault directly (see sections above). A hosted connector for
ChatGPT and Claude.ai on the web is on the roadmap; lifetime buyers get first
access.

### Method B: Custom GPT with Knowledge Files

1. Create a Custom GPT in ChatGPT
2. Upload the vault index (`index.md`) as knowledge
3. Configure Custom GPT instructions:

```
You are an ICT trading expert. You have access to the ICT Knowledge
Vault index. When asked about ICT concepts, reference the vault index.
```

### Method C: API Script (For Developers)

```python
# chatgpt_with_ict.py
from openai import OpenAI
from ict_search import ICTVault

vault = ICTVault()
client = OpenAI()

question = input("Ask about ICT: ")

# Search vault
results = vault.search(question, top_k=5)

# Build prompt with context
context = "\n\n".join([f"Source: {r.title} ({r.start_ts})\n{r.text[:500]}" 
                        for r in results])

response = client.chat.completions.create(
    model="gpt-4",
    messages=[
        {"role": "system", "content": "You are an ICT trading expert. Answer based on the provided sources. Cite timestamps."},
        {"role": "user", "content": f"Sources:\n{context}\n\nQuestion: {question}"}
    ]
)

print(response.choices[0].message.content)
```

---

## 5. Codex CLI (OpenAI)

Codex CLI supports MCP. Same config as Claude/Cursor:

```bash
# Add to codex config
codex mcp add ict-knowledge-vault -- python C:/ict-knowledge-vault/mcp_server.py
```

Then in Codex session:
```
> Search ICT for Fair Value Gap definition
```

---

## 6. Any Python AI Agent

### Direct API

```python
# your_agent.py
import sys
sys.path.insert(0, r"C:\ict-knowledge-vault")
from ict_search import ICTVault

vault = ICTVault()

def ask_agent(question, llm_call):
    """Query ICT vault + feed to any LLM"""
    # Step 1: Search vault
    results = vault.search(question, top_k=5)
    
    # Step 2: Build context
    context_parts = []
    for r in results:
        context_parts.append(
            f"SOURCE: {r.title} | Timestamp: {r.start_ts}\n"
            f"TEXT: {r.text[:600]}..."
        )
    context = "\n\n---\n\n".join(context_parts)
    
    # Step 3: Prompt your LLM
    prompt = f"""You are an ICT (Inner Circle Trader) expert.
Answer the question based ONLY on the sources below.
Cite the source title and timestamp for each claim.

SOURCES:
{context}

QUESTION: {question}

ANSWER:"""
    
    # Step 4: Call your LLM (Claude, GPT, DeepSeek, etc.)
    answer = llm_call(prompt)
    return answer

# Usage with any LLM:
answer = ask_agent(
    "How does ICT use Silver Bullet in London session?",
    your_llm_function
)
print(answer)
```

---

## 7. LangChain / LlamaIndex / RAG Frameworks

```python
from ict_search import ICTVault
from langchain.llms import OpenAI

vault = ICTVault()

# Get relevant chunks
results = vault.search("FVG", top_k=5)
chunks = [r.text for r in results]

# Feed to LangChain
from langchain.chains import RetrievalQA
# ... standard LangChain RAG setup with chunks as context
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "license.key not found" | Ensure `license.key` is in the vault folder |
| "VAULT_PATH not set" | Set environment variable or pass path directly |
| "ChromaDB connection error" | Delete `_vectors/` folder and re-extract vault |
| "FTS5 module not found" | SQLite version too old. Install Python 3.10+ |
| "MCP server not connecting" | Check Python is in PATH. Run `python --version` |
| Claude can't find vault | Use full absolute path in config, not relative |
| Slow first search | Cross-encoder loading (~30s first time). Subsequent: <1s |

### System Requirements

| Component | Minimum |
|---|---|
| Python | 3.10+ |
| RAM | 4GB (8GB recommended) |
| Disk | 500MB free |
| OS | Windows 10+, macOS 12+, Linux |
| AI Agent | Any (Claude, GPT, Hermes, Cursor, Codex) |

---

## 50 Example Queries

### Core ICT Concepts
1. "What is a Fair Value Gap?"
2. "How does ICT define Order Block?"
3. "What is a Breaker?"
4. "Explain Mitigation Block"
5. "What is CISD?"
6. "Define MSS"
7. "What is a Liquidity Sweep?"
8. "Explain Imbalance vs Rebalance"
9. "What is the IPDA?"
10. "Define SMT divergence"

### Trading Models & Strategies
11. "What is the Silver Bullet model?"
12. "How to trade Silver Bullet in London session?"
13. "ICT Venom model explained"
14. "What is the Reaper model?"
15. "Explain the Gauntlet framework"
16. "Turtle Soup pattern ICT"
17. "Judas Swing explained"
18. "MMSM vs MMBM"
19. "How does ICT use PD Arrays?"
20. "Opening Range Gap strategy"

### Sessions & Killzones
21. "London Killzone strategy"
22. "New York Killzone ICT"
23. "Asian session ICT"
24. "What time is London Open Killzone?"
25. "PM Session reversal model"
26. "Lunch Macro ICT"
27. "How to trade FOMC with ICT"

### Price Action & Tape Reading
28. "How does ICT read tape?"
29. "What is ICT's tape reading process?"
30. "Order Flow ICT explained"
31. "How to identify institutional order flow?"
32. "Algorithmic Price Delivery"
33. "Market Maker models ICT"
34. "How to identify manipulation ICT"

### Practical Trading
35. "ICT risk management"
36. "ICT position sizing"
37. "How to journal trades ICT method"
38. "ICT backtesting approach"
39. "How many models should I trade?"
40. "ICT funded account strategy"

### Market Structure
41. "Institutional Market Structure ICT"
42. "How to draw market structure correctly"
43. "Change of Character vs Break of Structure"
44. "Premium vs Discount ICT"
45. "Equilibrium in ICT"

### Advanced
46. "Intermarket analysis ICT"
47. "CFDs vs Futures ICT"
48. "Micro vs Mini contracts ICT"
49. "How to manage missed entries"
50. "ICT's advice for struggling traders"
