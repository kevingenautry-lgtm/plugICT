"""
vault_core.py — Shared core for the ICT Knowledge Vault
========================================================
Single source of truth for everything the buyer-side tools need:

  * License loading & validation
  * Streaming vault decryption (low RAM) + integrity check
  * Vault format v1 (raw) and v2 (zstd-compressed) support
  * ICT shortform glossary (+ categories for real related terms)
  * Session detection, playlist classification, FTS sanitisation

Both query.py and mcp_server.py import from here so the decrypt logic can
never drift out of sync again.
"""

import io
import os
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
from pathlib import Path

VAULT_DIR = Path(__file__).parent.resolve()
# Paths default to next-to-the-script, but can be overridden via env vars
# (handy for tests, and for buyers who keep the vault on another drive).
VAULT_FILE = Path(os.environ.get("ICT_VAULT_FILE", VAULT_DIR / "ict-vault.kevin"))
LICENSE_FILE = Path(os.environ.get("ICT_VAULT_LICENSE", VAULT_DIR / "license.key"))

TEMP_PREFIX = "ict_vault_"
_CHUNK = 4 * 1024 * 1024  # 4 MB streaming chunk

# Vault format versions understood by this build.
FORMAT_V1_RAW = 1   # [ver:4][db_size:8][chroma_size:8][db][chroma]
FORMAT_V2_ZSTD = 2  # [ver:4][db_size:8][chroma_size:8][ zstd(db + chroma) ]
HEADER = struct.Struct(">IQQ")  # 20 bytes


# ── Errors ───────────────────────────────────────────────────────────────────
class VaultError(Exception):
    """A buyer-facing problem with a clear, actionable message."""


# ── Temp lifecycle ───────────────────────────────────────────────────────────
_temp_dirs = []


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
    """Expand user-typed uppercase ICT acronyms to their full term.

    Only expands tokens the user wrote in ALL CAPS that exactly match a
    shortform key. This avoids false-expanding lowercase words like 'ms' or
    'bs' that occur in ordinary sentences.  Returns (expanded, changed).
    """
    if not query:
        return query, False
    out = []
    changed = False
    for tok in query.split():
        core = tok.strip('?!.,;:()')
        if core and core == core.upper() and core in ICT_SHORTFORMS:
            full = ICT_SHORTFORMS[core].split(' — ')[0].split(' / ')[0].strip()
            out.append(full)
            changed = True
        else:
            out.append(tok)
    expanded = ' '.join(out)
    return expanded, changed


# ── Build-side packaging (used by build.py; keeps format symmetric) ──────────
def pack_and_encrypt(db_bytes, chroma_bytes, compress=True, level=19):
    """Package db+chroma, optionally zstd-compress, then AES-256-CTR encrypt.

    Returns (encrypted_blob, vault_key, sha256_hex). The blob layout is
    [iv:16][ciphertext] where the plaintext is the versioned header + body.
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

    vault_key = secrets.token_bytes(32)
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
            "  Place the license.key we sent you next to query.py, then try again."
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
    if not vault_file.exists():
        raise VaultError(
            "ict-vault.kevin not found next to query.py.\n"
            "  Make sure the vault file downloaded fully and sits in this folder."
        )

    info = load_license(license_file)
    vault_key = _unwrap_vault_key(info)
    expected_hash = info.get('VAULT_HASH', '')

    tmpdir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    _temp_dirs.append(tmpdir)
    db_path = os.path.join(tmpdir, 'master.db')
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

    # 2) Route the rest of the stream into master.db + chroma.tar.
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

    # 4) Extract ChromaDB tar safely, then drop the tar.
    with tarfile.open(chroma_tar_path) as tar:
        _safe_extractall(tar, chroma_dir)
    try:
        os.remove(chroma_tar_path)
    except OSError:
        pass

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=OFF")
    return db, chroma_dir, info.get('LICENSED_TO', 'unknown')


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
