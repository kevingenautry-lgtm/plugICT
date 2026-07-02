"""
ICT Knowledge Vault — Search Tool v2.1
Features: FTS5 + ChromaDB + KG + Reranker + Cache + Filters + Stats
"""

import sys, os, io, tarfile, struct, sqlite3, tempfile, shutil, atexit, argparse, time
from pathlib import Path
from cryptography.fernet import Fernet
import chromadb
from chromadb.config import Settings

VAULT_DIR = Path(__file__).parent.resolve()
VAULT_FILE = VAULT_DIR / "ict-vault.kevin"
LICENSE_FILE = VAULT_DIR / "license.key"

_temp_dirs = []
_reranker = None
_cache_db = None
_CACHE_HITS = 0

def _cleanup_temp():
    for d in _temp_dirs:
        try:
            if os.path.exists(d):
                shutil.rmtree(d)
        except Exception:
            pass

atexit.register(_cleanup_temp)

# ── ICT Shortform Glossary (comprehensive) ──
ICT_SHORTFORMS = {
    # Market Structure
    'MS': 'Market Structure — The overall trend and key swing points on a chart',
    'MSS': 'Market Structure Shift — Change from bullish to bearish (or vice versa)',
    'BMS': 'Break in Market Structure — Price breaks a key structural level',
    'BOS': 'Break of Structure — Price breaking a key swing high/low (SMC term)',
    'CHoCH': 'Change of Character — Confirmed shift in market structure',
    'HH': 'Higher High — Each successive peak is higher than the last (uptrend)',
    'HL': 'Higher Low — Each successive trough is higher than the last (uptrend)',
    'LH': 'Lower High — Each successive peak is lower than the last (downtrend)',
    'LL': 'Lower Low — Each successive trough is lower than the last (downtrend)',
    
    # Liquidity
    'BSL': 'Buy Side Liquidity — Stops above highs, targeted by sell programs',
    'SSL': 'Sell Side Liquidity — Stops below lows, targeted by buy programs',
    'ERL': 'External Range Liquidity — Stops outside established range extremes',
    'IRL': 'Internal Range Liquidity — Liquidity within the current dealing range',
    'EQH': 'Equal Highs — Two or more swing highs at the same level, liquidity target',
    'EQL': 'Equal Lows — Two or more swing lows at the same level, liquidity target',
    'PDH': 'Previous Day High — Yesterday\'s highest price, often a liquidity level',
    'PDL': 'Previous Day Low — Yesterday\'s lowest price, often a liquidity level',
    'PWH': 'Previous Week High — Last week\'s highest price',
    'PWL': 'Previous Week Low — Last week\'s lowest price',
    'PMH': 'Previous Month High — Last month\'s highest price',
    'PML': 'Previous Month Low — Last month\'s lowest price',
    'BS': 'Buy Stops — Buy orders triggered above current price (stop losses)',
    'SS': 'Sell Stops — Sell orders triggered below current price (stop losses)',
    'LS': 'Liquidity Sweep — Price briefly moves into liquidity before reversing',
    'DOL': 'Draw on Liquidity — Price probing toward a liquidity pool before reacting',
    
    # Fair Value Concepts
    'FVG': 'Fair Value Gap — 3-candle imbalance pattern created by aggressive price movement',
    'IFVG': 'Inverse Fair Value Gap — FVG in the opposite direction, used for reversal entries',
    'BISI': 'Buy Side Imbalance Sell Side Inefficiency — FVG where buying inefficiency remains',
    'SIBI': 'Sell Side Imbalance Buy Side Inefficiency — FVG where selling inefficiency remains',
    'VI': 'Volume Imbalance — Abnormal volume indicating aggressive buying/selling',
    'CE': 'Consequent Encroachment — 50% retracement level of an FVG or imbalance',
    
    # Order Blocks
    'OB': 'Order Block — A consolidation zone where institutional orders sit (5,283x mentions)',
    'BB': 'Breaker Block — An order block that has been broken and is now acting as support/resistance flipped',
    'MB': 'Mitigation Block — An order block that has been partly mitigated by price returning to it',
    'RB': 'Rejection Block — An order block where price strongly rejected on first touch',
    'PB': 'Propulsion Block — A strong order block that propelled price through multiple levels',
    
    # Premium & Discount
    'PD Array': 'Price Delivery Array — Set of levels where price is expected to react',
    'OTE': 'Optimal Trade Entry — Entry zone at 61.8-79% retracement of a move',
    'EQ': 'Equilibrium — Midpoint of a range (50% level), acts as magnet for price',
    'OTE Zone': 'Optimal Trade Entry Zone — 62%-79% retracement region for entries',
    'M50': 'Mean Threshold 50% — The 50% retracement level, midpoint of a range',
    'PD': 'Premium / Discount — Above equilibrium (premium/sell) vs below (discount/buy)',
    
    # Price Delivery
    'IPDA': 'Interbank Price Delivery Algorithm — ICT\'s model of how institutional price moves',
    'AMD': 'Accumulation, Manipulation, Distribution — The three-phase market cycle',
    'MMSM': 'Market Maker Sell Model — Institutional sell-side liquidity algorithm',
    'MMBM': 'Market Maker Buy Model — Institutional buy-side liquidity algorithm',
    'MM': 'Market Maker — Major institutions moving price to access liquidity',
    
    # Time Concepts
    'HTF': 'Higher Time Frame — Chart timeframes above your trading timeframe (e.g. daily for intraday)',
    'LTF': 'Lower Time Frame — Chart timeframes below your trading timeframe (e.g. 15m for 1h)',
    'MTF': 'Multiple Time Frame — Analyzing across multiple chart timeframes for confluences',
    
    # Kill Zones / Sessions
    'AZ': 'Asian Range — Price range during the Asian trading session',
    'KZ': 'Kill Zone — Specific time window when ICT expects institutional moves',
    'LO': 'London Open — The opening of the London session (key ICT timing)',
    'NYO': 'New York Open — The opening of the NY session, often after London',
    'LC': 'London Close — The close of the London session, often overlapping with NY',
    'ADR': 'Average Daily Range — The average daily price movement range from recent days',
    'ODR': 'Opening Range — Price range established at the beginning of a session',
    
    # Dealing Range
    'DR': 'Dealing Range — Price range where institutional orders are being built',
    'BPR': 'Balanced Price Range — Range where buy/sell orders are balanced',
    
    # Fibonacci
    '0.50': 'Equilibrium (50%) — The midpoint retracement level, often acted on',
    '0.62': 'OTE Beginning (62%) — Start of the Optimal Trade Entry zone',
    '0.705': 'Optimal Entry (70.5%) — ICT\'s preferred specific entry level in OTE zone',
    '0.79': 'Deep Discount/Premium (79%) — The deepest retracement before a reversal',
    
    # ICT Models
    'SB': 'Silver Bullet — Specific hour window (10am-11am or 2pm-3pm NY) for entries',
    'FTA': 'Fair Value Gap + Time Alignment — FVG aligned with specific session timing',
    'CRT': 'Candle Range Theory — Using candle bodies and wicks to determine next move',
    'CRDB': 'Consolidation, Raid, Displacement, Balance — Four-phase market cycle model',
    'NWOG': 'New Week Opening Gap — Gap between previous week close and this week open',
    'NDOG': 'New Day Opening Gap — Gap between previous day close and today open',
    'NWOB': 'New Week Opening Balance — Opening range of the new week',
    'NQOB': 'New Quarter Opening Balance — Opening range of the new quarter',
    'DOP': 'Daily Open Price — The opening price of the current daily candle',
    'PO3': 'Power of 3 — Accumulation, Manipulation, Distribution market cycle',
    'CISD': 'Change In State of Delivery — Shift in how price is being delivered (fast vs slow)',
    
    # Candlestick & Price Action
    'Displacement': 'Displacement — Strong impulsive candle beyond the recent range',
    'Retracement': 'Retracement — A pullback against the current trend direction',
    'Expansion': 'Expansion — Strong directional move after consolidation',
    'Raid': 'Raid — A liquidity sweep into a pool of stops before reversing',
    'Repricing': 'Repricing — Fast displacement into a new price discovery area',
    'Rebalancing': 'Rebalancing — Price returning to fill inefficiencies (FVGs, OBs)',
    
    # Economic/News
    'CPI': 'Consumer Price Index — Key inflation report affecting all markets',
    'PPI': 'Producer Price Index — Wholesale inflation gauge',
    'NFP': 'Non-Farm Payrolls — Monthly US employment report (high impact)',
    'FOMC': 'Federal Open Market Committee — US Fed rate decision (high impact)',
    
    # Misc
    'SMT': 'Smart Money Technique / Synchronicity — Divergence between correlated assets',
    'SMT Div': 'SMT Divergence — Price difference between two correlated instruments',
    'SMC': 'Smart Money Concepts — The broader trading community term for ICT-style trading',
    'Judas Swing': 'Judas Swing — A brief false move in one direction to trap traders before the real move',
    'IB': 'Initial Balance — The first hour(s) range, used to project the day',
    'LVN': 'Low Volume Node — Price level with minimal historical trading activity',
    'HVN': 'High Volume Node — Price level with heavy historical trading activity',
    
    # Timeframes
    'H4': '4-hour chart timeframe',
    'H1': '1-hour chart timeframe',
    'M15': '15-minute chart timeframe',
    'M5': '5-minute chart timeframe',
    'M1': '1-minute chart timeframe',
    
    # General
    'RTH': 'Regular Trading Hours — Standard market session (9:30am-4pm ET)',
    'ETH': 'Electronic Trading Hours — Extended hours, includes overnight trading',
    'YY': 'Year — Usually refers to ICT Mentorship year (e.g. YY 2022, YY 2023)',
}

