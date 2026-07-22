"""
vault_core.py — Shared core for the ICT Knowledge Vault
========================================================
Single source of truth for everything the buyer-side tools need:

  * License loading & validation
  * Streaming vault decryption (low RAM) + integrity check
  * Vault format v1 (raw) and v2 (zstd-compressed) support
  * ICT shortform glossary (+ categories for real related terms)
  * Session detection, playlist classification, FTS sanitisation

mcp_server.py (and the benchmark harness) import from here so the decrypt logic can
never drift out of sync again.
"""

import io
import os
import re
import sys
import glob
import signal
import struct
import shutil
import sqlite3
import hashlib
import tarfile
import tempfile
import atexit
import secrets
import time
from collections import Counter, OrderedDict
from pathlib import Path

VAULT_DIR = Path(__file__).parent.resolve()
# Paths default to next-to-the-script, but can be overridden via env vars
# (handy for tests, and for buyers who keep the vault on another drive).
VAULT_FILE = Path(os.environ.get("ICT_VAULT_FILE", VAULT_DIR / "ict-vault.kevin"))
LICENSE_FILE = Path(os.environ.get("ICT_VAULT_LICENSE", VAULT_DIR / "license.key"))

TEMP_PREFIX = "ict_vault_"


def resolve_temp_base():
    """Return an explicit override or the platform's private temp directory."""
    base = os.environ.get("ICT_TEMP_DIR") or tempfile.gettempdir()
    os.makedirs(base, mode=0o700, exist_ok=True)
    return str(Path(base).resolve())


_TEMP_BASE = resolve_temp_base()
_CHUNK = 4 * 1024 * 1024  # 4 MB streaming chunk
MIN_RERANK_SCORE = 0.0
MMR_LAMBDA = 0.7
SEARCH_CACHE_MAX = 100
SNIPPET_DEFAULT_CHARS = 500
SNIPPET_MAX_CHARS = 1000
CONTEXT_BEFORE_MAX_CHARS = 500
CONTEXT_CURRENT_MAX_CHARS = 1000
CONTEXT_AFTER_MAX_CHARS = 500
CONTEXT_TOTAL_MAX_CHARS = 2000
MAX_QUERY_VARIANTS = 4
MAX_TOP_K = 25
RESEARCH_MAX_TOP_K = 50
RESULT_REF_TTL_SECONDS = 15 * 60
RESULT_REF_MAX_USES = 1

# Agent Layer v1.1a — source diversity (video-level)
MAX_RESULTS_PER_VIDEO = 2
MERGE_GAP_SEC = 90
DISTINCT_GAP_SEC = 600
# Rerank a larger pool, then diversify down to top_k
DIVERSITY_RERANK_POOL_MULT = 3
DIVERSITY_RERANK_POOL_EXTRA = 8

EMBEDDING_MODEL_KEY = "embedding_model_name"
EMBEDDING_DIM_KEY = "embedding_dimension"
EMBEDDING_NORMALIZE_KEY = "embedding_normalize"
EMBEDDING_REVISION_KEY = "embedding_revision"
QUERY_INSTRUCTION_KEY = "query_instruction_version"
VECTOR_SCHEMA_KEY = "vector_schema_version"
CHUNK_SCHEMA_KEY = "chunk_schema_version"
CHUNK_ID_STRATEGY_KEY = "chunk_id_strategy"
SNIPPET_DEFAULT_KEY = "snippet_default_chars"
SNIPPET_MAX_KEY = "snippet_max_chars"
CONTEXT_MAX_KEY = "context_max_chars"

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
QUERY_INSTRUCTION_VERSION = "bge-v1.5-no-query-instruction-v1"
VECTOR_SCHEMA_VERSION = "3"  # v3 = documentless Chroma (transcript text lives only in the SQLite/FTS DB)
CHUNK_SCHEMA_VERSION = "3"
CHUNK_ID_STRATEGY = "sha1-source-chunker-times-content-v3"

EMBEDDING_SPECS = {
    "BAAI/bge-small-en-v1.5": {
        EMBEDDING_MODEL_KEY: "BAAI/bge-small-en-v1.5",
        EMBEDDING_DIM_KEY: "384",
        EMBEDDING_NORMALIZE_KEY: "true",
        EMBEDDING_REVISION_KEY: DEFAULT_EMBEDDING_REVISION,
        QUERY_INSTRUCTION_KEY: QUERY_INSTRUCTION_VERSION,
        VECTOR_SCHEMA_KEY: VECTOR_SCHEMA_VERSION,
    },
    "all-MiniLM-L6-v2": {
        EMBEDDING_MODEL_KEY: "all-MiniLM-L6-v2",
        EMBEDDING_DIM_KEY: "384",
        EMBEDDING_NORMALIZE_KEY: "true",
        EMBEDDING_REVISION_KEY: "chroma-onnx-all-MiniLM-L6-v2",
        QUERY_INSTRUCTION_KEY: "none-v1",
        VECTOR_SCHEMA_KEY: VECTOR_SCHEMA_VERSION,
    },
}

CASE_INSENSITIVE_SHORTFORMS = {"FVG", "IFVG", "BISI", "SIBI"}

# Vault format versions understood by this build.
FORMAT_V1_RAW = 1   # [ver:4][db_size:8][chroma_size:8][db][chroma]
FORMAT_V2_ZSTD = 2  # [ver:4][db_size:8][chroma_size:8][ zstd(db + chroma) ]
HEADER = struct.Struct(">IQQ")  # 20 bytes


# ── Errors ───────────────────────────────────────────────────────────────────
class VaultError(Exception):
    """A buyer-facing problem with a clear, actionable message."""


# ── Temp lifecycle ───────────────────────────────────────────────────────────
_temp_dirs = []
vault_hash = ""
_vault_embedding_cache_fingerprint = ""
_search_cache = OrderedDict()
_search_cache_hits = 0
_search_cache_misses = 0


def _cleanup_temp(*_args):
    for d in list(_temp_dirs):
        try:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
        finally:
            if d in _temp_dirs:
                _temp_dirs.remove(d)


def sweep_stale_temp():
    """Remove decrypted vaults left behind by crashed previous runs.

    atexit does not fire on SIGKILL / power loss, so a crashed session can
    leave a full plaintext copy of the vault on disk. Clean those on startup.
    """
    root = _TEMP_BASE
    for path in glob.glob(os.path.join(root, TEMP_PREFIX + "*")):
        if path in _temp_dirs:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


def _install_cleanup_handlers():
    atexit.register(_cleanup_temp)
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            prev = signal.getsignal(sig)

            def handler(signum, frame, _prev=prev):
                _cleanup_temp()
                if callable(_prev):
                    _prev(signum, frame)
                else:
                    raise KeyboardInterrupt()

            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not in main thread (e.g. under some MCP hosts) — atexit still covers us.
            pass


_install_cleanup_handlers()


# ── ICT Shortform Glossary (single source of truth, grouped by category) ─────
# Categories give us *real* related-term suggestions instead of "first 5 keys".
ICT_GLOSSARY = {
    "Market Structure": {
        'MS': 'Market Structure — The overall trend and key swing points on a chart',
        'MSS': 'Market Structure Shift — Change from bullish to bearish (or vice versa)',
        'BMS': 'Break in Market Structure — Price breaks a key structural level',
        'BOS': 'Break of Structure — Price breaking a key swing high/low (SMC term, not original ICT)',
        'CHoCH': 'Change of Character — Confirmed shift in market structure (SMC term, not original ICT)',
        'HH': 'Higher High — Each successive peak is higher than the last (uptrend)',
        'HL': 'Higher Low — Each successive trough is higher than the last (uptrend)',
        'LH': 'Lower High — Each successive peak is lower than the last (downtrend)',
        'LL': 'Lower Low — Each successive trough is lower than the last (downtrend)',
    },
    "Liquidity": {
        'BSL': 'Buy Side Liquidity — Stops above highs, targeted by sell programs',
        'SSL': 'Sell Side Liquidity — Stops below lows, targeted by buy programs',
        'ERL': 'External Range Liquidity — Stops outside established range extremes',
        'IRL': 'Internal Range Liquidity — Liquidity within the current dealing range',
        'EQH': 'Equal Highs — Two or more swing highs at the same level, liquidity target',
        'EQL': 'Equal Lows — Two or more swing lows at the same level, liquidity target',
        'REH': 'Relative Equal Highs — Roughly equal swing highs forming a buy-side liquidity pool',
        'REL': 'Relative Equal Lows — Roughly equal swing lows forming a sell-side liquidity pool',
        'PDH': 'Previous Day High — Yesterday\'s highest price, often a liquidity level',
        'PDL': 'Previous Day Low — Yesterday\'s lowest price, often a liquidity level',
        'PWH': 'Previous Week High — Last week\'s highest price',
        'PWL': 'Previous Week Low — Last week\'s lowest price',
        'PMH': 'Previous Month High — Last month\'s highest price',
        'PML': 'Previous Month Low — Last month\'s lowest price',
        'BS': 'Buy Stops — Buy orders triggered above current price (stop losses)',
        'SS': 'Sell Stops — Sell orders triggered below current price (stop losses)',
        'LIQUIDITY': 'Liquidity — Pool of stop-loss orders above highs (BSL) or below lows (SSL)',
        'LS': 'Liquidity Sweep — Price briefly moves into liquidity before reversing',
        'DOL': 'Draw on Liquidity — Price probing toward a liquidity pool before reacting',
    },
    "Fair Value Concepts": {
        'FVG': 'Fair Value Gap — 3-candle imbalance pattern created by aggressive price movement',
        'IFVG': 'Inverse Fair Value Gap — FVG in the opposite direction, used for reversal entries',
        'BISI': 'Buy Side Imbalance Sell Side Inefficiency — FVG where buying inefficiency remains',
        'SIBI': 'Sell Side Imbalance Buy Side Inefficiency — FVG where selling inefficiency remains',
        'VI': 'Volume Imbalance — Abnormal volume indicating aggressive buying/selling',
        'CE': 'Consequent Encroachment — 50% retracement level of an FVG or imbalance',
    },
    "Order Blocks": {
        'OB': 'Order Block — A consolidation zone where institutional orders sit',
        'BB': 'Breaker Block — An order block that has been broken and now acts as flipped support/resistance',
        'BREAKER BLOCK': 'Breaker Block — A specific candlestick pattern indicating a shift in market structure',
        'MB': 'Mitigation Block — An order block partly mitigated by price returning to it',
        'RB': 'Rejection Block — An order block where price strongly rejected on first touch',
        'PB': 'Propulsion Block — A strong order block that propelled price through multiple levels',
    },
    "Premium & Discount": {
        'PD Array': 'Price Delivery Array — Set of levels where price is expected to react',
        'OTE': 'Optimal Trade Entry — Entry zone at 61.8-79% retracement of a move',
        'EQ': 'Equilibrium — Midpoint of a range (50% level), acts as magnet for price',
        'OTE Zone': 'Optimal Trade Entry Zone — 62%-79% retracement region for entries',
        'OTE RANGE': 'OTE Range — The 62-79% retracement zone for Optimal Trade Entry',
        'M50': 'Mean Threshold 50% — The 50% retracement level, midpoint of a range',
        'PD': 'Premium / Discount — Above equilibrium (premium/sell) vs below (discount/buy)',
    },
    "Price Delivery": {
        'IPDA': 'Interbank Price Delivery Algorithm — ICT\'s model of how institutional price moves',
        'AMD': 'Accumulation, Manipulation, Distribution — The three-phase market cycle',
        'MMSM': 'Market Maker Sell Model — Institutional sell-side liquidity algorithm',
        'MMBM': 'Market Maker Buy Model — Institutional buy-side liquidity algorithm',
        'MMXM': 'Market Maker Model — The general market maker buy/sell model (buy or sell variant)',
        'MM': 'Market Maker — Major institutions moving price to access liquidity',
    },
    "Time Concepts": {
        'HTF': 'Higher Time Frame — Chart timeframes above your trading timeframe',
        'LTF': 'Lower Time Frame — Chart timeframes below your trading timeframe',
        'MTF': 'Multiple Time Frame — Analyzing across multiple chart timeframes for confluences',
    },
    "Kill Zones / Sessions": {
        'AZ': 'Asian Range — Price range during the Asian trading session',
        'KZ': 'Kill Zone — Specific time window when ICT expects institutional moves',
        'KILL ZONE': 'Kill Zone — Specific time windows (London, New York, Asian) where ICT trades',
        'LO': 'London Open — The opening of the London session (key ICT timing)',
        'NYO': 'New York Open — The opening of the NY session, often after London',
        'LC': 'London Close — The close of the London session, often overlapping with NY',
        'ADR': 'Average Daily Range — The average daily price movement range from recent days',
        'ODR': 'Opening Range — Price range established at the beginning of a session',
    },
    "Dealing Range": {
        'DR': 'Dealing Range — Price range where institutional orders are being built',
        'BPR': 'Balanced Price Range — Range where buy/sell orders are balanced',
    },
    "Fibonacci": {
        '0.50': 'Equilibrium (50%) — The midpoint retracement level, often acted on',
        '0.62': 'OTE Beginning (62%) — Start of the Optimal Trade Entry zone',
        '0.705': 'Optimal Entry (70.5%) — ICT\'s preferred specific entry level in OTE zone',
        '0.79': 'Deep Discount/Premium (79%) — The deepest retracement before a reversal',
    },
    "ICT Models": {
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
        'POWER OF 3': 'Power of 3 — Accumulation, Manipulation, Distribution (AMD) cycle',
        'CISD': 'Change In State of Delivery — Shift in how price is being delivered (fast vs slow)',
        'ORB': 'Opening Range Break — A break of the range set at the open of a session',
    },
    "Candlestick & Price Action": {
        'Displacement': 'Displacement — Strong impulsive candle beyond the recent range',
        'Retracement': 'Retracement — A pullback against the current trend direction',
        'Expansion': 'Expansion — Strong directional move after consolidation',
        'Raid': 'Raid — A liquidity sweep into a pool of stops before reversing',
        'Repricing': 'Repricing — Fast displacement into a new price discovery area',
        'Rebalancing': 'Rebalancing — Price returning to fill inefficiencies (FVGs, OBs)',
    },
    "Economic / News": {
        'CPI': 'Consumer Price Index — Key inflation report affecting all markets',
        'PPI': 'Producer Price Index — Wholesale inflation gauge',
        'NFP': 'Non-Farm Payrolls — Monthly US employment report (high impact)',
        'FOMC': 'Federal Open Market Committee — US Fed rate decision (high impact)',
        'COT': 'Commitment of Traders — Weekly report showing positions of commercial & retail traders',
    },
    "Misc": {
        'SMT': 'Smart Money Technique / Synchronicity — Divergence between correlated assets',
        'SMT Div': 'SMT Divergence — Price difference between two correlated instruments',
        'SMC': 'Smart Money Concepts — The broader trading community term for ICT-style trading',
        'Judas Swing': 'Judas Swing — A brief false move to trap traders before the real move',
        'IB': 'Initial Balance — The first hour(s) range, used to project the day',
        'LVN': 'Low Volume Node — Price level with minimal historical trading activity',
        'HVN': 'High Volume Node — Price level with heavy historical trading activity',
    },
    "Timeframes": {
        'H4': '4-hour chart timeframe',
        'H1': '1-hour chart timeframe',
        'M15': '15-minute chart timeframe',
        'M5': '5-minute chart timeframe',
        'M1': '1-minute chart timeframe',
    },
    "General": {
        'RTH': 'Regular Trading Hours — Standard market session (9:30am-4pm ET)',
        'ETH': 'Electronic Trading Hours — Extended hours, includes overnight trading',
        'YY': 'Year — Usually refers to ICT Mentorship year (e.g. YY 2022, YY 2023)',
    },
}

