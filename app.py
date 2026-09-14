from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
import asyncio, concurrent.futures, os, re, time, logging, requests as http_requests, hashlib, json, threading
from datetime import datetime, timedelta
from collections import OrderedDict
from dotenv import load_dotenv
from sqlalchemy import create_engine, text as sa_text

# sqlglot — optional, degrades gracefully if not installed
try:
    import sqlglot
    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Show outbound HTTP requests (Ollama calls, external APIs) in the terminal
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.INFO)

DB_SCHEMA  = os.getenv("DB_SCHEMA", "")
TABLE_NAME = os.getenv("DB_TABLE", "citizen_master_records")
FULL_TABLE = f"{DB_SCHEMA}.{TABLE_NAME}" if DB_SCHEMA else TABLE_NAME

# Generic mode: when on, the app introspects whatever SQLite DB it is pointed at
# instead of assuming the citizen schema. Templates, the synonym map, the
# location guard and the citizen intelligibility vocabulary are all bypassed —
# every question goes to the model against the discovered schema.
GENERIC_MODE    = os.getenv("GENERIC_MODE", "false").lower() in ("1", "true", "yes")
_GENERIC_SCHEMA = None       # populated at startup when GENERIC_MODE is on
_GENERIC_HINT   = ""

app = FastAPI(title="Text-to-SQL Chatbot")
# Was allow_origins=["*"], which let any website call this API through a
# visitor's browser. Defaults to localhost; set ALLOWED_ORIGINS when deploying.
_ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000"
).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

# ── Database ──────────────────────────────────────────────────────────────────
from langchain_community.utilities import SQLDatabase

db_uri = os.getenv("DB_URI")
if not db_uri:
    host     = os.getenv("DB_HOST", "localhost")
    port     = os.getenv("DB_PORT", "5432")
    username = os.getenv("DB_USER") or os.getenv("DB_USERNAME", "root")
    password = os.getenv("DB_PASSWORD", "")
    dbname   = os.getenv("DB_NAME") or os.getenv("DB_SCHEMA", "")
    dialect  = os.getenv("DB_DIALECT", "postgresql").lower()
    from urllib.parse import quote_plus
    u, p = quote_plus(username), quote_plus(password)
    if dialect == "mysql":
        db_uri = f"mysql+pymysql://{u}:{p}@{host}:{port}/{dbname}"
    elif dialect == "sqlite":
        db_uri = f"sqlite:///{dbname}"
    else:
        db_uri = f"postgresql+psycopg2://{u}:{p}@{host}:{port}/{dbname}"

db     = SQLDatabase.from_uri(db_uri, sample_rows_in_table_info=2)
engine = create_engine(db_uri)

# In generic mode, discover the schema from the database itself and override the
# hardcoded citizen table name with whatever is actually there.
if GENERIC_MODE:
    try:
        import generic_schema as _gs
        _GENERIC_SCHEMA = _gs.introspect(db_uri, preferred_table=os.getenv("DB_TABLE") or None)
        _GENERIC_HINT   = _gs.build_schema_hint(_GENERIC_SCHEMA)
        TABLE_NAME = _GENERIC_SCHEMA.primary_table
        FULL_TABLE = TABLE_NAME
        logger.info("GENERIC_MODE on — using discovered table '%s'.", TABLE_NAME)
    except Exception as e:
        logger.error("Generic introspection failed (%s); falling back to citizen defaults.", e)
        GENERIC_MODE = False

# ── Column vocabulary ─────────────────────────────────────────────────────────
# The prompt described values but never listed the actual column names, so the
# model was free to invent plausible ones — "most females in a city" produced
# GROUP BY city, which does not exist. Read the real columns from the database
# so the prompt can state them and so wrong ones can be repaired.
def _load_table_columns() -> list[str]:
    try:
        from sqlalchemy import inspect as _sa_inspect
        return [c["name"] for c in _sa_inspect(engine).get_columns(TABLE_NAME)]
    except Exception as e:
        logger.warning("Could not read columns for %s: %s", TABLE_NAME, e)
        return []

_TABLE_COLUMNS = _load_table_columns()
_TABLE_COLUMNS_LOWER = {c.lower() for c in _TABLE_COLUMNS}
if _TABLE_COLUMNS:
    logger.info("Loaded %d columns from %s.", len(_TABLE_COLUMNS), TABLE_NAME)

# Names the model reaches for that have a real equivalent here. Only applied
# when the invented name is NOT a real column and the target IS one, so this
# can never rewrite a valid query.
_COLUMN_ALIASES = {
    "city": "district", "town": "district", "place": "district",
    "location": "district", "area": "district", "region": "state",
    "province": "state", "sex": "gender", "income": "individual_income",
    "salary": "individual_income", "earnings": "individual_income",
    "wage": "individual_income", "wages": "individual_income",
    "caste": "category", "community": "category", "dob": "date_of_birth",
    "occupation": "employment_status", "job": "employment_status",
    "profession": "employment_status", "family_size": "family_member_count",
    "household_size": "family_member_count", "vehicles": "vehicle_count",
    "properties": "property_count",
}

def _repair_columns(sql: str) -> str:
    """Swap invented column names for the real ones. Returns the SQL unchanged
    when nothing needs fixing."""
    if not sql or not _TABLE_COLUMNS_LOWER:
        return sql

    # Mask quoted literals first, so a value like 'Kansas City' or a district
    # name containing one of these words is never rewritten.
    literals: list[str] = []
    def _mask(m):
        literals.append(m.group(0))
        return f"\x00{len(literals) - 1}\x00"
    masked = re.sub(r"'(?:[^']|'')*'", _mask, sql)

    fixed, changes = masked, []
    for wrong, right in _COLUMN_ALIASES.items():
        if wrong in _TABLE_COLUMNS_LOWER or right.lower() not in _TABLE_COLUMNS_LOWER:
            continue                      # never rewrite a column that exists
        pat = re.compile(rf'(?<![\w.]){re.escape(wrong)}(?![\w(])', re.IGNORECASE)
        if pat.search(fixed):
            fixed = pat.sub(right, fixed)
            changes.append(f"{wrong}->{right}")

    fixed = re.sub(r"\x00(\d+)\x00", lambda m: literals[int(m.group(1))], fixed)
    if changes:
        logger.info("Column repair applied (%s): %s", ", ".join(changes), fixed[:120])
    return fixed


# ── Turso (remote execution) ───────────────────────────────────────────────────
_TURSO_URL   = os.getenv("TURSO_URL", "").replace("libsql://", "https://").rstrip("/")
_TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
_USE_TURSO   = bool(_TURSO_URL and _TURSO_TOKEN)

def _turso_execute(sql: str) -> tuple:
    headers = {"Authorization": f"Bearer {_TURSO_TOKEN}", "Content-Type": "application/json"}
    body    = {"requests": [{"type": "execute", "stmt": {"sql": sql}}, {"type": "close"}]}
    resp    = http_requests.post(f"{_TURSO_URL}/v2/pipeline", json=body, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    for item in data.get("results", []):
        if item.get("type") == "error":
            raise RuntimeError(f"Turso error: {item.get('error', item)}")
    result  = data["results"][0]["response"]["result"]
    columns = [col["name"] for col in result["cols"]]
    rows    = [[None if cell["type"] == "null" else cell.get("value") for cell in row] for row in result["rows"]]
    return columns, rows

MAX_ROWS = 500

# ── Query cache (bounded LRU, TTL = 10 min) ───────────────────────────────────
# Bounded so a long-running server cannot grow without limit: eviction only ever
# happened on a cache HIT, so a question asked once and never repeated stayed
# resident forever.
_query_cache: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_TTL  = timedelta(minutes=int(os.getenv("CACHE_TTL_MIN", "10")))
_CACHE_MAX  = int(os.getenv("CACHE_MAX_ENTRIES", "500"))
_cache_lock = threading.Lock()

# Shared thread pool for narration calls (Ollama path).
# Created once at module load — avoids per-request thread spawn/teardown overhead.
_NARRATION_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="narration")

def _cache_key(q: str) -> str:
    return hashlib.md5(q.lower().strip().encode()).hexdigest()

def _cache_sweep_locked(now: datetime):
    """Drop expired entries. Caller must hold _cache_lock."""
    dead = [k for k, e in _query_cache.items() if now - e["ts"] >= _CACHE_TTL]
    for k in dead:
        _query_cache.pop(k, None)

def _cache_get(q: str):
    k, now = _cache_key(q), datetime.now()
    with _cache_lock:
        entry = _query_cache.get(k)
        if entry is None:
            return None
        if now - entry["ts"] >= _CACHE_TTL:
            _query_cache.pop(k, None)
            return None
        _query_cache.move_to_end(k)          # mark as recently used
        return dict(entry["data"])

def _cache_set(q: str, data: dict):
    k, now = _cache_key(q), datetime.now()
    with _cache_lock:
        _query_cache[k] = {"data": data, "ts": now}
        _query_cache.move_to_end(k)
        _cache_sweep_locked(now)
        while len(_query_cache) > _CACHE_MAX:
            _query_cache.popitem(last=False)  # evict least-recently-used

# ── Rate limiter ───────────────────────────────────────────────────────────────
# Also bounded: stale IP buckets are swept so the dict cannot accumulate one
# entry per client address seen since boot. Locked because FastAPI runs sync
# endpoints across a threadpool.
_rate_limits: dict[str, list] = {}
_RATE_LIMIT_RPM   = int(os.getenv("RATE_LIMIT_RPM", "20"))
_rate_lock        = threading.Lock()
_rate_last_sweep  = datetime.now()
_RATE_SWEEP_EVERY = timedelta(minutes=5)

def _check_rate_limit(ip: str):
    global _rate_last_sweep
    now    = datetime.now()
    window = now - timedelta(minutes=1)
    with _rate_lock:
        if now - _rate_last_sweep > _RATE_SWEEP_EVERY:
            for other in [k for k, v in _rate_limits.items() if not v or v[-1] < window]:
                _rate_limits.pop(other, None)
            _rate_last_sweep = now
        hits = [t for t in _rate_limits.get(ip, []) if t > window]
        if len(hits) >= _RATE_LIMIT_RPM:
            _rate_limits[ip] = hits
            raise HTTPException(status_code=429, detail=f"Rate limit: {_RATE_LIMIT_RPM} req/min. Try again shortly.")
        hits.append(now)
        _rate_limits[ip] = hits


