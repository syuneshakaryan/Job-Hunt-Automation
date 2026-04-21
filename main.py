"""
main.py
=======
STEP 6 — Prefect Orchestration

Ties all pipeline nodes into a single scheduled flow:

  load_domains_task        CSV → DB
       │
  fetch_batch_task         DB → 50 pending companies
       │
  crawl_domain_task        domain → CrawlResult   (per company, concurrent)
       │
  persist_crawl_task       CrawlResult → DB        (update companies + insert raw jobs)
       │
  evaluate_job_task        raw job → JobEvaluation  (per job, via similarity evaluator)
       │
  persist_evaluation_task  JobEvaluation → DB       (update fit_score, tech_stack …)
       │
  build_resume_task        high-score job → PDF     (per qualifying job)
       │
  notify_task              PDF + job data → Telegram ping
       │
  digest_task              daily stats → Telegram summary

Scheduling:
  - Default: run once immediately (manual trigger)
  - Scheduled: add a CronSchedule to the deployment for 08:00 daily
  - Start Prefect UI:  prefect server start
  - Deploy:            prefect deploy main.py:job_hunter_flow

Usage:
  python main.py                      # run one batch now
  python main.py --load-csv           # seed DB from data/company_domains.csv first
  python main.py --batch-size 100     # override batch size
  prefect server start                # open http://127.0.0.1:4200
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

def _utcnow() -> str:
    """Return current UTC time as ISO string (timezone-aware, no deprecation warning)."""
    return datetime.now(timezone.utc).isoformat()
from pathlib import Path

from prefect import flow, task, get_run_logger
from prefect.task_runners import ConcurrentTaskRunner

from config import settings
from crawler import CrawlResult, crawl_batch
from database import (
    bulk_insert_domains,
    fetch_pending_companies,
    get_pipeline_stats,
    init_db,
    insert_job,
    load_domains_from_csv,
    update_company,
    update_job,
)
from evaluator import check_evaluator_health, evaluate_with_gate, batch_evaluate_parallel
from resume_builder import build_resume
from telegram_bot import notify_job_match, send_daily_digest
from typing import Optional

logger = logging.getLogger("main")


# ─────────────────────────────────────────────────────────────────────────────
# TASK 1 — Load domains from CSV into DB
# ─────────────────────────────────────────────────────────────────────────────

@task(name="load-domains", retries=1)
def load_domains_task(csv_path: Optional[Path] = None) -> int:
    """
    Seed the companies table from a CSV file.
    Safe to call multiple times — INSERT OR IGNORE deduplicates.

    Args:
        csv_path: Path to CSV with a 'domain' column.
                  Defaults to data/company_domains.csv

    Returns:
        Number of newly inserted domains.
    """
    log = get_run_logger()
    path = csv_path or (settings.base_dir / "data" / "company_domains.csv")

    if not path.exists():
        log.warning(f"CSV not found: {path} — skipping domain load")
        return 0

    inserted = load_domains_from_csv(path)
    log.info(f"Loaded domains from CSV: {inserted} new rows inserted")
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# TASK 2 — Fetch a batch of pending companies from DB
# ─────────────────────────────────────────────────────────────────────────────

@task(name="fetch-batch")
def fetch_batch_task(batch_size: Optional[int] = None) -> list[dict]:

    """
    Pull up to `batch_size` un-scraped companies from the DB.

    Returns a list of plain dicts (sqlite3.Row → dict conversion)
    so they are serialisable across Prefect task boundaries.
    """
    log = get_run_logger()
    size  = batch_size or settings.batch_size
    rows  = fetch_pending_companies(limit=size)
    batch = [dict(r) for r in rows]
    log.info(f"Fetched {len(batch)} pending companies (batch_size={size})")
    return batch


# ─────────────────────────────────────────────────────────────────────────────
# TASK 3 — Crawl a batch of domains
# ─────────────────────────────────────────────────────────────────────────────

@task(name="crawl-domains", retries=1, retry_delay_seconds=10)
def crawl_domains_task(companies: list[dict]) -> list[dict]:
    """
    Crawl all domains in the batch concurrently.

    Marks each company as 'crawling' before starting, then 'scraped'
    or 'failed' after. Returns serialisable dicts of CrawlResult data.
    """
    log = get_run_logger()

    if not companies:
        log.info("No companies to crawl")
        return []

    # Mark all as crawling so re-runs skip them if interrupted mid-batch
    for company in companies:
        update_company(company["id"], scraped_status="crawling")

    domains = [c["domain"] for c in companies]
    log.info(f"Crawling {len(domains)} domains...")

    # Run the async batch crawler synchronously inside this Prefect task
    results: list[CrawlResult] = asyncio.run(
        crawl_batch(domains, concurrency=3)
    )

    # Serialise CrawlResult dataclasses to plain dicts
    serialised = []
    for res in results:
        serialised.append({
            "domain":      res.domain,
            "company_url": res.company_url,
            "career_url":  res.career_url,
            "emails":      res.emails,
            "status":      res.status,
            "error":       res.error,
            "html_hash":   res.html_hash,
            "jobs": [
                {
                    "title":       j.title,
                    "url":         j.url,
                    "description": j.description,
                    "source_page": j.source_page,
                }
                for j in res.jobs
            ],
        })

    log.info(
        f"Crawl complete: "
        f"{sum(1 for r in serialised if r['status'] == 'ok')} ok | "
        f"{sum(1 for r in serialised if r['status'] == 'no_careers')} no_careers | "
        f"{sum(1 for r in serialised if r['status'] == 'failed')} failed"
    )
    return serialised


# ─────────────────────────────────────────────────────────────────────────────
# TASK 4 — Persist crawl results to DB
# ─────────────────────────────────────────────────────────────────────────────

@task(name="persist-crawl")
def persist_crawl_task(
    companies:    list[dict],
    crawl_results: list[dict],
) -> list[dict]:
    """
    Write crawl results back to the companies table.
    Insert raw job listings into the jobs table.

    Returns a flat list of raw job dicts enriched with company_id,
    ready for the evaluator.
    """
    log = get_run_logger()

    # Build domain → company_id lookup
    domain_to_id = {c["domain"]: c["id"] for c in companies}

    raw_jobs: list[dict] = []
    now = _utcnow()

    for result in crawl_results:
        domain     = result["domain"]
        company_id = domain_to_id.get(domain)

        if company_id is None:
            log.warning(f"No company_id found for domain: {domain}")
            continue

        # Update company record
        update_kwargs = {
            "scraped_status":        result["status"] if result["status"] != "ok" else "scraped",
            "scraped_at":            now,
            "career_url":            result["career_url"],
            "extracted_emails_json": json.dumps(result["emails"]),
            "homepage_html_hash":    result["html_hash"],
        }
        if result["error"]:
            update_kwargs["last_error"] = result["error"][:500]

        update_company(company_id, **update_kwargs)

        # Insert each job found
        for job in result.get("jobs", []):
            job_id = insert_job(
                company_id=company_id,
                title=job["title"],
                url=job["url"],
                description=job["description"],
            )
            if job_id is not None:
                raw_jobs.append({
                    "job_id":     job_id,
                    "company_id": company_id,
                    "domain":     domain,
                    "emails":     result["emails"],
                    "title":      job["title"],
                    "url":        job["url"],
                    "description": job["description"],
                })

    log.info(
        f"Persisted {len(crawl_results)} crawl results | "
        f"{len(raw_jobs)} new jobs inserted"
    )
    return raw_jobs


# ─────────────────────────────────────────────────────────────────────────────
# TASK 5 — Evaluate jobs with local similarity evaluator
# ─────────────────────────────────────────────────────────────────────────────

@task(name="evaluate-jobs", retries=2, retry_delay_seconds=5)
def evaluate_jobs_task(raw_jobs: list[dict]) -> list[dict]:
    """
    Run each raw job through the two-stage similarity evaluator:
      1. Gate check (fast)
      2. Full JobEvaluation (if gate passes)

    Only jobs with fit_score >= settings.fit_score_threshold are returned.
    All results (pass and fail) are written back to the DB.
    """
    log = get_run_logger()

    if not raw_jobs:
        log.info("No raw jobs to evaluate")
        return []

    now = _utcnow()

    # Parallel evaluation using the local similarity evaluator
    max_workers = 4
    log.info(f"Evaluating {len(raw_jobs)} jobs | workers={max_workers}")

    evaluated = batch_evaluate_parallel(raw_jobs, max_workers=max_workers)

    # Persist ALL evaluated results to DB (pass and fail)
    # First pass the failed ones (below threshold)
    evaluated_urls = {j["url"] for j in evaluated}
    for job in raw_jobs:
        if job["url"] not in evaluated_urls:
            update_job(
                job["job_id"],
                fit_score=0,
                is_backend=0,
                rejection_reason="gated_out_or_error",
                evaluated_at=now,
            )

    qualified: list[dict] = []
    for job in evaluated:
        update_job(
            job["job_id"],
            fit_score=job["fit_score"],
            is_backend=int(job["is_backend"]),
            tech_stack=json.dumps(job["tech_stack"]),
            rejection_reason=job.get("rejection_reason", ""),
            evaluated_at=now,
        )
        
        # Only include jobs that meet the score threshold
        if job["fit_score"] >= settings.fit_score_threshold:
            evaluation = job.get("_evaluation")
            qualified.append({
                **job,
                "tech_stack":  evaluation.core_tech_stack if evaluation else [],
                "seniority":   job.get("seniority", "unknown"),
                "is_remote":   job.get("is_remote_friendly"),
                "strengths":   json.loads(job.get("matching_strengths", "[]")) if isinstance(job.get("matching_strengths"), str) else job.get("matching_strengths", []),
                "gaps":        json.loads(job.get("potential_gaps", "[]")) if isinstance(job.get("potential_gaps"), str) else job.get("potential_gaps", []),
            })
            log.info(f"  ✓ QUALIFIED — score={job['fit_score']} {job.get('title', '')!r}")
        else:
            log.debug(f"  ✗ REJECTED — score={job['fit_score']} {job.get('title', '')!r}")

    log.info(f"Evaluation done: {len(qualified)}/{len(raw_jobs)} jobs qualified")
    return qualified


# ─────────────────────────────────────────────────────────────────────────────
# TASK 6 — Build tailored resume PDFs
# ─────────────────────────────────────────────────────────────────────────────

@task(name="build-resumes")
def build_resumes_task(qualified_jobs: list[dict]) -> list[dict]:
    """
    Generate a tailored PDF resume for every qualified job.
    Stores the PDF path back into the jobs table.

    Returns the same list enriched with a 'resume_path' key.
    """
    log = get_run_logger()

    if not qualified_jobs:
        log.info("No qualified jobs — no resumes to build")
        return []

    enriched: list[dict] = []

    for job in qualified_jobs:
        company_name = job.get("domain", "Company").replace(".com", "").replace(".io", "").title()
        job_title    = job.get("title", "Python Developer")
        tech_stack   = job.get("tech_stack", [])

        try:
            pdf_path = build_resume(
                company_name=company_name,
                job_title=job_title,
                tech_stack=tech_stack,
                output_dir=settings.resumes_dir,
                job_description=job.get("description", ""),  # FIX: keyword injection
            )

            # Save path to DB
            update_job(job["job_id"], resume_path=str(pdf_path))

            log.info(f"Resume built: {pdf_path.name}")
            enriched.append({**job, "resume_path": pdf_path})

        except Exception as exc:
            log.error(f"Resume build failed for job_id={job['job_id']}: {exc}")
            # Still include job without resume — notify will handle gracefully
            enriched.append({**job, "resume_path": None})

    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# TASK 7 — Send Telegram notifications
# ─────────────────────────────────────────────────────────────────────────────

@task(name="notify-matches")
def notify_task(qualified_jobs: list[dict]) -> int:
    """
    Send a Telegram job alert for each qualified job.
    Returns count of successfully sent notifications.
    """
    log = get_run_logger()

    if not qualified_jobs:
        log.info("No qualified jobs to notify")
        return 0

    sent = 0
    for job in qualified_jobs:
        resume_path = job.get("resume_path")

        # Gracefully handle missing resume
        if resume_path and not Path(resume_path).exists():
            log.warning(f"Resume not found at {resume_path}, notifying without attachment")
            resume_path = None

        success = notify_job_match(
            job_id=job["job_id"],
            company=job.get("domain", "Unknown"),
            job_title=job.get("title", "Python Developer"),
            job_url=job.get("url", ""),
            fit_score=job.get("fit_score", 0),
            tech_stack=job.get("tech_stack", []),
            emails=job.get("emails", []),
            resume_path=resume_path,
            strengths=job.get("strengths", []),
            gaps=job.get("gaps", []),
            is_remote=job.get("is_remote"),
            seniority=job.get("seniority", "unknown"),
        )

        if success:
            sent += 1
            log.info(f"Alert sent: job_id={job['job_id']} {job.get('title')!r}")
        else:
            log.warning(f"Alert failed: job_id={job['job_id']}")

    log.info(f"Notifications sent: {sent}/{len(qualified_jobs)}")
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# TASK 8 — Daily digest
# ─────────────────────────────────────────────────────────────────────────────

@task(name="send-digest")
def digest_task() -> None:
    """Send pipeline stats digest to Telegram."""
    log = get_run_logger()
    stats = get_pipeline_stats()
    log.info(f"Pipeline stats: {stats}")

    asyncio.run(send_daily_digest())
    log.info("Daily digest sent")


# ─────────────────────────────────────────────────────────────────────────────
# PRE-FLIGHT CHECKS
# ─────────────────────────────────────────────────────────────────────────────

@task(name="preflight-checks")
def preflight_task() -> dict:
    """
    Verify the environment before running the pipeline.
    Raises ValueError if a blocking issue is found.
    Returns a dict of capability flags.
    """
    log = get_run_logger()
    flags = {
        "db_ok":        False,
        "evaluator_ok": False,
        "telegram_ok":  False,
        "csv_found":    False,
    }

    # DB init (idempotent)
    try:
        init_db()
        flags["db_ok"] = True
        log.info("✅ Database ready")
    except Exception as exc:
        raise ValueError(f"Database init failed: {exc}") from exc

    # Local similarity evaluator
    if check_evaluator_health():
        flags["evaluator_ok"] = True
        log.info("✅ Similarity evaluator ready")
    else:
        log.warning(
            "⚠️  Similarity evaluator not available — evaluation will be skipped."
        )

    # Telegram
    if settings.telegram_bot_token != "CHANGE_ME":
        flags["telegram_ok"] = True
        log.info("✅ Telegram token configured")
    else:
        log.warning("⚠️  Telegram token not set — notifications will be skipped")

    # CSV
    csv_path = settings.base_dir / "data" / "company_domains.csv"
    if csv_path.exists():
        flags["csv_found"] = True
        log.info(f"✅ CSV found: {csv_path}")
    else:
        log.warning(f"⚠️  CSV not found: {csv_path}")

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# MAIN FLOW
# ─────────────────────────────────────────────────────────────────────────────

@flow(
    name="job-hunter",
    description="AI-powered job discovery, evaluation, and application pipeline",
    task_runner=ConcurrentTaskRunner(),
    log_prints=True,
)
def job_hunter_flow(
    batch_size:  Optional[int] = None,
    load_csv:    bool = False,
    csv_path:    Optional[str] = None,
    skip_notify: bool = False,
) -> dict:
    """
    Master pipeline flow.

    Args:
        batch_size:  How many companies to process this run (default: settings.batch_size)
        load_csv:    If True, seed DB from company_domains.csv before crawling
        csv_path:    Custom path to the domains CSV
        skip_notify: If True, skip Telegram notifications (useful for dry runs)

    Returns:
        Summary dict with counts for this run.
    """
    log = get_run_logger()
    run_start = datetime.now(timezone.utc)
    log.info(f"{'='*60}")
    log.info(f"Job Hunter Flow starting at {run_start.strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"{'='*60}")

    # ── 0. Pre-flight ────────────────────────────────────────────────────────
    flags = preflight_task()

    # ── 1. Optionally seed DB from CSV ──────────────────────────────────────
    if load_csv:
        loaded = load_domains_task(
            csv_path=Path(csv_path) if csv_path else None
        )
        log.info(f"CSV loaded: {loaded} new domains")

    # ── 2. Fetch pending companies ───────────────────────────────────────────
    companies = fetch_batch_task(batch_size=batch_size)

    if not companies:
        log.info("No pending companies — all domains processed. Add more via CSV.")
        digest_task()
        return {"status": "no_pending_companies", "companies": 0}

    log.info(f"Processing {len(companies)} companies this run")

    # ── 3. Crawl all domains ─────────────────────────────────────────────────
    crawl_results = crawl_domains_task(companies)

    # ── 4. Persist crawl results + raw jobs to DB ───────────────────────────
    raw_jobs = persist_crawl_task(companies, crawl_results)
    log.info(f"Raw jobs discovered this run: {len(raw_jobs)}")

    # ── 5. Job evaluation ───────────────────────────────────────────────────
    if not flags.get("evaluator_ok"):
        log.warning("Skipping job evaluation (similarity evaluator not available)")
        qualified_jobs: list[dict] = []
    else:
        qualified_jobs = evaluate_jobs_task(raw_jobs)

    log.info(f"Qualified jobs (score ≥ {settings.fit_score_threshold}): {len(qualified_jobs)}")

    # ── 6. Build tailored resumes ────────────────────────────────────────────
    if qualified_jobs:
        qualified_with_resumes = build_resumes_task(qualified_jobs)
    else:
        qualified_with_resumes = []

    # ── 7. Telegram notifications ────────────────────────────────────────────
    notifications_sent = 0
    if qualified_with_resumes and not skip_notify:
        if flags.get("telegram_ok"):
            notifications_sent = notify_task(qualified_with_resumes)
        else:
            log.warning("Skipping notifications (Telegram not configured)")
            for job in qualified_with_resumes:
                log.info(
                    f"[DRY RUN] Would notify: {job.get('title')} @ {job.get('domain')} "
                    f"| score={job.get('fit_score')} | emails={job.get('emails', [])[:2]}"
                )

    # ── 8. Daily digest ──────────────────────────────────────────────────────
    if flags.get("telegram_ok") and not skip_notify:
        digest_task()

    # ── 9. Summary ───────────────────────────────────────────────────────────
    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    stats   = get_pipeline_stats()

    summary = {
        "status":               "ok",
        "run_duration_seconds": round(elapsed, 1),
        "companies_crawled":    len(companies),
        "raw_jobs_found":       len(raw_jobs),
        "jobs_qualified":       len(qualified_jobs),
        "resumes_built":        len(qualified_with_resumes),
        "notifications_sent":   notifications_sent,
        "pipeline_totals":      stats,
    }

    log.info(f"{'='*60}")
    log.info(f"Flow complete in {elapsed:.1f}s")
    log.info(f"  Companies crawled:   {summary['companies_crawled']}")
    log.info(f"  Raw jobs found:      {summary['raw_jobs_found']}")
    log.info(f"  Jobs qualified:      {summary['jobs_qualified']}")
    log.info(f"  Resumes built:       {summary['resumes_built']}")
    log.info(f"  Notifications sent:  {summary['notifications_sent']}")
    log.info(f"  DB totals:           {stats}")
    log.info(f"{'='*60}")

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Deployment helper (optional — Prefect scheduling)
# ─────────────────────────────────────────────────────────────────────────────

def create_deployment():
    """
    Register a daily 08:00 AM deployment with the local Prefect server.

    Run once:
        python main.py --deploy

    Then start the Prefect worker:
        prefect worker start --pool "default-agent-pool"
    """
    from prefect.deployments import Deployment
    from prefect.server.schemas.schedules import CronSchedule

    deployment = Deployment.build_from_flow(
        flow=job_hunter_flow,
        name="daily-job-hunt",
        schedule=CronSchedule(cron="0 8 * * *", timezone="Asia/Yerevan"),
        parameters={
            "batch_size": settings.batch_size,
            "load_csv":   False,
        },
        tags=["job-hunter", "daily"],
    )
    deployment_id = deployment.apply()
    print(f"Deployment created: {deployment_id}")
    print("Start worker: prefect worker start --pool default-agent-pool")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Job Hunter Pipeline — run or deploy the Prefect flow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                        # Run one batch now
  python main.py --load-csv             # Seed DB from CSV first, then run
  python main.py --load-csv --csv data/company_domains.csv
  python main.py --batch-size 25        # Smaller batch (useful for testing)
  python main.py --dry-run              # Run without Telegram notifications
  python main.py --deploy               # Register daily cron deployment
  prefect server start                  # Open the Prefect UI at :4200
        """,
    )
    parser.add_argument("--load-csv",   action="store_true", help="Seed DB from CSV before running")
    parser.add_argument("--csv",        type=str,            help="Path to domains CSV file")
    parser.add_argument("--batch-size", type=int,            help="Domains to process this run")
    parser.add_argument("--dry-run",    action="store_true", help="Skip Telegram notifications")
    parser.add_argument("--deploy",     action="store_true", help="Register Prefect deployment")
    parser.add_argument("--stats",      action="store_true", help="Print DB stats and exit")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # ── Stats only ───────────────────────────────────────────────────────────
    if args.stats:
        init_db()
        stats = get_pipeline_stats()
        print("\n📊  Pipeline Stats")
        print(f"  Companies total:   {stats['total_companies']}")
        print(f"  Companies scraped: {stats['scraped']}")
        print(f"  Jobs found:        {stats['total_jobs']}")
        print(f"  High-score jobs:   {stats['high_score_jobs']} (≥{settings.fit_score_threshold})")
        print(f"  Applied:           {stats['applied']}")
        print(f"  Pending review:    {stats['pending_review']}")
        sys.exit(0)

    # ── Deploy ───────────────────────────────────────────────────────────────
    if args.deploy:
        create_deployment()
        sys.exit(0)

    # ── Run the flow ─────────────────────────────────────────────────────────
    result = job_hunter_flow(
        batch_size=args.batch_size,
        load_csv=args.load_csv,
        csv_path=args.csv,
        skip_notify=args.dry_run,
    )

    print("\n✅  Flow complete:")
    for k, v in result.items():
        if k != "pipeline_totals":
            print(f"  {k:<30} {v}")