# Flat lookup + reverse category map, derived from the grouped source.
ICT_SHORTFORMS = {}
_TERM_CATEGORY = {}
for _cat, _terms in ICT_GLOSSARY.items():
    for _k, _v in _terms.items():
        ICT_SHORTFORMS[_k] = _v
        _TERM_CATEGORY[_k] = _cat


def related_terms(term, limit=5):
    """Real related terms: other shortforms in the same category."""
    cat = _TERM_CATEGORY.get(term)
    if not cat:
        return []
    return [k for k in ICT_GLOSSARY[cat] if k != term][:limit]


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
    text_lower = (text or "").lower()
    matches = []
    for session, keywords in SESSION_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            matches.append(session)
    return matches


def classify_playlist(name):
    """Single source of truth for filename → playlist. Used by ingest & build."""
    if '2023 ICT Mentorship' in name:
        return '2023 ICT Mentorship'
    if '2025 Lecture Series' in name:
        return '2025 Lecture Series'
    if 'ICT 2024 Mentorship' in name:
        return 'ICT 2024 Mentorship'
    if '2026' in name and 'SMC' in name:
        return '2026 SMC Lecture'
    if '2022 ICT Mentorship' in name:
        return '2022 ICT Mentorship'
    if '2016' in name or '2017' in name:
        return '2016/2017 Mentorship'
    if 'Forex' in name:
        return 'Forex Series'
    if 'Storytellers' in name:
        return '2025 Storytellers'
    if 'Charter' in name:
        return 'ICT Charter Content'
    return 'Other / Misc'


class _SentenceTransformerEF:
    def __init__(self, model_name, revision, normalize_embeddings):
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name, revision=revision)
        except Exception as e:
            raise VaultError(
                _embedding_error_message(model_name, EMBEDDING_SPECS[model_name][EMBEDDING_DIM_KEY],
                                         "unavailable", "0") +
                f" Install/compatibility detail: {e}"
            )
        self.model_name = model_name
        self.revision = revision
        self.normalize_embeddings = normalize_embeddings

    def __call__(self, input):
        texts = [input] if isinstance(input, str) else list(input)
        embeddings = self._model.encode(
            texts,
            normalize_embeddings=self.normalize_embeddings,
        )
        return embeddings.tolist() if hasattr(embeddings, "tolist") else embeddings

    def embed_documents(self, input):
        texts = [input] if isinstance(input, str) else list(input)
        embeddings = self._model.encode(
            texts,
            normalize_embeddings=self.normalize_embeddings,
        )
        return embeddings.tolist() if hasattr(embeddings, "tolist") else embeddings

    def embed_query(self, input):
        # ChromaDB 0.6+ calls embed_query(input=...) with keyword arg
        if isinstance(input, dict):
            input = input.get("input", list(input.values())[0])
        embedding = self._model.encode(
            input,
            normalize_embeddings=self.normalize_embeddings,
        )
        return embedding.tolist() if hasattr(embedding, "tolist") else embedding

    def name(self):
        return f"{self.model_name}@{self.revision}"


def configured_embedding_metadata(model_name=None):
    model_name = model_name or os.environ.get("ICT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    if model_name not in EMBEDDING_SPECS:
        raise VaultError(f"Unsupported embedding model for production: {model_name}")
    meta = dict(EMBEDDING_SPECS[model_name])
    if model_name == DEFAULT_EMBEDDING_MODEL:
        meta[EMBEDDING_REVISION_KEY] = os.environ.get(
            "ICT_EMBEDDING_REVISION", DEFAULT_EMBEDDING_REVISION)
    return meta


def embedding_metadata_rows(meta):
    return [(k, str(meta[k])) for k in (
        EMBEDDING_MODEL_KEY,
        EMBEDDING_DIM_KEY,
        EMBEDDING_NORMALIZE_KEY,
        EMBEDDING_REVISION_KEY,
        QUERY_INSTRUCTION_KEY,
        VECTOR_SCHEMA_KEY,
    )]


def schema_metadata_rows():
    return [
        (CHUNK_SCHEMA_KEY, CHUNK_SCHEMA_VERSION),
        (CHUNK_ID_STRATEGY_KEY, CHUNK_ID_STRATEGY),
        (SNIPPET_DEFAULT_KEY, str(SNIPPET_DEFAULT_CHARS)),
        (SNIPPET_MAX_KEY, str(SNIPPET_MAX_CHARS)),
        (CONTEXT_MAX_KEY, str(CONTEXT_TOTAL_MAX_CHARS)),
    ]


def store_embedding_metadata(conn, meta):
    conn.execute("CREATE TABLE IF NOT EXISTS vault_metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT OR REPLACE INTO vault_metadata (key, value) VALUES (?, ?)",
        embedding_metadata_rows(meta),
    )


def store_schema_metadata(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS vault_metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT OR REPLACE INTO vault_metadata (key, value) VALUES (?, ?)",
        schema_metadata_rows(),
    )


def verify_chunk_schema(db):
    cols = set(_fts_columns(db))
    missing = [c for c in ('chunk_id', 'chunk_index', 'end_ts') if c not in cols]
    if missing:
        raise VaultError(
            "transcripts_fts is missing required chunk schema columns: "
            + ", ".join(missing)
            + ". Run ict_ingest.py before build.py."
        )
    try:
        row = db.execute(
            "SELECT chunk_id, chunk_index, end_ts FROM transcripts_fts LIMIT 1"
        ).fetchone()
    except sqlite3.Error as e:
        raise VaultError(f"Unable to verify transcripts_fts chunk schema: {e}") from e
    if row and (not row[0] or row[1] is None or row[2] in (None, '')):
        raise VaultError(
            "transcripts_fts has empty chunk_id, chunk_index, or end_ts values. "
            "Run ict_ingest.py before build.py."
        )


def read_embedding_metadata(db):
    try:
        rows = dict(db.execute(
            "SELECT key, value FROM vault_metadata WHERE key IN (?,?,?,?,?,?)",
            (
                EMBEDDING_MODEL_KEY,
                EMBEDDING_DIM_KEY,
                EMBEDDING_NORMALIZE_KEY,
                EMBEDDING_REVISION_KEY,
                QUERY_INSTRUCTION_KEY,
                VECTOR_SCHEMA_KEY,
            ),
        ).fetchall())
    except sqlite3.Error:
        return {}
    return rows


def _embedding_error_message(required_model, required_dim, actual_model, actual_dim):
    return (f"This vault requires {required_model} ({required_dim}-dim). "
            f"Loaded: {actual_model} ({actual_dim}-dim). "
            "Please install the correct model.")


def _embedding_dim(embedding_function):
    return len(embedding_function(["dimension check"])[0])


def _normalize_bool(value):
    return "true" if str(value).strip().lower() in ("1", "true", "yes") else "false"


def get_embedding_function(required_metadata=None, return_metadata=False):
    """Load exactly the requested embedding model. No dimensional fallback."""
    required = dict(required_metadata or configured_embedding_metadata())
    model_name = required.get(EMBEDDING_MODEL_KEY, DEFAULT_EMBEDDING_MODEL)
    if model_name not in EMBEDDING_SPECS:
        raise VaultError(f"Unsupported embedding model for this vault: {model_name}")

    spec = dict(EMBEDDING_SPECS[model_name])
    spec.update({k: str(v) for k, v in required.items() if v is not None})
    normalize = _normalize_bool(spec.get(EMBEDDING_NORMALIZE_KEY)) == "true"

    if model_name == DEFAULT_EMBEDDING_MODEL:
        ef = _SentenceTransformerEF(
            model_name,
            spec.get(EMBEDDING_REVISION_KEY, DEFAULT_EMBEDDING_REVISION),
            normalize,
        )
    else:
        try:
            from chromadb.utils import embedding_functions
            ef = embedding_functions.ONNXMiniLM_L6_V2()
        except Exception as e:
            raise VaultError(
                _embedding_error_message(model_name, spec[EMBEDDING_DIM_KEY], "unavailable", "0") +
                f" Install/compatibility detail: {e}"
            )

    actual_dim = str(_embedding_dim(ef))
    actual = dict(spec)
    actual[EMBEDDING_DIM_KEY] = actual_dim
    if actual.get(EMBEDDING_MODEL_KEY) != model_name:
        actual[EMBEDDING_MODEL_KEY] = model_name

    if return_metadata:
        return ef, actual
    return ef


def _set_vault_embedding_cache_metadata(meta):
    global _vault_embedding_cache_fingerprint
    fingerprint = ""
    if meta:
        fingerprint = f"{meta.get(EMBEDDING_MODEL_KEY, '')}:{meta.get(EMBEDDING_DIM_KEY, '')}"
    if fingerprint != _vault_embedding_cache_fingerprint:
        clear_search_cache()
        _vault_embedding_cache_fingerprint = fingerprint


def validate_embedding_compatibility(db, chroma_dir=None, require_metadata=False):
    """Load and validate the exact embedding model required by the vault."""
    if chroma_dir is not None:
        sqlite_path = Path(chroma_dir) / "chroma.sqlite3"
        if sqlite_path.exists() and not chroma_store_usable(chroma_dir):
            return None

    required = read_embedding_metadata(db)
    _set_vault_embedding_cache_metadata(required)
    if not required:
        if require_metadata:
            raise VaultError(
                "This vault is missing embedding compatibility metadata. "
                "Rebuild it with the current ict_ingest.py."
            )
        return None

    required_model = required.get(EMBEDDING_MODEL_KEY, "")
    required_dim = str(required.get(EMBEDDING_DIM_KEY, ""))
    ef, actual = get_embedding_function(required, return_metadata=True)
    actual_model = actual.get(EMBEDDING_MODEL_KEY, "")
    actual_dim = str(actual.get(EMBEDDING_DIM_KEY, ""))

    if actual_model != required_model or actual_dim != required_dim:
        raise VaultError(_embedding_error_message(
            required_model, required_dim, actual_model, actual_dim))
    return ef


# Words that shouldn't constrain a keyword search (recall-oriented FTS).
_FTS_STOP = {
    "a", "an", "the", "of", "to", "is", "are", "was", "were", "be", "what",
    "whats", "what's", "how", "why", "when", "where", "do", "does", "did",
    "in", "on", "for", "and", "or", "vs", "about", "me", "my", "i", "it",
    "that", "this", "he", "she", "they", "say", "says", "said", "explain",
    "tell", "define", "definition", "mean", "means", "get", "with",
}


def demo_info(db):
    """Return {'count', 'total', 'cta'} if this is a demo vault, else None.

    Demo vaults are built by store/build_demo.py, which stamps vault_metadata.
    """
    try:
        rows = dict(db.execute(
            "SELECT key, value FROM vault_metadata WHERE key IN "
            "('demo','demo_count','demo_total','demo_cta')").fetchall())
    except sqlite3.Error:
        return None
    if rows.get('demo') != '1':
        return None
    return {
        'count': rows.get('demo_count', '?'),
        'total': rows.get('demo_total', '775'),
        'cta': rows.get('demo_cta', 'https://YOUR-SITE/#pricing'),
    }


def youtube_link(video_id, start_ts=None, start_seconds=None):
    """Deep link to the exact moment: https://youtu.be/ID?t=SECONDS.

    Prefer canonical stored ``start_seconds`` when supplied. ``start_ts`` is a
    legacy/display fallback for older rows that do not carry numeric provenance.
    """
    if not video_id:
        return ""
    base = f"https://youtu.be/{video_id}"
    if start_seconds is not None:
        try:
            secs = int(start_seconds)
        except (TypeError, ValueError):
            return base
        return f"{base}?t={secs}" if secs > 0 else base
    if not start_ts:
        return base
    try:
        parts = [int(p) for p in str(start_ts).strip().split(":")]
    except ValueError:
        return base
    if not 1 < len(parts) <= 3:
        return base
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return f"{base}?t={secs}" if secs > 0 else base


def sanitize_fts(query, mode="or"):
    """Make arbitrary user input safe for an FTS5 MATCH.

    Bare input like `buy-side liquidity` or `what's an order block?` is invalid
    FTS5 syntax and raises. We quote every token as a phrase (escaping embedded
    quotes) and join with OR for recall — the cross-encoder reranker restores
    precision. Common question words are dropped so they don't over-constrain.
    Returns None if nothing searchable remains.
    """
    if not query:
        return None
    tokens = []
    for tok in query.split():
        cleaned = tok.strip().strip('?!.,;:()"\'')
        if cleaned:
            tokens.append(cleaned)
    if not tokens:
        return None
    keep = [t for t in tokens if t.lower() not in _FTS_STOP]
    if not keep:  # query was all stopwords — keep them rather than match nothing
        keep = tokens
    joiner = " OR " if mode == "or" else " "
    return joiner.join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in keep)


