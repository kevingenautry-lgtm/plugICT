"""
ICT Knowledge Vault — Standalone Search Infrastructure Builder
=============================================================
Run sekali: python ict_ingest.py
Output: ChromaDB + FTS5 + KG — all self-contained, ready to encrypt for buyer.

Adapted from LLM Wiki's _search.py, _kg.py, _fusion.py
"""

import os, re, sys, json, sqlite3, hashlib, shutil, uuid
from pathlib import Path
from datetime import datetime

import vault_core as vc
from build_integrity import finalize_ingestion_attestation
from ingest_resume import (
    make_resume_manifest,
    plan_resume,
    read_resume_manifest,
    validate_embedding_dimension,
    validate_resume_manifest,
    write_manifest_atomic,
)
from semantic_chunking import (
    build_semantic_units,
    chunk_segments,
    detect_semantic_breaks,
    parse_markdown_transcript,
)

# ── Config ──────────────────────────────────────────────────────────────────
SOURCE_DIR = Path(os.environ.get("ICT_SOURCE_DIR", r"C:\Users\kevin\Hermes ICT Selling Idea"))
BUILD_DIR = Path(os.environ.get("ICT_BUILD_DIR", str(SOURCE_DIR)))
VECTOR_DIR = BUILD_DIR / "_vectors"
KG_DB_PATH = BUILD_DIR / "kg.db"
TARGET_TOKENS = int(os.environ.get("ICT_TARGET_TOKENS", "240"))
HARD_MAX_TOKENS = int(os.environ.get("ICT_HARD_MAX_TOKENS", "350"))
SEMANTIC_UNIT_TOKENS = int(os.environ.get("ICT_SEMANTIC_UNIT_TOKENS", "80"))
MIN_SPLIT_TOKENS = int(os.environ.get("ICT_MIN_SPLIT_TOKENS", "120"))
SEMANTIC_THRESHOLD = float(os.environ.get("ICT_SEMANTIC_THRESHOLD", "0.65"))
BATCH_SIZE = 128
CHUNKER_VERSION = "semantic-v3.0.0"
RESUME_BUILD = os.environ.get("ICT_RESUME", "0") == "1"
EXPECTED_FINAL_CHUNKS = int(os.environ.get("ICT_EXPECTED_FINAL_CHUNKS", "0"))
RESUME_SEMANTIC_BOUNDARIES = int(os.environ.get("ICT_RESUME_SEMANTIC_BOUNDARIES", "0"))

# Vector schema v3 (documentless Chroma) does not store transcript text in the
# vector store, so resume verification — which re-reads Chroma documents to check
# content hashes — cannot run. Require a clean rebuild until resume is redesigned
# to validate against regenerated source chunks. Do NOT silently weaken this.
if RESUME_BUILD:
    sys.exit(
        "ERROR: ICT_RESUME=1 is not supported by the documentless vector schema "
        f"(v{vc.VECTOR_SCHEMA_VERSION}).\n"
        "  Resume verification depends on transcript text stored inside Chroma, which\n"
        "  this schema deliberately omits. Run a clean rebuild (unset ICT_RESUME)."
    )
if BUILD_DIR.resolve() == SOURCE_DIR.resolve() and os.environ.get("ICT_ALLOW_INPLACE") != "1":
    sys.exit("ERROR: v3 build must use an isolated ICT_BUILD_DIR (refusing in-place mutation)")
BUILD_DIR.mkdir(parents=True, exist_ok=True)

