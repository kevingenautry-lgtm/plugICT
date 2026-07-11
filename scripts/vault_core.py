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
from collections import OrderedDict
from pathlib import Path

VAULT_DIR = Path(__file__).parent.resolve()
# Paths default to next-to-the-script, but can be overridden via env vars
# (handy for tests, and for buyers who keep the vault on another drive).
VAULT_FILE = Path(os.environ.get("ICT_VAULT_FILE", VAULT_DIR / "ict-vault.kevin"))
LICENSE_FILE = Path(os.environ.get("ICT_VAULT_LICENSE", VAULT_DIR / "license.key"))

TEMP_PREFIX = "ict_vault_"
_CHUNK = 4 * 1024 * 1024  # 4 MB streaming chunk
MIN_RERANK_SCORE = -6.0
MMR_LAMBDA = 0.7
SEARCH_CACHE_MAX = 100
SNIPPET_DEFAULT_CHARS = 500
SNIPPET_MAX_CHARS = 1000
CONTEXT_BEFORE_MAX_CHARS = 500
CONTEXT_CURRENT_MAX_CHARS = 1000
CONTEXT_AFTER_MAX_CHARS = 500
CONTEXT_TOTAL_MAX_CHARS = 2000
MAX_QUERY_VARIANTS = 4
MAX_TOP_K = 5
RESEARCH_MAX_TOP_K = 10
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
VECTOR_SCHEMA_VERSION = "2"
CHUNK_SCHEMA_VERSION = "2"
CHUNK_ID_STRATEGY = "sha1-source-file-chunk-index-start-ts-v1"

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
    root = tempfile.gettempdir()
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
        'MB': 'Mitigation Block — An order block partly mitigated by price returning to it',
        'RB': 'Rejection Block — An order block where price strongly rejected on first touch',
        'PB': 'Propulsion Block — A strong order block that propelled price through multiple levels',
    },
    "Premium & Discount": {
        'PD Array': 'Price Delivery Array — Set of levels where price is expected to react',
        'OTE': 'Optimal Trade Entry — Entry zone at 61.8-79% retracement of a move',
        'EQ': 'Equilibrium — Midpoint of a range (50% level), acts as magnet for price',
        'OTE Zone': 'Optimal Trade Entry Zone — 62%-79% retracement region for entries',
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
        'total': rows.get('demo_total', '576'),
        'cta': rows.get('demo_cta', 'https://YOUR-SITE/#pricing'),
    }


def youtube_link(video_id, start_ts=None):
    """Deep link to the exact moment: https://youtu.be/ID?t=SECONDS.

    start_ts is the transcript timestamp like '12:34' or '1:02:07'. Falls back
    to a plain video link when the timestamp is missing/zero/unparsable.
    """
    if not video_id:
        return ""
    base = f"https://youtu.be/{video_id}"
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
    global vault_hash
    if not vault_file.exists():
        raise VaultError(
            "ict-vault.kevin not found next to mcp_server.py.\n"
            "  Make sure the vault file downloaded fully and sits in this folder."
        )

    info = load_license(license_file)
    vault_key = _unwrap_vault_key(info)
    expected_hash = info.get('VAULT_HASH', '')

    tmpdir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    _temp_dirs.append(tmpdir)
    db_fd, db_path = tempfile.mkstemp(prefix='sqlite_', suffix='.db', dir=tmpdir)
    os.close(db_fd)
    chroma_dir = os.path.join(tmpdir, 'chroma')
    os.makedirs(chroma_dir, exist_ok=True)
    chroma_tar_path = os.path.join(tmpdir, 'chroma.tar')

    # 1) Read the 20-byte header first (needs only the first plaintext chunk).
    stream = _decrypt_stream(vault_key, vault_file, on_progress)
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

    # 3) Verify integrity (hash covers the whole encrypted file).
    computed = getattr(_decrypt_stream, "last_hash", "")
    if expected_hash and computed and computed != expected_hash:
        raise VaultError("Vault file failed its integrity check (corrupted download). Please re-download ict-vault.kevin.")
    if computed and computed != vault_hash:
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
    """Attach Reciprocal Rank Fusion scores before dedup/rerank."""
    ranks = {}
    scores = {}
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
            scores[key] = scores.get(key, 0.0) + (1.0 / (60 + int(rank)))
    for c in cands:
        c['rrf_score'] = scores.get(_cand_key(c), 0.0)
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
    try:
        if has_chunk_id:
            select = ["chunk_id", "title", "video_id", "start_ts", "playlist", "source_file", "content"]
            if has_chunk_index:
                select.append("chunk_index")
            if has_end_ts:
                select.append("end_ts")
            sql = ("SELECT " + ", ".join(select) + " "
                   "FROM transcripts_fts WHERE content MATCH ?")
        else:
            select = ["rowid", "title", "video_id", "start_ts", "playlist", "source_file", "content"]
            if has_chunk_index:
                select.append("chunk_index")
            if has_end_ts:
                select.append("end_ts")
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
            out.append(item)
    except sqlite3.Error as e:
        print(f"(keyword search unavailable: {e})", file=sys.stderr)
    return out