def expand_query(query):
    """Expand ICT acronyms to their full term.

    FVG/IFVG/BISI/SIBI are unambiguous, so case does not matter. Ambiguous
    shortforms like MS/BS/CE still require the user's all-caps spelling.
    """
    if not query:
        return query, False
    out = []
    changed = False
    for tok in query.split():
        core = tok.strip('?!.,;:()')
        lookup = core.upper() if core else ""
        should_expand = (
            lookup in CASE_INSENSITIVE_SHORTFORMS
            or (core and core == core.upper() and core in ICT_SHORTFORMS)
        )
        key = lookup if lookup in CASE_INSENSITIVE_SHORTFORMS else core
        if should_expand and key in ICT_SHORTFORMS:
            full = ICT_SHORTFORMS[key].split(' — ')[0].split(' / ')[0].strip()
            out.append(tok)
            out.append(full)
            changed = True
        else:
            out.append(tok)
    expanded = ' '.join(out)
    return expanded, changed


_TIMESTAMP_LINE = re.compile(r'^(\d+:\d{2}(?::\d{2})?)\s+')


def line_timestamp(line):
    m = _TIMESTAMP_LINE.match((line or "").strip())
    return m.group(1) if m else None


def _first_timestamp(lines):
    for line in lines:
        ts = line_timestamp(line)
        if ts:
            return ts
    return None


def _last_timestamp(lines):
    for line in reversed(lines):
        ts = line_timestamp(line)
        if ts:
            return ts
    return None


def _overlap_lines(lines, max_chars):
    if max_chars <= 0:
        return []
    out = []
    total = 0
    for line in reversed(lines):
        if not line_timestamp(line):
            continue
        add = len(line) + (1 if out else 0)
        if out and total + add > max_chars:
            break
        out.append(line)
        total += add
        if total >= max_chars:
            break
    return list(reversed(out))


def chunk_transcript_body(body, chunk_size=900, overlap_chars=100):
    """Split timestamped transcript text without inventing overlap timestamps."""
    chunks = []
    current = []
    current_len = 0
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        ts = line_timestamp(line)
        if ts and current and current_len > chunk_size:
            chunks.append({
                "text": "\n".join(current),
                "start_ts": _first_timestamp(current) or "0:00",
                "end_ts": _last_timestamp(current) or _first_timestamp(current) or "0:00",
            })
            current = _overlap_lines(current, overlap_chars) + [line]
            current_len = len("\n".join(current))
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append({
            "text": "\n".join(current),
            "start_ts": _first_timestamp(current) or "0:00",
            "end_ts": _last_timestamp(current) or _first_timestamp(current) or "0:00",
        })
    return chunks


def stable_chunk_id(source_file, chunk_index, start_ts):
    raw = f"{source_file}\n{chunk_index}\n{start_ts or ''}".encode("utf-8", errors="replace")
    return "ck_" + hashlib.sha1(raw).hexdigest()[:20]


# ── Build-side packaging (used by build.py; keeps format symmetric) ──────────
def pack_and_encrypt(db_bytes, chroma_bytes, compress=True, level=19, vault_key=None):
    """Package db+chroma, optionally zstd-compress, then AES-256-CTR encrypt.

    Returns (encrypted_blob, vault_key, sha256_hex). The blob layout is
    [iv:16][ciphertext] where the plaintext is the versioned header + body.

    Pass an existing 32-byte `vault_key` to keep the key STABLE across rebuilds
    — this is what lets you ship new videos to existing buyers without
    re-licensing them (their license wraps this same key). Omit it to mint a
    fresh key (first build, or a deliberate security rotation).
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    import secrets

    if compress:
        import zstandard
        cctx = zstandard.ZstdCompressor(level=level)
        body = cctx.compress(db_bytes + chroma_bytes)
        version = FORMAT_V2_ZSTD
    else:
        body = db_bytes + chroma_bytes
        version = FORMAT_V1_RAW

    plaintext = HEADER.pack(version, len(db_bytes), len(chroma_bytes)) + body

    if vault_key is None:
        vault_key = secrets.token_bytes(32)
    elif len(vault_key) != 32:
        raise ValueError(f"vault_key must be 32 bytes, got {len(vault_key)}")
    iv = secrets.token_bytes(16)
    cipher = Cipher(algorithms.AES(vault_key), modes.CTR(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    blob = iv + ciphertext
    return blob, vault_key, hashlib.sha256(blob).hexdigest()


# ── License ──────────────────────────────────────────────────────────────────
def load_license(license_file=LICENSE_FILE):
    if not license_file.exists():
        raise VaultError(
            "license.key not found.\n"
            "  Place the license.key we sent you next to mcp_server.py, then try again."
        )
    info = {}
    with open(license_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            info[k.strip()] = v.strip()
    if not info.get('BUYER_KEY') or not info.get('ENCRYPTED_VAULT_KEY'):
        raise VaultError(
            "license.key is invalid or incomplete (missing key fields).\n"
            "  Re-download the license.key from your purchase email."
        )
    return info


def _unwrap_vault_key(info):
    from cryptography.fernet import Fernet
    try:
        buyer_cipher = Fernet(info['BUYER_KEY'].encode())
        return buyer_cipher.decrypt(info['ENCRYPTED_VAULT_KEY'].encode())
    except Exception:
        raise VaultError(
            "Could not unlock the vault — this license.key does not match this vault.\n"
            "  Make sure license.key and ict-vault.kevin are from the same purchase."
        )


# ── Streaming decrypt router ─────────────────────────────────────────────────
class _SplitSink:
    """Routes a decompressed byte stream into master.db then chroma.tar by size."""

    def __init__(self, db_path, chroma_tar_path, db_size, chroma_size):
        self._db = open(db_path, 'wb')
        self._chroma = open(chroma_tar_path, 'wb')
        self._db_left = db_size
        self._chroma_left = chroma_size

    def write(self, data):
        if not data:
            return
        if self._db_left > 0:
            take = min(self._db_left, len(data))
            self._db.write(data[:take])
            self._db_left -= take
            data = data[take:]
        if data and self._chroma_left > 0:
            take = min(self._chroma_left, len(data))
            self._chroma.write(data[:take])
            self._chroma_left -= take

    def close(self):
        self._db.close()
        self._chroma.close()


def _copy_and_hash_encrypted(source, destination):
    """Copy ciphertext into private temp storage and return its SHA-256."""
    hasher = hashlib.sha256()
    with open(source, 'rb') as src, open(destination, 'xb') as dst:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    return hasher.hexdigest()


def _decrypt_stream(vault_key, encrypted_file, on_progress=None):
    """Yield decrypted plaintext chunks while verifying the file hash.

    Reads the encrypted file once. Never holds the whole file (or the whole
    plaintext) in memory. Returns the computed sha256 via the final yield's
    StopIteration value is awkward, so we stash it on the generator object.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    total = os.path.getsize(encrypted_file)
    hasher = hashlib.sha256()
    done = 0
    with open(encrypted_file, 'rb') as f:
        iv = f.read(16)
        if len(iv) < 16:
            raise VaultError("Vault file is truncated or corrupted. Please re-download ict-vault.kevin.")
        hasher.update(iv)
        done += 16
        cipher = Cipher(algorithms.AES(vault_key), modes.CTR(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)
            yield decryptor.update(chunk)
        tail = decryptor.finalize()
        if tail:
            yield tail
    _decrypt_stream.last_hash = hasher.hexdigest()


def open_vault(vault_file=VAULT_FILE, license_file=LICENSE_FILE, on_progress=None):
    """Decrypt the vault to a fresh temp dir and return (db, chroma_dir, licensed_to).

    Streaming + low memory. Supports format v1 (raw) and v2 (zstd). Verifies
    the vault hash from the license when present.
    """
    # ── Guard: reject direct imports from scripts / interactive shells ──────
    # Only mcp_server.py (or test files with $ICT_OPEN_VAULT_BYPASS=1) may
    # call open_vault.  This prevents a casual "copy-paste the function name"
    # bulk dump.  A determined coder can still bypass it (the source is
    # readable), but the guard raises the effort from ~10 s to ~5 min.
    if not os.environ.get("ICT_OPEN_VAULT_BYPASS"):
        import traceback as _tb
        _stack = _tb.extract_stack()
        _caller_files = {_f.filename.replace("\\", "/") for _f in _stack}
        if not any(x in _f for x in ("mcp_server.py", "test_") for _f in _caller_files):
            raise RuntimeError(
                "Direct vault access is not supported. "
                "Use the ICT Knowledge Vault MCP server to query transcripts."
            )

    global vault_hash
    if not vault_file.exists():
        raise VaultError(
            "ict-vault.kevin not found next to mcp_server.py.\n"
            "  Make sure the vault file downloaded fully and sits in this folder."
        )

    info = load_license(license_file)
    vault_key = _unwrap_vault_key(info)
    expected_hash = info.get('VAULT_HASH', '').strip().lower()
    if len(expected_hash) != 64 or any(c not in '0123456789abcdef' for c in expected_hash):
        raise VaultError("License is missing a valid VAULT_HASH. Request a replacement license.")

    tmpdir = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=_TEMP_BASE)
    _temp_dirs.append(tmpdir)
    authenticated_vault = os.path.join(tmpdir, 'vault.authenticated')
    computed_before_decrypt = _copy_and_hash_encrypted(vault_file, authenticated_vault)
    if not secrets.compare_digest(computed_before_decrypt, expected_hash):
        shutil.rmtree(tmpdir, ignore_errors=True)
        _temp_dirs.remove(tmpdir)
        raise VaultError("Vault file failed its integrity check (corrupted download). Please re-download ict-vault.kevin.")
    db_fd, db_path = tempfile.mkstemp(prefix='sqlite_', suffix='.db', dir=tmpdir)
    os.close(db_fd)
    chroma_dir = os.path.join(tmpdir, 'chroma')
    os.makedirs(chroma_dir, exist_ok=True)
    chroma_tar_path = os.path.join(tmpdir, 'chroma.tar')

    # 1) Read the 20-byte header first (needs only the first plaintext chunk).
    stream = _decrypt_stream(vault_key, authenticated_vault, on_progress)
    buffer = bytearray()
    try:
        while len(buffer) < HEADER.size:
            buffer += next(stream)
    except StopIteration:
        raise VaultError("Vault file is too small or corrupted. Please re-download ict-vault.kevin.")

    version, db_size, chroma_size = HEADER.unpack(bytes(buffer[:HEADER.size]))
    body_prefix = bytes(buffer[HEADER.size:])

    if version not in (FORMAT_V1_RAW, FORMAT_V2_ZSTD):
        raise VaultError(
            f"This vault uses format v{version}, which this app version does not understand.\n"
            "  Please update to the latest ICT Vault release."
        )

    # 2) Route the rest of the stream into a short-lived SQLite file + chroma.tar.
    sink = _SplitSink(db_path, chroma_tar_path, db_size, chroma_size)
    try:
        if version == FORMAT_V1_RAW:
            sink.write(body_prefix)
            for chunk in stream:
                sink.write(chunk)
        else:  # FORMAT_V2_ZSTD
            import zstandard
            dctx = zstandard.ZstdDecompressor()
            decompressor = dctx.decompressobj()
            sink.write(decompressor.decompress(body_prefix))
            for chunk in stream:
                sink.write(decompressor.decompress(chunk))
            sink.write(decompressor.flush())
    finally:
        sink.close()

    # 3) Defend against any authenticated-copy corruption during decryption.
    computed = getattr(_decrypt_stream, "last_hash", "")
    if not computed or not secrets.compare_digest(computed, expected_hash):
        raise VaultError("Vault file failed its integrity check (corrupted download). Please re-download ict-vault.kevin.")
    try:
        os.remove(authenticated_vault)
    except OSError:
        pass
    if computed != vault_hash:
        clear_search_cache()
        vault_hash = computed

    # 4) Extract ChromaDB tar safely, then drop the tar.
    with tarfile.open(chroma_tar_path) as tar:
        _safe_extractall(tar, chroma_dir)
    try:
        os.remove(chroma_tar_path)
    except OSError:
        pass

    disk_db = sqlite3.connect(db_path)
    db = sqlite3.connect(':memory:')
    try:
        disk_db.backup(db)
    finally:
        disk_db.close()
        try:
            os.remove(db_path)
        except OSError:
            pass
    db.execute("PRAGMA journal_mode=OFF")
    license_id = info.get('LICENSE_ID', 'unknown')
    license_id_hash = hashlib.sha256(license_id.encode('utf-8', errors='replace')).hexdigest()
    db.execute(
        "INSERT OR REPLACE INTO vault_metadata (key, value) VALUES (?, ?)",
        ('buyer_id', license_id_hash),
    )
    _set_vault_embedding_cache_metadata(read_embedding_metadata(db))
    db.commit()
    return db, chroma_dir, info.get('LICENSED_TO', 'unknown')


