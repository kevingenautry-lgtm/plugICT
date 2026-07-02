"""
ICT Knowledge Vault — Standalone Search Infrastructure Builder
=============================================================
Run sekali: python ict_ingest.py
Output: ChromaDB + FTS5 + KG — all self-contained, ready to encrypt for buyer.

Adapted from LLM Wiki's _search.py, _kg.py, _fusion.py
"""

import os, re, sys, json, sqlite3, hashlib
from pathlib import Path
from datetime import datetime

import vault_core as vc

# ── Config ──────────────────────────────────────────────────────────────────
VAULT_DIR = Path(os.environ.get("ICT_SOURCE_DIR", r"C:\Users\kevin\Hermes ICT Selling Idea"))
VECTOR_DIR = VAULT_DIR / "_vectors"
KG_DB_PATH = VAULT_DIR / "kg.db"
CHUNK_SIZE = 900
CHUNK_OVERLAP = 100
BATCH_SIZE = 128

print("=" * 60)
print("ICT Knowledge Vault — Search Infrastructure Builder")
print("=" * 60)
print(f"Vault: {VAULT_DIR}")
print(f"Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# ── Step 1: Discover all ICT transcripts ────────────────────────────────────
print("[1/5] Scanning transcripts...")

md_files = sorted(VAULT_DIR.glob("*.md"))
transcripts = [f for f in md_files if f.name not in ('index.md', 'README.md', 'CATALOG.md')]

print(f"  Found {len(transcripts)} transcript files")

# Count stats
total_lines = 0
playlist_counts = {}
for fp in transcripts:
    with open(fp, encoding='utf-8', errors='replace') as f:
        content = f.read()
        total_lines += content.count('\n')
        playlist = vc.classify_playlist(fp.name)
        playlist_counts[playlist] = playlist_counts.get(playlist, 0) + 1

print(f"  Lines: {total_lines:,}")
print(f"  Playlists: {len(playlist_counts)}")
for pl, count in sorted(playlist_counts.items(), key=lambda x: -x[1]):
    print(f"    {pl}: {count}")
print()

# ── Step 2: Chunk all transcripts ───────────────────────────────────────────
print("[2/5] Chunking transcripts...")

all_chunks = []
chunk_id = 0

for fp in transcripts:
    with open(fp, encoding='utf-8', errors='replace') as f:
        content = f.read()
    
    # Extract frontmatter
    fm = {}
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split('\n'):
                if ':' in line:
                    k, v = line.split(':', 1)
                    fm[k.strip()] = v.strip().strip('"')
            body = parts[2]
        else:
            body = content
    else:
        body = content
    
    title = fm.get('title', fp.stem)
    video_id = fm.get('video_id', '')
    duration = fm.get('duration', '')
    playlist = vc.classify_playlist(fp.name)
    
    # Clean body — strip markdown formatting for cleaner chunks
    body = re.sub(r'^#.*$', '', body, flags=re.MULTILINE)
    body = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', body)
    body = re.sub(r'\*\*([^*]+)\*\*', r'\1', body)
    body = re.sub(r'[*>|]', ' ', body)
    body = re.sub(r'\n{3,}', '\n\n', body)
    body = body.strip()
    
    if not body:
        continue
    
    # Timestamp-based chunking (ICT transcripts have "0:00 ..." format)
    lines = body.split('\n')
    current_chunk = []
    current_len = 0
    chunk_start_ts = None
    chunk_counter = 0  # Per-file counter for O(1) chunk indexing
    
    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        
        # Detect timestamp
        ts_match = re.match(r'^(\d+:\d{2})\s', line_clean)
        
        if ts_match and current_len > CHUNK_SIZE:
            # Save current chunk
            chunk_text = '\n'.join(current_chunk)
            all_chunks.append({
                'id': f"chunk_{chunk_id:06d}",
                'text': chunk_text,
                'title': title,
                'video_id': video_id,
                'playlist': playlist,
                'duration': duration,
                'source_file': fp.name,
                'start_ts': chunk_start_ts or '0:00',
                'chunk_index': chunk_counter,
            })
            chunk_id += 1
            chunk_counter += 1
            current_chunk = [line_clean]
            current_len = len(line_clean)
            chunk_start_ts = ts_match.group(1)
        else:
            if chunk_start_ts is None and ts_match:
                chunk_start_ts = ts_match.group(1)
            current_chunk.append(line_clean)
            current_len += len(line_clean)
    
    # Don't forget last chunk
    if current_chunk:
        chunk_text = '\n'.join(current_chunk)
        if len(chunk_text) > 100:  # Skip tiny chunks
            all_chunks.append({
                'id': f"chunk_{chunk_id:06d}",
                'text': chunk_text,
                'title': title,
                'video_id': video_id,
                'playlist': playlist,
                'duration': duration,
                'source_file': fp.name,
                'start_ts': chunk_start_ts or '0:00',
                'chunk_index': chunk_counter,
            })
            chunk_id += 1
            chunk_counter += 1

print(f"  Total chunks: {len(all_chunks):,}")
avg_chunk_len = sum(len(c['text']) for c in all_chunks) / max(len(all_chunks), 1)
print(f"  Avg chunk size: {avg_chunk_len:.0f} chars")
print()

# ── Step 3: Build ChromaDB ──────────────────────────────────────────────────
print("[3/5] Building ChromaDB vector database...")

os.makedirs(str(VECTOR_DIR), exist_ok=True)

import chromadb
from chromadb.config import Settings

client = chromadb.PersistentClient(
    path=str(VECTOR_DIR),
    settings=Settings(anonymized_telemetry=False)
)

# Delete existing if present
try:
    client.delete_collection("ict_vault")
except:
    pass

# ONNX embedding function
from chromadb.utils import embedding_functions
ef = embedding_functions.ONNXMiniLM_L6_V2()

collection = client.create_collection(
    name="ict_vault",
    embedding_function=ef,
    metadata={"description": "ICT Knowledge Vault — Inner Circle Trader transcripts"}
)

# Embed in batches (avoid memory issues)
for i in range(0, len(all_chunks), BATCH_SIZE):
    batch = all_chunks[i:i+BATCH_SIZE]
    collection.add(
        ids=[c['id'] for c in batch],
        documents=[c['text'] for c in batch],
        metadatas=[{
            'title': c['title'],
            'video_id': c['video_id'],
            'playlist': c['playlist'],
            'source_file': c['source_file'],
            'start_ts': c['start_ts'],
        } for c in batch],
    )
    if (i // BATCH_SIZE) % 5 == 0:
        print(f"  Embedded {min(i+BATCH_SIZE, len(all_chunks))}/{len(all_chunks)} chunks...")

print(f"  ✅ ChromaDB ready — {len(all_chunks)} chunks indexed")
print()

# ── Step 4: Build FTS5 + Knowledge Graph ────────────────────────────────────
print("[4/5] Building FTS5 keyword index + Knowledge Graph...")

# Remove old DB
if KG_DB_PATH.exists():
    KG_DB_PATH.unlink()

conn = sqlite3.connect(str(KG_DB_PATH))
conn.execute("PRAGMA journal_mode=WAL")

# FTS5 table
conn.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
        title,
        video_id,
        playlist,
        start_ts,
        source_file,
        content,
        tokenize='porter unicode61'
    )
""")

# Insert into FTS5
for c in all_chunks:
    conn.execute(
        "INSERT INTO transcripts_fts VALUES (?, ?, ?, ?, ?, ?)",
        (c['title'], c['video_id'], c['playlist'], c['start_ts'], c['source_file'], c['text'])
    )

# ── Knowledge Graph Tables ──
conn.execute("""
    CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        type TEXT,
        description TEXT,
        source_file TEXT,
        source_count INTEGER DEFAULT 1
    )
