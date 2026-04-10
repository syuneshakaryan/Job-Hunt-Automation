"""
database.py
===========
Initializes and manages the local SQLite database for the Job Hunter pipeline.

Tables:
  - companies : one row per domain (crawl target)
  - jobs      : one row per job posting found
"""

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "job_hunter.db"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "logs" / "pipeline.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("database")


# ─────────────────────────────────────────────────────────────────────────────
# Schema SQL
# ─────────────────────────────────────────────────────────────────────────────

CREATE_COMPANIES_TABLE = """
CREATE TABLE IF NOT EXISTS companies (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,

    -- identity
    domain                TEXT    NOT NULL UNIQUE,   -- e.g. "stripe.com"
    company_name          TEXT,                       -- parsed or discovered name

    -- crawl results
    career_url            TEXT,                       -- final careers page URL
    extracted_emails_json TEXT    DEFAULT '[]',       -- JSON array of email strings
    homepage_html_hash    TEXT,                       -- md5 of homepage, for change detection

    -- pipeline control
    scraped_status        TEXT    NOT NULL DEFAULT 'pending',
    -- VALUES: pending | crawling | scraped | no_careers | failed | skipped

    -- timestamps
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    scraped_at            TEXT,
    last_error            TEXT    -- last error message if status = failed
);
"""

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,

    -- job identity
    title          TEXT    NOT NULL,
    url            TEXT    NOT NULL UNIQUE,
    description    TEXT,                 -- raw scraped text of the posting

    -- LLM evaluation results
    fit_score      INTEGER DEFAULT 0,    -- 0–100 from Ollama evaluator
    is_backend     INTEGER DEFAULT 0,    -- 0/1 bool
    tech_stack     TEXT    DEFAULT '[]', -- JSON array e.g. ["Python","FastAPI"]
    rejection_reason TEXT,               -- why LLM scored it low (optional)

    -- application workflow
    applied_status TEXT    NOT NULL DEFAULT 'pending',
    -- VALUES: pending | approved | applying | applied | skipped | failed

    resume_path    TEXT,                 -- path to generated PDF
    applied_at     TEXT,                 -- timestamp when submitted

    -- timestamps
    found_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    evaluated_at   TEXT
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(scraped_status);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_fit_score   ON jobs(fit_score);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(applied_status);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_company     ON jobs(company_id);",
]


# ─────────────────────────────────────────────────────────────────────────────
# Connection helper
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def get_connection(db_path: Path = DB_PATH):
    """
    Context manager that yields a sqlite3 connection with:
      - WAL mode  (allows concurrent readers during writes)
      - row_factory set to sqlite3.Row  (dict-like access by column name)
      - foreign keys enforced
    """
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Init
# ─────────────────────────────────────────────────────────────────────────────

def init_db(db_path: Path = DB_PATH) -> None:
    """Create all tables and indexes. Safe to call multiple times (IF NOT EXISTS)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as conn:
        conn.execute(CREATE_COMPANIES_TABLE)
        conn.execute(CREATE_JOBS_TABLE)
        for idx_sql in CREATE_INDEXES:
            conn.execute(idx_sql)

    logger.info(f"Database ready at: {db_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Company helpers
# ─────────────────────────────────────────────────────────────────────────────

def bulk_insert_domains(domains: list[str]) -> int:
    """
    Insert a list of domain strings.
    Ignores duplicates (INSERT OR IGNORE).
    Returns the number of newly inserted rows.
    """
    rows = [(d.strip().lower(),) for d in domains if d.strip()]
    with get_connection() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO companies (domain) VALUES (?);", rows
        )
        inserted = cur.rowcount
    logger.info(f"bulk_insert_domains: {inserted} new domains added ({len(rows)} total provided)")
    return inserted


def fetch_pending_companies(limit: int = 50) -> list[sqlite3.Row]:
    """Return up to `limit` companies with scraped_status = 'pending'."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM companies
            WHERE scraped_status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?;
            """,
            (limit,),
        ).fetchall()
    return rows


def update_company(company_id: int, **kwargs) -> None:
    """
    Generic updater for companies table.
    Usage: update_company(3, scraped_status='scraped', career_url='https://...')
    """
    if not kwargs:
        return
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [company_id]
    with get_connection() as conn:
        conn.execute(
            f"UPDATE companies SET {set_clause} WHERE id = ?;", values
        )


# ─────────────────────────────────────────────────────────────────────────────
# Job helpers
# ─────────────────────────────────────────────────────────────────────────────

def insert_job(
    company_id: int,
    title: str,
    url: str,
    description: str = "",
) -> int | None:
    """
    Insert a newly discovered job.
    Returns the new job_id, or None if URL already exists (duplicate).
    """
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO jobs (company_id, title, url, description)
                VALUES (?, ?, ?, ?);
                """,
                (company_id, title, url, description),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # URL already in DB – duplicate
            logger.debug(f"Duplicate job URL skipped: {url}")
            return None


def update_job(job_id: int, **kwargs) -> None:
    """Generic updater for jobs table."""
    if not kwargs:
        return
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    with get_connection() as conn:
        conn.execute(
            f"UPDATE jobs SET {set_clause} WHERE job_id = ?;", values
        )


def fetch_jobs_for_review() -> list[sqlite3.Row]:
    """Return all high-score jobs that haven't been actioned yet."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT j.*, c.domain, c.extracted_emails_json
            FROM jobs j
            JOIN companies c ON c.id = j.company_id
            WHERE j.fit_score >= 75
              AND j.applied_status = 'pending'
            ORDER BY j.fit_score DESC;
            """
        ).fetchall()


def get_pipeline_stats() -> dict:
    """Return a summary dict for logging / Telegram daily digest."""
    with get_connection() as conn:
        total_companies = conn.execute("SELECT COUNT(*) FROM companies;").fetchone()[0]
        scraped         = conn.execute("SELECT COUNT(*) FROM companies WHERE scraped_status='scraped';").fetchone()[0]
        total_jobs      = conn.execute("SELECT COUNT(*) FROM jobs;").fetchone()[0]
        high_score      = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 75;").fetchone()[0]
        applied         = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_status='applied';").fetchone()[0]

    return {
        "total_companies": total_companies,
        "scraped":         scraped,
        "total_jobs":      total_jobs,
        "high_score_jobs": high_score,
        "applied":         applied,
        "pending_review":  high_score - applied,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CSV loader utility
# ─────────────────────────────────────────────────────────────────────────────

def load_domains_from_csv(csv_path: str | Path, column: str = "domain") -> int:
    """
    Read a CSV file and bulk-insert all domains.

    Expected CSV format (header row required):
        domain
        stripe.com
        notion.so
        ...

    The `column` param lets you specify the column name if it differs.
    Returns count of newly inserted rows.
    """
    import csv
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    domains = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            val = row.get(column, "").strip()
            if val:
                domains.append(val)

    logger.info(f"Loaded {len(domains)} domains from {path.name}")
    return bulk_insert_domains(domains)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point – run directly to initialise DB and test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    # ── Quick smoke-test ──────────────────────────────────────────────────────
    test_domains = [
        "stripe.com", "notion.so", "linear.app",
        "vercel.com", "railway.app", "fly.io",
    ]
    inserted = bulk_insert_domains(test_domains)
    print(f"\n✅  Inserted {inserted} test domains")

    pending = fetch_pending_companies(limit=10)
    print(f"✅  Pending companies fetched: {len(pending)}")
    for row in pending:
        print(f"    id={row['id']}  domain={row['domain']}  status={row['scraped_status']}")

    stats = get_pipeline_stats()
    print(f"\n📊  Pipeline stats: {stats}")