def _chunk_select_columns(db):
    cols = _fts_columns(db)
    wanted = ['chunk_id', 'title', 'video_id', 'playlist', 'start_ts',
              'end_ts', 'chunk_index', 'source_file', 'content']
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
                'end_ts', 'chunk_index', 'source_file'):
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
    """Best-effort preload of the cross-encoder so the buyer's FIRST real query
    isn't stalled by a model download. Called from run_doctor (setup). Returns
    True if the model is ready, False if unavailable (rerank then no-ops)."""
    global _reranker
    if _reranker is not None:
        return True
    try:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
        return True
    except Exception:
        return False


def rerank(query, candidates, top_k):
    """Cross-encoder rerank; degrades gracefully if the model isn't available."""
    global _reranker
    if len(candidates) <= 1 and _reranker is None:
        return candidates[:top_k]
    rrf_norm = _normalized_rrf(candidates)
    try:
        if _reranker is None:
            from sentence_transformers import CrossEncoder
            _reranker = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
        scores = _reranker.predict([(query, _cand_text(c)[:1500]) for c in candidates])
        for c, s in zip(candidates, scores):
            boosted = float(s) + (0.5 if c.get('dual_hit') else 0.0)
            c['rerank_score'] = boosted
            c['final_score'] = boosted + (0.1 * rrf_norm[id(c)])
        candidates.sort(key=lambda c: c.get('final_score', c.get('rerank_score', 0.0)), reverse=True)
        filtered = [c for c in candidates if c.get('rerank_score', 0.0) >= MIN_RERANK_SCORE]
        if not filtered:
            return []
        return apply_mmr(filtered, top_k)
    except Exception as e:
        print(f"(rerank skipped: {e})", file=sys.stderr)
        candidates.sort(key=lambda c: c.get('rrf_score', 0.0), reverse=True)
        return candidates[:top_k]


def _clamp_chars(value, default, hard_cap):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, hard_cap))


def make_snippet(text, max_chars=SNIPPET_DEFAULT_CHARS):
    max_chars = _clamp_chars(max_chars, SNIPPET_DEFAULT_CHARS, SNIPPET_MAX_CHARS)
    clean = (text or "").replace("<b>", "").replace("</b>", "").strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean[:max_chars]


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
    """Keep higher-score chunk; union metadata; prefer longer text for expand."""
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
    if len(_cand_text(drop)) > len(_cand_text(keep)):
        for field in ('_full_text', 'text', 'snippet'):
            if drop.get(field):
                keep[field] = drop[field]
    keep['_merged_from'] = (keep.get('_merged_from') or 1) + (drop.get('_merged_from') or 1)
    return keep