""")

conn.execute("""
    CREATE TABLE IF NOT EXISTS relations (
        id INTEGER PRIMARY KEY,
        from_entity TEXT,
        to_entity TEXT,
        relation_type TEXT,
        evidence TEXT,
        source_file TEXT
    )
""")

# ICT concept dictionary — key concepts to find
ICT_CONCEPTS = {
    # Core Structural
    'FVG': 'Fair Value Gap — 3-candle imbalance pattern',
    'Fair Value Gap': 'Fair Value Gap — 3-candle imbalance pattern',
    'Order Block': 'Key structural level where institutional orders reside',
    'OB': 'Order Block — key structural level',
    'Breaker': 'Failed Order Block that becomes support/resistance',
    'Mitigation Block': 'Order Block that has been tested',
    'Imbalance': 'Price inefficiency that tends to get filled',
    'Rebalance': 'Price returning to fill an imbalance',
    'Displacement': 'Sharp, decisive move signaling institutional intent',
    
    # Market Structure
    'CISD': 'Change In State of Delivery — market structure shift',
    'MSS': 'Market Structure Shift — trend change signal',
    'BOS': 'Break of Structure — key level broken',
    'CHoCH': 'Change of Character — potential reversal',
    'Market Structure': 'Overall framework of highs and lows',
    
    # Time-Based
    'Silver Bullet': 'Time-based trading model during London/NY killzone',
    'Killzone': 'Specific time windows for high-probability setups',
    'London Killzone': 'London session trading window',
    'NY Killzone': 'New York session trading window',
    'Asian Killzone': 'Asian session trading window',
    
    # Models
    'PO3': 'Power of 3 — Accumulation, Manipulation, Distribution',
    'Power of 3': 'Power of 3 — Accumulation, Manipulation, Distribution',
    'Venom': 'ICT\'s specific trade entry model',
    'Turtle Soup': 'False breakout pattern trapping retail traders',
    'Judas Swing': 'False move to trap traders before real direction',
    'Reaper': 'ICT\'s advanced PD Array model',
    'Gauntlet': 'ICT\'s backtesting/validation framework',
    
    # Liquidity
    'Liquidity': 'Areas where stop losses and pending orders accumulate',
    'Liquidity Sweep': 'Price running through liquidity before reversing',
    'Buyside Liquidity': 'Liquidity above current price (stops + breakout traders)',
    'Sellside Liquidity': 'Liquidity below current price',
    
    # Gaps
    'NDOG': 'New Day Opening Gap',
    'NWOG': 'New Week Opening Gap',
    'Opening Range': 'Initial price range after market open',
    
    # Advanced
    'IPDA': 'Interbank Price Delivery Algorithm',
    'SMT': 'Smart Money Technique — divergence between correlated pairs',
    'PD Array': 'Price Delivery Array — institutional price levels',
    'MMSM': 'Market Maker Sell Model',
    'MMBM': 'Market Maker Buy Model',
    'OTE': 'Optimal Trade Entry — Fibonacci-based entry zone',
    'CE': 'Consequent Encroachment — midpoint of FVG',
    'Consequent Encroachment': 'Consequent Encroachment — midpoint of FVG',
    'BPR': 'Balanced Price Range — equilibrium zone',
    'EQ': 'Equilibrium — 50% of a dealing range',
}

# Entity extraction — find concepts mentioned in transcripts
entity_counts = {}
entity_sources = {}

for c in all_chunks:
    text_lower = c['text'].lower()
    for concept, desc in ICT_CONCEPTS.items():
        if concept.lower() in text_lower:
            if concept not in entity_counts:
                entity_counts[concept] = 0
                entity_sources[concept] = set()
            entity_counts[concept] += 1
            entity_sources[concept].add(c['source_file'])

# Insert entities
for concept, desc in ICT_CONCEPTS.items():
    if concept in entity_counts and entity_counts[concept] >= 3:
        conn.execute(
            "INSERT OR IGNORE INTO entities (name, type, description, source_count) VALUES (?, ?, ?, ?)",
            (concept.upper() if len(concept) <= 5 else concept, 
             'ICT Concept', desc, entity_counts[concept])
        )

# Build concept relationships — fully connected Knowledge Graph
RELATIONS = [
    # FVG connections
    ('FVG', 'Order Block', 'related_to', 'Both are key structural concepts'),
    ('FVG', 'Imbalance', 'type_of', 'FVG is a specific type of imbalance'),
    ('FVG', 'Liquidity', 'interacts_with', 'FVG often aligns with liquidity levels'),
    ('FVG', 'Displacement', 'created_by', 'FVG is created by displacement'),
    ('FVG', 'CE', 'contains', 'Consequent Encroachment is midpoint of FVG'),
    ('FVG', 'OTE', 'related_to', 'OTE often aligns with FVG'),
    ('FVG', 'BPR', 'related_to', 'FVG can form within BPR'),
    
    # Order Block connections
    ('Order Block', 'Breaker', 'transforms_into', 'Failed OB becomes Breaker'),
    ('Order Block', 'Mitigation Block', 'related_to', 'Mitigation Block is tested OB'),
    ('Order Block', 'Liquidity', 'interacts_with', 'Order Blocks sit at liquidity levels'),
    
    # Market Structure
    ('CISD', 'MSS', 'precedes', 'CISD often precedes MSS'),
    ('MSS', 'Displacement', 'creates', 'MSS is confirmed by displacement'),
    ('BOS', 'CHoCH', 'related_to', 'BOS is continuation, CHoCH is reversal'),
    ('CISD', 'BOS', 'related_to', 'Both indicate structural change'),
    ('MSS', 'Liquidity', 'requires', 'MSS requires liquidity sweep'),
    
    # Time-based
    ('Silver Bullet', 'Killzone', 'used_in', 'Silver Bullet during killzones'),
    ('Silver Bullet', 'London Killzone', 'used_in', 'London Silver Bullet hour'),
    ('Silver Bullet', 'NY Killzone', 'used_in', 'NY Silver Bullet hour'),
    ('Killzone', 'Liquidity', 'interacts_with', 'Killzones target liquidity'),
    
    # PO3
    ('PO3', 'Liquidity', 'requires', 'Power of 3 requires liquidity'),
    ('PO3', 'Displacement', 'creates', 'PO3 manipulation creates displacement'),
    ('PO3', 'FVG', 'creates', 'PO3 distribution often leaves FVG'),
    
    # Models
    ('Turtle Soup', 'Liquidity', 'targets', 'Turtle Soup hunts retail liquidity'),
    ('Judas Swing', 'Liquidity', 'creates', 'Judas Swing creates false sweep'),
    ('Reaper', 'PD Array', 'type_of', 'Reaper is an advanced PD Array'),
    ('Venom', 'Silver Bullet', 'related_to', 'Venom model related to Silver Bullet'),
    ('Venom', 'OTE', 'uses', 'Venom uses OTE entry'),
    
    # Gaps
    ('NDOG', 'NWOG', 'related_to', 'Daily and weekly opening gaps'),
    ('NDOG', 'Liquidity', 'interacts_with', 'NDOG aligns with liquidity'),
    ('NWOG', 'Liquidity', 'interacts_with', 'NWOG aligns with liquidity'),
    ('Opening Range', 'Liquidity', 'contains', 'Opening range contains liquidity'),
    
    # Advanced
    ('IPDA', 'Liquidity', 'uses', 'IPDA targets liquidity'),
    ('SMT', 'Liquidity', 'divergence_at', 'SMT shows divergence at liquidity'),
    ('MMSM', 'Liquidity', 'targets', 'MMSM targets buyside liquidity'),
    ('MMBM', 'Liquidity', 'targets', 'MMBM targets sellside liquidity'),
    ('OTE', 'FVG', 'aligns_with', 'OTE often within FVG zone'),
    ('CE', 'FVG', 'within', 'CE is the midpoint of FVG'),
    ('BPR', 'EQ', 'contains', 'BPR contains equilibrium'),
    ('EQ', 'Liquidity', 'related_to', 'Equilibrium attracts liquidity'),
    
    # Cross connections
    ('Silver Bullet', 'MMSM', 'related_to', 'Both are directional models'),
    ('Silver Bullet', 'MMBM', 'related_to', 'Both are directional models'),
    ('PD Array', 'Order Block', 'contains', 'PD Array includes Order Blocks'),
    ('PD Array', 'FVG', 'contains', 'PD Array includes FVG'),
]

for from_e, to_e, rel_type, evidence in RELATIONS:
    if entity_counts.get(from_e, 0) >= 3 and entity_counts.get(to_e, 0) >= 3:
        conn.execute(
            "INSERT OR IGNORE INTO relations (from_entity, to_entity, relation_type, evidence) VALUES (?, ?, ?, ?)",
            (from_e.upper() if len(from_e) <= 5 else from_e,
             to_e.upper() if len(to_e) <= 5 else to_e,
             rel_type, evidence)
        )

conn.commit()

# Stats
entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
rel_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

print(f"  ✅ FTS5 ready — {len(all_chunks):,} chunks indexed")
print(f"  ✅ KG ready — {entity_count} entities, {rel_count} relations")
print()

# ── Step 5: Quick verification ──────────────────────────────────────────────
print("[5/5] Verifying...")

# ChromaDB test
test_query = "Fair Value Gap"
results = collection.query(query_texts=[test_query], n_results=3)
print(f"  Vector search '{test_query}': {len(results['ids'][0])} results")

# FTS5 test
fts_results = conn.execute(
    "SELECT title, snippet(transcripts_fts, 5, '<b>', '</b>', '...', 50) FROM transcripts_fts WHERE content MATCH ? LIMIT 3",
    (test_query,)
).fetchall()
print(f"  FTS5 search '{test_query}': {len(fts_results)} results")

# KG test
kg_results = conn.execute(
    "SELECT * FROM relations WHERE from_entity = 'FVG' OR to_entity = 'FVG'"
).fetchall()
print(f"  KG relations for FVG: {len(kg_results)}")

conn.close()

print()
print("=" * 60)
print("✅ BUILD COMPLETE")
print(f"   {len(transcripts)} transcripts")
print(f"   {len(all_chunks):,} chunks")
print(f"   ChromaDB: {VECTOR_DIR}")
print(f"   FTS5 + KG: {KG_DB_PATH}")
print(f"   Total vault: ready for encryption")
print("=" * 60)