# ══════════════════════════════════════════════════════════════════════════════
# 1. ENTITY EXTRACTOR
# Maps natural-language synonyms → (column, canonical_value)
# Runs BEFORE the LLM so the prompt gets exact DB values, not the user's wording.
# ══════════════════════════════════════════════════════════════════════════════
ENTITY_SYNONYMS: dict[str, tuple[str, str]] = {
    # gender
    "women":       ("gender", "Female"), "woman":     ("gender", "Female"),
    "female":      ("gender", "Female"), "females":   ("gender", "Female"),
    "ladies":      ("gender", "Female"), "lady":      ("gender", "Female"),
    "girl":        ("gender", "Female"), "girls":     ("gender", "Female"),
    "men":         ("gender", "Male"),   "man":        ("gender", "Male"),
    "male":        ("gender", "Male"),   "males":      ("gender", "Male"),
    "boys":        ("gender", "Male"),   "boy":        ("gender", "Male"),
    "transgender": ("gender", "Transgender"), "transgenders": ("gender", "Transgender"),
    # district aliases
    "trivandrum":          ("district", "Thiruvananthapuram"),
    "thiruvananthapuram":  ("district", "Thiruvananthapuram"),
    "tvm":                 ("district", "Thiruvananthapuram"),
    "cochin":              ("district", "Ernakulam"),
    "kochi":               ("district", "Ernakulam"),
    "ernakulam":           ("district", "Ernakulam"),
    "calicut":             ("district", "Kozhikode"),
    "kozhikode":           ("district", "Kozhikode"),
    "trichur":             ("district", "Thrissur"),
    "thrissur":            ("district", "Thrissur"),
    "alleppey":            ("district", "Alappuzha"),
    "alappuzha":           ("district", "Alappuzha"),
    "quilon":              ("district", "Kollam"),
    "kollam":              ("district", "Kollam"),
    "palghat":             ("district", "Palakkad"),
    "palakkad":            ("district", "Palakkad"),
    "malappuram":          ("district", "Malappuram"),
    "kottayam":            ("district", "Kottayam"),
    "wayanad":             ("district", "Wayanad"),
    "kannur":              ("district", "Kannur"),
    "cannanore":           ("district", "Kannur"),
    "kasaragod":           ("district", "Kasaragod"),
    "kasargod":            ("district", "Kasaragod"),
    "idukki":              ("district", "Idukki"),
    "iduki":               ("district", "Idukki"),
    "pathanamthitta":      ("district", "Pathanamthitta"),
    "bengaluru":           ("district", "Bengaluru"),
    "bangalore":           ("district", "Bengaluru"),
    "bengalore":           ("district", "Bengaluru"),
    "chennai":             ("district", "Chennai"),
    "madras":              ("district", "Chennai"),
    "hyderabad":           ("district", "Hyderabad"),
    "mumbai":              ("district", "Mumbai"),
    "bombay":              ("district", "Mumbai"),
    "new delhi":           ("district", "New Delhi"),
    "north delhi":         ("district", "North Delhi"),
    "pune":                ("district", "Pune"),
    "ahmedabad":           ("district", "Ahmedabad"),
    "surat":               ("district", "Surat"),
    "vadodara":            ("district", "Vadodara"),
    "baroda":              ("district", "Vadodara"),
    "lucknow":             ("district", "Lucknow"),
    "kanpur":              ("district", "Kanpur"),
    "agra":                ("district", "Agra"),
    "coimbatore":          ("district", "Coimbatore"),
    "madurai":             ("district", "Madurai"),
    "salem":               ("district", "Salem"),
    "mysuru":              ("district", "Mysuru"),
    "mysore":              ("district", "Mysuru"),
    "mangaluru":           ("district", "Mangaluru"),
    "mangalore":           ("district", "Mangaluru"),
    "vijayawada":          ("district", "Vijayawada"),
    "visakhapatnam":       ("district", "Visakhapatnam"),
    "vizag":               ("district", "Visakhapatnam"),
    "guntur":              ("district", "Guntur"),
    "warangal":            ("district", "Warangal"),
    "nagpur":              ("district", "Nagpur"),
    # category / caste
    "sc":                  ("category", "SC"),
    "scheduled caste":     ("category", "SC"),
    "st":                  ("category", "ST"),
    "scheduled tribe":     ("category", "ST"),
    "obc":                 ("category", "OBC"),
    "other backward":      ("category", "OBC"),
    "general":             ("category", "General"),
    "ews":                 ("category", "EWS"),
    "economically weaker": ("category", "EWS"),
    # religion
    "hindu":      ("religion", "Hindu"),   "hindus":     ("religion", "Hindu"),
    "muslim":     ("religion", "Muslim"),  "muslims":    ("religion", "Muslim"),
    "islam":      ("religion", "Muslim"),  "islamic":    ("religion", "Muslim"),
    "christian":  ("religion", "Christian"), "christians": ("religion", "Christian"),
    # ration card
    "bpl":              ("ration_card_type", "BPL"),
    "below poverty":    ("ration_card_type", "BPL"),
    "below poverty line": ("ration_card_type", "BPL"),
    "apl":              ("ration_card_type", "APL"),
    "above poverty":    ("ration_card_type", "APL"),
    "aay":              ("ration_card_type", "AAY"),
    "antyodaya":        ("ration_card_type", "AAY"),
    "phh":              ("ration_card_type", "PHH"),
    "nphh":             ("ration_card_type", "NPHH"),
    # employment status
    "unemployed":    ("employment_status", "Unemployed"),
    "self employed": ("employment_status", "Self-Employed"),
    "self-employed": ("employment_status", "Self-Employed"),
    "student":       ("employment_status", "Student"),
    "students":      ("employment_status", "Student"),
    "retired":       ("employment_status", "Retired"),
    "homemaker":     ("employment_status", "Homemaker"),
    "housewife":     ("employment_status", "Homemaker"),
    # marital status
    "single":    ("marital_status", "Single"),
    "married":   ("marital_status", "Married"),
    "widowed":   ("marital_status", "Widowed"),
    "widow":     ("marital_status", "Widowed"),
    "divorced":  ("marital_status", "Divorced"),
    "separated": ("marital_status", "Separated"),
    # boolean flags
    "disabled":          ("disability_flag", "1"),
    "disability":        ("disability_flag", "1"),
    "government employee": ("govt_employee_flag", "1"),
    "govt employee":     ("govt_employee_flag", "1"),
    "tax payer":         ("income_tax_payee", "1"),
    "taxpayer":          ("income_tax_payee", "1"),
    "minority":          ("minority_flag", "1"),
    # state
    "kerala":          ("state", "Kerala"),
    "tamil nadu":      ("state", "Tamil Nadu"),
    "tamilnadu":       ("state", "Tamil Nadu"),
    "karnataka":       ("state", "Karnataka"),
    "andhra":          ("state", "Andhra Pradesh"),
    "andhra pradesh":  ("state", "Andhra Pradesh"),
    "maharashtra":     ("state", "Maharashtra"),
    "gujarat":         ("state", "Gujarat"),
    "telangana":       ("state", "Telangana"),
    "uttar pradesh":   ("state", "Uttar Pradesh"),
    "up":              ("state", "Uttar Pradesh"),
    "delhi":           ("state", "Delhi"),
}

# Sort once by length descending so multi-word phrases match before single words
# ── Plural derivation ─────────────────────────────────────────────────────────
# The map hand-lists some plurals ("females", "muslims") but missed others, so
# "widows in Wayanad" silently lost its marital_status filter. Derive the rest.
def _pluralise(word: str) -> str | None:
    if " " in word or len(word) < 4:
        return None
    if word.endswith("fe"):
        return word[:-2] + "ves"          # housewife -> housewives
    if word.endswith("y") and word[-2] not in "aeiou":
        return word[:-1] + "ies"          # minority -> minorities
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"

# ── Ambiguity guards ──────────────────────────────────────────────────────────
# Some keys are also ordinary English words. Case-insensitive matching turned
# "break up the population by district" into state='Uttar Pradesh'. These keys
# match CASE-SENSITIVELY in their acronym form only — users type acronyms in
# caps, prose does not. Longhand forms ("scheduled caste") are unaffected.
_ACRONYM_ONLY = {"up", "st", "sc", "ews", "aay", "phh", "nphh", "obc", "apl", "bpl"}

# Others are real words that only sometimes mean what the map says. If the
# suppression pattern matches the question, the key is ignored for that question.
_SUPPRESS_IF = {
    "general": re.compile(r'\bin general\b|\bgenerally\b|\bgeneral (idea|sense|question|overview)\b', re.IGNORECASE),
    "single":  re.compile(r'\b(a|the|any|every|each|one)\s+single\b'
                          r'|\bsingle\s+(district|state|table|query|row|column|record|day|year|largest|biggest|source)\b',
                          re.IGNORECASE),
    "minority": re.compile(r'\bminority (report|opinion|view)\b', re.IGNORECASE),
}

def _build_synonym_patterns() -> list:
    expanded: dict[str, tuple[str, str]] = dict(ENTITY_SYNONYMS)
    for kw, (col, val) in list(ENTITY_SYNONYMS.items()):
        if kw in _ACRONYM_ONLY:
            continue                       # "up" -> "ups" would reintroduce the bug
        pl = _pluralise(kw)
        if pl and pl not in expanded:
            expanded[pl] = (col, val)
    # Longest first so multi-word phrases match before single words
    ordered = sorted(expanded.items(), key=lambda x: len(x[0]), reverse=True)
    out = []
    for kw, (col, val) in ordered:
        if kw in _ACRONYM_ONLY:
            pat = re.compile(r'\b' + re.escape(kw.upper()) + r'\b')
        else:
            pat = re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
        out.append((pat, col, val, _SUPPRESS_IF.get(kw)))
    return out

_SYNONYM_PATTERNS = _build_synonym_patterns()

def extract_entities(question: str) -> dict:
    """Return {column: value} where value is a str, or a list of str when the
    question names several values for the same column.

    Collecting every match (rather than stopping at the first per column) fixes
    the case where "compare men and women" resolved to gender='Female' and
    silently answered a narrower question than the one that was asked.
    """
    found: dict[str, list[str]] = {}
    for pattern, column, value, suppress in _SYNONYM_PATTERNS:
        if suppress is not None and suppress.search(question):
            continue
        if not pattern.search(question):
            continue
        bucket = found.setdefault(column, [])
        if value not in bucket:
            bucket.append(value)

    entities: dict[str, str | list[str]] = {}
    for column, values in found.items():
        entities[column] = values[0] if len(values) == 1 else values
    if entities:
        logger.info("Entities extracted: %s", entities)
    return entities