# ── Reusable search session (used by mcp path indirectly + benchmark) ────────
_reranker = None


def _cand_text(c):
    """Text a candidate exposes for reranking — supports both the session path
    ('text') and the MCP path ('snippet'). Strips FTS highlight tags."""
    t = c.get('_full_text') or c.get('text') or c.get('snippet') or ''
    return t.replace('<b>', '').replace('</b>', '')


def _cand_key(c):
    """Identity of a chunk for dedup: its source video + timestamp (falls back
    to title when video_id is absent). Both search paths carry these fields
    (session uses start_ts, MCP uses timestamp)."""
    chunk_id = c.get('chunk_id') or c.get('id')
    if chunk_id:
        return ('chunk_id', str(chunk_id))
    vid = c.get('video_id') or c.get('title') or ''
    ts = c.get('start_ts') or c.get('timestamp') or ''
    return (vid.strip().lower(), str(ts).strip())


def _merge_unique_values(*values):
    out = []
    seen = set()
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            if item is None:
                continue
            text = str(item)
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def dedup_candidates(cands):
    """Drop duplicate chunks (same video+timestamp) so one chunk never occupies
    two result slots. Keeps first occurrence; on a duplicate, prefers the longer
    text (fuller rerank context) and marks it as matching both sources."""
    seen = {}
    order = []
    for c in cands:
        k = _cand_key(c)
        if k not in seen:
            seen[k] = c
            order.append(k)
        else:
            kept = seen[k]
            merged_rrf = max(kept.get('rrf_score', 0.0), c.get('rrf_score', 0.0))
            kept['dual_hit'] = True
            kept['rrf_score'] = merged_rrf
            kept['_dup_of'] = c.get('source') or c.get('method')
            kept['matched_queries'] = _merge_unique_values(
                kept.get('matched_queries'), c.get('matched_queries'))
            kept['retrieval_sources'] = _merge_unique_values(
                kept.get('retrieval_sources'), c.get('retrieval_sources'),
                kept.get('source'), c.get('source'), kept.get('method'), c.get('method'))
            if len(_cand_text(c)) > len(_cand_text(kept)):
                # keep the fuller text but remember it matched both retrievers
                c['matched_queries'] = kept['matched_queries']
                c['retrieval_sources'] = kept['retrieval_sources']
                c['_dup_of'] = kept.get('source') or kept.get('method')
                c['dual_hit'] = True
                c['rrf_score'] = merged_rrf
                seen[k] = c
    out = []
    for k in order:
        c = seen[k]
        c['matched_queries'] = _merge_unique_values(c.get('matched_queries'))
        c['retrieval_sources'] = _merge_unique_values(
            c.get('retrieval_sources'), c.get('source'), c.get('method'))
        out.append(c)
    return out


def apply_rrf_scores(cands):
    """Attach Reciprocal Rank Fusion scores before dedup/rerank.

    Also retain per-query-variant keyword/semantic scores so multi-search can
    preserve facet coverage instead of letting broad cross-query hits occupy
    every result slot. KG expansions remain global support, not facet anchors.
    """
    ranks = {}
    scores = {}
    variant_scores = {}
    seen_sources = set()
    for c in cands:
        key = _cand_key(c)
        source = c.get('_rrf_source') or c.get('source') or c.get('method') or 'unknown'
        rank = c.get('_rank_in_source')
        if rank is None:
            rank = ranks.get(source, 0)
            ranks[source] = rank + 1
        if (key, source) not in seen_sources:
            seen_sources.add((key, source))
            contribution = 1.0 / (60 + int(rank))
            scores[key] = scores.get(key, 0.0) + contribution
            match = re.fullmatch(r"(q\d+):(keyword|semantic)", str(source))
            if match:
                bucket = variant_scores.setdefault(key, {})
                variant = match.group(1)
                bucket[variant] = bucket.get(variant, 0.0) + contribution
    for c in cands:
        key = _cand_key(c)
        c['rrf_score'] = scores.get(key, 0.0)
        c['_variant_scores'] = dict(variant_scores.get(key, {}))
    return cands


def _normalized_rrf(candidates):
    best = max((c.get('rrf_score', 0.0) for c in candidates), default=0.0)
    if best <= 0:
        return {id(c): 0.0 for c in candidates}
    return {id(c): c.get('rrf_score', 0.0) / best for c in candidates}


def clear_search_cache():
    _search_cache.clear()


def _cache_key(query, top_k, playlist=None):
    return (
        (query or "").lower(),
        int(top_k),
        playlist or '',
        vault_hash,
        _vault_embedding_cache_fingerprint,
    )


def get_cached_results(query, top_k, playlist=None):
    global _search_cache_hits, _search_cache_misses
    key = _cache_key(query, top_k, playlist)
    if key in _search_cache:
        _search_cache_hits += 1
        _search_cache.move_to_end(key)
        print(f"(search cache hit: hits={_search_cache_hits} misses={_search_cache_misses})", file=sys.stderr)
        return [dict(r) for r in _search_cache[key]]
    _search_cache_misses += 1
    print(f"(search cache miss: hits={_search_cache_hits} misses={_search_cache_misses})", file=sys.stderr)
    return None


def put_cached_results(query, top_k, playlist, results):
    key = _cache_key(query, top_k, playlist)
    _search_cache[key] = [dict(r) for r in results]
    _search_cache.move_to_end(key)
    while len(_search_cache) > SEARCH_CACHE_MAX:
        _search_cache.popitem(last=False)


def _fts_columns(db):
    try:
        return [r[1] for r in db.execute("PRAGMA table_info(transcripts_fts)").fetchall()]
    except sqlite3.Error:
        return []


def fts_candidates(db, query_text, limit, playlist=None, source='keyword', rrf_source=None,
                   matched_query=None):
    out = []
    fts_query = sanitize_fts(query_text)
    if not fts_query:
        return out
    cols = _fts_columns(db)
    has_chunk_id = 'chunk_id' in cols
    has_chunk_index = 'chunk_index' in cols
    has_end_ts = 'end_ts' in cols
    optional_provenance = [
        name for name in ('timing_precision', 'start_seconds', 'end_seconds')
        if name in cols
    ]
    try:
        if has_chunk_id:
            select = ["chunk_id", "title", "video_id", "start_ts", "playlist", "source_file", "content"]
            if has_chunk_index:
                select.append("chunk_index")
            if has_end_ts:
                select.append("end_ts")
            select.extend(optional_provenance)
            sql = ("SELECT " + ", ".join(select) + " "
                   "FROM transcripts_fts WHERE content MATCH ?")
        else:
            select = ["rowid", "title", "video_id", "start_ts", "playlist", "source_file", "content"]
            if has_chunk_index:
                select.append("chunk_index")
            if has_end_ts:
                select.append("end_ts")
            select.extend(optional_provenance)
            sql = ("SELECT " + ", ".join(select) + " "
                   "FROM transcripts_fts WHERE content MATCH ?")
        params = [fts_query]
        if playlist:
            sql += " AND playlist = ?"
            params.append(playlist)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        for i, r in enumerate(db.execute(sql, params).fetchall()):
            chunk_id = str(r[0]) if has_chunk_id else f"{r[5]}:{r[3]}:{r[0]}"
            item = {
                'source': source,
                'method': source,
                'chunk_id': chunk_id,
                'title': r[1],
                'video_id': r[2],
                'start_ts': r[3],
                'timestamp': r[3],
                'playlist': r[4],
                'source_file': r[5],
                '_full_text': r[6],
                '_rank_in_source': i,
                '_rrf_source': rrf_source or source,
                'retrieval_sources': [source],
                'matched_queries': [matched_query or query_text],
            }
            idx = 7
            if has_chunk_index:
                item['chunk_index'] = r[idx]
                idx += 1
            if has_end_ts:
                item['end_ts'] = r[idx]
                idx += 1
            for name in optional_provenance:
                item[name] = r[idx]
                idx += 1
            out.append(item)
    except sqlite3.Error as e:
        print(f"(keyword search unavailable: {e})", file=sys.stderr)
    return out


def _chunk_select_columns(db):
    cols = _fts_columns(db)
    wanted = ['chunk_id', 'title', 'video_id', 'playlist', 'start_ts',
              'end_ts', 'chunk_index', 'source_file', 'timing_precision',
              'start_seconds', 'end_seconds', 'content']
    return [c for c in wanted if c in cols]


