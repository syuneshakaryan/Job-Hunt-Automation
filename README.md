# Job Hunter Automation Pipeline

A fully automated job hunting system that discovers career opportunities, evaluates job fit using local semantic similarity, generates customized resumes, and applies to positions via ATS platforms or contact forms. **100% local processing, privacy-first architecture**—no data leaves your machine.

**Tech Stack:** Python 3.11+, Playwright (headless browser), Prefect (workflow orchestration), Sentence-Transformers (`all-MiniLM-L6-v2`), SQLite3 (local database), WeasyPrint (PDF generation), Jinja2 (templating), Telegram Bot API, asyncio (concurrent I/O).

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Feature Set](#feature-set)
3. [Project Structure](#project-structure)
4. [Database Schema](#database-schema)
5. [Configuration](#configuration)
6. [Setup & Installation](#setup--installation)
7. [Pipeline Workflow](#pipeline-workflow)
8. [Module Reference](#module-reference)
9. [Running the Pipeline](#running-the-pipeline)
10. [Advanced Configuration](#advanced-configuration)
11. [Performance Optimization](#performance-optimization)
12. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

The pipeline implements a **six-stage asynchronous data transformation**:

```
┌─────────────────────────────────────────────────────────────────┐
│ STAGE 1: DOMAIN LOADING                                         │
│ CSV (company_domains.csv) → SQLite DB (companies table)         │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│ STAGE 2: BATCH FETCHING                                         │
│ DB (pending companies) → In-memory list (for concurrent crawl)  │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│ STAGE 3: CONCURRENT WEB CRAWLING (Playwright)                  │
│ Homepage → Career page detection → Job listing extraction       │
│ Async concurrency: 3-5 domains per second                       │
│ Output: CrawlResult(domain, careers_url, jobs[], emails[])     │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│ STAGE 4: PERSISTENCE                                            │
│ CrawlResult → DB (companies + jobs tables)                      │
│ Creates job records with raw descriptions                       │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│ STAGE 5: PARALLEL JOB EVALUATION (Sentence-Transformer)        │
│ Resume + Job Description → Semantic Similarity Score (0-100)    │
│ Two-stage gate: fast semantic check → full eval                │
│ Output: JobEvaluation(fit_score, tech_stack[], is_backend)     │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│ STAGE 6: RESUME GENERATION & NOTIFICATION                       │
│ High-score jobs → Tailored PDF (Jinja2 + WeasyPrint)           │
│ → Telegram alert + inline action buttons                        │
│ Optional: ATS form-filling & contact form submission            │
└─────────────────────────────────────────────────────────────────┘
```

**Key Design Principles:**
- **100% Local Processing:** No API calls to external LLMs. Uses local `sentence-transformers` model.
- **Asynchronous I/O:** Concurrent domain crawling & parallel job evaluation via `asyncio` + `ThreadPoolExecutor`.
- **Resilient Crawling:** Retry logic (3 attempts), random delays (anti-bot), HTML change detection via MD5 hashing.
- **ATS Optimization:** Jinja2 templates with single-column layouts, clean typography, searchable text-based PDFs.
- **Stateful Pipeline:** Prefect handles workflow orchestration, failure recovery, and scheduling.

---

## Feature Set

### Core Features

| Feature | Implementation | Details |
|---------|---|---|
| **Web Scraping** | Playwright + asyncio | Async headless browser; stealth mode to bypass basic detection |
| **Career Page Detection** | Regex pattern matching | ~15 patterns (careers, jobs, hiring, greenhouse.io, lever.co, etc.) |
| **Target Role Filtering** | Regex keywords | Filters for backend/Python roles (customizable list) |
| **Email Extraction** | DOM parsing + link extraction | High-value prefixes (hr, hiring, careers, cto, founder, etc.) |
| **Job Evaluation** | Sentence-Transformer similarity | `all-MiniLM-L6-v2` model; cosine similarity scoring |
| **Resume Customization** | TF-IDF keyword extraction + Jinja2 | Top 3–5 keywords from JD injected into summary & skills |
| **ATS Form-Filling** | Playwright + XPath/CSS selectors | Supports Greenhouse, Lever, Ashby, BambooHR, Workable, generic |
| **Contact Form Fallback** | Automated detection & generic submission | Finds contact/contact-us page; submits outreach form if ATS fails |
| **Telegram Integration** | Async Telegram Bot API | Inline keyboards for apply/skip/view actions; daily digest summaries |
| **Scheduling** | Prefect with CronSchedule | Deploy for daily runs at specified time; UI dashboard at localhost:4200 |

### Anti-Detection Features

- **Random request delays:** Configurable 0.5–3 second random delay between requests
- **HTML MD5 hashing:** Detects whether a page changed since last crawl (skip if identical)
- **Dynamic concurrency:** Adapts crawl rate based on success rate (configurable threshold)
- **Stealth browser mode:** Playwright + `playwright-stealth` to bypass detection
- **Realistic user-agent:** HTTP headers mimic real browsers

---

## Project Structure

```
jobhunter_automation/
├── README.md                          # This file
├── main.py                            # Prefect flow orchestration (6 tasks)
├── config.py                          # Pydantic settings management
├── requirements.txt                   # Python dependencies
├── crawler.py                         # NODE 1–2: Domain & job scraping
├── evaluator.py                       # NODE 3: Semantic similarity scoring
├── resume_builder.py                  # NODE 4: Tailored resume generation
├── ats_apply.py                       # NODE 5: ATS form-filling
├── telegram_bot.py                    # NODE 5a: Telegram notifications & callbacks
├── database.py                        # SQLite schema & helpers
│
├── data/
│   ├── company_domains.csv            # Input: domain list (domain, status)
│   ├── master_resume.json             # Your full resume (manually created)
│   └── job_hunter.db                  # SQLite database (auto-created)
│
├── templates/
│   └── resume.html.j2                 # Jinja2 resume template
│
├── output/
│   ├── resumes/                       # Generated PDF resumes
│   └── screenshots/                   # ATS/contact form screenshots (on error)
│
├── logs/
│   └── pipeline.log                   # Structured log output
│
└── venv/                              # Python virtual environment
```

---

## Database Schema

### `companies` Table

Tracks domain crawl status and extracted contact info.

```sql
CREATE TABLE companies (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    domain                TEXT NOT NULL UNIQUE,        -- e.g. "stripe.com"
    company_name          TEXT,                         -- discovered/manual
    career_url            TEXT,                         -- final careers page URL
    extracted_emails_json TEXT DEFAULT '[]',            -- JSON array of strings
    homepage_html_hash    TEXT,                         -- MD5 (change detection)
    scraped_status        TEXT DEFAULT 'pending',       -- pending|crawling|scraped|no_careers|failed|skipped
    created_at            TEXT DEFAULT datetime('now'),
    scraped_at            TEXT,
    last_error            TEXT                          -- last error if status='failed'
);
```

**Indexes:** `idx_companies_status` on `scraped_status` (for batch fetching).

### `jobs` Table

Stores discovered job postings and evaluation results.

```sql
CREATE TABLE jobs (
    job_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id        INTEGER NOT NULL,                 -- FK to companies
    title             TEXT NOT NULL,                    -- job title
    url               TEXT NOT NULL UNIQUE,             -- job posting URL
    description       TEXT,                             -- raw HTML/text
    fit_score         INTEGER DEFAULT 0,                -- 0–100 similarity score
    is_backend        INTEGER DEFAULT 0,                -- 0/1 boolean
    tech_stack        TEXT DEFAULT '[]',                -- JSON array of keywords
    rejection_reason  TEXT,                             -- why score was low
    applied_status    TEXT DEFAULT 'pending',           -- pending|applying|applied|skipped|failed
    resume_path       TEXT,                             -- path to PDF if generated
    applied_at        TEXT,                             -- timestamp when submitted
    found_at          TEXT DEFAULT datetime('now'),
    evaluated_at      TEXT                              -- timestamp of evaluation
);
```

**Indexes:** 
- `idx_jobs_fit_score` on `fit_score` (for threshold filtering)
- `idx_jobs_status` on `applied_status` (for workflow queries)
- `idx_jobs_company` on `company_id` (for join queries)

**Relationships:** 
- FK constraint: `company_id → companies.id` (ON DELETE CASCADE)
- Foreign keys enforced via `PRAGMA foreign_keys=ON`

---

## Configuration

### Environment Variables & Settings

Configuration is **centralized in `config.py`**, loaded from `.env` via `pydantic-settings`:

```python
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # ─── Telegram ───────────────────────────────────────────────
    telegram_bot_token: str = "YOUR_TOKEN"        # @BotFather
    telegram_chat_id: str = "YOUR_CHAT_ID"        # Your personal chat
    
    # ─── Personal Info (ATS form-filling) ───────────────────────
    your_full_name: str = "YOUR_NAME"
    your_email: str = "YOUR_EMAIL"
    your_phone: str = "YOUR_PHONE"
    your_linkedin: str = "YOUR_LINKEDIN_URL"
    your_github: str = "YOUR_GITHUB_URL"
    your_location: str = "YOUR_LOCATION"
    
    # ─── Pipeline Tuning ────────────────────────────────────────
    batch_size: int = 100                         # domains per run
    fit_score_threshold: int = 75                 # only notify jobs ≥ 75
    crawl_delay_seconds: float = 2.0              # base delay per domain
    page_timeout_ms: int = 15_000                 # Playwright timeout
    
    # ─── Crawling Anti-Detection ────────────────────────────────
    max_retries: int = 3                          # retry failed domains
    base_retry_delay: float = 1.0                 # seconds
    random_delay_min: float = 0.5
    random_delay_max: float = 3.0
    dynamic_concurrency: bool = True              # adapt rate based on success
    success_rate_check_interval: int = 10         # check every N domains
    
    # ─── Derived Paths (auto-calculated) ────────────────────────
    @property
    def base_dir(self) -> Path:
        return Path(__file__).parent
    
    @property
    def db_path(self) -> Path:
        return self.base_dir / "data" / "job_hunter.db"
    
    @property
    def resumes_dir(self) -> Path:
        return self.base_dir / "output" / "resumes"
    
    @property
    def templates_dir(self) -> Path:
        return self.base_dir / "templates"

settings = Settings()
```

### `.env` File Template

Create `.env` in the project root:

```bash
# Telegram Bot
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklmnoPQRstuvWXYZ
TELEGRAM_CHAT_ID=987654321

# Your Info
YOUR_FULL_NAME=John Doe
YOUR_EMAIL=john@example.com
YOUR_PHONE=+1-555-0100
YOUR_LINKEDIN=https://linkedin.com/in/johndoe
YOUR_GITHUB=https://github.com/johndoe
YOUR_LOCATION=San Francisco, CA

# Pipeline
BATCH_SIZE=100
FIT_SCORE_THRESHOLD=75
CRAWL_DELAY_SECONDS=2.0
PAGE_TIMEOUT_MS=15000

# Anti-Detection
MAX_RETRIES=3
RANDOM_DELAY_MIN=0.5
RANDOM_DELAY_MAX=3.0
DYNAMIC_CONCURRENCY=true
```

---

## Setup & Installation

### Prerequisites

- **Python 3.11+**
- **pip** (package manager)
- ~500 MB disk space (SQLite DB + PDFs)
- **RAM:** 2+ GB (Playwright + model loading)

### Step 1: Clone Repository

```bash
git clone https://github.com/syuneshakaryan/Job-Hunt-Automation.git
cd Job-Hunt-Automation
```

### Step 2: Create Virtual Environment

```bash
# Create venv
python -m venv venv

# Activate
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

**Dependencies Overview:**
- **Orchestration:** `prefect>=3.1.0`
- **Web Scraping:** `playwright>=1.44.0`, `beautifulsoup4>=4.12.3`, `lxml>=5.2.2`
- **Semantic Search:** `sentence-transformers>=2.2.2`
- **Resume Generation:** `jinja2>=3.1.4`, `weasyprint>=62.3`
- **Telegram:** `python-telegram-bot>=21.3`
- **Database:** Built-in `sqlite3` (no install needed)
- **Utilities:** `pydantic>=2.7.1`, `python-dotenv>=1.0.1`, `tenacity>=8.3.0`, `tqdm>=4.66.4`

### Step 4: Configure Settings

Create `.env` with your Telegram token, personal info, etc.:

```bash
cp .env.example .env  # (if available, or create manually)
nano .env             # Edit with your values
```

### Step 5: Prepare Data Files

#### `data/master_resume.json`

Your full resume as structured JSON. Example:

```json
{
  "name": "John Doe",
  "email": "john@example.com",
  "phone": "+1-555-0100",
  "location": "San Francisco, CA",
  "linkedin": "https://linkedin.com/in/johndoe",
  "github": "https://github.com/johndoe",
  "summary": "Full-stack Python engineer with 5+ years building scalable APIs and data pipelines.",
  "experience": [
    {
      "company": "Tech Corp",
      "title": "Senior Backend Engineer",
      "start_date": "2020-01",
      "end_date": null,
      "bullets": [
        "Designed and implemented event-driven ETL pipeline in Python/Kafka processing 1M+ events/day",
        "Led migration from monolith to microservices (FastAPI), reducing latency by 40%",
        "Mentored 3 junior engineers on REST API design and PostgreSQL optimization",
        "Achieved 99.9% uptime SLA for critical payment processing service"
      ]
    }
  ],
  "skills": [
    "Python", "FastAPI", "Django", "Flask",
    "PostgreSQL", "Redis", "Docker", "Kubernetes",
    "AWS (EC2, RDS, S3, Lambda)", "GCP",
    "REST APIs", "GraphQL", "Kafka", "Celery",
    "Linux", "Git", "CI/CD"
  ],
  "projects": [
    {
      "title": "Job Hunter Automation",
      "description": "Full-stack Python pipeline for job discovery and ATS automation",
      "url": "https://github.com/johndoe/job-hunter",
      "tech": ["Python", "Playwright", "Sentence-Transformers", "SQLite", "Prefect"]
    }
  ],
  "education": [
    {
      "school": "University of California",
      "degree": "B.S.",
      "field": "Computer Science",
      "graduation_year": 2018
    }
  ]
}
```

#### `data/company_domains.csv`

List of companies to crawl. Example:

```csv
domain,status
stripe.com,pending
databricks.com,pending
anthropic.com,pending
openai.com,pending
huggingface.co,pending
```

### Step 6: Initialize Database

```bash
python main.py --load-csv
```

This loads all domains from `company_domains.csv` into the `companies` table. Safe to run multiple times (INSERT OR IGNORE deduplicates).

---

## Pipeline Workflow

### Full Flow Diagram (Task Dependency Graph)

```
┌───────────────────────────┐
│ TASK 1: Load Domains      │
│ CSV → DB (companies)      │
└──────────────┬────────────┘
               │
┌──────────────▼────────────┐
│ TASK 2: Fetch Batch       │
│ DB → List[companies]      │
└──────────────┬────────────┘
               │
┌──────────────▼────────────┐
│ TASK 3: Crawl Domains     │
│ [domains] → [CrawlResult] │
│ (concurrent: 3 domains/s) │
└──────────────┬────────────┘
               │
┌──────────────▼────────────┐
│ TASK 4: Persist Crawl     │
│ CrawlResult → DB + jobs   │
└──────────────┬────────────┘
               │
┌──────────────▼────────────┐
│ TASK 5: Evaluate Jobs     │
│ [jobs] → [JobEvaluation]  │
│ (parallel: 4 workers)     │
└──────────────┬────────────┘
               │
┌──────────────▼─────────────────────────┐
│ TASK 6: Build & Notify                 │
│ high-score jobs → PDF + Telegram        │
│ (optional: trigger ATS auto-apply)      │
└───────────────────────────────────────┘
```

### Execution Modes

#### **Mode 1: Manual One-Shot Run**

```bash
python main.py
```

- Processes `batch_size` pending domains (default 100)
- Crawls → evaluates → builds resumes → sends Telegram alerts
- No persistence between runs (suitable for testing)

#### **Mode 2: CSV Seed + Single Run**

```bash
python main.py --load-csv
```

- Loads `company_domains.csv` into DB
- Runs pipeline once
- All state persisted to SQLite

#### **Mode 3: Custom Batch Size**

```bash
python main.py --batch-size 50
```

- Override `batch_size` setting for this run

#### **Mode 4: Prefect Scheduled Deployment**

```bash
# Terminal 1: Start Prefect server
prefect server start
# → UI at http://127.0.0.1:4200

# Terminal 2: Deploy the flow
prefect deploy main.py:job_hunter_flow --name "Daily Job Hunt"

# In Prefect UI:
# → Create a schedule (e.g., "0 8 * * *" = 8 AM daily)
# → Monitor runs, see logs, trigger manually
```

---

## Module Reference

### `main.py` – Prefect Orchestration

**Exports:**
- `load_domains_task(csv_path)` → `int` (domains inserted)
- `fetch_batch_task(batch_size)` → `List[dict]` (companies)
- `crawl_domains_task(companies)` → `List[dict]` (crawl results)
- `persist_crawl_task(companies, crawl_results)` → `List[dict]` (raw jobs)
- `evaluate_jobs_task(raw_jobs)` → `List[dict]` (qualified jobs)
- `build_resumes_task(jobs)` → `int` (resumes generated)
- `notify_task(jobs)` → `bool` (notification sent)
- `digest_task()` → `str` (summary message)
- `job_hunter_flow()` – Main Prefect flow (ties all tasks together)

**CLI Arguments:**
```
--load-csv              Load domains from CSV before running
--batch-size N          Override batch size
```

### `config.py` – Settings Management

**Exports:**
- `settings: Settings` – Singleton instance
- Loads from `.env` via pydantic-settings
- Type-safe settings with validation
- Derived path properties (db_path, resumes_dir, etc.)

### `crawler.py` – Web Scraping (NODE 1–2)

**Key Functions:**
- `crawl_batch(domains, concurrency)` → `List[CrawlResult]` *(async)*
  - Concurrent domain crawling with stealth + retry logic
  - Returns CrawlResult objects with jobs, emails, career URL

**Data Structures:**
```python
@dataclass
class JobListing:
    title: str
    url: str
    description: str
    source_page: str

@dataclass
class CrawlResult:
    domain: str
    company_url: str
    career_url: str
    emails: List[str]
    jobs: List[JobListing]
    status: str  # ok | no_careers | no_jobs | failed
    error: str
    html_hash: str
```

**Features:**
- Career page detection via regex patterns (15+ patterns)
- Target role keyword filtering (Python, backend, DevOps, etc.)
- Email extraction with high-value prefix prioritization
- HTML MD5 change detection (skips re-crawl if identical)
- Async concurrency: 3–5 domains/sec (configurable)
- Retry logic: 3 attempts with exponential backoff
- Stealth mode to bypass bot detection

### `evaluator.py` – Semantic Similarity (NODE 3)

**Key Functions:**
- `check_evaluator_health()` → `bool` – Verify model is loaded
- `evaluate_with_gate(job, resume_text)` → `Optional[JobEvaluation]` – Two-stage evaluation
- `batch_evaluate_parallel(jobs, max_workers)` → `List[dict]` – Parallel evaluation

**Algorithm:**
1. **Gate (Fast):** Checks if job description contains ≥1 backend keyword
2. **Full Eval (Slow):** Computes cosine similarity between resume embedding and job description embedding
3. **Tech Stack Extraction:** Identifies technology keywords from JD

**Data Structure:**
```python
class JobEvaluation(BaseModel):
    is_backend_role: bool
    fit_score: int  # 0–100
    core_tech_stack: List[str]
    rejection_reason: str
```

**Model:** `sentence-transformers/all-MiniLM-L6-v2` (~33 MB, loads on first use)

### `resume_builder.py` – Resume Generation (NODE 4)

**Key Functions:**
- `load_master_resume()` → `dict` – Load `master_resume.json`
- `extract_jd_keywords(job_description, top_n)` → `List[str]` – TF-IDF-like extraction
- `select_bullets(master_bullets, tech_stack)` → `List[str]` – Score + rank bullets
- `build_resume(company, job, job_description, resume_path)` → `str` (PDF path)

**Resume Customization:**
- Selects top 5 bullet points matching job's tech stack
- Extracts top 3–5 keywords from JD
- Injects keywords into summary and skills sections
- Single-column HTML layout (ATS-safe)

**Output:** PDF at `output/resumes/{company}_{job_slug}.pdf`

### `ats_apply.py` – Form-Filling (NODE 5)

**Key Functions:**
- `detect_ats(url)` → `ATSPlatform` – Detect ATS system (Greenhouse, Lever, etc.)
- `apply_to_ats(job_url, resume_pdf_path)` → `bool` *(async)* – Submit application
- `find_and_apply_contact_form(domain)` → `bool` *(async)* – Fallback contact form submission

**Supported Platforms:**
- Greenhouse.io
- Lever.co
- Ashby.hq.com
- BambooHR
- Workable
- Generic fallback (best-effort XPath/CSS matching)

**Features:**
- Smart submit button detection (scrolls, tries JS click)
- Pre-flight URL validation (HEAD check for 404/5xx)
- Contact form auto-detection
- Screenshots on error for manual review

### `telegram_bot.py` – Notifications (NODE 5a)

**Key Functions:**
- `notify_job_match(job_id, company, title, fit_score, resume_pdf_path)` → `bool` *(async)*
  - Sends formatted message + PDF to user
  - Inline keyboard: 🚀 Apply | ⏭ Skip | 📋 View JD

- `send_daily_digest(stats)` → `bool` *(async)*
  - Summary of crawl results, evaluations, applications

**Callback Handlers:**
- `/apply {job_id}` → Triggers ATS form-filling
- `/skip {job_id}` → Marks job as skipped
- `/view {job_id}` → Sends full job description

**State:** All stored in SQLite (no in-memory state needed)

### `database.py` – Data Layer

**Key Functions:**
- `init_db()` – Create tables & indexes
- `bulk_insert_domains(domains)` → `int` – Batch insert from CSV
- `fetch_pending_companies(limit)` → `List[Row]` – Get unscraped domains
- `update_company(id, **kwargs)` – Generic update
- `insert_job(company_id, title, url, description)` → `Optional[int]`
- `update_job(id, **kwargs)` – Generic update
- `fetch_jobs_for_review()` → `List[Row]` – High-score pending jobs
- `get_pipeline_stats()` → `dict` – Summary counts (crawled, evaluated, applied, etc.)

**Connection Helpers:**
- `get_connection()` – Context manager with WAL mode + foreign keys enforced

---

## Running the Pipeline

### Command Reference

| Command | Purpose |
|---------|---------|
| `python main.py` | Run one batch (interactive) |
| `python main.py --load-csv` | Load CSV + run batch |
| `python main.py --batch-size 50` | Run with custom batch size |
| `prefect server start` | Start Prefect UI (http://localhost:4200) |
| `prefect deploy main.py:job_hunter_flow` | Deploy for scheduling |
| `prefect flow run job_hunter_flow` | Trigger manually from CLI |

### Example: Full Workflow

```bash
# 1. Activate virtual environment
source venv/bin/activate  # or venv\Scripts\activate on Windows

# 2. Load domains from CSV
python main.py --load-csv
# → Logs: "Loaded domains from CSV: 42 new rows inserted"

# 3. Run first batch
python main.py --batch-size 10
# → Crawls 10 domains
# → Extracts ~30 jobs
# → Evaluates against your resume
# → Generates PDFs for high-scoring jobs
# → Sends Telegram notifications

# 4. Set up scheduled runs
prefect server start &
prefect deploy main.py:job_hunter_flow --name "Daily Job Hunt"
# → Configure cron schedule in UI
# → Monitor runs in dashboard
```

### Monitoring & Logging

**Log Output:**
- Console: Real-time progress (via Prefect task logging)
- File: `logs/pipeline.log` (structured logs with timestamps)

**Database Inspection:**

```bash
sqlite3 data/job_hunter.db

# Check crawl progress
SELECT scraped_status, COUNT(*) FROM companies GROUP BY scraped_status;
# → pending | 50, scraped | 100, failed | 5, no_careers | 15

# Find high-score jobs
SELECT title, fit_score, applied_status FROM jobs WHERE fit_score >= 75 ORDER BY fit_score DESC;

# Check a specific company
SELECT * FROM companies WHERE domain = 'stripe.com';
SELECT title, fit_score FROM jobs WHERE company_id = 3;
```

---

## Advanced Configuration

### Tuning Crawl Performance

```python
# In config.py (or .env):
batch_size = 200                  # Crawl more per run
crawl_delay_seconds = 1.0         # Faster (but riskier)
page_timeout_ms = 20_000          # Longer timeout for slow sites
max_retries = 5                   # More resilient to failures
dynamic_concurrency = True        # Auto-adapt concurrency
success_rate_check_interval = 20  # Check every N domains
```

### Target Role Customization

Edit `TARGET_ROLE_KEYWORDS` in `crawler.py` to match your specialization:

```python
TARGET_ROLE_KEYWORDS = [
    re.compile(r"\bfrontend\b", re.IGNORECASE),
    re.compile(r"\breact\b", re.IGNORECASE),
    re.compile(r"\bjavascript\b", re.IGNORECASE),
    # ... add your keywords
]
```

### Resume Template Customization

Edit `templates/resume.html.j2` for different styling, sections, fonts, etc.:

```html
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: 'Arial', sans-serif; margin: 20px; }
        h1 { color: #333; font-size: 24px; }
        .experience { margin-top: 20px; }
    </style>
</head>
<body>
    <h1>{{ resume.name }}</h1>
    <p>{{ resume.email }} | {{ resume.phone }}</p>
    
    <h2>Summary</h2>
    <p>{{ resume.summary }}</p>
    {{ highlighted_skills }}
    
    <h2>Experience</h2>
    {% for job in selected_experience %}
        <div class="experience">
            <strong>{{ job.title }}</strong> at {{ job.company }}
            <ul>
            {% for bullet in job.selected_bullets %}
                <li>{{ bullet }}</li>
            {% endfor %}
            </ul>
        </div>
    {% endfor %}
</body>
</html>
```

### ATS Platform-Specific Tuning

Edit field selectors in `ats_apply.py`:

```python
FIELD_SELECTORS = {
    "first_name": [
        "input[name='first_name']",
        "input[name='firstName']",
        "input[placeholder*='First']",
    ],
    # ... add more patterns for each field
}
```

---

## Performance Optimization

### Concurrency Tuning

**Crawler Concurrency (domain crawling):**
- Default: 3 domains/sec
- Adjust in `crawler.py`: `CONCURRENT_LIMIT = 3`
- Higher = faster but risks detection; lower = safer but slower

**Evaluator Parallelism (job scoring):**
- Default: 4 thread workers (ThreadPoolExecutor)
- Adjust in `evaluator.py`: `max_workers = 4`
- CPU-bound (sentence-transformer embedding), so 4–8 is optimal

**Memory Footprint:**
- Playwright browser: ~150 MB per instance
- Sentence-transformer model: ~33 MB (cached after first load)
- SQLite DB: ~5–10 MB per 1000 jobs
- Total RAM usage: 500 MB – 1 GB depending on config

### Batch Size Optimization

| Batch Size | Time (est.) | Domains/Run | Use Case |
|---|---|---|---|
| 10 | 5–10 min | 10 | Testing, hourly runs |
| 50 | 30–45 min | 50 | Daily runs |
| 100 | 60–90 min | 100 | Balanced |
| 200+ | 2+ hours | 200+ | Weekly bulk crawls |

### Database Optimization

```sql
-- Vacuum database (reclaim space)
VACUUM;

-- Check index usage
SELECT * FROM sqlite_master WHERE type='index';

-- Force index recompute
REINDEX;
```

---

## Troubleshooting

### Common Issues & Solutions

| Issue | Cause | Solution |
|---|---|---|
| **ModuleNotFoundError: sentence_transformers** | Missing dependency | `pip install sentence-transformers` |
| **Playwright: chromium not found** | Browser not installed | `playwright install chromium` |
| **Telegram: 401 Unauthorized** | Invalid bot token | Verify token from @BotFather |
| **Database locked** | Concurrent writes | Ensure WAL mode: `PRAGMA journal_mode=WAL;` |
| **No careers page found** | Career page not detected | Add regex pattern to `CAREERS_LINK_PATTERNS` |
| **Low fit scores** | Resume not matching JD | Ensure `master_resume.json` is detailed with rich keywords |
| **ATS form not filling** | Unsupported platform / field names changed | Check form with browser DevTools; update selectors in `ats_apply.py` |
| **Slow crawling** | Concurrency too low or site is slow | Increase `CONCURRENT_LIMIT` or `page_timeout_ms` |

### Debug Mode

Enable verbose logging:

```python
# In main.py
import logging
logging.basicConfig(level=logging.DEBUG)  # verbose output
```

Inspect database:

```bash
sqlite3 data/job_hunter.db ".dump" > backup.sql  # Export
sqlite3 data/job_hunter.db ".mode column" "SELECT * FROM companies LIMIT 5;"  # Browse
```

### Reset Pipeline

```bash
# Delete database (START FRESH)
rm data/job_hunter.db

# Re-initialize
python main.py --load-csv
```

---

## Contributing

Contributions welcome! Areas for improvement:

- [ ] Multi-language support (non-English JDs)
- [ ] LinkedIn profile scraping (no-cookie approach)
- [ ] Salary extraction & filtering
- [ ] Cover letter generation
- [ ] Email outreach templates
- [ ] Additional ATS platform support
- [ ] Dashboard UI (web-based progress tracking)
- [ ] Email notifications (alternative to Telegram)

**Contribution Guidelines:**
1. Fork the repo
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit: `git commit -am "Add feature"`
4. Push: `git push origin feature/my-feature`
5. Open a Pull Request

**Security:** Do NOT commit secrets (API keys, tokens). Use `.env` file and add to `.gitignore`.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## Disclaimer

**Automated job application tools operate in a legal gray zone.** Always respect:
- Site ToS (many forbid automation)
- robots.txt rules
- Rate limits & server load
- Local employment laws

**Use responsibly:** This tool is for personal learning & productivity enhancement. Verify ATS submissions before sending personal data.

---

## Support & Questions

- **Issues:** Open a GitHub issue with details (logs, error messages, config)
- **Discussions:** Use GitHub Discussions for feature ideas
- **Security:** Report vulnerabilities privately (do NOT open public issues with secrets)