# ══════════════════════════════════════════════════════════════════════════════
# 2. SCHEMA RETRIEVER
# Groups columns by domain. Only sends relevant groups to the LLM prompt,
# reducing tokens by 100-150 and cutting hallucinations on irrelevant columns.
# ══════════════════════════════════════════════════════════════════════════════
_SCHEMA_BASE = f"""DATABASE: Single table called {FULL_TABLE} — Indian citizen data.

COLUMN TYPES:
- INTEGER: unified_id, age, family_member_count, vehicle_count, property_count, batch_id
- REAL: individual_income, disability_percentage
- BOOLEAN (stored as INTEGER 0/1): govt_employee_flag, income_tax_payee, disability_flag,
  matriculate, twelfth, graduate, post_graduate, minority_flag, is_active, is_deleted
  → Filter booleans as: WHERE disability_flag = 1  (NOT TRUE/FALSE)
- DATE: date_of_birth (TEXT 'YYYY-MM-DD') → year via CAST(SUBSTR(date_of_birth,1,4) AS INTEGER)
- All other columns: TEXT

SQL RULES:
- No JOINs needed — all data is in {FULL_TABLE}.
- COUNT(*) for counts, AVG() for averages, no rounding.
- Boolean columns: WHERE disability_flag = 1 / WHERE govt_employee_flag = 0
- Age ranges: WHERE age BETWEEN 18 AND 60
- Income ranges: WHERE individual_income > 500000
- NULL-safe: WHERE column IS NOT NULL AND column != ''
- Date of birth year: CAST(SUBSTR(date_of_birth,1,4) AS INTEGER)
- Top N per group: use CTE with ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC)
- Use clear AS aliases for computed columns.
- Limit large sets with LIMIT unless user asks for all."""

_SCHEMA_GROUPS: dict[str, dict] = {
    "demographics": {
        "keywords": {"age", "gender", "birth", "marital", "born", "old", "young", "youngest", "oldest"},
        "rules": "- gender: 'Male', 'Female', 'Transgender'\n- age: INTEGER\n- date_of_birth: TEXT YYYY-MM-DD\n- marital_status: 'Single', 'Married', 'Widowed', 'Divorced', 'Separated'",
    },
    "location": {
        "keywords": {"district", "state", "area", "region", "city", "place", "location", "where"},
        "rules": (
            "- state: 'Kerala','Tamil Nadu','Karnataka','Andhra Pradesh','Maharashtra','Delhi','Gujarat','Telangana','Uttar Pradesh'\n"
            "- district: 'Alappuzha','Ernakulam','Idukki','Kannur','Kasaragod','Kollam','Kottayam','Kozhikode',\n"
            "  'Malappuram','Palakkad','Pathanamthitta','Thiruvananthapuram','Thrissur','Wayanad',\n"
            "  'Bengaluru','Chennai','Hyderabad','Mumbai','New Delhi','Pune','Ahmedabad','Surat',\n"
            "  'Vadodara','Lucknow','Kanpur','Agra','Coimbatore','Madurai','Salem','Mysuru',\n"
            "  'Mangaluru','Vijayawada','Visakhapatnam','Guntur','Warangal','Nagpur','North Delhi'"
        ),
    },
    "financial": {
        "keywords": {"income", "salary", "earning", "ration", "bank", "bpl", "apl", "tax", "rich", "poor", "money", "wage"},
        "rules": (
            "- individual_income: REAL\n"
            "- ration_card_type: 'APL','BPL','AAY','PHH','NPHH'\n"
            "- ration_card_presence: 'Present','Absent'\n"
            "- bank_account_type: 'Savings','Current','Jan Dhan'\n"
            "- income_tax_payee: 0/1"
        ),
    },
    "health": {
        "keywords": {"disability", "disabled", "health", "insurance", "medical", "handicap", "ailment"},
        "rules": (
            "- disability_flag: 0/1\n"
            "- disability_percentage: REAL\n"
            "- disability_type: 'Locomotor','Visual','Hearing','Speech','Intellectual','Mental','Multiple'\n"
            "- health_insurance_type: 'Karunya','PMJAY','RSBY','ESI','Private'"
        ),
    },
    "education": {
        "keywords": {"education", "educated", "graduate", "matric", "school", "college", "degree", "literate", "study", "qualified"},
        "rules": "- matriculate: 0/1\n- twelfth: 0/1\n- graduate: 0/1\n- post_graduate: 0/1",
    },
    "employment": {
        "keywords": {"employ", "job", "work", "government", "govt", "private", "retire", "occupation", "profession", "service"},
        "rules": (
            "- employment_status: 'Employed','Unemployed','Self-Employed','Student','Retired','Homemaker'\n"
            "- employment_type: 'Government','Private','Semi-Government','Contract','Daily Wage','Self','Others'\n"
            "- govt_employee_flag: 0/1"
        ),
    },
    "identity": {
        "keywords": {"religion", "caste", "category", "minority", "hindu", "muslim", "christian", "obc", "sc", "st", "community"},
        "rules": (
            "- religion: 'Hindu','Muslim','Christian','Others'\n"
            "- category: 'General','OBC','SC','ST','EWS'\n"
            "- minority_flag: 0/1"
        ),
    },
    "assets": {
        "keywords": {"vehicle", "car", "bike", "property", "house", "land", "asset", "two wheeler", "four wheeler"},
        "rules": (
            "- vehicle_count: INTEGER\n"
            "- vehicle_type: 'Two-Wheeler','Four-Wheeler','Auto-Rickshaw','Truck'\n"
            "- property_count: INTEGER"
        ),
    },
    "welfare": {
        "keywords": {"pension", "lpg", "gas", "family", "member", "household", "scheme", "benefit"},
        "rules": (
            "- pension_status: 'Active','Inactive','Applied'\n"
            "- lpg_type: 'Indane','HP Gas','Bharat','Others'\n"
            "- family_member_count: INTEGER"
        ),
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# UNSUPPORTED LOCATION GUARD
# Indian states/cities that are NOT in this dataset. Checked before any LLM
# call so the user gets an instant, clear message instead of wrong/empty results.
# ══════════════════════════════════════════════════════════════════════════════
_DB_STATES_DISPLAY = (
    "Kerala, Tamil Nadu, Karnataka, Andhra Pradesh, Maharashtra, "
    "Delhi, Gujarat, Telangana, Uttar Pradesh"
)

_UNSUPPORTED_LOCATION_MAP: dict[str, str] = {
    "rajasthan":          "Rajasthan",
    "bihar":              "Bihar",
    "madhya pradesh":     "Madhya Pradesh",
    "punjab":             "Punjab",
    "haryana":            "Haryana",
    "himachal pradesh":   "Himachal Pradesh",
    "himachal":           "Himachal Pradesh",
    "uttarakhand":        "Uttarakhand",
    "uttaranchal":        "Uttarakhand",
    "chhattisgarh":       "Chhattisgarh",
    "jharkhand":          "Jharkhand",
    "odisha":             "Odisha",
    "orissa":             "Odisha",
    "west bengal":        "West Bengal",
    "assam":              "Assam",
    "goa":                "Goa",
    "jammu":              "Jammu & Kashmir",
    "kashmir":            "Jammu & Kashmir",
    "manipur":            "Manipur",
    "meghalaya":          "Meghalaya",
    "mizoram":            "Mizoram",
    "nagaland":           "Nagaland",
    "sikkim":             "Sikkim",
    "tripura":            "Tripura",
    "arunachal pradesh":  "Arunachal Pradesh",
    "arunachal":          "Arunachal Pradesh",
    "chandigarh":         "Chandigarh",
    "puducherry":         "Puducherry",
    "pondicherry":        "Puducherry",
    "lakshadweep":        "Lakshadweep",
    "andaman":            "Andaman & Nicobar",
    "ladakh":             "Ladakh",
    "jaipur":             "Rajasthan",
    "jodhpur":            "Rajasthan",
    "udaipur":            "Rajasthan",
    "patna":              "Bihar",
    "bhopal":             "Madhya Pradesh",
    "indore":             "Madhya Pradesh",
    "bhubaneswar":        "Odisha",
    "kolkata":            "West Bengal",
    "calcutta":           "West Bengal",
    "guwahati":           "Assam",
    "chandigarh city":    "Chandigarh",
    "raipur":             "Chhattisgarh",
    "ranchi":             "Jharkhand",
    "dehradun":           "Uttarakhand",
    "shimla":             "Himachal Pradesh",
    "amritsar":           "Punjab",
    "ludhiana":           "Punjab",
    "gurugram":           "Haryana",
    "faridabad":          "Haryana",
}

# Sort longest first so multi-word names match before single-word prefixes
_UNSUPPORTED_PATTERNS = [
    (re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE), display)
    for kw, display in sorted(_UNSUPPORTED_LOCATION_MAP.items(), key=lambda x: len(x[0]), reverse=True)
]

def check_unsupported_location(question: str):
    """Raise HTTPException if the question mentions a location not in the database."""
    for pattern, display in _UNSUPPORTED_PATTERNS:
        if pattern.search(question):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No data available for {display} in this database. "
                    f"This dataset covers: {_DB_STATES_DISPLAY}."
                ),
            )


def get_relevant_schema(question: str, entities: dict) -> str:
    """Return the base schema rules + only the categorical groups relevant to this question."""
    # Generic mode: the citizen schema groups do not apply — hand the model the
    # schema discovered from the actual database instead.
    if GENERIC_MODE:
        return _GENERIC_HINT

    q_words = set(re.sub(r"[^a-z0-9\s]", "", question.lower()).split())
    # Also add column names from extracted entities
    entity_cols = set(entities.keys())

    relevant_rules: list[str] = []
    for group_name, group in _SCHEMA_GROUPS.items():
        hit = bool(q_words & group["keywords"])
        # Also include group if its column was extracted as an entity
        if not hit:
            for col in entity_cols:
                if col in group["rules"]:
                    hit = True
                    break
        if hit:
            relevant_rules.append(group["rules"])

    # Fallback: if nothing matched, include location + demographics (most common)
    if not relevant_rules:
        relevant_rules = [
            _SCHEMA_GROUPS["location"]["rules"],
            _SCHEMA_GROUPS["demographics"]["rules"],
        ]

    schema = _SCHEMA_BASE
    if relevant_rules:
        schema += "\n\nKEY VALUES FOR THIS QUERY:\n" + "\n".join(relevant_rules)
    # Spell out the column vocabulary. Without this the model invents plausible
    # names — "city" for district, "salary" for individual_income.
    if _TABLE_COLUMNS:
        schema += ("\n\nVALID COLUMN NAMES (use ONLY these, exactly as written):\n"
                   + ", ".join(_TABLE_COLUMNS)
                   + "\nThere is no city, town, region or location column — the geographic "
                     "columns are district and state. Income is individual_income.")
    return schema