def _row_to_dict(cursor, row):
    if row is None:
        return None
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def _chunk_where_for_candidate(cols, candidate):
    chunk_id = candidate.get('chunk_id')
    if chunk_id and 'chunk_id' in cols and not str(chunk_id).startswith(candidate.get('source_file', '') + ":"):
        return "chunk_id = ?", [chunk_id]
    source_file = candidate.get('source_file')
    chunk_index = candidate.get('chunk_index')
    if source_file and chunk_index is not None and 'source_file' in cols and 'chunk_index' in cols:
        return "source_file = ? AND chunk_index = ?", [source_file, int(chunk_index)]
    start_ts = candidate.get('start_ts') or candidate.get('timestamp')
    if source_file and start_ts and 'source_file' in cols and 'start_ts' in cols:
        return "source_file = ? AND start_ts = ?", [source_file, start_ts]
    return None, []


def fetch_chunk(db, candidate):
    cols = _chunk_select_columns(db)
    if not cols:
        return None
    where, params = _chunk_where_for_candidate(cols, candidate)
    if not where:
        return None
    try:
        cur = db.execute(
            "SELECT " + ", ".join(cols) + " FROM transcripts_fts WHERE " + where + " LIMIT 1",
            params,
        )
        return _row_to_dict(cur, cur.fetchone())
    except sqlite3.Error:
        return None


def hydrate_candidate_text(db, candidate):
    row = fetch_chunk(db, candidate)
    if not row:
        return candidate
    out = dict(candidate)
    for key in ('chunk_id', 'title', 'video_id', 'playlist', 'start_ts',
                'end_ts', 'chunk_index', 'source_file', 'timing_precision',
                'start_seconds', 'end_seconds'):
        if row.get(key) not in (None, ''):
            out[key] = row[key]
    out['timestamp'] = out.get('start_ts') or out.get('timestamp') or ''
    out['_full_text'] = row.get('content') or out.get('_full_text') or ''
    return out


def adjacent_chunk(db, candidate, offset):
    cols = _chunk_select_columns(db)
    if 'source_file' not in cols or 'chunk_index' not in cols:
        return None
    source_file = candidate.get('source_file')
    chunk_index = candidate.get('chunk_index')
    if not source_file or chunk_index is None:
        return None
    wanted = int(chunk_index) + int(offset)
    if wanted < 0:
        return None
    select = ", ".join(cols)
    try:
        cur = db.execute(
            f"SELECT {select} FROM transcripts_fts WHERE source_file = ? AND chunk_index = ? LIMIT 1",
            (source_file, wanted),
        )
        return _row_to_dict(cur, cur.fetchone())
    except sqlite3.Error:
        return None


def _word_set(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    union = a | b
    return (len(a & b) / len(union)) if union else 0.0


def timestamp_seconds(value):
    if value is None:
        return None
    try:
        parts = [int(p) for p in str(value).split(":")]
    except ValueError:
        return None
    if len(parts) < 2 or len(parts) > 3:
        return None
    total = 0
    for p in parts:
        total = (total * 60) + p
    return total


def _same_video(a, b):
    av = (a.get('video_id') or '').strip().lower()
    bv = (b.get('video_id') or '').strip().lower()
    return bool(av and bv and av == bv)


def _diversity_penalty(c, s, word_sets):
    penalty = _jaccard(word_sets[id(c)], word_sets[id(s)])
    if _same_video(c, s):
        penalty = max(penalty, 0.55)
        ct = timestamp_seconds(c.get('start_ts') or c.get('timestamp'))
        st = timestamp_seconds(s.get('start_ts') or s.get('timestamp'))
        if ct is not None and st is not None and abs(ct - st) <= 30:
            penalty = max(penalty, 0.85)
    return min(1.0, penalty)


def _normalized_relevance(candidates):
    scores = [float(c.get('final_score', c.get('rerank_score', 0.0))) for c in candidates]
    if not scores:
        return {}
    lo, hi = min(scores), max(scores)
    if hi == lo:
        return {id(c): 1.0 for c in candidates}
    if lo < 0:
        return {id(c): (float(c.get('final_score', c.get('rerank_score', 0.0))) - lo) / (hi - lo)
                for c in candidates}
    if hi <= 0:
        return {id(c): 0.0 for c in candidates}
    return {id(c): float(c.get('final_score', c.get('rerank_score', 0.0))) / hi
            for c in candidates}


def apply_mmr(candidates, top_k, lambda_=MMR_LAMBDA):
    """Greedy MMR with normalized relevance and explicit video/time penalties."""
    remaining = list(candidates)
    selected = []
    word_sets = {id(c): _word_set(_cand_text(c)) for c in remaining}
    relevance = _normalized_relevance(remaining)
    while remaining and len(selected) < top_k:
        best = None
        best_score = None
        for c in remaining:
            max_sim = max(
                (_diversity_penalty(c, s, word_sets) for s in selected),
                default=0.0,
            )
            mmr_score = (lambda_ * relevance[id(c)]) - ((1 - lambda_) * max_sim)
            if best is None or mmr_score > best_score:
                best = c
                best_score = mmr_score
        selected.append(best)
        remaining.remove(best)
    return selected


def kg_expand(db, text, max_related=3):
    """Auto knowledge-graph expansion: find KG entities the query mentions and
    return their directly-related entity names, to widen retrieval. Empty on any
    problem (missing tables, older vault) — callers degrade to normal search.

    The reranker then judges every widened candidate against the ORIGINAL query,
    so related-concept chunks only surface if they're actually relevant.
    """
    if not text:
        return []
    try:
        names = [r[0] for r in db.execute("SELECT name FROM entities").fetchall() if r[0]]
    except Exception:
        return []
    low = text.lower()
    # word-boundary match so short entities ('OB','MS') don't match inside words
    def mentioned(name):
        return re.search(r'(?<![a-z0-9])' + re.escape(name.lower()) + r'(?![a-z0-9])', low)
    hits = [n for n in names if mentioned(n)]
    if not hits:
        return []
    seen = {h.lower() for h in hits}
    related = []
    for e in hits:
        try:
            rows = db.execute(
                "SELECT from_entity, to_entity FROM relations "
                "WHERE from_entity=? OR to_entity=?", (e, e)).fetchall()
        except Exception:
            continue
        for a, b in rows:
            other = b if a == e else a
            if other and other.lower() not in seen:
                seen.add(other.lower())
                related.append(other)
                if len(related) >= max_related:
                    return related
    return related


def chroma_store_usable(chroma_dir):
    """Return False for fixture/corrupt Chroma sqlite stores before import/query."""
    db_path = Path(chroma_dir) / "chroma.sqlite3"
    if not db_path.exists():
        return True
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.execute("PRAGMA schema_version").fetchone()
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        if con is not None:
            con.close()


def warm_reranker():
    """Cross-encoder disabled — buyer's LLM does relevance filtering instead.
    Returns False to signal RRF fallback should always be used."""
    global _reranker
    _reranker = None
    return False


def rerank(query, candidates, top_k):
    """Use an explicitly loaded cross-encoder, otherwise deterministic RRF.

    Production keeps the cross-encoder disabled for buyer resource usage. Tests
    and opt-in callers may inject one; that path remains functional rather than
    silently ignoring the caller.
    """
    global _reranker
    if len(candidates) <= 1 and _reranker is None:
        return candidates[:top_k]
    rrf_norm = _normalized_rrf(candidates)
    if _reranker is not None:
        try:
            scores = _reranker.predict([(query, _cand_text(c)[:1500]) for c in candidates])
            for c, score in zip(candidates, scores):
                boosted = float(score) + (0.5 if c.get('dual_hit') else 0.0)
                c['rerank_score'] = boosted
                c['final_score'] = boosted + (0.1 * rrf_norm.get(id(c), 0.0))
            candidates.sort(
                key=lambda c: c.get('final_score', c.get('rerank_score', 0.0)),
                reverse=True,
            )
            filtered = [c for c in candidates
                        if c.get('rerank_score', 0.0) >= MIN_RERANK_SCORE]
            return apply_mmr(filtered, top_k) if filtered else []
        except Exception as exc:
            print(f"(rerank skipped: {exc})", file=sys.stderr)

    for c in candidates:
        c['rerank_score'] = rrf_norm.get(id(c), 0.0)
        c['final_score'] = c['rerank_score']
    candidates.sort(key=lambda c: c.get('final_score', 0.0), reverse=True)
    return candidates[:top_k]


def _clamp_chars(value, default, hard_cap):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, hard_cap))


def make_snippet(text, max_chars=SNIPPET_DEFAULT_CHARS, query=""):
    """Build a snippet. When query is provided, find the most relevant
    sentence window inside the text instead of returning the first chars."""
    max_chars = _clamp_chars(max_chars, SNIPPET_DEFAULT_CHARS, SNIPPET_MAX_CHARS)
    clean = (text or "").replace("<b>", "").replace("</b>", "").strip()
    clean = re.sub(r"\s+", " ", clean)
    q = (query or "").strip()
    if not q or len(clean) <= max_chars:
        return clean[:max_chars]

    # Split into sentences on . ! ? followed by whitespace + capital/quote
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z\"\'(])", clean)
    if len(sents) <= 2:
        return clean[:max_chars]

    # Score each sentence by word overlap with query (ignoring case)
    q_words = set(q.lower().split())
    q_len = len(q_words)
    best_i = 0
    best_s = -1
    for i, s in enumerate(sents):
        common = len(q_words & set(s.lower().split()))
        score = common / q_len if q_len else 0.0
        if score > best_s:
            best_s = score
            best_i = i

    # If no query match found, fall back to first chars
    if best_s <= 0:
        return clean[:max_chars]

    # Build a window of ±2 sentences around the best sentence
    start = max(0, best_i - 2)
    end = min(len(sents), best_i + 3)
    window = " ".join(sents[start:end])

    # If window fits within max_chars, return it
    if len(window) <= max_chars:
        return window

    # Trim edges while keeping the best sentence centred
    half = max_chars // 2
    best_start = window.find(sents[best_i])
    best_end = best_start + len(sents[best_i])
    before = min(half, best_start)
    after = min(half, len(window) - best_end)
    trimmed = window[best_start - before:best_end + after]
    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars - 3] + "..."
    if best_start > 0:
        trimmed = "..." + trimmed
    return trimmed


def _cand_score(c):
    """Best available ranking score for diversity selection."""
    for key in ('final_score', 'rerank_score', 'rrf_score'):
        if c.get(key) is not None:
            try:
                return float(c[key])
            except (TypeError, ValueError):
                pass
    return 0.0


def _cand_ts_seconds(c):
    return timestamp_seconds(c.get('start_ts') or c.get('timestamp'))


def _video_bucket_key(c):
    vid = (c.get('video_id') or '').strip().lower()
    if vid:
        return ('video', vid)
    return ('chunk', str(_cand_key(c)))


def _merge_two_candidates(a, b):
    """Keep one complete cited chunk; union only retrieval/ranking metadata."""
    if _cand_score(b) > _cand_score(a):
        keep, drop = dict(b), a
    else:
        keep, drop = dict(a), b
    keep['matched_queries'] = _merge_unique_values(
        keep.get('matched_queries'), drop.get('matched_queries'))
    keep['retrieval_sources'] = _merge_unique_values(
        keep.get('retrieval_sources'), drop.get('retrieval_sources'),
        keep.get('source'), drop.get('source'),
        keep.get('method'), drop.get('method'))
    keep['rrf_score'] = max(keep.get('rrf_score', 0.0) or 0.0,
                            drop.get('rrf_score', 0.0) or 0.0)
    if drop.get('final_score') is not None or keep.get('final_score') is not None:
        keep['final_score'] = max(_cand_score(keep), _cand_score(drop))
    if drop.get('rerank_score') is not None or keep.get('rerank_score') is not None:
        try:
            keep['rerank_score'] = max(
                float(keep.get('rerank_score') or -1e9),
                float(drop.get('rerank_score') or -1e9))
        except (TypeError, ValueError):
            pass
    if keep.get('_variant_priority') is not None or drop.get('_variant_priority') is not None:
        keep['_variant_priority'] = max(
            int(keep.get('_variant_priority') or 0),
            int(drop.get('_variant_priority') or 0),
        )
    keep['_merged_from'] = (keep.get('_merged_from') or 1) + (drop.get('_merged_from') or 1)
    return keep


def _merge_query_signature(candidate):
    return tuple(sorted({
        str(query).strip().lower()
        for query in (candidate.get('matched_queries') or [])
        if str(query).strip()
    }))