def show_glossary(term=None):
    """Display ICT shortform glossary."""
    if term:
        term = term.upper().strip()
        if term in ICT_SHORTFORMS:
            print(f"{'=' * 60}")
            print(f"📘 {term}")
            print(f"{'=' * 60}")
            print(f"  {ICT_SHORTFORMS[term]}")
            # Find related terms
            related = [k for k in ICT_SHORTFORMS if k != term and k not in ('H1','H4','M5','M15','RTH','ETH')][:5]
            print()
            print("  Related terms:")
            for r in related[:5]:
                print(f"    {r} — {ICT_SHORTFORMS[r][:60]}...")
            print()
        else:
            print(f"'{term}' not found in glossary. Try: python query.py --glossary")
    else:
        print(f"{'=' * 60}")
        print("ICT SHORTFORM GLOSSARY")
        print(f"{'=' * 60}")
        for key in sorted(ICT_SHORTFORMS.keys()):
            val = ICT_SHORTFORMS[key]
            print(f"  {key:<8} = {val}")
        print()
SESSION_KEYWORDS = {
    'london': ['london', 'london open', 'london session', 'london killzone'],
    'ny': ['new york', 'ny session', 'ny open', 'ny killzone', 'am session'],
    'asia': ['asia', 'asian session', 'asian range', 'tokyo'],
    'silver-bullet': ['silver bullet', 'silver bullet hour'],
    'power-hour': ['power hour', 'final hour'],
    'lunch': ['lunch', 'lunch macro', 'midday'],
    'fomc': ['fomc', 'fed', 'powell', 'rate decision'],
    'nfp': ['nfp', 'nonfarm', 'non-farm', 'payroll'],
}