# ══════════════════════════════════════════════════════════════════════════════
# 3. QUERY TEMPLATES  (fast path — no LLM call)
# Only triggered for clear, simple patterns with fully extracted entities.
# Falls through to LLM for anything complex.
# ══════════════════════════════════════════════════════════════════════════════
_TPL_TOTAL    = re.compile(r'\b(how many|total|count)\b.{0,60}\b(citizens?|people|persons?|population)\b', re.IGNORECASE)
_TPL_COUNT_Q  = re.compile(r'\b(how many|count|total number)\b', re.IGNORECASE)
_TPL_NO_GROUP = re.compile(r'\b(by|per|each|group|wise|breakdown|split)\b', re.IGNORECASE)
_TPL_BY_DIST  = re.compile(r'\b(by|per|each|wise)\b.{0,20}\bdistrict\b|\bdistrict.{0,20}\b(wise|count|breakdown)\b', re.IGNORECASE)
_TPL_BY_GEN   = re.compile(r'\b(by|per|each)\b.{0,20}\bgender\b|\bgender.{0,20}\b(wise|count|breakdown)\b', re.IGNORECASE)
_TPL_BY_CAT   = re.compile(r'\b(by|per|each)\b.{0,20}\b(category|caste)\b|\b(category|caste).{0,20}\b(wise|count)\b', re.IGNORECASE)
_TPL_AVG_INC  = re.compile(r'\baverage\b.{0,30}\b(income|salary|earning)\b', re.IGNORECASE)
_TPL_TOP_DIST = re.compile(r'\btop\s+(\d+)\b.{0,40}\bdistrict', re.IGNORECASE)

# ── Template safety guards ────────────────────────────────────────────────────
# The count templates below produce COUNT(*). A question that asks for any other
# measure (income, age, an average, a share) cannot be answered by a row count,
# so it must fall through to the model. Without this, "average income by
# employment status in each district" matched _TPL_BY_DIST on the words "each
# district" and came back as a plain population count — wrong SQL, narrated
# confidently, and then cached.
_TPL_MEASURE = re.compile(
    r'\b(average|avg|mean|median|sum|income|salary|earning|earnings|wage|wages|'
    r'age|percentage|percent|proportion|ratio|literacy|rate)\b', re.IGNORECASE)

# A count template is only appropriate when the question really is asking how
# many rows there are.
_TPL_COUNT_INTENT = re.compile(
    r'\b(how many|count|number of|population|total|distribution|breakdown|'
    r'split|share|citizens?|people|persons?)\b', re.IGNORECASE)

# Grouping dimensions the single-column templates can each express one of.
_TPL_DIMENSIONS = ("district", "state", "gender", "category", "caste", "religion",
                   "employment", "marital", "education", "occupation")

def _count_template_safe(question: str) -> bool:
    """True only when a COUNT(*) template genuinely answers the question."""
    if _TPL_MEASURE.search(question):
        return False
    if not _TPL_COUNT_INTENT.search(question):
        return False
    # Two grouping dimensions ("by district and gender") need a two-column
    # GROUP BY, which these single-column templates cannot express.
    dims = sum(bool(re.search(rf'\b{d}\b', question, re.IGNORECASE))
               for d in _TPL_DIMENSIONS)
    return dims <= 1


_BOOL_COLS = ("disability_flag", "govt_employee_flag", "income_tax_payee", "minority_flag")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9 &().,'\-/]{1,64}$")

def _sql_literal(val) -> str:
    """Quote a value for inline SQL. Values originate from the closed synonym
    map, but escaping here means a future map entry (or a hand-edit) cannot
    break out of the string context."""
    s = str(val)
    if not _SAFE_VALUE_RE.match(s):
        raise HTTPException(status_code=400, detail="Unsupported filter value.")
    return "'" + s.replace("'", "''") + "'"

def _build_where(entities: dict) -> str:
    parts = []
    for col, val in entities.items():
        vals = val if isinstance(val, list) else [val]
        if col in _BOOL_COLS:
            nums = []
            for v in vals:
                if str(v) not in ("0", "1"):
                    raise HTTPException(status_code=400, detail="Unsupported flag value.")
                nums.append(str(v))
            parts.append(f"{col} = {nums[0]}" if len(nums) == 1
                         else f"{col} IN ({', '.join(nums)})")
        else:
            lits = [_sql_literal(v) for v in vals]
            parts.append(f"{col} = {lits[0]}" if len(lits) == 1
                         else f"{col} IN ({', '.join(lits)})")
    return ("WHERE " + " AND ".join(parts)) if parts else ""

def try_template(question: str, entities: dict) -> str | None:
    """Return ready SQL from a template, or None to fall through to the LLM."""
    w = _build_where(entities)
    and_w = ("AND " + w[6:]) if w else ""

    # Average income — only for simple "what is the average income?" type queries.
    # Questions asking WHICH district/group has highest/lowest income need GROUP BY,
    # so let those fall through to the LLM instead of returning a plain AVG().
    # Checked before the count-only gate below, since this one IS a measure query.
    _avg_needs_grouping = re.search(
        r'\b(which|who|district|state|gender|category|religion|group|by|per|each|'
        r'highest|lowest|top|most|least|'
        # comparisons and extra dimensions also need a GROUP BY this cannot express
        r'compare|compared|comparison|versus|vs|between|difference|differ|'
        r'employment|employed|marital|education|occupation|wise|breakdown|distribution)\b',
        question, re.IGNORECASE
    )
    if _TPL_AVG_INC.search(question) and not _avg_needs_grouping:
        sql = f"SELECT AVG(individual_income) AS average_income FROM {FULL_TABLE} WHERE individual_income > 0 {and_w}".strip()
        logger.info("Template hit [avg_income]: %s", sql)
        return sql

    # Nothing below can answer a non-count measure; hand those to the model.
    if not _count_template_safe(question):
        logger.info("Template skipped (needs a real aggregate or multi-column GROUP BY): %s",
                    question[:80])
        return None

    # Simple total count — no grouping intent
    if _TPL_TOTAL.search(question) and not re.search(r'\b(by|per|each|group|district|gender|category)\b', question, re.IGNORECASE):
        sql = f"SELECT COUNT(*) AS total_citizens FROM {FULL_TABLE} {w}".strip()
        logger.info("Template hit [total_count]: %s", sql)
        return sql

    # Count by district
    if _TPL_BY_DIST.search(question):
        sql = f"SELECT district, COUNT(*) AS population FROM {FULL_TABLE} WHERE 1=1 {and_w} GROUP BY district ORDER BY population DESC".strip()
        logger.info("Template hit [by_district]: %s", sql)
        return sql

    # Count by gender
    if _TPL_BY_GEN.search(question):
        sql = f"SELECT gender, COUNT(*) AS count FROM {FULL_TABLE} WHERE 1=1 {and_w} GROUP BY gender ORDER BY count DESC".strip()
        logger.info("Template hit [by_gender]: %s", sql)
        return sql

    # Count by category
    if _TPL_BY_CAT.search(question):
        sql = f"SELECT category, COUNT(*) AS count FROM {FULL_TABLE} WHERE 1=1 {and_w} GROUP BY category ORDER BY count DESC".strip()
        logger.info("Template hit [by_category]: %s", sql)
        return sql

    # Top N districts
    m = _TPL_TOP_DIST.search(question)
    if m:
        n = int(m.group(1))
        sql = (
            f"SELECT district, COUNT(*) AS population FROM {FULL_TABLE} "
            f"WHERE 1=1 {and_w} GROUP BY district ORDER BY population DESC LIMIT {n}"
        ).strip()
        logger.info("Template hit [top_%d_districts]: %s", n, sql)
        return sql

    # Filtered count: "how many women are in Gujarat?", "how many OBC citizens in Kerala?" etc.
    # Fires when we have a count-type question + at least one entity + no grouping keyword.
    # Must have entities so we don't accidentally swallow open-ended LLM questions.
    if _TPL_COUNT_Q.search(question) and entities and not _TPL_NO_GROUP.search(question):
        sql = f"SELECT COUNT(*) AS count FROM {FULL_TABLE} {w}".strip()
        logger.info("Template hit [filtered_count]: %s", sql)
        return sql

    return None


# ══════════════════════════════════════════════════════════════════════════════
# 4. STRUCTURED SESSION MEMORY
# Server-side per-session filter state. The browser passes session_id with each
# request; filters accumulate across turns so follow-up questions work correctly.
# ══════════════════════════════════════════════════════════════════════════════
# session_id → {"filters": {col: val}, "ts": datetime}
# A browser generates a fresh session_id per conversation, so without a TTL this
# dict grows for the lifetime of the process.
_session_filters: "OrderedDict[str, dict]" = OrderedDict()
_session_lock    = threading.Lock()
_SESSION_TTL     = timedelta(hours=int(os.getenv("SESSION_TTL_HOURS", "6")))
_SESSION_MAX     = int(os.getenv("SESSION_MAX", "1000"))

def _session_sweep_locked(now: datetime):
    dead = [k for k, e in _session_filters.items() if now - e["ts"] >= _SESSION_TTL]
    for k in dead:
        _session_filters.pop(k, None)
    while len(_session_filters) > _SESSION_MAX:
        _session_filters.popitem(last=False)

def get_session_filters(session_id: str | None) -> dict:
    if not session_id:
        return {}
    now = datetime.now()
    with _session_lock:
        entry = _session_filters.get(session_id)
        if entry is None:
            return {}
        if now - entry["ts"] >= _SESSION_TTL:
            _session_filters.pop(session_id, None)
            return {}
        _session_filters.move_to_end(session_id)
        return dict(entry["filters"])

def update_session_filters(session_id: str | None, new_entities: dict, sql: str):
    """Merge newly extracted entities into the session's running filter state."""
    if not session_id:
        return
    # Also pull entities out of the generated SQL (catches LLM-resolved ones)
    sql_vals = re.findall(r"(\w+)\s*=\s*'([^']+)'", sql or "")
    now = datetime.now()
    with _session_lock:
        entry = _session_filters.get(session_id)
        if entry is None or now - entry["ts"] >= _SESSION_TTL:
            entry = {"filters": {}, "ts": now}
            _session_filters[session_id] = entry
        state = entry["filters"]
        for col, val in new_entities.items():
            state[col] = val
        for col, val in sql_vals:
            if col in ("district", "state", "gender", "category", "religion",
                       "marital_status", "employment_status", "ration_card_type"):
                state[col] = val
        entry["ts"] = now
        _session_filters.move_to_end(session_id)
        _session_sweep_locked(now)

def clear_session_filters(session_id: str | None):
    if session_id:
        with _session_lock:
            _session_filters.pop(session_id, None)


