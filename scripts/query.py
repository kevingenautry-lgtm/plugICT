"""
ICT Knowledge Vault — Search Tool v3.0
======================================
Hybrid search: FTS5 (keyword) + ChromaDB (semantic) + cross-encoder rerank,
knowledge-graph related concepts, session/playlist filters, glossary.

New in v3:
  * Decrypt ONCE per session (interactive REPL) instead of per query
  * Streaming, low-memory decrypt + zstd vault support (see vault_core)
  * Sanitised FTS input (no more silent zero-result queries on punctuation)
  * Visible, actionable errors instead of silent except: pass
  * Optional colour/progress via `rich`, with plain-text fallback
  * --doctor preflight, --explain result provenance, correct cache keys
"""

import sys
import os
import time
import sqlite3
import argparse

import vault_core as vc
from vault_core import VaultError

# ── Optional pretty output (graceful fallback) ───────────────────────────────
_USE_RICH = False
if os.environ.get("NO_COLOR") is None and sys.stdout.isatty():
    try:
        from rich.console import Console
        from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
        _console = Console()
        _USE_RICH = True
    except Exception:
        _USE_RICH = False


def info(msg):
    if _USE_RICH:
        _console.print(msg)
    else:
        # Strip simple rich markup for plain terminals
        import re
        print(re.sub(r"\[/?[a-z0-9_ ]+\]", "", msg))


def warn(msg):
    print(msg, file=sys.stderr)


def error_exit(msg, code=1):
    if _USE_RICH:
        _console.print(f"[bold red]✖[/bold red] {msg}")
    else:
        print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ── Semantic cache (persistent, correctly keyed) ─────────────────────────────
_CACHE_MAX = 500


def _cache_db():
    db = sqlite3.connect(str(vc.VAULT_DIR / ".query_cache.db"))
    db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, result TEXT, ts REAL)")
    return db


def _cache_key(query, playlist, session, top_k, vault_hash):
    # vault_hash invalidates the whole cache when a new vault ships.
    return f"{(vault_hash or '')[:12]}|{query.strip().lower()}|{playlist or ''}|{session or ''}|{top_k}"


def cache_get(key):
    try:
        db = _cache_db()
        row = db.execute("SELECT result FROM cache WHERE key=?", (key,)).fetchone()
        db.close()
        return row[0] if row else None
    except sqlite3.Error as e:
        warn(f"(cache read skipped: {e})")
        return None


def cache_put(key, text):
    try:
        db = _cache_db()
        db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, text, time.time()))
        # Evict oldest beyond the cap.
        db.execute(
            "DELETE FROM cache WHERE key IN "
            "(SELECT key FROM cache ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (_CACHE_MAX,),
        )
        db.commit()
        db.close()
    except sqlite3.Error as e:
        warn(f"(cache write skipped: {e})")


# ── Reranker (optional dependency) ───────────────────────────────────────────
_reranker = None


def _get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
    return _reranker


def rerank(query, candidates, top_k):
    if len(candidates) <= 1:
        return candidates[:top_k]
    try:
        model = _get_reranker()
        pairs = [(query, c.get('text', '')[:512]) for c in candidates]
        scores = model.predict(pairs)
        for c, s in zip(candidates, scores):
            c['rerank_score'] = float(s)
        candidates.sort(key=lambda c: c.get('rerank_score', 0.0), reverse=True)
        return candidates[:top_k]
    except ImportError:
        warn("(reranker unavailable — install sentence-transformers for best ordering)")
        return candidates[:top_k]
    except Exception as e:
        warn(f"(rerank skipped: {e})")
        return candidates[:top_k]


