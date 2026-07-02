"""
ICT Knowledge Vault — MCP Server v3.0
=====================================
Exposes the vault as tools for any MCP-compatible AI agent
(Claude Desktop, Cursor, Hermes, ...).

    python mcp_server.py

Shares vault_core with query.py, so the decrypt path can never drift out of
sync. IMPORTANT: an MCP stdio server speaks JSON-RPC over stdout — every
diagnostic here goes to stderr, never stdout.
"""

import sys
import sqlite3

import vault_core as vc
from vault_core import VaultError


def log(*args):
    """Diagnostics must go to stderr; stdout is the JSON-RPC channel."""
    print(*args, file=sys.stderr, flush=True)


# ── Vault state (decrypt once on startup) ────────────────────────────────────
_db = None
_chroma_dir = None
_collection = None
_licensed_to = "unknown"


def ensure_vault():
    global _db, _chroma_dir, _licensed_to
    if _db is None:
        _db, _chroma_dir, _licensed_to = vc.open_vault()
    return _db


def _get_collection():
    global _collection
    if _collection is None:
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(
            path=_chroma_dir, settings=Settings(anonymized_telemetry=False))
        _collection = client.get_collection('ict_vault')
    return _collection


# ── Search functions ─────────────────────────────────────────────────────────
def search_vault(query, top_k=5, playlist=None):
    ensure_vault()
    results = []
    expanded, _ = vc.expand_query(query)

    # FTS5 — snippet from the CONTENT column (index 5), sanitised input.
    fts_query = vc.sanitize_fts(expanded)
    if fts_query:
        try:
            sql = ("SELECT title, video_id, start_ts, playlist, "
                   "snippet(transcripts_fts, 5, '<b>', '</b>', '...', 80) "
                   "FROM transcripts_fts WHERE content MATCH ?")
            params = [fts_query]
            if playlist:
                sql += " AND playlist = ?"
                params.append(playlist)
            sql += " ORDER BY rank LIMIT ?"
            params.append(top_k)
            for r in _db.execute(sql, params).fetchall():
                results.append({'method': 'keyword', 'title': r[0], 'video_id': r[1],
                                'timestamp': r[2], 'playlist': r[3], 'snippet': r[4]})
        except sqlite3.Error as e:
            log(f"[warn] keyword search unavailable: {e}")

    # ChromaDB — semantic, uses original query.
    try:
        where = {'playlist': playlist} if playlist else None
        out = _get_collection().query(query_texts=[query], n_results=top_k, where=where)
        docs = out.get('documents', [[]])[0]
        metas = out.get('metadatas', [[]])[0]
        for i, doc in enumerate(docs):
            m = metas[i] if i < len(metas) else {}
            if not any(r.get('title') == m.get('title') and r.get('timestamp') == m.get('start_ts')
                       for r in results):
                results.append({'method': 'semantic', 'title': m.get('title', 'Unknown'),
                                'video_id': m.get('video_id', ''), 'timestamp': m.get('start_ts', ''),
                                'playlist': m.get('playlist', ''), 'snippet': doc[:500]})
    except ImportError:
        log("[warn] semantic search unavailable — chromadb not installed")
    except Exception as e:
        log(f"[warn] semantic search unavailable: {e}")

    return results[:top_k]


def get_all_playlists():
    ensure_vault()
    rows = _db.execute("SELECT playlist, COUNT(*) FROM transcript_files "
                       "GROUP BY playlist ORDER BY COUNT(*) DESC").fetchall()
    return [{'playlist': r[0], 'video_count': r[1]} for r in rows]


def explore_concept(concept):
    ensure_vault()
    concept_upper = concept.upper() if len(concept) <= 5 else concept
    relations = _db.execute(
        "SELECT from_entity, to_entity, relation_type, evidence FROM relations "
        "WHERE from_entity = ? OR to_entity = ?", (concept_upper, concept)).fetchall()
    entity = _db.execute(
        "SELECT name, type, description, source_count FROM entities WHERE name = ?",
        (concept_upper,)).fetchone()
    content = search_vault(f"What is {concept}", top_k=3)
    return {
        'concept': concept,
        'entity_info': ({'name': entity[0], 'type': entity[1], 'description': entity[2],
                         'mention_count': entity[3]} if entity else None),
        'relations': [{'from': r[0], 'to': r[1], 'type': r[2], 'evidence': r[3]} for r in relations],
        'top_content': content,
        'glossary': vc.ICT_SHORTFORMS.get(concept_upper),
    }