def _merge_adjacent_same_video(cands, merge_gap_sec=MERGE_GAP_SEC):
    """Merge nearby chunks only when they support the same query evidence.

    Distinct multi-query facets must keep their own chunk text, ID, and timestamp;
    collapsing them can erase a required facet even though provenance stays valid.
    """
    if not cands:
        return [], 0
    by_vid = {}
    orphan = []
    for c in cands:
        key = _video_bucket_key(c)
        if key[0] != 'video':
            orphan.append(c)
            continue
        by_vid.setdefault(key[1], []).append(c)

    merged = list(orphan)
    merges = 0
    for _vid, items in by_vid.items():
        items = sorted(items, key=lambda c: (_cand_ts_seconds(c) is None,
                                             _cand_ts_seconds(c) or 0,
                                             -_cand_score(c)))
        clusters = []
        for c in items:
            ts = _cand_ts_seconds(c)
            if not clusters or ts is None:
                clusters.append(c)
                continue
            prev = clusters[-1]
            pts = _cand_ts_seconds(prev)
            same_evidence = _merge_query_signature(prev) == _merge_query_signature(c)
            if pts is not None and abs(ts - pts) <= merge_gap_sec and same_evidence:
                clusters[-1] = _merge_two_candidates(prev, c)
                merges += 1
            else:
                clusters.append(c)
        merged.extend(clusters)
    return merged, merges


def diversify_by_video(candidates, top_k=5,
                       max_per_video=MAX_RESULTS_PER_VIDEO,
                       merge_gap_sec=MERGE_GAP_SEC,
                       distinct_gap_sec=DISTINCT_GAP_SEC):
    """Post-rerank diversity: merge nearby same-video chunks, cap per video.

    Returns (selected_list, meta_dict).
    """
    top_k = max(1, int(top_k or MAX_TOP_K))
    max_per_video = max(1, int(max_per_video or MAX_RESULTS_PER_VIDEO))
    if not candidates:
        return [], {
            'unique_videos': 0,
            'max_per_video': max_per_video,
            'merged_chunks': 0,
            'selected': 0,
        }

    pool, merges = _merge_adjacent_same_video(candidates, merge_gap_sec=merge_gap_sec)
    pool = sorted(
        pool,
        key=lambda c: (int(c.get('_variant_priority') or 0), _cand_score(c)),
        reverse=True,
    )

    selected = []
    per_video = {}
    last_candidate_for_video = {}

    for c in pool:
        if len(selected) >= top_k:
            break
        vkey = _video_bucket_key(c)
        count = per_video.get(vkey, 0)
        if count >= max_per_video:
            continue
        if count >= 1 and vkey[0] == 'video':
            ts = _cand_ts_seconds(c)
            previous = last_candidate_for_video.get(vkey)
            prev_ts = _cand_ts_seconds(previous) if previous else None
            if ts is not None and prev_ts is not None:
                same_evidence = _merge_query_signature(previous) == _merge_query_signature(c)
                if abs(ts - prev_ts) < distinct_gap_sec and same_evidence:
                    continue
            elif ts is None:
                continue
        selected.append(c)
        per_video[vkey] = count + 1
        last_candidate_for_video[vkey] = c

    if len(selected) < top_k:
        selected_ids = {id(c) for c in selected}
        for c in pool:
            if len(selected) >= top_k:
                break
            if id(c) in selected_ids:
                continue
            vkey = _video_bucket_key(c)
            if per_video.get(vkey, 0) >= max_per_video:
                continue
            selected.append(c)
            selected_ids.add(id(c))
            per_video[vkey] = per_video.get(vkey, 0) + 1

    vids = {(c.get('video_id') or '').strip() for c in selected if (c.get('video_id') or '').strip()}
    meta = {
        'unique_videos': len(vids),
        'max_per_video': max_per_video,
        'merged_chunks': merges,
        'selected': len(selected),
        'pool_in': len(candidates),
        'pool_after_merge': len(pool),
    }
    return selected, meta


_ANSWERABILITY_STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'between', 'do', 'does', 'for',
    'from', 'how', 'i', 'in', 'is', 'it', 'of', 'on', 'or', 'the', 'to', 'vs',
    'what', 'when', 'where', 'which', 'who', 'why', 'with', 'you', 'your',
}


