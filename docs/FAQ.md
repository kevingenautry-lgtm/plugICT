# ICT Knowledge Vault — FAQ

## General

**Q: What exactly is this?**
A: A searchable library of 576 Inner Circle Trader YouTube videos — fully transcribed, indexed, and optimized for AI agent queries. Not raw files. Not PDFs. You can search by concept, keyword, or meaning.

**Q: Is this all of ICT's content?**
A: We've transcribed 576 videos across 10 playlists from 2016-2026. This covers the major mentorship series (2022, 2023, 2024), lecture series (2025, 2026 SMC), charter content, forex series, and more.

**Q: Can I browse the raw transcript files?**
A: No. The vault is encrypted — you search and get results. Raw files are not extractable. This protects the content from unauthorized sharing.

**Q: Do I need an AI agent to use this?**
A: Yes — ICT Vault works by connecting to your own AI agent (Claude Desktop, Cursor, Hermes or any MCP-compatible agent). You ask your AI questions in natural conversation, and it searches the vault to answer with cited timestamps.

**Q: What AI agents can I connect?**
A: Claude Desktop, Cursor IDE, Hermes Agent, ChatGPT (via API), Codex CLI, or any MCP-compatible agent. See `AI-AGENT-GUIDE.md`.

---

## Technical

**Q: Do I need internet?**
A: No. Everything runs locally. The vault, search engine, and embeddings are all on your machine. Zero API calls.

**Q: How big is it?**
A: ~388MB on disk. The vault expands to ~420MB when loaded (in RAM, temporary).

**Q: Can I use this on Mac/Linux?**
A: Yes. Requirements: Python 3.10+, 4GB RAM.

**Q: Why is the first search slow?**
A: The cross-encoder model loads on first use (~30 seconds). Subsequent searches are <2 seconds.

**Q: How does the search work?**
A: Multi-signal fusion — keyword (FTS5) + semantic (ChromaDB vectors) + knowledge graph. Results are ranked by relevance, not just keyword match count.

**Q: Can I search by playlist?**
A: Yes. Ask your AI to focus on one, e.g. *"What does the 2022 Mentorship say about FVG?"* — the `search_ict` tool accepts a playlist filter.

---

## License & Security

**Q: Can I share this with a friend?**
A: No. Your license key is unique and contains your email. Sharing is traceable to you.

**Q: What if I lose my license key?**
A: Contact us with your purchase ID for a replacement.

**Q: Is there DRM?**
A: No. The vault is encrypted and your license key is required to decrypt. There's no phoning home. The protection is encryption + watermarking, not DRM.

**Q: Can I get updates?**
A: Future updates may include new transcripts, features, and content. Purchase includes the current vault version.

---

## Content

**Q: Are these official ICT transcripts?**
A: These are automated transcriptions of publicly available YouTube videos from the Inner Circle Trader channel. They are not official or endorsed by ICT.

**Q: What's the transcription quality?**
A: High. Transcribed using faster-whisper (medium model). Timestamps are included. Minor errors possible in fast speech or overlapping audio.

**Q: Can I contribute or request specific videos?**
A: Not currently. This is a curated product.

**Q: What's the Knowledge Graph?**
A: We've extracted ICT concepts (FVG, Order Block, Silver Bullet, etc.) and their relationships. You can explore concept connections beyond simple search.

---

## Support

**Q: Something's not working.**
A: Check:
1. `license.key` is in the same folder as `mcp_server.py`
2. Python 3.10+ is installed (`python --version`)
3. Run `pip install -r requirements.txt` again
4. Try deleting `_vectors` folder and re-extract the vault

**Q: How do I contact support?**
A: [Support contact info to be added]

**Q: Refund policy?**
A: Due to the nature of digital products, refunds are not available once the license key has been issued. Please review the product description carefully before purchasing.