def vault_stats():
    ensure_vault()
    meta = {row[0]: row[1] for row in _db.execute("SELECT key, value FROM vault_metadata").fetchall()}
    return {
        'version': meta.get('version', '1.0.0'),
        'build_date': meta.get('build_date', ''),
        'transcripts': _db.execute("SELECT COUNT(*) FROM transcript_files").fetchone()[0],
        'chunks': _db.execute("SELECT COUNT(*) FROM transcripts_fts").fetchone()[0],
        'entities': _db.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        'playlists': _db.execute("SELECT COUNT(DISTINCT playlist) FROM transcript_files").fetchone()[0],
        'licensed_to': _licensed_to,
    }


# ── MCP wiring ───────────────────────────────────────────────────────────────
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

SERVER_NAME = "ict-knowledge-vault"
SERVER_VERSION = "3.0.0"
server = Server(SERVER_NAME)


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="search_ict",
            description=("Search the ICT (Inner Circle Trader) Knowledge Vault — a large library "
                         "of transcribed ICT trading-mentorship videos. Use this to find what ICT "
                         "says about any trading concept."),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "What to search for, e.g. 'Fair Value Gap', 'Silver Bullet London'"},
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10,
                              "description": "Number of results (default 5, max 10)"},
                    "playlist": {"type": "string",
                                 "description": "Optional playlist filter, e.g. '2022 ICT Mentorship'"},
                },
                "required": ["query"],
            },
        ),
        Tool(name="list_playlists",
             description="List all playlists in the ICT vault with video counts.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="explore_concept",
             description="Explore an ICT concept — its definition, related concepts and relevant content.",
             inputSchema={
                 "type": "object",
                 "properties": {"concept": {"type": "string",
                                "description": "ICT concept: FVG, Order Block, Silver Bullet, CISD, MSS, ..."}},
                 "required": ["concept"],
             }),
        Tool(name="vault_stats",
             description="Get statistics about the ICT Knowledge Vault.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    try:
        try:
            ensure_vault()
        except VaultError as e:
            return [TextContent(type="text", text=f"Vault unavailable: {e}")]

        if name == "search_ict":
            results = search_vault(arguments.get('query', ''),
                                   top_k=arguments.get('top_k', 5),
                                   playlist=arguments.get('playlist'))
            if not results:
                return [TextContent(type="text",
                        text="No results found. Try different keywords or list_playlists.")]
            out = [f"Search results for: \"{arguments['query']}\"", f"Licensed to: {_licensed_to}", ""]
            for i, r in enumerate(results, 1):
                out.append(f"{i}. {r['title']}")
                out.append(f"   Method: {r['method']} | Timestamp: {r['timestamp']} | Playlist: {r['playlist']}")
                out.append(f"   \"{r['snippet'][:300]}...\"")
                if r.get('video_id'):
                    out.append(f"   Video: {vc.youtube_link(r['video_id'], r.get('timestamp'))}")
                out.append("")
            return [TextContent(type="text", text="\n".join(out))]

        if name == "list_playlists":
            out = ["ICT Knowledge Vault — Playlists", ""]
            for p in get_all_playlists():
                out.append(f"- {p['playlist']}: {p['video_count']} videos")
            return [TextContent(type="text", text="\n".join(out))]

        if name == "explore_concept":
            result = explore_concept(arguments['concept'])
            out = [f"ICT Concept: {result['concept']}", ""]
            if result.get('glossary'):
                out.append(f"Glossary: {result['glossary']}")
            if result['entity_info']:
                ei = result['entity_info']
                out.append(f"Definition: {ei['description']}")
                out.append(f"Mentioned in {ei['mention_count']} transcript chunks")
            if result['relations']:
                out.append("\nRelated Concepts:")
                for rel in result['relations']:
                    out.append(f"  {rel['from']} → {rel['type']} → {rel['to']}")
            if result['top_content']:
                out.append("\nTop Content:")
                for c in result['top_content']:
                    out.append(f"  - {c['title']} ({c['timestamp']})")
            return [TextContent(type="text", text="\n".join(out))]

        if name == "vault_stats":
            s = vault_stats()
            out = ["ICT Knowledge Vault — Statistics", "",
                   f"Version: {s['version']}", f"Built: {s['build_date']}",
                   f"Transcripts: {s['transcripts']}", f"Searchable chunks: {s['chunks']:,}",
                   f"Entities: {s['entities']}", f"Playlists: {s['playlists']}",
                   f"Licensed to: {s['licensed_to']}"]
            return [TextContent(type="text", text="\n".join(out))]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        log(f"[error] tool {name} failed: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    log("=" * 50)
    log("ICT Knowledge Vault — MCP Server v" + SERVER_VERSION)
    log("=" * 50)
    try:
        ensure_vault()
        log(f"Vault loaded. Licensed to: {_licensed_to}")
    except VaultError as e:
        log(f"WARNING: vault not loaded yet: {e}")
        log("Server will start; tools will report the problem until it's fixed.")
    log("Waiting for AI agent connection (stdio)...")

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio
    vc.sweep_stale_temp()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