def detect_session(text):
    text_lower = text.lower()
    matches = []
    for session, keywords in SESSION_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                matches.append(session)
                break
    return matches


# ── Semantic Cache (SQLite persistent) ──
def get_cache_db():
    global _cache_db
    if _cache_db is None:
        path = VAULT_DIR / ".query_cache.db"
        _cache_db = sqlite3.connect(str(path))
        _cache_db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, result TEXT, ts REAL)")
    return _cache_db

def cache_key(query, playlist, session):
    return f"{query.strip().lower()}|{playlist or ''}|{session or ''}"

def check_cache(key):
    try:
        db = get_cache_db()
        row = db.execute("SELECT result FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            global _CACHE_HITS
            _CACHE_HITS += 1
            return row[0]
    except:
        pass
    return None

def store_cache(key, result_text):
    try:
        db = get_cache_db()
        db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, result_text, time.time()))
        db.commit()
    except:
        pass


# ── Cross-Encoder Reranker ──
def get_reranker():
    global _reranker
    if _reranker is None:
        print("   Loading reranker (first time ~30s)...")
        sys.stdout.flush()
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
    return _reranker

def rerank(query, candidates, top_k=5):
    if len(candidates) <= 1:
        return candidates[:top_k]
    try:
        model = get_reranker()
        pairs = [(query, c.get('text', '')[:512]) for c in candidates]
        import numpy as np
        scores = model.predict(pairs)
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in scored[:top_k]]
    except ImportError:
        return candidates[:top_k]
    except Exception:
        return candidates[:top_k]