print("=" * 60)
print("ICT Knowledge Vault — Search Infrastructure Builder")
print("=" * 60)
print(f"Source: {SOURCE_DIR}")
print(f"Build:  {BUILD_DIR}")
print(f"Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# ── Step 1: Discover all ICT transcripts ────────────────────────────────────
print("[1/5] Scanning transcripts...")

md_files = sorted(SOURCE_DIR.glob("*.md"))
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

# ── Step 2: Parse, detect semantic boundaries, and chunk ─────────────────────
print("[2/5] Building timestamp-preserving semantic chunks...")

ef, embedding_meta = vc.get_embedding_function(return_metadata=True)
print(f"  Embedding function: {ef.__class__.__name__}")


def _embedding_dim(embedding_function):
    return len(embedding_function(["dimension check"])[0])


def _collection_dim(collection):
    data = collection.get(limit=1, include=["embeddings"])
    embeddings = data.get("embeddings")
    if embeddings is None or len(embeddings) == 0:
        return None
    return len(embeddings[0])


chunker_config = {
    "chunker_version": CHUNKER_VERSION,
    "target_tokens": TARGET_TOKENS,
    "hard_max_tokens": HARD_MAX_TOKENS,
    "semantic_unit_tokens": SEMANTIC_UNIT_TOKENS,
    "minimum_semantic_split_tokens": MIN_SPLIT_TOKENS,
    "semantic_similarity_threshold": SEMANTIC_THRESHOLD,
    "overlap_units": 1,
}
resume_manifest_path = BUILD_DIR / ".ict-v3-resume-manifest.json"
current_resume_manifest = make_resume_manifest(
    transcripts,
    chunker_config,
    embedding_meta,
    expected_final_chunks=EXPECTED_FINAL_CHUNKS,
)
effective_expected_chunks = EXPECTED_FINAL_CHUNKS
if RESUME_BUILD:
    effective_expected_chunks = validate_resume_manifest(
        resume_manifest_path,
        current_resume_manifest,
        EXPECTED_FINAL_CHUNKS,
    )
    saved_resume_manifest = read_resume_manifest(resume_manifest_path)
    build_id = saved_resume_manifest["build_id"]
    current_resume_manifest["build_id"] = build_id
else:
    # Remove any prior collection before publishing a manifest for this build.
    # A crash during parsing therefore cannot pair a new manifest with old vectors.
    if VECTOR_DIR.exists():
        shutil.rmtree(VECTOR_DIR)
    build_id = uuid.uuid4().hex
    current_resume_manifest["build_id"] = build_id
    write_manifest_atomic(resume_manifest_path, current_resume_manifest)

all_chunks = []
parse_failures = []
semantic_break_count = 0
resume_client = None
resume_collection = None
existing_ids = set()
resume_cutoff_source = None
resume_cutoff_index = None

if RESUME_BUILD:
    if not (VECTOR_DIR / "chroma.sqlite3").exists():
        raise RuntimeError("resume requested but no existing ChromaDB was found")
    import chromadb
    from chromadb.config import Settings

    resume_client = chromadb.PersistentClient(
        path=str(VECTOR_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    resume_collection = resume_client.get_collection(
        "ict_vault", embedding_function=ef
    )
    if (resume_collection.metadata or {}).get("build_id") != build_id:
        raise RuntimeError("resume collection build_id does not match the manifest")
    validate_embedding_dimension(
        _collection_dim(resume_collection),
        _embedding_dim(ef),
    )
    existing = resume_collection.get(include=["metadatas", "documents"])
    resume_cutoff_index, resume_cutoff_source, stale_ids, existing_ids = plan_resume(
        transcripts,
        existing.get("ids") or [],
        existing.get("metadatas") or [],
        existing.get("documents") or [],
        CHUNKER_VERSION,
    )
    if stale_ids:
        resume_collection.delete(ids=sorted(stale_ids))
    print(
        f"  Resume mode: preserving {len(existing_ids):,} verified vectors; "
        f"deleted {len(stale_ids):,} stale/partial vectors; "
        f"rebuilding from {resume_cutoff_source or 'first source'}"
    )

for file_number, fp in enumerate(transcripts, 1):
    if resume_cutoff_index is not None and (file_number - 1) < resume_cutoff_index:
        continue
    with open(fp, encoding='utf-8', errors='replace') as f:
        content = f.read()

    fm = {}
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split('\n'):
                if ':' in line:
                    k, v = line.split(':', 1)
                    fm[k.strip()] = v.strip().strip('"')

    title = fm.get('title', fp.stem)
    video_id = fm.get('video_id', '')
    duration = fm.get('duration', '')
    playlist = vc.classify_playlist(fp.name)
    segments = parse_markdown_transcript(content)
    if not segments:
        parse_failures.append(fp.name)
        continue

    units = build_semantic_units(segments, target_tokens=SEMANTIC_UNIT_TOKENS)
    breaks = set()
    if len(units) > 1:
        unit_embeddings = []
        for i in range(0, len(units), BATCH_SIZE):
            unit_embeddings.extend(ef([u.text for u in units[i:i+BATCH_SIZE]]))
        breaks = detect_semantic_breaks(
            units, unit_embeddings, similarity_threshold=SEMANTIC_THRESHOLD)
    semantic_break_count += len(breaks)

    chunks = chunk_segments(
        segments,
        video_id=video_id,
        target_tokens=TARGET_TOKENS,
        hard_max_tokens=HARD_MAX_TOKENS,
        min_split_tokens=MIN_SPLIT_TOKENS,
        semantic_break_seconds=breaks,
        overlap_units=1,
    )
    previous_start = -1
    for chunk_index, chunk in enumerate(chunks):
        if chunk.end_seconds < chunk.start_seconds:
            raise RuntimeError(f"invalid timestamp range in {fp.name}: {chunk}")
        if chunk.start_seconds < previous_start:
            raise RuntimeError(f"non-monotonic chunk start in {fp.name}: {chunk}")
        previous_start = chunk.start_seconds
        content_hash = hashlib.sha256(chunk.text.encode('utf-8')).hexdigest()
        identity = (
            f"{fp.name}|{CHUNKER_VERSION}|{chunk.start_seconds}|"
            f"{chunk.end_seconds}|{content_hash}"
        )
        stable_id = hashlib.sha1(identity.encode('utf-8')).hexdigest()[:20]
        all_chunks.append({
            'id': stable_id,
            'text': chunk.text,
            'title': title,
            'video_id': video_id,
            'playlist': playlist,
            'duration': duration,
            'source_file': fp.name,
            'start_ts': chunk.start_ts,
            'end_ts': chunk.end_ts,
            'start_seconds': chunk.start_seconds,
            'end_seconds': chunk.end_seconds,
            'timing_precision': chunk.timing_precision,
            'chunker_version': CHUNKER_VERSION,
            'content_hash': content_hash,
            'chunk_index': chunk_index,
        })

    if file_number % 25 == 0:
        print(f"  Parsed {file_number}/{len(transcripts)} videos; {len(all_chunks):,} chunks")

if parse_failures:
    print(f"  WARNING: {len(parse_failures)} files produced no detailed transcript rows")
    for name in parse_failures[:10]:
        print(f"    - {name}")

if RESUME_BUILD and RESUME_SEMANTIC_BOUNDARIES:
    semantic_break_count = RESUME_SEMANTIC_BOUNDARIES
print(f"  Candidate chunks generated this run: {len(all_chunks):,}")
print(f"  Semantic boundaries: {semantic_break_count:,}")
avg_chunk_len = sum(len(c['text']) for c in all_chunks) / max(len(all_chunks), 1)
print(f"  Avg chunk size: {avg_chunk_len:.0f} chars")
print()

# ── Step 3: Build ChromaDB ──────────────────────────────────────────────────
print("[3/5] Building ChromaDB vector database...")

if VECTOR_DIR.exists() and not RESUME_BUILD:
    shutil.rmtree(VECTOR_DIR)
os.makedirs(str(VECTOR_DIR), exist_ok=True)

import chromadb
from chromadb.config import Settings

client = resume_client or chromadb.PersistentClient(
    path=str(VECTOR_DIR),
    settings=Settings(anonymized_telemetry=False)
)

if not RESUME_BUILD:
    try:
        old = client.get_collection("ict_vault")
        old_dim = _collection_dim(old)
        new_dim = _embedding_dim(ef)
        if old_dim is not None and old_dim != new_dim:
            print(f"  Existing collection dimension {old_dim} != {new_dim}; recreating...")
            client.delete_collection("ict_vault")
    except Exception:
        pass

collection = resume_collection or client.get_or_create_collection(
    name="ict_vault",
    embedding_function=ef,
    metadata={
        "description": "ICT Knowledge Vault — Inner Circle Trader transcripts",
        "build_id": build_id,
    }
)

# Embed in batches (avoid memory issues). Resume mode skips verified existing IDs.
for i in range(0, len(all_chunks), BATCH_SIZE):
    candidate_batch = all_chunks[i:i+BATCH_SIZE]
    batch = [c for c in candidate_batch if c['id'] not in existing_ids]
    if not batch:
        continue
    # Documentless vector store (schema v3): compute embeddings explicitly and
    # store vectors + metadata only. The transcript text is NEVER written to the
    # Chroma store — it lives solely in the encrypted SQLite/FTS DB and is hydrated
    # at query time. Metadata below is non-sensitive (public titles / video ids /
    # timestamps) and is kept so playlist filtering and provenance keep working.
    batch_embeddings = ef([c['text'] for c in batch])
    collection.upsert(
        ids=[c['id'] for c in batch],
        embeddings=batch_embeddings,
        metadatas=[{
            'title': c['title'],
            'video_id': c['video_id'],
            'playlist': c['playlist'],
            'source_file': c['source_file'],
            'chunk_id': c['id'],
            'start_ts': c['start_ts'],
            'end_ts': c['end_ts'],
            'start_seconds': c['start_seconds'],
            'end_seconds': c['end_seconds'],
            'timing_precision': c['timing_precision'],
            'chunker_version': c['chunker_version'],
            'content_hash': c['content_hash'],
            'chunk_index': c['chunk_index'],
        } for c in batch],
    )
    existing_ids.update(c['id'] for c in batch)
    if (i // BATCH_SIZE) % 5 == 0:
        print(f"  ChromaDB now contains {collection.count()}/{effective_expected_chunks or '?'} chunks...")

# Documentless schema: the transcript text is NOT stored in Chroma, so the FTS
# must be built from the in-memory source chunks. Resume is disabled for this
# schema (guarded at startup), so `all_chunks` generated above is already the
# complete, canonical set. Sort it for deterministic FTS ordering, then assert a
# strict 1:1 correspondence with the vectors actually stored in Chroma so a
# partial/failed embed can never ship a vector without its retrievable text.
all_chunks.sort(key=lambda c: (c['source_file'], c['chunk_index'], c['id']))
if effective_expected_chunks and len(all_chunks) != effective_expected_chunks:
    raise RuntimeError(
        f"chunk count {len(all_chunks)} != expected {effective_expected_chunks}"
    )
chroma_ids = set(collection.get(include=[]).get("ids") or [])
source_ids = {c['id'] for c in all_chunks}
if chroma_ids != source_ids:
    only_chroma = len(chroma_ids - source_ids)
    only_source = len(source_ids - chroma_ids)
    raise RuntimeError(
        "Vector/FTS id mismatch — refusing to ship a documentless vault where a "
        f"vector has no retrievable text (chroma-only={only_chroma}, "
        f"source-only={only_source}). Run a clean rebuild."
    )
print(f"  ✅ ChromaDB ready — {len(all_chunks)} chunks indexed (documentless)")
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
        chunk_id,
        chunk_index,
        title,
        video_id,
        playlist,
        start_ts,
        end_ts,
        source_file,
        content,
        start_seconds UNINDEXED,
        end_seconds UNINDEXED,
        timing_precision UNINDEXED,
        chunker_version UNINDEXED,
        content_hash UNINDEXED,
        tokenize='porter unicode61'
    )
""")

# Insert into FTS5
for c in all_chunks:
    conn.execute(
        "INSERT INTO transcripts_fts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (c['id'], c['chunk_index'], c['title'], c['video_id'], c['playlist'],
         c['start_ts'], c['end_ts'], c['source_file'], c['text'],
         c['start_seconds'], c['end_seconds'], c['timing_precision'],
         c['chunker_version'], c['content_hash'])
    )

vc.store_embedding_metadata(conn, embedding_meta)
vc.store_schema_metadata(conn)
build_metadata = {
    'chunker_version': CHUNKER_VERSION,
    'semantic_similarity_threshold': str(SEMANTIC_THRESHOLD),
    'semantic_unit_tokens': str(SEMANTIC_UNIT_TOKENS),
    'minimum_semantic_split_tokens': str(MIN_SPLIT_TOKENS),
    'target_tokens': str(TARGET_TOKENS),
    'hard_max_tokens': str(HARD_MAX_TOKENS),
    'video_count': str(len(transcripts)),
    'indexed_video_count': str(len(transcripts) - len(parse_failures)),
    'chunk_count': str(len(all_chunks)),
    'build_git_sha': os.environ.get('ICT_BUILD_GIT_SHA', 'unknown'),
}
manifest = hashlib.sha256()
for fp in transcripts:
    manifest.update(fp.name.encode('utf-8'))
    manifest.update(hashlib.sha256(fp.read_bytes()).digest())
build_metadata['corpus_manifest_hash'] = manifest.hexdigest()
conn.executemany(
    "INSERT OR REPLACE INTO vault_metadata(key, value) VALUES (?, ?)",
    build_metadata.items(),
)
vc.verify_chunk_schema(conn)

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
    "SELECT title, snippet(transcripts_fts, 8, '<b>', '</b>', '...', 50) FROM transcripts_fts WHERE content MATCH ? LIMIT 3",
    (test_query,)
).fetchall()
print(f"  FTS5 search '{test_query}': {len(fts_results)} results")

# KG test
kg_results = conn.execute(
    "SELECT * FROM relations WHERE from_entity = 'FVG' OR to_entity = 'FVG'"
).fetchall()
print(f"  KG relations for FVG: {len(kg_results)}")

bad_ranges = conn.execute(
    "SELECT COUNT(*) FROM transcripts_fts "
    "WHERE CAST(end_seconds AS INTEGER) < CAST(start_seconds AS INTEGER)"
).fetchone()[0]
non_monotonic = conn.execute("""
    SELECT COUNT(*) FROM (
      SELECT source_file, chunk_index, CAST(start_seconds AS INTEGER) AS start_sec,
             LAG(CAST(start_seconds AS INTEGER)) OVER (
                 PARTITION BY source_file ORDER BY CAST(chunk_index AS INTEGER)
             ) AS previous_start
      FROM transcripts_fts
    ) WHERE previous_start IS NOT NULL AND start_sec < previous_start
""").fetchone()[0]
if bad_ranges or non_monotonic:
    raise RuntimeError(
        f"timestamp integrity failed: bad_ranges={bad_ranges}, non_monotonic={non_monotonic}")

fts_ids = {row[0] for row in conn.execute("SELECT chunk_id FROM transcripts_fts")}
chroma_ids = set(collection.get(include=[])['ids'])
if fts_ids != chroma_ids:
    raise RuntimeError(
        f"FTS/Chroma ID mismatch: fts={len(fts_ids)} chroma={len(chroma_ids)}")
print(f"  Timestamp integrity: 0 invalid, 0 non-monotonic")
print(f"  FTS/Chroma parity: {len(fts_ids):,} identical chunk IDs")
finalize_ingestion_attestation(
    conn,
    collection,
    resume_manifest_path,
    current_resume_manifest,
    expected_ids=fts_ids,
)
print(f"  Build identity: {build_id} attested complete across manifest, Chroma, and kg.db")

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