# ══════════════════════════════════════════════════════════════════════════════
# 5. QUERY LOGGER
# Writes structured logs to query_logs.db (SQLite). Non-blocking — each write
# happens in a background thread so it never slows down query responses.
# ══════════════════════════════════════════════════════════════════════════════
_LOG_DB_PATH = os.path.join(os.path.dirname(__file__), "query_logs.db")
_log_engine  = None

def _init_query_log():
    global _log_engine
    try:
        _log_engine = create_engine(f"sqlite:///{_LOG_DB_PATH}")
        with _log_engine.connect() as conn:
            conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS query_logs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           TEXT    NOT NULL,
                    session_id   TEXT,
                    question     TEXT    NOT NULL,
                    generated_sql TEXT,
                    source       TEXT,
                    exec_ms      INTEGER,
                    rows_returned INTEGER,
                    tokens_in    INTEGER,
                    tokens_out   INTEGER,
                    cache_hit    INTEGER DEFAULT 0,
                    error        TEXT
                )
            """))
            conn.commit()
        logger.info("Query log DB ready: %s", _LOG_DB_PATH)
    except Exception as e:
        logger.warning("Could not init query log DB: %s", e)
        _log_engine = None

def log_query(question: str, sql: str | None, source: str,
              exec_ms: int, rows: int, tokens_in: int, tokens_out: int,
              session_id: str | None = None, cache_hit: bool = False, error: str | None = None):
    """Fire-and-forget: write one row to query_logs in a daemon thread."""
    if _log_engine is None:
        return
    def _write():
        try:
            with _log_engine.connect() as conn:
                conn.execute(sa_text("""
                    INSERT INTO query_logs
                      (ts, session_id, question, generated_sql, source,
                       exec_ms, rows_returned, tokens_in, tokens_out, cache_hit, error)
                    VALUES
                      (:ts, :sid, :q, :sql, :src,
                       :ms, :rows, :ti, :to_, :ch, :err)
                """), {
                    "ts":   datetime.now().isoformat(),
                    "sid":  session_id,
                    "q":    question[:500],
                    "sql":  (sql or "")[:1000],
                    "src":  source,
                    "ms":   exec_ms,
                    "rows": rows,
                    "ti":   tokens_in,
                    "to_":  tokens_out,
                    "ch":   int(cache_hit),
                    "err":  error,
                })
                conn.commit()
        except Exception as ex:
            logger.debug("Log write failed: %s", ex)
    threading.Thread(target=_write, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# WARMUP
# ══════════════════════════════════════════════════════════════════════════════
_is_ready = False

@app.on_event("startup")
async def warmup_all():
    _init_query_log()

    def _warm():
        global _is_ready
        try:
            if _USE_TURSO:
                _turso_execute(f"SELECT COUNT(*) FROM {FULL_TABLE}")
            else:
                with engine.connect() as conn:
                    conn.execute(sa_text(f"SELECT COUNT(*) FROM {FULL_TABLE}"))
            logger.info("Database connection verified.")
        except Exception as e:
            logger.warning("DB warmup failed: %s", e)

        provider = os.getenv("LLM_PROVIDER", "gemini").lower()
        if provider == "ollama":
            ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
            try:
                http_requests.get(ollama_url, timeout=5)
            except Exception:
                logger.error("Ollama not running at %s", ollama_url)
                return
        try:
            logger.info("Loading SQL model — warm-up (takes ~3 min once)…")
            _warm_q = (f"How many rows are in {TABLE_NAME}?" if GENERIC_MODE
                       else "How many citizens are there in total?")
            generate_sql(_warm_q)
            logger.info("SQL model ready.")
        except Exception as e:
            logger.warning("SQL LLM warmup failed: %s", e)

        global narration_llm
        try:
            logger.info("Loading narration model (%s)…", os.getenv("LLM_PROVIDER", "gemini").lower())
            narration_llm = get_narration_llm()
            if narration_llm is not None:
                narration_llm.invoke("Answer in one sentence.\nQuestion: total citizens?\nData: count: 1000000\nAnswer:")
                logger.info("Narration model ready.")
        except Exception as e:
            logger.warning("Narration model warmup failed: %s", e)

        _is_ready = True
        logger.info("All systems ready.")
    threading.Thread(target=_warm, daemon=True).start()

@app.get("/ready")
def ready():
    return {"ready": _is_ready}

@app.post("/cache/clear")
def clear_cache():
    count = len(_query_cache)
    _query_cache.clear()
    return {"cleared": count}

@app.get("/logs")
def get_logs(limit: int = 50):
    """Return the last N query log entries for observability."""
    if _log_engine is None:
        return {"logs": [], "error": "Query logging not available"}
    try:
        with _log_engine.connect() as conn:
            rows = conn.execute(sa_text(
                f"SELECT ts, session_id, question, source, exec_ms, rows_returned, "
                f"tokens_in, tokens_out, cache_hit, error FROM query_logs "
                f"ORDER BY id DESC LIMIT {min(limit, 200)}"
            )).fetchall()
        return {"logs": [dict(zip(r._fields, r)) for r in rows]}
    except Exception as e:
        return {"logs": [], "error": str(e)}


@app.get("/meta")
def get_meta():
    """Every value the entity extractor can resolve, grouped by column.

    The frontend has no way to know which districts exist, so it cannot
    autocomplete or warn before sending. This exposes the closed vocabulary the
    backend already holds.
    """
    by_col: dict[str, set] = {}
    aliases: dict[str, list] = {}
    for phrase, (col, val) in ENTITY_SYNONYMS.items():
        by_col.setdefault(col, set()).add(val)
        aliases.setdefault(val, []).append(phrase)
    return {
        "values":      {c: sorted(v) for c, v in by_col.items()},
        "aliases":     {v: sorted(a) for v, a in aliases.items()},
        "unsupported": sorted(set(_UNSUPPORTED_LOCATION_MAP.values())),
        "table":       FULL_TABLE,
        "max_rows":    MAX_ROWS,
        "generic_mode": GENERIC_MODE,
    }


@app.delete("/session/{session_id}")
def delete_session(session_id: str):
    """Forget one conversation: its carried-over filters and its query logs.

    Chat transcripts live in the browser's localStorage, so the frontend removes
    those itself. This clears the server-side traces that would otherwise
    outlive a deleted chat.
    """
    clear_session_filters(session_id)
    removed = 0
    if _log_engine is not None:
        try:
            with _log_engine.connect() as conn:
                res = conn.execute(sa_text("DELETE FROM query_logs WHERE session_id = :sid"),
                                   {"sid": session_id})
                removed = res.rowcount or 0
                conn.commit()
        except Exception as e:
            logger.warning("Log purge failed for %s: %s", session_id, e)
    return {"session_id": session_id, "filters_cleared": True, "logs_deleted": removed}


@app.delete("/sessions")
def delete_all_sessions():
    """Clear every session's server-side state and wipe the query log."""
    with _session_lock:
        n_sessions = len(_session_filters)
        _session_filters.clear()
    removed = 0
    if _log_engine is not None:
        try:
            with _log_engine.connect() as conn:
                res = conn.execute(sa_text("DELETE FROM query_logs"))
                removed = res.rowcount or 0
                conn.commit()
        except Exception as e:
            logger.warning("Full log purge failed: %s", e)
    with _cache_lock:
        _query_cache.clear()
    return {"sessions_cleared": n_sessions, "logs_deleted": removed, "cache_cleared": True}


@app.get("/health")
def health():
    """Operational counters — confirms at a glance that nothing grows unbounded."""
    with _cache_lock:
        cache_n = len(_query_cache)
    with _session_lock:
        sess_n = len(_session_filters)
    with _rate_lock:
        ip_n = len(_rate_limits)
    return {
        "ready": _is_ready,
        "provider": os.getenv("LLM_PROVIDER", "gemini").lower(),
        "narration_enabled": narration_llm is not None,
        "generic_mode": GENERIC_MODE,
        "table": FULL_TABLE,
        "cache_entries": cache_n, "cache_max": _CACHE_MAX,
        "active_sessions": sess_n, "session_max": _SESSION_MAX,
        "tracked_ips": ip_n,
        "execute_endpoint": _ENABLE_EXECUTE,
    }


# ══════════════════════════════════════════════════════════════════════════════
# LLM SETUP
# ══════════════════════════════════════════════════════════════════════════════
def get_llm():
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            api_key=os.getenv("GOOGLE_API_KEY"),
        )
    elif provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=os.getenv("GROQ_MODEL", "gemma2-9b-it"), api_key=os.getenv("GROQ_API_KEY"))
    elif provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            temperature=0, timeout=None, keep_alive="30m", num_ctx=4096,
        )
    else:
        raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")

llm = get_llm()

def get_narration_llm():
    """Small/fast model used for result narration and conversational answers.

    The original returned None for every provider except Ollama, which silently
    switched off narration and conversational follow-ups on Gemini
    and Groq — queries still worked, they just came back as bare tables with no
    explanation and no obvious reason why.
    """
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=os.getenv("OLLAMA_NARRATION_MODEL", "qwen2.5:1.5b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            temperature=0.3, timeout=None, keep_alive="30m", num_ctx=512,
        )

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            # Defaults to the cheapest flash tier; narration is a small job.
            model=os.getenv("GEMINI_NARRATION_MODEL",
                            os.getenv("GEMINI_MODEL", "gemini-2.0-flash")),
            api_key=os.getenv("GOOGLE_API_KEY"),
            temperature=0.3,
        )

    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=os.getenv("GROQ_NARRATION_MODEL",
                            os.getenv("GROQ_MODEL", "gemma2-9b-it")),
            api_key=os.getenv("GROQ_API_KEY"),
            temperature=0.3,
        )

    logger.warning("No narration model for provider '%s' — narration disabled.", provider)
    return None

narration_llm = None


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT CHAIN
# ══════════════════════════════════════════════════════════════════════════════
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

_PROMPT_STANDARD = ChatPromptTemplate.from_template(
    """You are an expert SQL writer. Write a single SQL query that precisely answers the question.

Rules:
1. Return ONLY the SQL query — no explanation, no markdown, no line breaks.
2. {schema}

{entity_hint}Question: {question}
SQL Query:"""
)

_PROMPT_SQLCODER = ChatPromptTemplate.from_template(
    """### Task
Generate a SQL query to answer [QUESTION]{question}[/QUESTION]

### Instructions
- If you cannot answer with the available schema, return 'I do not know'.
{schema}
{entity_hint}
### Answer
Given the database schema, here is the SQL query that answers [QUESTION]{question}[/QUESTION]
[SQL]"""
)

def _is_sqlcoder() -> bool:
    return os.getenv("LLM_PROVIDER", "gemini").lower() == "ollama" and "sqlcoder" in os.getenv("OLLAMA_MODEL", "").lower()