# ── A single decrypted session (decrypt once, query many) ────────────────────
class VaultSession:
    def __init__(self):
        self.db = None
        self.chroma_dir = None
        self.licensed_to = "unknown"
        self._collection = None
        self.vault_hash = self._read_vault_hash()

    @staticmethod
    def _read_vault_hash():
        try:
            return vc.load_license().get("VAULT_HASH", "")
        except VaultError:
            return ""

    def open(self):
        if _USE_RICH:
            with Progress(
                SpinnerColumn(), TextColumn("[cyan]Unlocking vault[/cyan]"),
                BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
                transient=True, console=_console,
            ) as prog:
                task = prog.add_task("decrypt", total=100)

                def cb(done, total):
                    prog.update(task, completed=min(100, done * 100 // max(total, 1)))

                self.db, self.chroma_dir, self.licensed_to = vc.open_vault(on_progress=cb)
        else:
            print("Unlocking vault (first load ~30s)...", file=sys.stderr)
            self.db, self.chroma_dir, self.licensed_to = vc.open_vault()
        return self

    def _get_collection(self):
        if self._collection is None:
            import chromadb
            from chromadb.config import Settings
            client = chromadb.PersistentClient(
                path=self.chroma_dir, settings=Settings(anonymized_telemetry=False))
            self._collection = client.get_collection('ict_vault')
        return self._collection

    def search(self, query, playlist=None, session=None, top_k=5, explain=False):
        expanded, changed = vc.expand_query(query)
        candidates = []

        # ── FTS5 (keyword) ──
        fts_query = vc.sanitize_fts(expanded)
        if fts_query:
            try:
                sql = ("SELECT title, video_id, start_ts, playlist, "
                       "snippet(transcripts_fts, 5, '<b>', '</b>', '...', 60) "
                       "FROM transcripts_fts WHERE content MATCH ?")
                params = [fts_query]
                if playlist:
                    sql += " AND playlist = ?"
                    params.append(playlist)
                sql += " ORDER BY rank LIMIT ?"
                params.append(top_k + 5)
                for r in self.db.execute(sql, params).fetchall():
                    candidates.append({'source': 'keyword', 'title': r[0], 'video_id': r[1],
                                       'start_ts': r[2], 'playlist': r[3], 'text': r[4]})
            except sqlite3.Error as e:
                warn(f"(keyword search unavailable: {e})")

        # ── ChromaDB (semantic) — use ORIGINAL query, embeddings handle acronyms ──
        try:
            where = {'playlist': playlist} if playlist else None
            out = self._get_collection().query(
                query_texts=[query], n_results=top_k + 5, where=where)
            docs = out.get('documents', [[]])[0]
            metas = out.get('metadatas', [[]])[0]
            for i, doc in enumerate(docs):
                m = metas[i] if i < len(metas) else {}
                candidates.append({'source': 'semantic', 'title': m.get('title', ''),
                                   'video_id': m.get('video_id', ''), 'start_ts': m.get('start_ts', ''),
                                   'playlist': m.get('playlist', ''), 'text': doc[:500]})
        except ImportError:
            warn("(semantic search unavailable — 'pip install chromadb'; run --doctor)")
        except Exception as e:
            warn(f"(semantic search unavailable: {e})")

        ranked = rerank(query, candidates, top_k) if candidates else []
        return ranked, expanded, changed

    def close(self):
        if self.db:
            self.db.close()
            self.db = None


# ── Rendering ────────────────────────────────────────────────────────────────
def render_results(ranked, query, licensed_to, session=None, explain=False):
    lines = []
    shown = 0
    for r in ranked:
        sessions = vc.detect_session(r['text'])
        if session and session not in sessions:
            continue
        shown += 1
        tag = "📖" if r['source'] == 'keyword' else "🧠"
        session_tag = f" [{', '.join(sessions)}]" if sessions else ""
        block = [f"{tag} {shown}. {r['title']}",
                 f"   📍 {r['start_ts']} | {r['playlist']}{session_tag}",
                 f"   {r['text'][:300]}..."]
        if r.get('video_id'):
            block.append(f"   🔗 {vc.youtube_link(r['video_id'], r.get('start_ts'))}")
        if explain:
            why = f"matched via {r['source']}"
            if r.get('rerank_score') is not None:
                why += f", rerank score {r['rerank_score']:.2f}"
            block.append(f"   ℹ️  {why}")
        lines.append("\n".join(block))
    if not shown:
        lines.append("No results found.\nTry: python query.py \"Fair Value Gap\"")
    return "\n\n".join(lines), shown


# ── Glossary ─────────────────────────────────────────────────────────────────
def show_glossary(term=None):
    if term:
        term = term.strip()
        key = term if term in vc.ICT_SHORTFORMS else term.upper()
        if key in vc.ICT_SHORTFORMS:
            info(f"[bold]📘 {key}[/bold]")
            info(f"  {vc.ICT_SHORTFORMS[key]}")
            rel = vc.related_terms(key)
            if rel:
                info("\n  Related terms:")
                for r in rel:
                    info(f"    {r} — {vc.ICT_SHORTFORMS[r][:60]}...")
        else:
            info(f"'{term}' not found. See full list: python query.py --glossary")
    else:
        info("[bold]ICT SHORTFORM GLOSSARY[/bold]")
        for cat, terms in vc.ICT_GLOSSARY.items():
            info(f"\n[cyan]{cat}[/cyan]")
            for k in sorted(terms):
                info(f"  {k:<8} = {terms[k]}")


# ── Stats / listings (need a decrypted db) ───────────────────────────────────
def show_stats(db):
    total = db.execute("SELECT COUNT(*) FROM transcript_files").fetchone()[0]
    chunks = db.execute("SELECT COUNT(*) FROM transcripts_fts").fetchone()[0]
    entities = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    playlists = db.execute("SELECT COUNT(DISTINCT playlist) FROM transcript_files").fetchone()[0]
    info("[bold]ICT KNOWLEDGE VAULT — STATISTICS[/bold]")
    info(f"  📚 Transcripts:   {total}")
    info(f"  📦 Search chunks: {chunks:,}")
    info(f"  🕸️  Entities:      {entities}")
    info(f"  📂 Playlists:     {playlists}\n")
    info("TOP CONCEPTS (by mentions):")
    for i, (name, count) in enumerate(db.execute(
            "SELECT name, source_count FROM entities ORDER BY source_count DESC LIMIT 15").fetchall()):
        bar = '█' * min(count // 50, 30)
        info(f"  {i+1:2}. {name:<25} {count:>5,}x  {bar}")


def list_playlists(db):
    info("[bold]AVAILABLE PLAYLISTS[/bold]")
    for name, count in db.execute(
            "SELECT playlist, COUNT(*) FROM transcript_files GROUP BY playlist "
            "ORDER BY COUNT(*) DESC").fetchall():
        info(f"  {name:<35} {count:>3} videos")


def list_sessions():
    info("[bold]SESSION FILTERS[/bold]")
    for s, kw in vc.SESSION_KEYWORDS.items():
        info(f"  --session {s}  ({', '.join(kw[:2])})")


def show_related(db, query):
    rows = db.execute(
        "SELECT name, description, source_count FROM entities "
        "WHERE name LIKE ? OR ? LIKE '%' || name || '%'",
        (f'%{query}%', query)).fetchall()
    if not rows:
        for w in query.split()[:3]:
            rows = db.execute("SELECT name, description, source_count FROM entities WHERE name LIKE ?",
                             (f'%{w}%',)).fetchall()
            if rows:
                break
    if not rows:
        return
    info("[bold]RELATED CONCEPTS[/bold]")
    seen = set()
    for name, desc, count in rows[:8]:
        if name in seen:
            continue
        seen.add(name)
        info(f"  📘 {name} ({count:,} mentions)")
        info(f"     {desc[:100]}")


# ── Doctor (preflight) ───────────────────────────────────────────────────────
def doctor():
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        mark = "✓" if cond else "✖"
        line = f"  {mark} {label}"
        if not cond and hint:
            line += f"\n      → {hint}"
        print(line)
        ok = ok and cond

    print("ICT Vault — environment check\n")
    check(f"Python {sys.version_info.major}.{sys.version_info.minor} (need 3.9+)",
          sys.version_info >= (3, 9), "Install Python 3.9 or newer from python.org")
    for mod, hint in [("cryptography", "pip install cryptography"),
                      ("chromadb", "pip install chromadb"),
                      ("sentence_transformers", "pip install sentence-transformers"),
                      ("zstandard", "pip install zstandard")]:
        try:
            __import__(mod)
            check(f"{mod} installed", True)
        except Exception:
            check(f"{mod} installed", False, hint)
    check("license.key present", vc.LICENSE_FILE.exists(),
          "Place the license.key from your purchase next to query.py")
    check("ict-vault.kevin present", vc.VAULT_FILE.exists(),
          "Place ict-vault.kevin next to query.py")
    if vc.LICENSE_FILE.exists() and vc.VAULT_FILE.exists():
        try:
            db, _, who = vc.open_vault()
            n = db.execute("SELECT COUNT(*) FROM transcript_files").fetchone()[0]
            db.close()
            check(f"vault opens & decrypts ({n} transcripts, licensed to {who})", True)
        except Exception as e:
            check("vault opens & decrypts", False, str(e))
    print("\n" + ("All good — you're ready to search." if ok else "Some checks failed; fix the items above."))
    return 0 if ok else 1


# ── Interactive REPL (decrypt once, ask many) ────────────────────────────────
def repl(session, args):
    info(f"[green]ICT Vault ready.[/green] Licensed to: {session.licensed_to}")
    info("Type a question, or :help for commands, :quit to exit.\n")
    while True:
        try:
            q = input("ict> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in (":quit", ":q", ":exit"):
            break
        if q in (":help", ":h"):
            info("  <question>            search the vault\n"
                 "  :stats                vault statistics\n"
                 "  :playlists            list playlists\n"
                 "  :g <TERM>             glossary lookup\n"
                 "  :quit                 exit")
            continue
        if q == ":stats":
            show_stats(session.db); continue
        if q == ":playlists":
            list_playlists(session.db); continue
        if q.startswith(":g"):
            show_glossary(q[2:].strip() or None); continue
        ranked, expanded, changed = session.search(
            q, args.playlist, args.session, args.top, args.explain)
        if changed:
            info(f"[dim]expanded: {q} → {expanded}[/dim]")
        text, shown = render_results(ranked, q, session.licensed_to, args.session, args.explain)
        info(text)
        if args.related and shown:
            show_related(session.db, q)
        print()


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='ICT Knowledge Vault v3.0')
    parser.add_argument('query', nargs='*', help='Search query (omit for interactive mode)')
    parser.add_argument('--playlist', '-p', help='Filter by playlist')
    parser.add_argument('--session', '-s', help='Filter by session')
    parser.add_argument('--top', '-t', type=int, default=5, help='Number of results')
    parser.add_argument('--related', '-r', action='store_true', help='Show related concepts')
    parser.add_argument('--explain', action='store_true', help='Show why each result matched')
    parser.add_argument('--interactive', '-i', action='store_true', help='Interactive session')
    parser.add_argument('--stats', action='store_true', help='Show vault statistics')
    parser.add_argument('--playlists', action='store_true', help='List playlists')
    parser.add_argument('--sessions', action='store_true', help='List session filters')
    parser.add_argument('--glossary', '-g', nargs='?', const='all', default=None,
                        help='Show ICT shortform glossary (optionally a single term)')
    parser.add_argument('--doctor', action='store_true', help='Check environment & vault health')
    args = parser.parse_args()

    # Commands that never need to decrypt the vault:
    if args.doctor:
        sys.exit(doctor())
    if args.glossary is not None:
        show_glossary(None if args.glossary == 'all' else args.glossary)
        return
    if args.sessions:
        list_sessions()
        return

    try:
        session = VaultSession().open()
    except VaultError as e:
        error_exit(str(e))

    try:
        if args.stats:
            show_stats(session.db); return
        if args.playlists:
            list_playlists(session.db); return

        query = ' '.join(args.query).strip()
        if args.interactive or not query:
            repl(session, args)
            return

        # Single-shot query (cache aware).
        ck = _cache_key(query, args.playlist, args.session, args.top, session.vault_hash)
        cached = cache_get(ck)
        if cached is not None:
            info(f"🔍 {query}   [dim](cached)[/dim]")
            info(cached)
            return

        info(f"🔍 Searching: [bold]{query}[/bold]")
        info(f"   Licensed to: {session.licensed_to}")
        ranked, expanded, changed = session.search(
            query, args.playlist, args.session, args.top, args.explain)
        if changed:
            info(f"   → also searching keywords: {expanded}")
        text, shown = render_results(ranked, query, session.licensed_to, args.session, args.explain)
        info(text)
        if args.related and shown:
            show_related(session.db, query)
        if shown:
            cache_put(ck, text)
    finally:
        session.close()


if __name__ == '__main__':
    vc.sweep_stale_temp()
    try:
        main()
    except VaultError as e:
        error_exit(str(e))
    except KeyboardInterrupt:
        print()
        sys.exit(130)