def _merge_adjacent_same_video(cands, merge_gap_sec=MERGE_GAP_SEC):
    """Within each video, merge chunks whose start times are within merge_gap_sec."""
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
            if pts is not None and abs(ts - pts) <= merge_gap_sec:
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
    pool = sorted(pool, key=_cand_score, reverse=True)

    selected = []
    per_video = {}
    last_ts_for_video = {}

    for c in pool:
        if len(selected) >= top_k:
            break
        vkey = _video_bucket_key(c)
        count = per_video.get(vkey, 0)
        if count >= max_per_video:
            continue
        if count >= 1 and vkey[0] == 'video':
            ts = _cand_ts_seconds(c)
            prev_ts = last_ts_for_video.get(vkey)
            if ts is not None and prev_ts is not None:
                if abs(ts - prev_ts) < distinct_gap_sec:
                    continue
            elif ts is None:
                continue
        selected.append(c)
        per_video[vkey] = count + 1
        ts = _cand_ts_seconds(c)
        if ts is not None:
            last_ts_for_video[vkey] = ts

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


def finalize_ranked_results(ranked, snippet_chars=SNIPPET_DEFAULT_CHARS):
    out = []
    for c in ranked:
        sources = _merge_unique_values(c.get('retrieval_sources'), c.get('source'), c.get('method'))
        item = {
            'title': c.get('title', ''),
            'video_id': c.get('video_id', ''),
            'timestamp': c.get('start_ts') or c.get('timestamp') or '',
            'start_ts': c.get('start_ts') or c.get('timestamp') or '',
            'end_ts': c.get('end_ts') or '',
            'playlist': c.get('playlist', ''),
            'method': "+".join(sources) if sources else (c.get('method') or c.get('source') or ''),
            'retrieval_sources': sources,
            'matched_queries': _merge_unique_values(c.get('matched_queries')),
            'snippet': make_snippet(_cand_text(c), snippet_chars),
        }
        if c.get('result_ref'):
            item['result_ref'] = c['result_ref']
        if c.get('video_id'):
            item['video_url'] = youtube_link(c.get('video_id'), item['timestamp'])
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
                'title': candidate.get('title'),
                'video_id': candidate.get('video_id'),
                'playlist': candidate.get('playlist'),
            },
            'created_at': now,
            'expires_at': now + self.ttl_seconds,
            'uses': 0,
        }
        return ref

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
            result = self._get_collection().query(query_texts=[query_text], n_results=limit, where=where)
            ids = result.get('ids', [[]])[0]
            docs = result.get('documents', [[]])[0]
            metas = result.get('metadatas', [[]])[0]
            for i, doc in enumerate(docs):
                m = metas[i] if i < len(metas) else {}
                out.append({'source': 'semantic', 'title': m.get('title', ''),
                            'method': 'semantic',
                            'chunk_id': ids[i] if i < len(ids) else m.get('chunk_id', ''),
                            'video_id': m.get('video_id', ''),
                            'start_ts': m.get('start_ts', ''),
                            'timestamp': m.get('start_ts', ''),
                            'end_ts': m.get('end_ts', ''),
                            'chunk_index': m.get('chunk_index'),
                            'playlist': m.get('playlist', ''),
                            'source_file': m.get('source_file', ''),
                            '_full_text': doc,
                            '_rank_in_source': i,
                            '_rrf_source': rrf_source,
                            'retrieval_sources': ['semantic'],
                            'matched_queries': [matched_query or query_text]})
        except ImportError:
            print("(semantic search unavailable - chromadb not installed)", file=sys.stderr)
        except Exception as e:
            print(f"(semantic search unavailable: {e})", file=sys.stderr)
        return out

    def search(self, query, playlist=None, session=None, top_k=5):
        """Return (ranked_candidates, expanded_query, expansion_changed)."""
        expanded, changed = expand_query(query)
        cached = get_cached_results(query, top_k, playlist)
        if cached is not None:
            return cached, expanded, changed
        candidates = []
        pool = top_k + 5

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

    def multi_search(self, question, queries, playlist=None, top_k=5, snippet_chars=SNIPPET_DEFAULT_CHARS):
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
    # Preload the reranker now (one-time ~90MB) so the first real search is fast.
    # Not a hard requirement — search still works (unranked) if it can't load.
    print("  … preparing the reranker model (one-time download, ~90MB)…")
    if warm_reranker():
        check("reranker model ready", True)
    else:
        print("  ⚠ reranker model unavailable — search still works, just unranked "
              "(install sentence-transformers to enable it)")
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