_MODEL_TOKEN_RE = re.compile(r"</?s>|<\|[^|>]+\|>")
_ALLOWED_RE     = re.compile(r'^\s*(SELECT|WITH)\b', re.IGNORECASE)
_DANGEROUS_RE   = re.compile(
    r'\b(DROP|DELETE|INSERT|UPDATE|ALTER|TRUNCATE|CREATE|MERGE|EXEC(?:UTE)?)\b'
    r'|\bREPLACE\b(?!\s*\()', re.IGNORECASE
)

_active_prompt = _PROMPT_SQLCODER if _is_sqlcoder() else _PROMPT_STANDARD
_stop_token    = ["[/SQL]"] if _is_sqlcoder() else ["\nSQLResult:"]
_prompt_chain  = RunnablePassthrough() | _active_prompt


# ══════════════════════════════════════════════════════════════════════════════
# 7. ENHANCED SQL VALIDATOR  (regex + sqlglot)
# ══════════════════════════════════════════════════════════════════════════════
def validate_sql(sql: str):
    """Raise HTTPException if SQL is unsafe or structurally invalid."""
    stripped = re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.DOTALL).strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="Empty SQL generated.")
    if not _ALLOWED_RE.match(sql):
        raise HTTPException(status_code=400, detail=f"Only SELECT queries allowed. Got: {sql[:80]}")
    if _DANGEROUS_RE.search(sql):
        raise HTTPException(status_code=400, detail="Query contains a disallowed operation (DROP/DELETE/etc.).")

    if _SQLGLOT_AVAILABLE:
        try:
            statements = sqlglot.parse(sql)
            if len(statements) != 1:
                raise HTTPException(status_code=400, detail=f"Multiple SQL statements detected ({len(statements)}).")
            root = statements[0]
            # Ensure root node is a SELECT or CTE-wrapped SELECT
            if not isinstance(root, (sqlglot.expressions.Select, sqlglot.expressions.With)):
                raise HTTPException(status_code=400, detail=f"Only SELECT statements permitted.")
        except HTTPException:
            raise
        except Exception as parse_err:
            # Parse errors often mean the LLM output was garbled — treat as invalid
            logger.warning("sqlglot parse warning: %s", parse_err)