# ── Vault Loading ──
def load_license():
    if not LICENSE_FILE.exists():
        print("ERROR: license.key not found.")
        sys.exit(1)
    with open(LICENSE_FILE) as f:
        content = f.read()
    info = {}
    for line in content.strip().split('\n'):
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            info[k.strip()] = v.strip()
    return info

def load_vault():
    info = load_license()
    buyer_key_raw = info.get('BUYER_KEY', '')
    encrypted_vault_key_raw = info.get('ENCRYPTED_VAULT_KEY', '')
    licensed_to = info.get('LICENSED_TO', 'unknown')
    
    if not buyer_key_raw or not encrypted_vault_key_raw:
        print("ERROR: Invalid license.key.")
        sys.exit(1)
    
    try:
        buyer_cipher = Fernet(buyer_key_raw.encode())
        vault_key = buyer_cipher.decrypt(encrypted_vault_key_raw.encode())
    except Exception:
        print("ERROR: Cannot unlock vault. License key invalid.")
        sys.exit(1)
    
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    import hashlib
    
    with open(VAULT_FILE, 'rb') as f:
        encrypted = f.read()
    
    actual_hash = hashlib.sha256(encrypted).hexdigest()
    expected_hash = info.get('VAULT_HASH', '')
    if expected_hash and actual_hash != expected_hash:
        print("ERROR: Vault file corrupted. Please re-download.")
        sys.exit(1)
    
    try:
        iv = encrypted[:16]
        ciphertext = encrypted[16:]
        cipher = Cipher(algorithms.AES(vault_key), modes.CTR(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(ciphertext) + decryptor.finalize()
    except Exception:
        print("ERROR: Cannot decrypt vault.")
        sys.exit(1)
    
    version, db_size, chroma_size = struct.unpack('>IQQ', decrypted[:20])
    db_bytes = decrypted[20:20+db_size]
    chroma_bytes = decrypted[20+db_size:20+db_size+chroma_size]
    
    tmpdir = tempfile.mkdtemp(prefix='ict_vault_')
    _temp_dirs.append(tmpdir)
    
    db_path = os.path.join(tmpdir, 'master.db')
    with open(db_path, 'wb') as f:
        f.write(db_bytes)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=OFF")
    
    chroma_dir = os.path.join(tmpdir, 'chroma')
    os.makedirs(chroma_dir, exist_ok=True)
    chroma_tar = io.BytesIO(chroma_bytes)
    with tarfile.open(fileobj=chroma_tar) as tar:
        tar.extractall(path=chroma_dir)
    
    return db, chroma_dir, licensed_to


# ── Related Concepts ──
def show_related(db, query):
    entities = db.execute(
        "SELECT name, description, source_count FROM entities "
        "WHERE name LIKE ? OR ? LIKE '%' || name || '%'",
        (f'%{query}%', query)
    ).fetchall()
    
    if not entities:
        words = query.split()
        for w in words[:3]:
            entities = db.execute(
                "SELECT name, description, source_count FROM entities WHERE name LIKE ?",
                (f'%{w}%',)
            ).fetchall()
            if entities:
                break
    
    if not entities:
        return
    
    print("=" * 60)
    print("RELATED CONCEPTS:")
    print("=" * 60)
    
    shown_entities = set()
    for e in entities[:8]:
        name, desc, count = e
        if name in shown_entities:
            continue
        shown_entities.add(name)
        print(f"  📘 {name} ({count:,} mentions)")
        print(f"     {desc[:100]}")
        
        rels = db.execute(
            "SELECT from_entity, to_entity, relation_type FROM relations "
            "WHERE from_entity = ? OR to_entity = ? LIMIT 5",
            (name, name)
        ).fetchall()
        
        if rels:
            related = []
            for fr, to_e, rtype in rels:
                target = to_e if fr == name else fr
                if target not in shown_entities:
                    related.append(f"{target} ({rtype})")
            if related:
                print(f"     Related: {', '.join(related[:5])}")
        print()
    print()


# ── Sessions, Stats, Playlists ──
def list_sessions():
    print("SESSION FILTERS:")
    print("=" * 40)
    for session, keywords in SESSION_KEYWORDS.items():
        print(f"  --session {session}  ({', '.join(keywords[:2])})")
    print()

def show_stats(db):
    total = db.execute("SELECT COUNT(*) FROM transcript_files").fetchone()[0]
    chunks = db.execute("SELECT COUNT(*) FROM transcripts_fts").fetchone()[0]
    entities = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    playlists = db.execute("SELECT COUNT(DISTINCT playlist) FROM transcript_files").fetchone()[0]
    
    print("=" * 60)
    print("ICT KNOWLEDGE VAULT — STATISTICS")
    print("=" * 60)
    print(f"  📚 Transcripts:    {total}")
    print(f"  📦 Search chunks:  {chunks:,}")
    print(f"  🕸️  Entities:       {entities}")
    print(f"  📂 Playlists:      {playlists}")
    print()
    
    top = db.execute(
        "SELECT name, source_count FROM entities ORDER BY source_count DESC LIMIT 15"
    ).fetchall()
    print("TOP CONCEPTS (by mentions):")
    print("-" * 40)
    for i, (name, count) in enumerate(top):
        bar = '█' * min(count // 50, 30)
        print(f"  {i+1:2}. {name:<25} {count:>5,}x  {bar}")
    print()
    
    pl = db.execute(
        "SELECT playlist, COUNT(*) FROM transcript_files GROUP BY playlist ORDER BY COUNT(*) DESC"
    ).fetchall()
    print("PLAYLISTS:")
    print("-" * 40)
    for name, count in pl:
        print(f"  {name:<30} {count:>3} videos")
    print()

def list_playlists(db):
    pl = db.execute(
        "SELECT playlist, COUNT(*) FROM transcript_files GROUP BY playlist ORDER BY COUNT(*) DESC"
    ).fetchall()
    print("AVAILABLE PLAYLISTS:")
    print("=" * 40)
    for name, count in pl:
        print(f"  {name:<35} {count:>3} videos")
    print()


# ── Main Search ──
def search(args):
    query = ' '.join(args.query) if args.query else ''
    ck = cache_key(query, args.playlist, args.session)
    
    cached = check_cache(ck)
    if cached:
        print(cached)
        print(f"[Cache hit: {_CACHE_HITS}]")
        return
    
    db, chroma_dir, licensed_to = load_vault()
    
    if args.stats:
        show_stats(db); db.close(); return
    if args.playlists:
        list_playlists(db); db.close(); return
    if args.sessions:
        list_sessions(); db.close(); return
    if not args.query:
        db.close(); return
    
    print(f"🔍 Searching: {query}")
    if args.playlist: print(f"   Playlist: {args.playlist}")
    if args.session: print(f"   Session: {args.session}")
    print(f"   Licensed to: {licensed_to}")
    print("=" * 60)
    print()
    
    top_k = args.top or 5
    candidates = []
    
    # ── FTS5 ──
    try:
        sql = "SELECT title, video_id, start_ts, playlist, snippet(transcripts_fts, 5, '<b>', '</b>', '...', 60) FROM transcripts_fts WHERE content MATCH ?"
        params = [query]
        if args.playlist:
            sql += " AND playlist = ?"
            params.append(args.playlist)
        sql += " LIMIT ?"
        params.append(top_k + 5)
        
        for r in db.execute(sql, params).fetchall():
            candidates.append({
                'source': 'keyword', 'title': r[0], 'video_id': r[1],
                'start_ts': r[2], 'playlist': r[3], 'text': r[4]
            })
    except Exception:
        pass
    
    # ── ChromaDB ──
    try:
        client = chromadb.PersistentClient(path=chroma_dir, settings=Settings(anonymized_telemetry=False))
        collection = client.get_collection('ict_vault')
        where_filter = {'playlist': args.playlist} if args.playlist else None
        vec_out = collection.query(query_texts=[query], n_results=top_k + 5, where=where_filter)
        docs = vec_out.get('documents', [[]])[0]
        metas = vec_out.get('metadatas', [[]])[0]
        
        for i, doc in enumerate(docs):
            meta = metas[i] if i < len(metas) else {}
            candidates.append({
                'source': 'semantic', 'title': meta.get('title', ''),
                'video_id': meta.get('video_id', ''),
                'start_ts': meta.get('start_ts', ''),
                'playlist': meta.get('playlist', ''),
                'text': doc[:500]
            })
    except Exception:
        pass
    
    # ── Rerank ──
    if candidates:
        print(f"   Found {len(candidates)} candidates, reranking...")
        sys.stdout.flush()
        ranked = rerank(query, candidates, top_k)
    else:
        ranked = []
    
    # ── Display ──
    shown = 0
    output_lines = []
    for r in ranked:
        sessions = detect_session(r['text'])
        session_tag = f" [{', '.join(sessions)}]" if sessions else ""
        
        if args.session and args.session not in sessions:
            continue
        
        shown += 1
        source_tag = "📖" if r['source'] == 'keyword' else "🧠"
        line = f"{source_tag} {shown}. {r['title']}\n"
        line += f"   📍 {r['start_ts']} | {r['playlist']}{session_tag}\n"
        line += f"   {r['text'][:300]}...\n"
        if r.get('video_id'):
            line += f"   🔗 https://youtu.be/{r['video_id']}\n"
        line += ""
        output_lines.append(line)
        print(line)
    
    if not shown:
        no_result = "No results found.\nTry: python query.py 'Fair Value Gap'"
        output_lines.append(no_result)
        print(no_result)
    
    # ── Related Concepts ──
    if args.related and shown > 0:
        show_related(db, query)
    
    db.close()
    
    # ── Store cache ──
    if shown > 0:
        store_cache(ck, '\n'.join(output_lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ICT Knowledge Vault v2.1')
    parser.add_argument('query', nargs='*', help='Search query')
    parser.add_argument('--playlist', '-p', help='Filter by playlist')
    parser.add_argument('--session', '-s', help='Filter by session')
    parser.add_argument('--top', '-t', type=int, default=5, help='Results count')
    parser.add_argument('--related', '-r', action='store_true', help='Show related concepts')
    parser.add_argument('--keyword-only', '-k', action='store_true', help='Keyword only')
    parser.add_argument('--stats', action='store_true', help='Show stats')
    parser.add_argument('--playlists', action='store_true', help='List playlists')
    parser.add_argument('--sessions', action='store_true', help='List all sessions')
    parser.add_argument('--glossary', '-g', nargs='?', const='all', default=None, help='Show ICT shortform glossary')
    
    args = parser.parse_args()
    
    if args.glossary:
        term = None if args.glossary == 'all' else args.glossary
        show_glossary(term)
    search(args)
