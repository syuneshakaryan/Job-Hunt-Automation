"""
crawler.py
==========
NODE 1 + NODE 2 of the Job Hunter pipeline.

  NODE 1 — Domain Crawler
    • Loads company homepage
    • Extracts emails (DOM text + mailto: hrefs)
    • Finds careers/jobs page link
    • Navigates to careers page

  NODE 2 — Job Extractor
    • Scans careers page for target-role links
    • Navigates to each job posting
    • Returns raw job text + metadata

All I/O is async. One browser instance is shared across the whole batch.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

from config import settings

logger = logging.getLogger("crawler")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Link text / href patterns that suggest a careers page
CAREERS_LINK_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bcareers?\b",
        r"\bjobs?\b",
        r"\bjoin\s+us\b",
        r"\bhiring\b",
        r"\bopenings?\b",
        r"\bwork\s+with\s+us\b",
        r"\bvacancies\b",
        r"\bpositions?\b",
        r"\bteam\b",           # "Join Our Team"
    ]
]

# href substring patterns (catches /careers, /jobs, /work-with-us, etc.)
CAREERS_HREF_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"/careers?",
        r"/jobs?",
        r"/join",
        r"/openings?",
        r"/vacancies",
        r"/work",
        r"/hiring",
        r"greenhouse\.io",
        r"lever\.co",
        r"ashby\.hq\.com",
        r"bamboohr\.com",
        r"workable\.com",
        r"smartrecruiters\.com",
    ]
]

# Keywords that flag a job link as a target role
TARGET_ROLE_KEYWORDS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bpython\b",
        r"\bbackend\b",
        r"\bback[\s\-]?end\b",
        r"\bsoftware\s+engineer\b",
        r"\bsoftware\s+developer\b",
        r"\bfull[\s\-]?stack\b",
        r"\bdata[\s\-]engineer\b",
        r"\bdata[\s\-]pipeline\b",
        r"\bml\s+engineer\b",
        r"\bautomation\s+engineer\b",
        r"\bdevops\b",
        r"\bplatform\s+engineer\b",
        r"\bapi\s+developer\b",
        r"\bfastapi\b",
        r"\bdjango\b",
        r"\bflask\b",
        r"\brest\s+api\b",
    ]
]

# Regex to find email addresses anywhere in DOM text or hrefs
EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Emails we always want to capture (high-value prefixes)
HIGH_VALUE_EMAIL_PREFIXES = {
    "hr", "hiring", "careers", "jobs", "recruit", "recruiter",
    "talent", "people", "team", "apply", "founder", "ceo",
    "cto", "engineering", "tech", "hello", "info", "contact",
}

# Domains to reject from email extraction (false positives)
JUNK_EMAIL_DOMAINS = {
    "example.com", "sentry.io", "w3.org", "schema.org",
    "cloudflare.com", "amazonaws.com", "googletagmanager.com",
}


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class JobListing:
    title:       str
    url:         str
    description: str          # raw text of job posting page
    source_page: str          # careers page URL this was found on


@dataclass
class CrawlResult:
    domain:      str
    company_url: str  = ""          # final URL after redirects
    career_url:  str  = ""
    emails:      list[str] = field(default_factory=list)
    jobs:        list[JobListing] = field(default_factory=list)
    status:      str  = "ok"  # ok | no_careers | no_jobs | failed
    error:       str  = ""
    html_hash:   str  = ""    # md5 of homepage HTML (change detection)


# ─────────────────────────────────────────────────────────────────────────────
# Email helpers
# ─────────────────────────────────────────────────────────────────────────────

def extract_emails_from_text(text: str) -> list[str]:
    """
    Find all email addresses in a string.
    Filters out junk domains and deduplicates.
    Sorts high-value prefixes first.
    """
    raw = set(EMAIL_REGEX.findall(text))
    clean: list[str] = []
    for email in raw:
        domain = email.split("@")[1].lower()
        if domain in JUNK_EMAIL_DOMAINS:
            continue
        if any(c in email for c in ["png", ".jpg", ".js", ".css"]):
            continue  # image/asset false positives
        clean.append(email.lower())

    # sort: high-value prefixes first
    def sort_key(e: str) -> int:
        prefix = e.split("@")[0].lower()
        return 0 if any(p in prefix for p in HIGH_VALUE_EMAIL_PREFIXES) else 1

    return sorted(clean, key=sort_key)


# ─────────────────────────────────────────────────────────────────────────────
# Link helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_careers_link(href: str, text: str) -> bool:
    """Return True if an anchor looks like a careers / jobs page link."""
    for pat in CAREERS_HREF_PATTERNS:
        if pat.search(href):
            return True
    for pat in CAREERS_LINK_PATTERNS:
        if pat.search(text):
            return True
    return False


def _is_target_job_link(href: str, text: str) -> bool:
    """Return True if an anchor looks like a Python/Backend job posting."""
    combined = f"{text} {href}"
    return any(pat.search(combined) for pat in TARGET_ROLE_KEYWORDS)


def _normalise_url(base: str, href: str) -> str:
    """Convert relative hrefs to absolute URLs."""
    if href.startswith("http"):
        return href
    return urljoin(base, href)


def _same_origin(url_a: str, url_b: str) -> bool:
    """True if both URLs share the same netloc."""
    return urlparse(url_a).netloc == urlparse(url_b).netloc


# ─────────────────────────────────────────────────────────────────────────────
# Page-level scraping helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _safe_goto(page: Page, url: str, timeout: int = None) -> bool:
    """
    Navigate to a URL, returning True on success.
    Swallows TimeoutError and common network errors.
    """
    t = timeout or settings.page_timeout_ms
    try:
        resp = await page.goto(url, timeout=t, wait_until="domcontentloaded")
        return resp is not None and resp.ok
    except PWTimeout:
        logger.warning(f"Timeout loading: {url}")
        return False
    except Exception as exc:
        logger.warning(f"Navigation error [{url}]: {exc}")
        return False


async def _page_text(page: Page) -> str:
    """Return all visible text from the page."""
    try:
        return await page.evaluate("() => document.body.innerText")
    except Exception:
        return ""


async def _page_html(page: Page) -> str:
    try:
        return await page.content()
    except Exception:
        return ""


async def _extract_links(page: Page, base_url: str) -> list[dict]:
    """
    Return list of {href, text} dicts for every <a> on the page.
    hrefs are normalised to absolute URLs.
    """
    try:
        raw = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href,
                text: (a.innerText || a.textContent || '').trim().slice(0, 200)
            }))
        """)
        return [
            {"href": _normalise_url(base_url, r["href"]), "text": r["text"]}
            for r in raw
            if r["href"] and not r["href"].startswith("javascript")
        ]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# NODE 1 — Homepage crawler