def _clean_sql(raw: str) -> str:
    raw = re.sub(r"\[/?(SQL|QUESTION)\]", "", raw, flags=re.IGNORECASE)
    raw = _MODEL_TOKEN_RE.sub("", raw).strip()
    m = re.search(r"```(?:sql)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
    raw = re.sub(r"^\s*SQL\s*Query\s*:\s*", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL).strip()
    raw = re.sub(r"--[^\n]*", "", raw).strip()
    if raw and not re.match(r'^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|EXPLAIN)\b', raw, re.IGNORECASE):
        raw = "SELECT " + raw
    return raw

_UNINTELLIGIBLE = re.compile(r'\bi do not know\b|\bI cannot\b|\bcannot answer\b', re.IGNORECASE)

_KNOWN_TERMS = {
    "how","many","much","what","which","where","who","show","list","give","find",
    "get","count","total","average","avg","sum","max","min","top","bottom","number",
    "percentage","percent","all","most","citizen","citizens","people","person",
    "persons","population","individual","individuals","member","members","household",
    "age","gender","state","district","religion","category","income","salary",
    "earning","disability","disabled","family","vehicle","employment","employed",
    "unemployed","married","marital","ration","card","pension","insurance",
    "education","graduate","matriculate","bank","account","lpg","property","born",
    "birth","date","year","male","female","transgender","kerala","tamil","karnataka",
    "andhra","maharashtra","delhi","gujarat","telangana","uttar","pradesh","hindu",
    "muslim","christian","general","obc","sc","st","ews","bpl","apl","aay","phh",
    "nphh","single","widowed","divorced","student","retired","homemaker","government",
    "govt","private","ernakulam","idukki","thrissur","kozhikode","malappuram",
    "kollam","thiruvananthapuram","alappuzha","kottayam","palakkad","wayanad",
    "kannur","kasaragod","pathanamthitta","two","four","wheeler","auto","truck",
    "karunya","pmjay","rsby","esi","indane","bharat","women","woman","men","man",
    "trivandrum","kochi","cochin","bangalore","bengaluru","chennai","hyderabad",
    "mumbai","bombay","youngest","oldest","highest","lowest","richest","poorest",
}

# Pre-sorted list for difflib (list required, set not accepted)
_KNOWN_TERMS_LIST = sorted(_KNOWN_TERMS)

from difflib import get_close_matches as _gcm

def _is_intelligible(question: str) -> bool:
    words = re.sub(r"[^a-z0-9\s]", "", question.lower()).split()
    for word in words:
        if len(word) < 3:
            continue
        # Exact hit — fastest path
        if word in _KNOWN_TERMS:
            return True
        # Fuzzy hit — handles arbitrary typos ("popullation", "distirct", "iduki", etc.)
        # cutoff=0.82 ≈ at most ~1-2 char edits for typical 6-10 char words
        if len(word) >= 4 and _gcm(word, _KNOWN_TERMS_LIST, n=1, cutoff=0.82):
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# SESSION SCOPE RESOLVER
# Removes session filters that conflict with the CURRENT question's scope.
#
# Rules:
#  • "all districts / all states / show all / list all / every district" → clear location
#  • "top N districts / by district / district-wise / across districts"   → clear location
#    (these are aggregation-across-locations queries; a stored state=Gujarat
#     would wrongly narrow them to Gujarat only)
#  • "all" as a standalone word with no new location entity → clear location
#  • Current question names a DIFFERENT state/district                  → already
#    handled by {**session_ctx, **entities} priority; nothing to strip.
# ══════════════════════════════════════════════════════════════════════════════
_RESET_ALL_GEO_RE = re.compile(
    r'\ball\s+(districts|states|areas|regions)\b'
    r'|\bevery\s+(district|state)\b'
    r'|\bdistrict[\s\-]?wise\b'
    r'|\bstate[\s\-]?wise\b'
    r'|\bacross\s+(all\s+)?(districts|states)\b'
    r'|\bby\s+district\b'
    r'|\bby\s+state\b'
    r'|\btop\s+\d+\s+districts\b'
    r'|\bshow\s+all\b'
    r'|\blist\s+all\b',
    re.IGNORECASE,
)

# "all" as standalone (e.g. "show top 5 by all", "population of all")
_RESET_STANDALONE_ALL = re.compile(r'\ball\b', re.IGNORECASE)

_GEO_COLS = {"district", "state"}

def _strip_conflicting_session_filters(question: str, current_entities: dict,
                                        session_ctx: dict) -> dict:
    """Return session_ctx with location filters removed when the current question
    is asking for a cross-location aggregation rather than a scoped lookup."""
    if not session_ctx:
        return session_ctx

    # Hard signal: explicit "all districts", "by district", "top N districts", etc.
    if _RESET_ALL_GEO_RE.search(question):
        filtered = {k: v for k, v in session_ctx.items() if k not in _GEO_COLS}
        if filtered != session_ctx:
            logger.info("Session geo-filter cleared (aggregation query): %s", question[:80])
        return filtered

    # Softer signal: "all" without a new location entity in the question
    if _RESET_STANDALONE_ALL.search(question):
        has_new_location = bool(current_entities.get("district") or current_entities.get("state"))
        if not has_new_location:
            filtered = {k: v for k, v in session_ctx.items() if k not in _GEO_COLS}
            if filtered != session_ctx:
                logger.info("Session geo-filter cleared ('all' with no location): %s", question[:80])
            return filtered

    return session_ctx


# ══════════════════════════════════════════════════════════════════════════════
# CORE PIPELINE — generate_sql, execute_sql, narrate
# ══════════════════════════════════════════════════════════════════════════════
def generate_sql(question: str, history=None, session_id: str | None = None):
    """
    Returns (sql, tokens_in, tokens_out, entities).
    Pipeline: intelligibility check → entity extraction → template fast-path
              → LLM with dynamic schema + entity hints.
    """
    # Generic mode: the intelligibility vocabulary, the location guard, the
    # synonym-based entity extractor and the SQL templates are all built around
    # the citizen schema, so every one of them is skipped. The question goes
    # straight to the model with the discovered schema.
    if GENERIC_MODE:
        schema      = _GENERIC_HINT
        entities    = {}
        entity_hint = ""
        return _generate_sql_via_llm(question, schema, entity_hint, history, entities)

    if not _is_intelligible(question):
        # A follow-up like "is this correct?" or "tell me more" contains no
        # citizen vocabulary, so the gate rejects it on its own. With previous
        # turns in hand it is a perfectly meaningful question, and the history
        # block in the prompt gives the model what it needs — so let it through
        # rather than telling the user their question was unintelligible.
        if not (history and _could_be_conversational(question)):
            raise HTTPException(
                status_code=422,
                detail=("I couldn't understand your question. Please ask something "
                        "about the citizen data."),
            )
        logger.info("Intelligibility gate bypassed for contextual follow-up: %s", question[:60])

    check_unsupported_location(question)  # fast reject before any LLM call

    # 1. Extract entities from current question
    entities = extract_entities(question)

    # 2. Merge with session filters, but strip filters that conflict with the
    #    current question's intent (e.g. user had Gujarat in session but now
    #    asks "all districts" or "top 5 districts" — a global aggregation).
    session_ctx = get_session_filters(session_id)
    session_ctx = _strip_conflicting_session_filters(question, entities, session_ctx)
    # Current question's entities take priority over session context
    merged_entities = {**session_ctx, **entities}

    # 3. Template fast-path — skip LLM entirely for simple known patterns
    tpl_sql = try_template(question, merged_entities)
    if tpl_sql:
        return tpl_sql, 0, 0, entities

    # 4. Build dynamic schema — only relevant column groups
    schema = get_relevant_schema(question, merged_entities)

    # 5. Build entity hint string for the prompt
    entity_hint = ""
    if merged_entities:
        bits = []
        for col, val in merged_entities.items():
            if isinstance(val, list):
                bits.append(f"{col} IN ({', '.join(chr(39) + str(v) + chr(39) for v in val)})")
            else:
                bits.append(f"{col}='{val}'")
        entity_hint = ("Detected entities (use these EXACT values in WHERE): "
                       + ", ".join(bits) + "\n")

    # 6-8. History context → LLM → clean/validate (shared with generic mode)
    return _generate_sql_via_llm(question, schema, entity_hint, history, entities)


def _generate_sql_via_llm(question: str, schema: str, entity_hint: str,
                          history, entities: dict):
    """Steps 6-8 of the pipeline: build the prompt, call the model, clean the SQL.

    Shared by the citizen pipeline and generic mode so both get identical
    history handling, token accounting and table-name correction.
    """
    # 6. Build question with history context
    effective_q = question
    if history:
        ctx_parts = []
        for h in history[-3:]:
            q = str(getattr(h, 'question', None) or (h.get('question', '') if isinstance(h, dict) else ''))[:200]
            s = str(getattr(h, 'sql', None) or (h.get('sql', '') if isinstance(h, dict) else ''))[:300]
            if q and s:
                ctx_parts.append(f"Previous question: {q}\nPrevious SQL: {s}")
        if ctx_parts:
            effective_q = (
                "Conversation history (use as context for follow-up questions):\n" +
                "\n\n".join(ctx_parts) +
                f"\n\nCurrent question: {question}\n\n"
                f"Rules for using history:\n"
                f"- If the current question is a follow-up (does NOT mention a new specific location, "
                f"city, state, name, or entity), you MAY inherit relevant filters from the previous SQL.\n"
                f"- If the current question explicitly names a DIFFERENT location, city, state, or entity "
                f"than the previous query, use ONLY the new one — do NOT mix filters from both."
            )

    # 7. Call LLM
    prompt_value = _prompt_chain.invoke({"question": effective_q, "schema": schema, "entity_hint": entity_hint})
    response     = llm.bind(stop=_stop_token).invoke(prompt_value)

    tokens_in, tokens_out = 0, 0
    try:
        meta = getattr(response, "usage_metadata", None)
        if isinstance(meta, dict):
            tokens_in  = meta.get("input_tokens", 0)
            tokens_out = meta.get("output_tokens", 0)
        elif meta is not None:
            tokens_in  = getattr(meta, "input_tokens", 0)
            tokens_out = getattr(meta, "output_tokens", 0)
    except Exception:
        pass

    raw = StrOutputParser().invoke(response)
    if _UNINTELLIGIBLE.search(raw):
        raise HTTPException(status_code=422, detail="I couldn't understand your question. Please rephrase it.")

    sql = _clean_sql(raw)

    # 8a. Auto-correct invented column names (city -> district, salary -> income)
    sql = _repair_columns(sql)

    # 8. Auto-correct wrong table name
    if sql and TABLE_NAME.lower() not in sql.lower() and FULL_TABLE.lower() not in sql.lower():
        fixed = re.sub(r'(?i)\bFROM\s+(?!\s*\()([\w\."\'`\[\]\.]+)', f'FROM {FULL_TABLE}', sql)
        if TABLE_NAME.lower() in fixed.lower():
            logger.info("Auto-corrected table name → %s", fixed[:120])
            sql = fixed
        else:
            detail = ("I couldn't map your question to the data in this database. Please rephrase it."
                      if GENERIC_MODE else
                      "I couldn't map your question to the citizen data. Please rephrase it.")
            raise HTTPException(status_code=422, detail=detail)

    return sql, tokens_in, tokens_out, entities


def execute_sql(sql: str):
    validate_sql(sql)   # enhanced validator (regex + sqlglot)
    if _USE_TURSO:
        columns, rows = _turso_execute(sql)
    else:
        from decimal import Decimal
        def _safe(v):
            if v is None: return None
            if isinstance(v, Decimal): return float(v)
            return v
        with engine.connect() as conn:
            result  = conn.execute(sa_text(sql))
            columns = list(result.keys())
            rows    = [[_safe(v) for v in row] for row in result.fetchmany(MAX_ROWS + 1)]
    truncated = len(rows) > MAX_ROWS
    if truncated:
        rows = rows[:MAX_ROWS]
    return columns, rows, truncated


def execute_sql_with_retry(sql: str, question: str):
    try:
        columns, rows, truncated = execute_sql(sql)
        return columns, rows, truncated, sql
    except HTTPException:
        raise
    except Exception as first_err:
        logger.warning("SQL execution failed (%s), attempting auto-fix…", first_err)
        try:
            # The retry used to state only the table name, so a query that failed
            # on a non-existent column had no way to learn the real ones and
            # would often reproduce the same mistake.
            cols_line = (f"The ONLY valid columns are: {', '.join(_TABLE_COLUMNS)}\n"
                         if _TABLE_COLUMNS else "")
            fix_prompt = (
                f"Fix this SQL query. Error: {str(first_err)}\n"
                f"The ONLY table is: {FULL_TABLE}\n"
                f"{cols_line}"
                f"Original question: {question}\nBroken SQL: {sql}\n"
                f"Return ONLY the corrected SQL query, nothing else."
            )
            raw       = llm.invoke(fix_prompt)
            fixed_sql = _repair_columns(_clean_sql(StrOutputParser().invoke(raw)))
            if TABLE_NAME.lower() not in fixed_sql.lower() and FULL_TABLE.lower() not in fixed_sql.lower():
                raise first_err
            columns, rows, truncated = execute_sql(fixed_sql)
            logger.info("Auto-fix succeeded.")
            return columns, rows, truncated, fixed_sql
        except HTTPException:
            raise
        except Exception:
            raise first_err


_MONEY_KEYWORDS = {"income", "salary", "earning", "wage", "amount", "revenue", "average_income",
                   "avg_income", "average income", "avg income"}

def _fmt_value(val, col: str) -> str:
    """Format a value for human-readable narration.
    - Income/money columns → Indian units (lakh / crore)
    - Large integers       → comma-separated (64,115)
    - Floats               → rounded to 2 decimal places
    """
    if val is None:
        return "unknown"
    col_lower = col.lower().replace("_", " ")
    is_money  = any(kw in col_lower for kw in _MONEY_KEYWORDS)
    try:
        n = float(val)
        if is_money:
            if n >= 1_00_00_000:          # ≥ 1 crore
                return f"₹{n / 1_00_00_000:.2f} crore"
            elif n >= 1_00_000:           # ≥ 1 lakh
                return f"₹{n / 1_00_000:.2f} lakh"
            elif n >= 1_000:
                return f"₹{n:,.0f}"
            else:
                return f"₹{n:.2f}"
        else:
            # Non-money number: comma-separate integers, round floats
            if n == int(n):
                return f"{int(n):,}"
            return f"{round(n, 2):,}"
    except (TypeError, ValueError, OverflowError):
        pass
    return str(val)


def _build_context(columns: list, rows: list, truncated: bool, max_rows: int = 10) -> str:
    if not rows:
        return "no results found"
    if len(rows) == 1:
        parts = [f"{col.replace('_',' ').lower()}: {_fmt_value(val, col)}" for col, val in zip(columns, rows[0])]
        return ", ".join(parts)
    lines = []
    for row in rows[:max_rows]:
        pairs = ", ".join(f"{col.replace('_',' ').lower()}: {_fmt_value(val, col)}" for col, val in zip(columns, row))
        lines.append(pairs)
    context = "; ".join(lines)
    if truncated or len(rows) > max_rows:
        context += f" ({len(rows) - max_rows} more rows not shown)"
    return context


def narrate_results(question: str, columns: list, rows: list, truncated: bool, sql: str = '') -> str | None:
    if len(columns) == 1 and len(rows) > 1:
        col   = columns[0].replace('_', ' ').lower()
        items = [str(r[0]) for r in rows if r[0] is not None]
        total = f"{len(items)}+" if truncated else str(len(items))
        if len(items) <= 10:
            return f"There are {total} {col}s: {', '.join(items)}."
        return f"There are {total} {col}s. The complete list is shown in the table above."

    if narration_llm is None:
        return None
    try:
        context = _build_context(columns, rows, truncated, max_rows=20)
        sql_vals = re.findall(r"'([^']+)'", sql) if sql else []
        spelling_note = (
            f"\nCorrect name spellings from SQL — use ONLY these, never the question's spelling: {', '.join(sql_vals)}"
            if sql_vals else ""
        )
        prompt = (
            f"Write ONE plain English sentence answering the question. "
            f"NEVER use technical words like SQL, query, table, column, record, GROUP BY, clause, database, or any database terms. "
            f"Write as if explaining to someone who has never used a computer. "
            f"Copy every number from the data EXACTLY as given — do not round, abbreviate, or change any digit. "
            f"Use ONLY the spellings listed below, never the spelling in the question.\n"
            f"Question: {question}\nData: {context}{spelling_note}\nSentence:"
        )
        result = narration_llm.invoke(prompt)
        text   = StrOutputParser().invoke(result).strip()
        text   = re.sub(r'^(Answer|Sentence)\s*(\([^)]*\))?\s*:\s*', '', text, flags=re.IGNORECASE).strip()
        text   = re.sub(r'(\d)\.\s+(\d)', r'\1.\2', text)
        return text if text else None
    except Exception as e:
        logger.warning("Narration failed: %s", e)
        return None


_NARRATION_PREFIX = re.compile(r'^(Answer|Sentence)\s*(\([^)]*\))?\s*:\s*', re.IGNORECASE)
_NARRATION_PREFIX_BUF = 30  # chars to buffer before stripping the prefix

async def astream_narration(question: str, columns: list, rows: list, truncated: bool, sql: str = '',
                            _token_out: dict | None = None):
    """Async generator: yields narration text fragments as the model produces them.

    Fast-path (single-column list) yields one string with no LLM call.
    All other cases stream tokens from narration_llm.astream().
    If _token_out dict is provided, it is updated with {"in": N, "out": N} after streaming.
    """
    if len(columns) == 1 and len(rows) > 1:
        col   = columns[0].replace('_', ' ').lower()
        items = [str(r[0]) for r in rows if r[0] is not None]
        total = f"{len(items)}+" if truncated else str(len(items))
        if len(items) <= 10:
            yield f"There are {total} {col}s: {', '.join(items)}."
        else:
            yield f"There are {total} {col}s. The complete list is shown in the table above."
        return

    if narration_llm is None:
        return

    context = _build_context(columns, rows, truncated, max_rows=20)
    sql_vals = re.findall(r"'([^']+)'", sql) if sql else []
    spelling_note = (
        f"\nCorrect name spellings from SQL — use ONLY these, never the question's spelling: {', '.join(sql_vals)}"
        if sql_vals else ""
    )
    prompt = (
        f"Write ONE plain English sentence answering the question. "
        f"NEVER use technical words like SQL, query, table, column, record, GROUP BY, clause, database, or any database terms. "
        f"Write as if explaining to someone who has never used a computer. "
        f"Copy every number from the data EXACTLY as given — do not round, abbreviate, or change any digit. "
        f"Use ONLY the spellings listed below, never the spelling in the question.\n"
        f"Question: {question}\nData: {context}{spelling_note}\nSentence:"
    )

    # Buffer the opening tokens to strip any "Answer:" / "Sentence:" prefix before
    # sending the first fragment to the client.
    buf = ""
    prefix_flushed = False
    try:
        async for chunk in narration_llm.astream(prompt):
            tok = getattr(chunk, "content", None) or ""
            # Capture token usage when the model reports it (usually on the last chunk)
            if _token_out is not None:
                meta = getattr(chunk, "usage_metadata", None)
                if meta:
                    if isinstance(meta, dict):
                        _token_out["in"]  = meta.get("input_tokens",  _token_out.get("in", 0))
                        _token_out["out"] = meta.get("output_tokens", _token_out.get("out", 0))
                    else:
                        _token_out["in"]  = getattr(meta, "input_tokens",  _token_out.get("in", 0))
                        _token_out["out"] = getattr(meta, "output_tokens", _token_out.get("out", 0))
            if not tok:
                continue
            if not prefix_flushed:
                buf += tok
                if len(buf) >= _NARRATION_PREFIX_BUF:
                    out = _NARRATION_PREFIX.sub("", buf).lstrip()
                    prefix_flushed = True
                    if out:
                        yield out
            else:
                yield tok
        # Flush remainder for short responses that never hit the buffer limit
        if not prefix_flushed and buf:
            out = _NARRATION_PREFIX.sub("", buf).lstrip()
            if out:
                yield out
    except Exception as e:
        logger.warning("Narration stream failed: %s", e)


_CONV_TRIGGERS = re.compile(
    r'\b(is this|does this|is that|does that|is it|was this|are these|are those|'
    r'belongs? to|belong|the same|the correct|confirm|right\??|correct\??|'
    r'really|actually|already|why is|what does this|means?|refer to|'
    r'about this|about that|for this|for that|tell me more|explain this|'
    r'does it|did it|will it|would it|could it)\b',
    re.IGNORECASE
)

def _could_be_conversational(question: str) -> bool:
    if len(question.split()) > 14:
        return False
    return bool(_CONV_TRIGGERS.search(question))

def check_conversational(question: str, history: list) -> str | None:
    # Falls back to the main model when no narration model is loaded. Previously
    # this returned None whenever narration_llm was missing, so every follow-up
    # ("is this correct?") skipped the conversational path and got rejected by
    # the intelligibility gate as if it were gibberish.
    _conv_llm = narration_llm or llm
    if not history or _conv_llm is None:
        return None
    if not _could_be_conversational(question):
        return None
    last      = history[-1]
    last_q    = str(getattr(last, 'question', None) or (last.get('question', '') if isinstance(last, dict) else ''))[:200]
    last_summ = str(getattr(last, 'result_summary', None) or (last.get('result_summary', '') if isinstance(last, dict) else ''))[:300]
    if not last_q or not last_summ:
        return None
    try:
        combined_prompt = (
            f"Previous question: {last_q}\nPrevious answer: {last_summ}\n"
            f"Follow-up question: {question}\n"
            f"If you can answer the follow-up from the previous answer alone, write exactly ONE sentence. "
            f"State the location/scope from the previous question directly. "
            f"Otherwise write exactly: CANNOT\nResponse:"
        )
        answer = StrOutputParser().invoke(_conv_llm.invoke(combined_prompt)).strip()
        answer = re.sub(r'^(Answer|Sentence|Response)\s*:\s*', '', answer, flags=re.IGNORECASE).strip()
        hedge  = re.compile(r'does not specify|cannot determine|not enough|unclear|don.t know', re.IGNORECASE)
        if answer.upper().startswith('CANNOT') or hedge.search(answer):
            return None
        return answer if len(answer) > 5 else None
    except Exception as e:
        logger.warning("Conversational answer failed: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# API MODELS
# ══════════════════════════════════════════════════════════════════════════════
class HistoryItem(BaseModel):
    """One past turn as the browser sends it.

    Every field is optional and nullable. The frontend omits `sql` for
    conversational turns and sends `null` for others, which the old strict
    model rejected with a 422 on /query and /query/stream — the request never
    reached the pipeline. Extra keys are tolerated for the same reason: the
    frontend is free to add fields without breaking the backend. The consumers
    (generate_sql, check_conversational) already skip turns with missing
    question/sql, so a loose model here is safe.
    """
    model_config = ConfigDict(extra="allow")

    question:       str | None = ""
    sql:            str | None = ""
    result_summary: str | None = ""

class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    question:   str
    history:    list[HistoryItem] = []
    session_id: str | None = None   # browser session ID for structured memory

class ExecuteRequest(BaseModel):
    sql: str


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/tables")
def list_tables():
    return {"tables": db.get_usable_table_names()}

@app.post("/query")
def query(request: Request, req: QueryRequest):
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    cached = _cache_get(req.question)
    if cached:
        cached["cached"] = True
        log_query(req.question, cached.get("sql"), "cache", 0,
                  len(cached.get("rows", [])), 0, 0, req.session_id, cache_hit=True)
        return cached

    conv_answer = check_conversational(req.question, req.history or [])
    if conv_answer:
        log_query(req.question, None, "conversational", 0, 0, 0, 0, req.session_id)
        return {"question": req.question, "sql": None, "columns": [], "rows": [],
                "truncated": False, "elapsed_ms": 0, "sql_ms": 0, "response_ms": 0,
                "tokens_in": 0, "tokens_out": 0, "response_text": conv_answer,
                "conversational": True}
    try:
        t0 = time.perf_counter()
        sql, tokens_in, tokens_out, entities = generate_sql(req.question, req.history or None, req.session_id)
        columns, rows, truncated, final_sql   = execute_sql_with_retry(sql, req.question)
        sql_ms = round((time.perf_counter() - t0) * 1000)

        update_session_filters(req.session_id, entities, final_sql)

        t1 = time.perf_counter()
        if narration_llm is not None:
            _nf = _NARRATION_POOL.submit(narrate_results, req.question, columns, rows, truncated, final_sql)
            response_text = _nf.result()
        else:
            response_text = narrate_results(req.question, columns, rows, truncated, final_sql)
        response_ms   = round((time.perf_counter() - t1) * 1000)

        result = {
            "question": req.question, "sql": final_sql, "columns": columns, "rows": rows,
            "truncated": truncated, "elapsed_ms": sql_ms + response_ms,
            "sql_ms": sql_ms, "response_ms": response_ms,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "response_text": response_text,
        }
        _cache_set(req.question, result)
        log_query(req.question, final_sql, "llm", sql_ms, len(rows), tokens_in, tokens_out, req.session_id)
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log_query(req.question, None, "error", 0, 0, 0, 0, req.session_id, error=str(e))
        logger.error("Query failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/query/stream")
async def query_stream(request: Request, req: QueryRequest):
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    async def _gen():
        t0 = time.perf_counter()
        try:
            if not _is_ready:
                yield f"data: {json.dumps({'type':'error','status':503,'detail':'Server is still warming up. Please wait.'})}\n\n"
                return

            cached = _cache_get(req.question)
            if cached:
                cached["type"] = "done"; cached["cached"] = True
                yield f"data: {json.dumps(cached)}\n\n"
                log_query(req.question, cached.get("sql"), "cache", 0,
                          len(cached.get("rows", [])), 0, 0, req.session_id, cache_hit=True)
                return

            conv_answer = await run_in_threadpool(check_conversational, req.question, req.history or [])
            if conv_answer:
                yield f"data: {json.dumps({'type':'conversational','response_text':conv_answer})}\n\n"
                log_query(req.question, None, "conversational", 0, 0, 0, 0, req.session_id)
                return

            sql, ti, to, entities = await run_in_threadpool(generate_sql, req.question, req.history or None, req.session_id)
            source = "template" if (ti == 0 and to == 0) else "llm"
            yield f"data: {json.dumps({'type':'sql','sql':sql,'source':source})}\n\n"

            columns, rows, truncated, final_sql = await run_in_threadpool(execute_sql_with_retry, sql, req.question)
            yield f"data: {json.dumps({'type':'results','sql':final_sql,'columns':columns,'rows':rows,'truncated':truncated})}\n\n"

            await run_in_threadpool(update_session_filters, req.session_id, entities, final_sql)

            # Stream narration tokens to the client as they arrive so the user sees
            # the answer building up word-by-word instead of waiting for the full sentence.
            response_text = ""
            narr_tokens: dict = {"in": 0, "out": 0}
            async for tok in astream_narration(req.question, columns, rows, truncated, final_sql,
                                               _token_out=narr_tokens):
                response_text += tok
                yield f"data: {json.dumps({'type': 'narration_token', 'token': tok})}\n\n"
            # Fix LLM artefact: "46. 94" → "46.94" (space inserted around decimal point)
            response_text = re.sub(r'(\d)\.\s+(\d)', r'\1.\2', response_text.strip())

            # Combine SQL-generation tokens with narration tokens for the total shown in the UI
            ti += narr_tokens["in"]
            to += narr_tokens["out"]

            exec_ms = round((time.perf_counter() - t0) * 1000)
            done = {
                "type": "done", "question": req.question, "sql": final_sql,
                "columns": columns, "rows": rows, "truncated": truncated,
                "response_text": response_text, "tokens_in": ti, "tokens_out": to,
            }
            yield f"data: {json.dumps(done)}\n\n"

            _cache_set(req.question, {k: v for k, v in done.items() if k != "type"})

            log_query(req.question, final_sql, source, exec_ms, len(rows), ti, to, req.session_id)

        except HTTPException as e:
            log_query(req.question, None, "error", 0, 0, 0, 0, req.session_id, error=e.detail)
            yield f"data: {json.dumps({'type':'error','status':e.status_code,'detail':e.detail})}\n\n"
        except Exception as e:
            log_query(req.question, None, "error", 0, 0, 0, 0, req.session_id, error=str(e))
            logger.error("Stream failed: %s", e, exc_info=True)
            yield f"data: {json.dumps({'type':'error','status':500,'detail':str(e)})}\n\n"

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


_ENABLE_EXECUTE = os.getenv("ENABLE_EXECUTE", "false").lower() in ("1", "true", "yes")

@app.post("/execute")
def execute_custom(request: Request, req: ExecuteRequest):
    # Raw-SQL passthrough. Read-only and validated, but it still lets any caller
    # run arbitrary SELECTs against citizen data, so it is off unless asked for.
    # The frontend never used it.
    if not _ENABLE_EXECUTE:
        raise HTTPException(status_code=403, detail="Raw SQL execution is disabled. Set ENABLE_EXECUTE=true to allow it.")
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)
    if not req.sql.strip():
        raise HTTPException(status_code=400, detail="SQL cannot be empty.")
    try:
        columns, rows, truncated = execute_sql(req.sql)
        return {"sql": req.sql, "columns": columns, "rows": rows, "truncated": truncated}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Static frontend ────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")