def assess_answerability(question, results):
    """Return a conservative, machine-readable retrieval evidence gate.

    This measures whether retrieved snippets cover the meaningful query terms. It
    does not claim factual truth and cannot infer conflicts unless retrieval or a
    later evidence-review stage explicitly marks them.
    """
    rows = list(results or [])
    unique_videos = len({r.get('video_id') for r in rows if r.get('video_id')})
    if any(r.get('evidence_conflict') or r.get('evidence_status') == 'conflicting'
           for r in rows):
        return {
            'status': 'conflicting', 'query_term_coverage': 0.0,
            'evidence_count': len(rows), 'unique_videos': unique_videos,
            'basis': 'explicit_conflict_marker', 'claim_support': False,
            'heuristic': True,
        }
    if not rows:
        return {
            'status': 'no_retrieved_evidence', 'query_term_coverage': 0.0,
            'evidence_count': 0, 'unique_videos': 0,
            'basis': 'no_retrieved_evidence', 'claim_support': False,
            'heuristic': True,
        }

    terms = []
    for token in re.findall(r"[a-z0-9]+", (question or '').lower()):
        if len(token) >= 3 and token not in _ANSWERABILITY_STOPWORDS and token not in terms:
            terms.append(token)
    evidence = ' '.join(
        ' '.join(str(r.get(k, '')) for k in ('title', 'snippet', '_full_text'))
        for r in rows
    ).lower()
    matched = [term for term in terms if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", evidence)]
    coverage = len(matched) / len(terms) if terms else 0.0
    if terms and coverage == 1.0:
        status = 'full_lexical_coverage'
    elif matched:
        status = 'partial_lexical_coverage'
    else:
        status = 'no_lexical_coverage'
    return {
        'status': status,
        'query_term_coverage': round(coverage, 3),
        'matched_query_terms': matched,
        'missing_query_terms': [term for term in terms if term not in matched],
        'evidence_count': len(rows),
        'unique_videos': unique_videos,
        'basis': 'lexical_telemetry_not_claim_support',
        'claim_support': False,
        'heuristic': True,
    }


def _snippet_terms(value):
    """Normalize query/snippet terms, including `10am` vs `10 a.m.` forms."""
    text = (value or "").lower()
    text = re.sub(r"\b([ap])\s*\.\s*m\.?", r"\1m", text)
    text = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", text)
    return set(re.findall(r"[a-z0-9]+", text))


def _best_snippet_query(text, matched_queries, fallback=""):
    """Choose the matched variant whose own evidence window covers it best.

    Multi-search variants target separate answer facets. Using only the broad
    parent question can retrieve the right chunk but display the wrong sentence.
    """
    variants = _merge_unique_values(matched_queries)
    if not variants:
        return fallback or ""
    best_query = variants[0]
    best_score = (-1.0, -1)
    for candidate_query in variants:
        terms = _snippet_terms(candidate_query)
        window = make_snippet(text, SNIPPET_MAX_CHARS, query=candidate_query)
        covered = terms & _snippet_terms(window)
        score = (len(covered) / len(terms) if terms else 0.0, len(covered))
        if score > best_score:
            best_query = candidate_query
            best_score = score
    return best_query


def finalize_ranked_results(ranked, snippet_chars=SNIPPET_DEFAULT_CHARS, query=""):
    enrichment_fields = (
        'year', 'playlist_family', 'video_number', 'lesson_type',
        'primary_concept', 'session_tag', 'is_definition', 'is_example',
        'is_warning', 'is_rule',
    )
    out = []
    for c in ranked:
        sources = _merge_unique_values(c.get('retrieval_sources'), c.get('source'), c.get('method'))
        full_text = _cand_text(c)
        sq = _best_snippet_query(full_text, c.get('matched_queries'), fallback=query)
        item = {
            'title': c.get('title', ''),
            'video_id': c.get('video_id', ''),
            'timestamp': c.get('start_ts') or c.get('timestamp') or '',
            'start_ts': c.get('start_ts') or c.get('timestamp') or '',
            'end_ts': c.get('end_ts') or '',
            'start_seconds': c.get('start_seconds'),
            'end_seconds': c.get('end_seconds'),
            'timing_precision': c.get('timing_precision') or 'legacy_unspecified',
            'playlist': c.get('playlist', ''),
            'method': "+".join(sources) if sources else (c.get('method') or c.get('source') or ''),
            'retrieval_sources': sources,
            'matched_queries': _merge_unique_values(c.get('matched_queries')),
            'snippet': make_snippet(full_text, snippet_chars, query=sq),
        }
        if c.get('result_ref'):
            item['result_ref'] = c['result_ref']
        for field in enrichment_fields:
            if field in c:
                item[field] = c[field]
        if c.get('video_id'):
            item['video_url'] = youtube_link(
                c.get('video_id'), item['timestamp'], item['start_seconds'])
        out.append(item)
    return out


def clamp_top_k(value, research_mode=False):
    try:
        value = int(value or MAX_TOP_K)
    except (TypeError, ValueError):
        value = MAX_TOP_K
    hard = RESEARCH_MAX_TOP_K if research_mode else MAX_TOP_K
    return max(1, min(value, hard))


def normalize_query_variants(question, queries):
    if isinstance(queries, str):
        queries = [queries]
    out = []
    seen = set()
    for q in queries or []:
        text = str(q or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    if not out and question:
        out.append(str(question).strip())
    if not out:
        raise ValueError("queries must contain 1 to 4 non-empty query variants")
    if len(out) > MAX_QUERY_VARIANTS:
        raise ValueError("queries accepts at most 4 variants")
    return out


def _tag_candidates(candidates, matched_query, source):
    for c in candidates:
        c['matched_queries'] = _merge_unique_values(c.get('matched_queries'), matched_query)
        c['retrieval_sources'] = _merge_unique_values(c.get('retrieval_sources'), source)
    return candidates


def estimate_multi_search_work_units(db, question, queries, kg=True, semantic=True):
    variants = normalize_query_variants(question, queries)
    units = 0
    for q in variants:
        expanded, _ = expand_query(q)
        units += 1  # FTS
        if semantic:
            units += 1
        if kg:
            units += len(kg_expand(db, q + ' ' + expanded))
    return units


def _prioritize_query_variant_coverage(ranked, variants, top_k,
                                       max_per_video=MAX_RESULTS_PER_VIDEO):
    """Reserve up to one strong keyword/semantic result per query variant.

    The remaining slots keep normal global RRF order. This makes multi-search
    variants behave as answer facets while preserving the existing ranker and
    video-diversity cap.
    """
    ranked = list(ranked or [])
    variants = list(variants or [])
    variant_count = len(variants)
    if top_k <= 0 or variant_count <= 1 or not ranked:
        return ranked
    reserved = []
    used = set()
    video_counts = {}
    for idx in range(1, min(int(variant_count), int(top_k)) + 1):
        variant = f"q{idx}"
        options = []
        for position, candidate in enumerate(ranked):
            key = _cand_key(candidate)
            if key in used:
                continue
            score = float((candidate.get('_variant_scores') or {}).get(variant, 0.0))
            if score <= 0:
                continue
            video_id = candidate.get('video_id') or ''
            if video_id and video_counts.get(video_id, 0) >= max_per_video:
                continue
            query_terms = _snippet_terms(variants[idx - 1])
            text_terms = _snippet_terms(_cand_text(candidate))
            coverage = (len(query_terms.intersection(text_terms)) / len(query_terms)
                        if query_terms else 0.0)
            options.append((coverage, score, -position, candidate))
        if not options:
            continue
        chosen = max(options, key=lambda item: (item[0], item[1], item[2]))[3]
        used.add(_cand_key(chosen))
        video_id = chosen.get('video_id') or ''
        if video_id:
            video_counts[video_id] = video_counts.get(video_id, 0) + 1
        reserved.append(chosen)
    for priority, candidate in enumerate(reserved[::-1], start=1):
        candidate['_variant_priority'] = priority
    return reserved + [c for c in ranked if _cand_key(c) not in used]


def collect_multi_search_candidates(db, semantic_retriever, question, queries, top_k=5,
                                    playlist=None, kg=True, research_mode=False):
    """Retrieve raw candidates for every query variant, then fuse and rerank once.

    semantic_retriever(query, limit, playlist, rrf_source, matched_query) must
    return raw semantic candidates, or [] if vector search is unavailable.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")
    variants = normalize_query_variants(question, queries)
    top_k = clamp_top_k(top_k, research_mode=research_mode)
    pool = top_k + 5
    candidates = []
    work_units = 0

    for idx, q in enumerate(variants):
        expanded, _ = expand_query(q)
        rrf_prefix = f"q{idx + 1}"

        fts = fts_candidates(
            db, expanded, pool, playlist, source='keyword',
            rrf_source=f"{rrf_prefix}:keyword", matched_query=q)
        candidates.extend(_tag_candidates(fts, q, 'keyword'))
        work_units += 1

        if semantic_retriever is not None:
            semantic = semantic_retriever(q, pool, playlist, f"{rrf_prefix}:semantic", q)
            candidates.extend(_tag_candidates(semantic, q, 'semantic'))
            work_units += 1

        if kg:
            for term in kg_expand(db, q + ' ' + expanded):
                kg_cands = fts_candidates(
                    db, term, max(2, min(pool, top_k + 1)), playlist,
                    source='kg', rrf_source=f"{rrf_prefix}:kg:{term}", matched_query=q)
                candidates.extend(_tag_candidates(kg_cands, q, 'kg'))
                work_units += 1

    candidates = apply_rrf_scores(candidates)
    candidates = dedup_candidates(candidates)
    candidates = [hydrate_candidate_text(db, c) for c in candidates]
    # Rerank a larger pool so diversity has alternatives after top hits
    pool_k = max(top_k * DIVERSITY_RERANK_POOL_MULT, top_k + DIVERSITY_RERANK_POOL_EXTRA)
    pool_k = min(pool_k, max(len(candidates), top_k))
    ranked = rerank(question, candidates, pool_k) if candidates else []
    ranked = _prioritize_query_variant_coverage(ranked, variants, top_k)
    ranked, diversity = diversify_by_video(ranked, top_k=top_k)
    diversity = dict(diversity or {})
    diversity['research_mode'] = bool(research_mode)
    return ranked, {
        'queries': variants,
        'work_units': work_units,
        'candidate_count': len(candidates),
        'diversity': diversity,
        'research_mode': bool(research_mode),
    }


class ResultRefStore:
    def __init__(self, ttl_seconds=RESULT_REF_TTL_SECONDS, max_uses=RESULT_REF_MAX_USES,
                 max_refs=200):
        self.ttl_seconds = ttl_seconds
        self.max_uses = max_uses
        self.max_refs = max_refs
        self._refs = {}

    def _sweep(self, now=None):
        now = time.time() if now is None else now
        expired = [ref for ref, item in self._refs.items() if item['expires_at'] <= now]
        for ref in expired:
            self._refs.pop(ref, None)
        while len(self._refs) > self.max_refs:
            oldest = min(self._refs.items(), key=lambda kv: kv[1]['created_at'])[0]
            self._refs.pop(oldest, None)

    def issue(self, candidate, now=None):
        now = time.time() if now is None else now
        self._sweep(now)
        ref = secrets.token_urlsafe(24)
        self._refs[ref] = {
            'candidate': {
                'chunk_id': candidate.get('chunk_id'),
                'source_file': candidate.get('source_file'),
                'chunk_index': candidate.get('chunk_index'),
                'start_ts': candidate.get('start_ts') or candidate.get('timestamp'),
                'end_ts': candidate.get('end_ts'),
                'timestamp': candidate.get('timestamp') or candidate.get('start_ts'),
                'start_seconds': candidate.get('start_seconds'),
                'end_seconds': candidate.get('end_seconds'),
                'timing_precision': candidate.get('timing_precision'),
                'title': candidate.get('title'),
                'video_id': candidate.get('video_id'),
                'playlist': candidate.get('playlist'),
            },
            'created_at': now,
            'expires_at': now + self.ttl_seconds,
            'uses': 0,
        }
        return ref

    def peek(self, ref, now=None):
        """Resolve without consuming a use (for size-only planning)."""
        now = time.time() if now is None else now
        self._sweep(now)
        item = self._refs.get(ref)
        if not item:
            raise VaultError("Invalid or expired result_ref.")
        if item['uses'] >= self.max_uses:
            self._refs.pop(ref, None)
            raise VaultError("This result_ref has already been expanded.")
        return dict(item['candidate'])

    def resolve(self, ref, now=None):
        now = time.time() if now is None else now
        self._sweep(now)
        item = self._refs.get(ref)
        if not item:
            raise VaultError("Invalid or expired result_ref.")
        if item['uses'] >= self.max_uses:
            self._refs.pop(ref, None)
            raise VaultError("This result_ref has already been expanded.")
        item['uses'] += 1
        return dict(item['candidate'])


def _section_from_row(position, row, max_chars):
    if not row:
        return None
    text = make_snippet(row.get('content') or '', max_chars)
    return {
        'position': position,
        'title': row.get('title', ''),
        'video_id': row.get('video_id', ''),
        'playlist': row.get('playlist', ''),
        'timestamp': row.get('start_ts', ''),
        'start_ts': row.get('start_ts', ''),
        'end_ts': row.get('end_ts', ''),
        'text': text,
    }


def expand_result_context(db, candidate, before=0, after=0):
    try:
        before = 1 if int(before or 0) == 1 else 0
    except (TypeError, ValueError):
        before = 0
    try:
        after = 1 if int(after or 0) == 1 else 0
    except (TypeError, ValueError):
        after = 0
    current = fetch_chunk(db, candidate)
    if not current:
        raise VaultError("Result reference no longer resolves to a vault chunk.")

    sections = []
    hydrated = dict(candidate)
    hydrated.update({k: v for k, v in current.items() if k != 'content'})
    if before:
        sections.append(_section_from_row(
            'before', adjacent_chunk(db, hydrated, -1), CONTEXT_BEFORE_MAX_CHARS))
    sections.append(_section_from_row('current', current, CONTEXT_CURRENT_MAX_CHARS))
    if after:
        sections.append(_section_from_row(
            'after', adjacent_chunk(db, hydrated, 1), CONTEXT_AFTER_MAX_CHARS))

    clean_sections = [s for s in sections if s]
    total = 0
    for section in clean_sections:
        remaining = CONTEXT_TOTAL_MAX_CHARS - total
        if remaining <= 0:
            section['text'] = ''
        elif len(section['text']) > remaining:
            section['text'] = section['text'][:remaining]
        total += len(section['text'])
    return {
        'sections': clean_sections,
        'total_chars': min(total, CONTEXT_TOTAL_MAX_CHARS),
    }


class VaultSession:
    """Decrypt once, query many. UI-agnostic — no printing, no colour."""

    def __init__(self):
        self.db = None
        self.chroma_dir = None
        self.licensed_to = "unknown"
        self.demo = None
        self._collection = None

    def open(self, vault_file=VAULT_FILE, license_file=LICENSE_FILE, on_progress=None):
        self.db, self.chroma_dir, self.licensed_to = open_vault(
            vault_file=vault_file, license_file=license_file, on_progress=on_progress)
        self.demo = demo_info(self.db)
        return self

    def _get_collection(self):
        if self._collection is None:
            import chromadb
            from chromadb.config import Settings
            ef = validate_embedding_compatibility(self.db, self.chroma_dir, require_metadata=True)
            client = chromadb.PersistentClient(
                path=self.chroma_dir, settings=Settings(anonymized_telemetry=False))
            self._collection = client.get_collection(
                'ict_vault',
                embedding_function=ef,
            )
        return self._collection

    def _fts_candidates(self, query_text, limit, playlist=None, source='keyword', rrf_source=None,
                        matched_query=None):
        return fts_candidates(
            self.db, query_text, limit, playlist, source, rrf_source, matched_query)

    def _semantic_candidates(self, query_text, limit, playlist=None, rrf_source='semantic',
                             matched_query=None):
        out = []
        try:
            if not chroma_store_usable(self.chroma_dir):
                raise RuntimeError("chroma store is not a valid sqlite database")
            where = {'playlist': playlist} if playlist else None
            # v3 vaults are documentless: request ids/metadatas only and hydrate the
            # transcript text from the in-memory SQLite/FTS. Iterating over ids (not
            # documents) keeps this working for both v2 (documents present) and v3.
            result = self._get_collection().query(
                query_texts=[query_text], n_results=limit, where=where,
                include=['metadatas', 'distances'])
            ids = (result.get('ids') or [[]])[0]
            docs = (result.get('documents') or [[]])[0]
            metas = (result.get('metadatas') or [[]])[0]
            for i, chunk_id in enumerate(ids):
                m = metas[i] if i < len(metas) else {}
                out.append({'source': 'semantic', 'title': m.get('title', ''),
                            'method': 'semantic',
                            'chunk_id': chunk_id or m.get('chunk_id', ''),
                            'video_id': m.get('video_id', ''),
                            'start_ts': m.get('start_ts', ''),
                            'timestamp': m.get('start_ts', ''),
                            'end_ts': m.get('end_ts', ''),
                            'start_seconds': m.get('start_seconds'),
                            'end_seconds': m.get('end_seconds'),
                            'timing_precision': m.get('timing_precision', ''),
                            'chunk_index': m.get('chunk_index'),
                            'playlist': m.get('playlist', ''),
                            'source_file': m.get('source_file', ''),
                            '_full_text': docs[i] if i < len(docs) else '',
                            '_rank_in_source': i,
                            '_rrf_source': rrf_source,
                            'retrieval_sources': ['semantic'],
                            'matched_queries': [matched_query or query_text]})
        except ImportError:
            print("(semantic search unavailable - chromadb not installed)", file=sys.stderr)
        except Exception as e:
            print(f"(semantic search unavailable: {e})", file=sys.stderr)
        return out

    def search(self, query, playlist=None, session=None, top_k=15):
        """Return (ranked_candidates, expanded_query, expansion_changed)."""
        expanded, changed = expand_query(query)
        cached = get_cached_results(query, top_k, playlist)
        if cached is not None:
            return cached, expanded, changed
        candidates = []
        pool = top_k + 10

        candidates.extend(self._fts_candidates(expanded, pool, playlist, matched_query=query))
        candidates.extend(self._semantic_candidates(query, pool, playlist, 'semantic', query))

        try:
            for term in kg_expand(self.db, query + ' ' + expanded):
                candidates.extend(self._fts_candidates(
                    term, 2, playlist, source='kg', rrf_source=f'kg:{term}',
                    matched_query=query))
        except Exception as e:
            print(f"(kg expansion skipped: {e})", file=sys.stderr)

        candidates = apply_rrf_scores(candidates)
        candidates = dedup_candidates(candidates)
        candidates = [hydrate_candidate_text(self.db, c) for c in candidates]
        ranked = rerank(query, candidates, top_k) if candidates else []
        ranked = finalize_ranked_results(ranked)
        put_cached_results(query, top_k, playlist, ranked)
        return ranked, expanded, changed

    def multi_search(self, question, queries, playlist=None, top_k=15, snippet_chars=SNIPPET_DEFAULT_CHARS):
        ranked, meta = collect_multi_search_candidates(
            self.db, self._semantic_candidates, question, queries, top_k, playlist)
        return finalize_ranked_results(ranked, snippet_chars), meta

    def close(self):
        if self.db:
            self.db.close()
            self.db = None


def run_doctor(vault_file=VAULT_FILE, license_file=LICENSE_FILE):
    """Preflight health check for buyers. Prints one actionable line per issue.

    Returns 0 if everything is ready, 1 otherwise. Used by
    `python mcp_server.py --doctor`.
    """
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        print(f"  {'✓' if cond else '✖'} {label}")
        if not cond and hint:
            print(f"      → {hint}")
        ok = ok and cond

    print("ICT Vault — environment check\n")
    check(f"Python {sys.version_info.major}.{sys.version_info.minor} (need 3.10+)",
          sys.version_info >= (3, 10), "Install Python 3.10 or newer from python.org")
    for mod, hint in [("cryptography", "pip install cryptography"),
                      ("chromadb", "pip install chromadb"),
                      ("sentence_transformers", "pip install sentence-transformers"),
                      ("mcp", "pip install mcp"),
                      ("zstandard", "pip install zstandard")]:
        try:
            __import__(mod)
            check(f"{mod} installed", True)
        except Exception:
            check(f"{mod} installed", False, hint)
    check("license.key present", license_file.exists(),
          "Place the license.key from your purchase next to mcp_server.py")
    check("ict-vault.kevin present", vault_file.exists(),
          "Place ict-vault.kevin next to mcp_server.py")
    if license_file.exists() and vault_file.exists():
        try:
            db, chroma_dir, who = open_vault(vault_file=vault_file, license_file=license_file)
            if chroma_store_usable(chroma_dir):
                validate_embedding_compatibility(db, chroma_dir, require_metadata=True)
            n = db.execute("SELECT COUNT(*) FROM transcript_files").fetchone()[0]
            db.close()
            check(f"vault opens & decrypts ({n} videos, licensed to {who})", True)
        except Exception as e:
            check("vault opens & decrypts", False, str(e))
    # Production ranking is deterministic RRF-only. The buyer's LLM performs the
    # final relevance judgement; no cross-encoder download or warm-up is needed.
    print("  ✓ retrieval ranking: deterministic RRF-only (cross-encoder disabled by design)")
    print("\n" + ("✅ All good — add the MCP config to your AI agent and start asking questions."
                  if ok else "Some checks failed; fix the items above and re-run."))
    return 0 if ok else 1


def _safe_extractall(tar, path):
    """Extract a tar safely on ANY Python version.

    Validation always runs first (version-independent): reject path traversal,
    reject symlink/hardlink entries, and only keep regular files/dirs. Then
    extract just the vetted members, using the 3.12+ data filter as extra
    defense when the runtime supports it.
    """
    dest = os.path.abspath(path)
    safe = []
    for m in tar.getmembers():
        target = os.path.abspath(os.path.join(path, m.name))
        if target != dest and not target.startswith(dest + os.sep):
            raise VaultError("Vault archive contains an unsafe path; refusing to extract.")
        if m.issym() or m.islnk():
            raise VaultError("Vault archive contains a link entry; refusing to extract.")
        if m.isfile() or m.isdir():
            safe.append(m)
        # else: silently skip devices/fifos/etc. — never present in our vaults
    try:
        tar.extractall(path=path, members=safe, filter='data')  # Python 3.12+ / backports
    except TypeError:
        tar.extractall(path=path, members=safe)  # older Python — already vetted above



def fast_search(db, semantic_retriever, query, top_k=3):
    """Fast single-query: FTS + BGE + RRF only. No KG, no reranker, no diversity."""
    t0 = time.perf_counter()
    fts = fts_candidates(db, query, limit=10)
    sem = semantic_candidates(db, semantic_retriever, query, top_k=10)
    combined = apply_rrf_scores(fts, sem)
    combined = dedup_candidates(combined)
    combined.sort(key=lambda c: c.get('_rrf_score', 0) or 0, reverse=True)
    top = combined[:top_k]
    results = []
    for c in top:
        h = hydrate_candidate_text(db, c)
        snip = (h.get('_full_text') or c.get('text') or '')[:500]
        ts = c.get('start_ts') or c.get('timestamp') or ''
        start_seconds = c.get('start_seconds')
        results.append({
            'video_id': c.get('video_id'),
            'timestamp': ts,
            'start_seconds': start_seconds,
            'title': (c.get('title') or '')[:150],
            'snippet': snip,
            'video_url': youtube_link(c.get('video_id'), ts, start_seconds),
            'retrieval_sources': ['keyword', 'semantic'],
            'matched_queries': [query],
        })
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    return {'results': results, 'total_ms': elapsed_ms, 'n_results': len(results)}


# ── Deterministic SQL-first retrieval ────────────────────────────────────
# Tried before the hybrid FTS5+Chroma+RRF path. Returns finalized result
# dicts when direct SQL evidence is strong, None otherwise (caller falls
# back to the full hybrid pipeline).

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "between", "by", "do", "does", "for",
    "difference", "from", "how", "i", "in", "is", "it", "model", "my", "of", "on",
    "or", "sometimes", "the", "this", "to", "utilise", "use", "what", "when", "why",
    "with", "your",
}
_TOKENIZE_RE = re.compile(r"[A-Za-z0-9']+")


def _corpus_count(db, term):
    """Total literal occurrences of a term across all chunk bodies."""
    row = db.execute(
        "SELECT COUNT(*) FROM transcripts_fts WHERE content LIKE ? COLLATE NOCASE",
        (f"%{term}%",),
    ).fetchone()
    return row[0] if row else 0


def _discover_search_facets(db, query):
    """Extract query facets: known entities, shortform expansions, then
    discriminative query tokens. Returns (facets, reasons) where each facet
    is a list of alternative string forms (alt1, alt2, ...)."""
    if not query:
        return [], []
    qlow = query.lower()
    facets = []
    reasons = []

    # 1) Known multi-word entities (e.g. "Silver Bullet") present verbatim.
    try:
        entities = [
            r[0] for r in db.execute(
                "SELECT name FROM entities ORDER BY LENGTH(name) DESC"
            ).fetchall() if r[0]
        ]
    except Exception:
        entities = []
    for entity in entities:
        if re.search(
            rf"(?<![A-Za-z0-9]){re.escape(entity.lower())}(?![A-Za-z0-9])", qlow
        ):
            facets.append([entity.lower()])
            reasons.append(f"entity:{entity}")

    # 2) Acronyms and their canonical expansions (e.g. NWOG→new week opening gap).
    for token in _TOKENIZE_RE.findall(query):
        upper = token.upper()
        if upper in ICT_SHORTFORMS and (
            token == upper or upper in CASE_INSENSITIVE_SHORTFORMS
        ):
            full = ICT_SHORTFORMS[upper].split(" — ")[0].split(" / ")[0].strip().lower()
            alts = [upper.lower()]
            if full and full not in alts:
                alts.append(full)
            facets.append(alts)
            reasons.append(f"shortform:{upper}->{full}")

    # Deduplicate facets
    unique = []
    seen_norm = set()
    for facet in facets:
        norm = tuple(sorted(set(facet)))
        if norm not in seen_norm:
            seen_norm.add(norm)
            unique.append(list(norm))
    facets = unique

    # 3) Discriminative query tokens not already covered by facets above.
    covered_words = set()
    for facet in facets:
        for alt in facet:
            covered_words.update(_TOKENIZE_RE.findall(alt.lower()))
    tokens = []
    for token in _TOKENIZE_RE.findall(query):
        low = token.lower()
        if len(low) >= 4 and low not in _STOP_WORDS and low not in covered_words:
            tokens.append(low)
    counts = [(t, _corpus_count(db, t)) for t in dict.fromkeys(tokens)]
    discriminative = [(t, c) for c, t in
                      sorted((c, t) for t, c in counts if 0 < c <= 1000)]
    for token, count in discriminative[:2]:
        facets.append([token])
        reasons.append(f"discriminative_token:{token}:{count}")

    return facets, reasons


def _sql_matching_rows(db, facets, limit=1000):
    """Run AND-combined LIKE clauses across all facets. Returns raw DB rows
    or [] on any error."""
    if not facets:
        return []
    clauses = []
    params = []
    for facet in facets:
        clauses.append(
            "(" + " OR ".join("content LIKE ? COLLATE NOCASE" for _ in facet) + ")"
        )
        params.extend(f"%{alt}%" for alt in facet)
    sql = (
        "SELECT chunk_id, chunk_index, title, video_id, playlist, "
        "start_ts, end_ts, start_seconds, end_seconds, content "
        "FROM transcripts_fts WHERE " + " AND ".join(clauses) + " LIMIT ?"
    )
    params.append(limit)
    try:
        return db.execute(sql, params).fetchall()
    except Exception:
        return []


def _row_match_score(row, facets):
    """Score a row by (facet_match_count, total_occurrences, -chunk_index).
    Prefers rows that cover many facets, then dense mentions."""
    text = row[9].lower()
    matched = 0
    occurrences = 0
    for facet in facets:
        counts = [text.count(alt.lower()) for alt in facet]
        if any(c > 0 for c in counts):
            matched += 1
            occurrences += max(counts)
    return (matched, occurrences, -int(row[1] or 0))


def _sql_row_to_packet(db, row):
    """Convert a raw SQL row into a result dict compatible with
    finalize_ranked_results, with adjacent context merged into the snippet."""
    chunk_id, idx, title, video_id, playlist, start_ts, end_ts, start_seconds, end_seconds, content = row

    # Fetch adjacent chunks (before + after) from the same video.
    adjacent = db.execute(
        "SELECT chunk_index, start_ts, end_ts, start_seconds, content "
        "FROM transcripts_fts WHERE video_id=? AND chunk_index BETWEEN ? AND ? "
        "ORDER BY chunk_index",
        (video_id, max(0, int(idx) - 1), int(idx) + 1),
    ).fetchall()

    # Build the full text from match + context.
    parts = []
    for a in adjacent:
        a_idx = a[0]
        if a_idx == idx:
            parts.append(a[4])  # the match itself
        else:
            parts.append(a[4])  # before/after context
    full_text = "\n\n".join(parts)

    return {
        "title": title,
        "video_id": video_id,
        "playlist": playlist,
        "chunk_id": chunk_id,
        "chunk_index": idx,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "timing_precision": "sql_raw",
        "timestamp": start_ts,
        "_full_text": full_text,
        "method": "sql_first",
        "retrieval_sources": ["keyword"],
        "matched_queries": [],
        "source_file": "",
    }


def search_sql_first(db, query, top_k=5):
    """Try deterministic SQL-first retrieval for queries that name known
    ICT entities, shortforms, or rare discriminative tokens.

    Returns a list of finalized result dicts (same format as
    finalize_ranked_results) when direct SQL evidence is strong, or None
    when evidence is too weak — the caller falls back to the hybrid path.

    Parameters
    ----------
    db : sqlite3.Connection
        Opened vault database handle.
    query : str
        The buyer's question.
    top_k : int
        Maximum results to return on the direct path.

    Returns
    -------
    list[dict] | None
    """
    if not query:
        return None

    t0 = time.perf_counter()
    facets, reasons = _discover_search_facets(db, query)

    # No discriminating facets at all → cannot target a literal search.
    if not facets:
        return None

    all_rows = _sql_matching_rows(db, facets)

    # When no single chunk covers every facet (common for comparison
    # questions), retrieve per-facet independently so both concepts get
    # evidence.
    facet_pools = []
    if not all_rows and len(facets) > 1:
        for facet in facets:
            fr = _sql_matching_rows(db, [facet], limit=200)
            fr.sort(key=lambda r: _row_match_score(r, [facet]), reverse=True)
            facet_pools.append((facet, fr))
            all_rows.extend(fr)
        seen = set()
        deduped = []
        for row in all_rows:
            if row[0] not in seen:
                seen.add(row[0])
                deduped.append(row)
        all_rows = deduped

    if not all_rows:
        return None

    # Select top_k with diversity (max 2 per video) and facet reservation.
    selected = []
    selected_ids = set()
    seen_videos = Counter()

    # Reserve evidence for every facet when per-facet pools were built
    # (comparison questions like "MSS vs CISD").
    if facet_pools:
        per_facet = max(1, top_k // len(facet_pools))
        for facet, rows in facet_pools:
            taken = 0
            for row in rows:
                if row[0] in selected_ids or seen_videos[row[3]] >= 2:
                    continue
                selected.append(row)
                selected_ids.add(row[0])
                seen_videos[row[3]] += 1
                taken += 1
                if taken >= per_facet or len(selected) >= top_k:
                    break

    all_rows.sort(key=lambda r: _row_match_score(r, facets), reverse=True)
    for row in all_rows:
        if len(selected) >= top_k:
            break
        if row[0] in selected_ids or seen_videos[row[3]] >= 2:
            continue
        selected.append(row)
        selected_ids.add(row[0])
        seen_videos[row[3]] += 1

    if not selected:
        return None

    # Coverage check: every facet's alternatives must appear somewhere
    # in the selected evidence text.
    joined = "\n".join(row[9] for row in selected).lower()
    coverage = [any(alt.lower() in joined for alt in facet) for facet in facets]
    if not all(coverage):
        # Weak coverage — caller should fall back to the hybrid pipeline.
        return None

    # Build rich packets with adjacent context, then finalize.
    packets = [_sql_row_to_packet(db, row) for row in selected]
    for p in packets:
        # Manually add the video URL since our packets include chunk_id
        # (hidden from buyer) and the proper fields.
        pass
    results = finalize_ranked_results(packets, query=query)
    # Tag each result with its reason and coverage for debuggability
    # (removed by finalize_ranked_results for privacy).
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    for r in results:
        r["_sql_route"] = "direct"
        r["_sql_facets"] = facets
        r["_sql_latency_ms"] = elapsed_ms
    return results