# ─────────────────────────────────────────────────────────────────────────────

async def crawl_homepage(
    page: Page,
    domain: str,
) -> tuple[str, list[str], list[str], str]:
    """
    Visit domain homepage. Returns:
      (final_url, emails, careers_candidate_urls, html_hash)
    """
    # Try https first, fall back to http
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        ok = await _safe_goto(page, url)
        if ok:
            break
    else:
        return ("", [], [], "")

    final_url = page.url
    html      = await _page_html(page)
    text      = await _page_text(page)
    html_hash = hashlib.md5(html.encode()).hexdigest()

    # ── Email extraction ──────────────────────────────────────────────────────
    # 1. From all visible text
    emails = extract_emails_from_text(text)
    # 2. From mailto: hrefs
    mailto_links = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href^="mailto:"]'))
                   .map(a => a.href.replace('mailto:', '').split('?')[0])
    """)
    emails = list(dict.fromkeys(emails + [e.lower() for e in mailto_links]))  # dedup, preserve order
    emails = [e for e in emails if "@" in e]

    # ── Careers link discovery ────────────────────────────────────────────────
    links = await _extract_links(page, final_url)
    careers_urls: list[str] = []
    seen: set[str] = set()

    for link in links:
        href, text_lnk = link["href"], link["text"]
        if not href or href in seen:
            continue
        if _is_careers_link(href, text_lnk):
            seen.add(href)
            careers_urls.append(href)

    # Deduplicate and keep same-origin links first
    careers_urls = sorted(
        set(careers_urls),
        key=lambda u: (0 if _same_origin(final_url, u) else 1, u)
    )

    logger.info(
        f"[{domain}] homepage scraped | "
        f"emails={len(emails)} | careers_candidates={len(careers_urls)}"
    )
    return final_url, emails, careers_urls, html_hash


# ─────────────────────────────────────────────────────────────────────────────
# Title + content helpers  (must be defined before scrape_careers_page)
# ─────────────────────────────────────────────────────────────────────────────

def _clean_title(raw: str) -> str:
    """Clean up a job title string."""
    title = re.sub(r"\s+", " ", raw).strip()
    for noise in ["Apply now", "Learn more", "View job", "Open role"]:
        title = title.replace(noise, "").strip()
    return title[:120] or "Software Engineer"


def _looks_like_service_page(title: str, description: str) -> bool:
    """
    Cheap pre-LLM heuristic to reject service/agency pages that sneak through
    keyword matching (e.g. 'Backend Development' service pages at outsourcing firms).
    Returns True if the content looks like a service offering, not a real job posting.
    """
    combined = (title + " " + description[:800]).lower()

    service_signals = [
        r"\bwe offer\b", r"\bour services?\b", r"\bhire our\b",
        r"\bhire us\b",  r"\bout[- ]?source\b", r"\bdedicated team\b",
        r"\bstaff augmentation\b", r"\bour (backend|python|devops) (team|developers? provide)\b",
        r"\bensuring robust server systems\b",
        r"\bsecurity integration into devops\b",
        r"\bautomating delivery and infrastructure\b",
        r"\bcustom software development\b",
        r"\bend[- ]to[- ]end (development|solutions)\b",
    ]
    job_signals = [
        r"\bwe (are|'re) (looking|hiring|seeking)\b",
        r"\bjoin (our|the) team\b",
        r"\byou will\b", r"\byou'll\b",
        r"\bresponsibilit", r"\brequirements?\b",
        r"\bapply (now|today|here)\b",
        r"\bwhat you('ll| will)\b",
        r"\bwho you are\b",
        r"\babout (the |this )?role\b",
        r"\bcompetitive (salary|compensation|pay)\b",
    ]

    service_hits = sum(1 for p in service_signals if re.search(p, combined))
    job_hits     = sum(1 for p in job_signals     if re.search(p, combined))
    return service_hits >= 2 and job_hits == 0


# ─────────────────────────────────────────────────────────────────────────────
# NODE 2 — Careers page scraper
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_careers_page(
    page: Page,
    careers_url: str,
    company_base_url: str,
    max_jobs: int = 10,
) -> tuple[str, list[JobListing]]:
    """
    Visit a careers page. Find job links matching target keywords.
    Navigate to each, extract title + raw description.

    Returns:
      (confirmed_careers_url, list[JobListing])
    """
    ok = await _safe_goto(page, careers_url)
    if not ok:
        return careers_url, []

    # Some ATS pages need a moment to render JS
    await asyncio.sleep(1.2)

    links = await _extract_links(page, page.url)
    confirmed_url = page.url

    # Score + collect matching job links
    job_links: list[dict] = []
    seen_hrefs: set[str] = set()

    for link in links:
        href = link["href"]
        text = link["text"]
        if not href or href in seen_hrefs:
            continue
        if _is_target_job_link(href, text):
            seen_hrefs.add(href)
            job_links.append(link)

    logger.info(
        f"[careers] {careers_url} → {len(job_links)} target job links found"
    )

    # ── Navigate to each job posting ─────────────────────────────────────────
    jobs: list[JobListing] = []

    for link in job_links[:max_jobs]:
        job_url = link["href"]
        title   = _clean_title(link["text"])

        ok = await _safe_goto(page, job_url)
        if not ok:
            continue

        await asyncio.sleep(0.8)

        # Try to get a better title from the page itself
        page_title = await _extract_page_title(page)
        if page_title and len(page_title) > len(title):
            title = page_title

        description = await _extract_job_description(page)

        if not description.strip():
            logger.debug(f"Empty description, skipping: {job_url}")
            continue

        # Quick heuristic: reject obvious service/agency pages before hitting LLM
        if _looks_like_service_page(title, description):
            logger.debug(f"Service page heuristic fired, skipping: {title!r}")
            continue

        jobs.append(JobListing(
            title=title,
            url=job_url,
            description=description,
            source_page=confirmed_url,
        ))
        logger.info(f"  ✓ Job extracted: {title[:60]!r} | {len(description)} chars")

        await asyncio.sleep(settings.crawl_delay_seconds * 0.5)

    return confirmed_url, jobs


async def _extract_page_title(page: Page) -> str:
    """Try several selectors to get the job title from a posting page."""
    selectors = [
        "h1",
        "[class*='job-title']",
        "[class*='position-title']",
        "[class*='role-title']",
        "[data-testid*='title']",
        "title",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            text = await el.inner_text(timeout=2000)
            text = text.strip()
            if text and len(text) < 120:
                return text
        except Exception:
            continue
    return ""


async def _extract_job_description(page: Page) -> str:
    """
    Extract the body text of a job posting.
    Tries semantic containers first, falls back to full body text.
    Max 6000 chars to keep LLM context manageable.
    """
    selectors = [
        "main",
        "article",
        "[class*='job-description']",
        "[class*='job-detail']",
        "[class*='posting-content']",
        "[class*='description']",
        "[id*='job-description']",
        "[id*='description']",
        ".content",
        "#content",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            text = await el.inner_text(timeout=2000)
            text = text.strip()
            if len(text) > 200:  # meaningful content found
                return text[:6000]
        except Exception:
            continue

    # Final fallback: whole page body
    return (await _page_text(page))[:6000]


def _clean_title(raw: str) -> str:
    """Clean up a job title string."""
    title = re.sub(r"\s+", " ", raw).strip()
    # Remove common noise
    for noise in ["Apply now", "Learn more", "View job", "Open role"]:
        title = title.replace(noise, "").strip()
    return title[:120] or "Software Engineer"


def _looks_like_service_page(title: str, description: str) -> bool:
    """
    Cheap pre-LLM heuristic to reject service/agency pages that sneak through
    keyword matching (e.g. 'Backend Development' service pages at outsourcing firms).

    Returns True if the content looks like a service offering, not a job posting.
    """
    combined = (title + " " + description[:800]).lower()

    # Strong service-page signals
    service_signals = [
        r"\bwe offer\b", r"\bour services?\b", r"\bhire our\b",
        r"\bhire us\b",  r"\bout[- ]?source\b", r"\bdedicated team\b",
        r"\bstaff augmentation\b", r"\bour (backend|python|devops) (team|developers? provide)\b",
        r"\bensuring robust server systems\b",
        r"\bsecurity integration into devops\b",
        r"\bautomating delivery and infrastructure\b",
        r"\bcustom software development\b",
        r"\bend[- ]to[- ]end (development|solutions)\b",
    ]
    # Signals that it IS a real job posting
    job_signals = [
        r"\bwe (are|'re) (looking|hiring|seeking)\b",
        r"\bjoin (our|the) team\b",
        r"\byou will\b", r"\byou'll\b",
        r"\bresponsibilit", r"\brequirements?\b",
        r"\bapply (now|today|here)\b",
        r"\bwhat you('ll| will)\b",
        r"\bwho you are\b",
        r"\babout (the |this )?role\b",
        r"\bcompetitive (salary|compensation|pay)\b",
    ]

    service_hits = sum(1 for p in service_signals if re.search(p, combined))
    job_hits     = sum(1 for p in job_signals     if re.search(p, combined))

    # Reject if service signals dominate
    return service_hits >= 2 and job_hits == 0


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point — crawl a single domain end-to-end
# ─────────────────────────────────────────────────────────────────────────────

async def crawl_domain(
    domain: str,
    context: BrowserContext,
    max_jobs_per_domain: int = 8,
) -> CrawlResult:
    """
    Full crawl for a single domain.

    Steps:
      1. Load homepage → extract emails + find careers URL
      2. Load careers page → find matching job links
      3. Load each job page → extract description

    Uses a single tab (page) per domain, reused across steps.
    """
    result = CrawlResult(domain=domain)
    page = await context.new_page()

    try:
        # ── Step 1: Homepage ──────────────────────────────────────────────────
        final_url, emails, careers_urls, html_hash = await crawl_homepage(page, domain)

        if not final_url:
            result.status = "failed"
            result.error  = "Could not load homepage"
            return result

        result.company_url = final_url
        result.emails      = emails
        result.html_hash   = html_hash

        if not careers_urls:
            logger.info(f"[{domain}] No careers links found on homepage")
            result.status = "no_careers"
            return result

        # ── Step 2: Try careers URLs until one yields jobs ────────────────────
        for careers_url in careers_urls[:3]:  # try up to 3 candidates
            await asyncio.sleep(settings.crawl_delay_seconds)
            confirmed_url, jobs = await scrape_careers_page(
                page,
                careers_url,
                final_url,
                max_jobs=max_jobs_per_domain,
            )
            if jobs:
                result.career_url = confirmed_url
                result.jobs       = jobs
                result.status     = "ok"
                break
            elif not result.career_url:
                result.career_url = confirmed_url  # record even if empty

        if not result.jobs:
            logger.info(f"[{domain}] Careers page found but no matching job links")
            result.status = "no_jobs"

        return result

    except Exception as exc:
        logger.exception(f"[{domain}] Unexpected crawl error: {exc}")
        result.status = "failed"
        result.error  = str(exc)
        return result

    finally:
        await page.close()


# ─────────────────────────────────────────────────────────────────────────────
# Batch crawl — run N domains concurrently
# ─────────────────────────────────────────────────────────────────────────────

async def crawl_batch(
    domains: list[str],
    concurrency: int = 3,
    headless: bool = True,
) -> list[CrawlResult]:
    """
    Crawl a list of domains with bounded concurrency.

    concurrency=3 is conservative — enough for speed without
    triggering bot-detection on most sites.
    """
    results: list[CrawlResult] = []
    semaphore = asyncio.Semaphore(concurrency)

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )

        # Single context with realistic browser fingerprint
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            java_script_enabled=True,
        )

        # Intercept and block heavy assets to speed up crawling
        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3}",
            lambda route: route.abort(),
        )

        async def _bounded_crawl(domain: str) -> CrawlResult:
            async with semaphore:
                start = time.perf_counter()
                res = await crawl_domain(domain, context)
                elapsed = time.perf_counter() - start
                logger.info(
                    f"[{domain}] done in {elapsed:.1f}s | "
                    f"status={res.status} | "
                    f"emails={len(res.emails)} | "
                    f"jobs={len(res.jobs)}"
                )
                return res

        tasks = [_bounded_crawl(d) for d in domains]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        await context.close()
        await browser.close()

    return list(results)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point — quick test on a few domains
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    test_domains = sys.argv[1:] or ["fly.io", "render.com"]

    async def _run():
        print(f"\n🕷  Crawling {len(test_domains)} domain(s): {test_domains}\n")
        results = await crawl_batch(test_domains, concurrency=2)

        for res in results:
            print(f"\n{'='*60}")
            print(f"Domain  : {res.domain}")
            print(f"Status  : {res.status}")
            print(f"URL     : {res.company_url}")
            print(f"Careers : {res.career_url}")
            print(f"Emails  : {res.emails}")
            print(f"Jobs    : {len(res.jobs)}")
            for j in res.jobs:
                print(f"  • {j.title[:70]!r}  [{j.url[:80]}]")
                print(f"    desc preview: {j.description[:120].strip()!r}")

    asyncio.run(_run())