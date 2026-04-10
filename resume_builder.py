"""
resume_builder.py
=================
NODE 4 of the Job Hunter pipeline.

Round 3 changes:
  - extract_jd_keywords(): pulls the top 3–5 keywords from a job description
    using a simple TF-IDF-like frequency approach (no LLM call needed).
  - render_resume_html() now injects those keywords into:
      • The summary paragraph (appended naturally as a skill-mention sentence)
      • A "Highlighted Skills" line visible to ATS parsers, using the exact
        keyword casing from the job description.
  - build_resume() now accepts a `job_description` parameter and runs keyword
    extraction automatically if tech_stack alone isn't enough.

Responsibilities:
  1. Load master_resume.json  (your full resume data)
  2. Score + select the best-matching bullet points for a given tech stack
  3. Inject top job-description keywords into summary + skills section
  4. Render Jinja2 HTML template with selected content
  5. Convert HTML → ATS-safe PDF via WeasyPrint

The bullet-selection engine uses a scoring algorithm — not random, not first-N:
  • Exact keyword match in bullet tags  → +3 pts per match
  • Partial / case-insensitive match    → +1 pt per match
  • Recency bonus (first job listed)    → +2 pts
  • Always includes top MAX_BULLETS highest-scoring bullets

ATS safety notes:
  - Single-column HTML layout (no tables, no CSS floats)
  - No headers/footers that might confuse parsers
  - Standard section names: Summary, Experience, Projects, Skills, Education
  - WeasyPrint produces clean, text-extractable PDFs
"""

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from slugify import slugify
from weasyprint import HTML as WeasyHTML
from weasyprint import CSS

from config import settings

logger = logging.getLogger("resume_builder")

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

RESUME_JSON_PATH  = settings.base_dir / "data" / "master_resume.json"
TEMPLATES_DIR     = settings.templates_dir
RESUMES_OUTPUT    = settings.resumes_dir

MAX_BULLETS       = 5    # bullets shown per job in the tailored resume
MAX_PROJECTS      = 3    # projects shown


# ─────────────────────────────────────────────────────────────────────────────
# Master resume loader
# ─────────────────────────────────────────────────────────────────────────────

def load_master_resume(path: Path = RESUME_JSON_PATH) -> dict:
    """Load and return master_resume.json as a dict."""
    if not path.exists():
        raise FileNotFoundError(
            f"master_resume.json not found at {path}.\n"
            "Copy data/master_resume.json and fill in your real details."
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    logger.debug(f"Loaded master resume from {path}")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# FIX: Keyword extraction from job description
# ─────────────────────────────────────────────────────────────────────────────

# Common English stop words to filter out
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "need",
    "that", "this", "these", "those", "it", "its", "we", "our", "you",
    "your", "they", "their", "us", "not", "no", "nor", "so", "yet",
    "both", "either", "neither", "each", "every", "all", "any", "both",
    "more", "most", "other", "than", "such", "too", "very", "just",
    "also", "well", "experience", "work", "working", "role", "team",
    "ability", "strong", "good", "great", "excellent", "must", "prefer",
    "preferred", "required", "requirements", "responsibilities", "skills",
    "knowledge", "understanding", "including", "etc", "e.g", "i.e",
    "like", "plus", "bonus", "nice", "would", "apply", "join", "help",
    "using", "use", "used", "build", "building", "built", "develop",
    "developing", "developed", "create", "creating", "created", "new",
    "high", "large", "scale", "scalable", "fast", "quickly", "highly",
    "level", "years", "year", "least", "least", "minimum", "up",
}

# Known tech keywords to always preserve (case-insensitive lookup, keep original case)
_TECH_PRIORITY = {
    "python", "fastapi", "flask", "django", "postgresql", "postgres", "mysql",
    "redis", "celery", "docker", "kubernetes", "k8s", "aws", "gcp", "azure",
    "terraform", "airflow", "spark", "kafka", "rabbitmq", "mongodb", "sqlite",
    "graphql", "grpc", "rest", "api", "apis", "asyncio", "async", "sqlalchemy",
    "pandas", "numpy", "sklearn", "scikit", "tensorflow", "pytorch", "llm",
    "openai", "langchain", "rag", "vector", "embedding", "ml", "ai",
    "machine learning", "deep learning", "data pipeline", "etl", "dbt",
    "git", "github", "gitlab", "ci", "cd", "github actions", "pytest",
    "microservices", "serverless", "lambda", "s3", "ec2", "rds", "nginx",
    "linux", "bash", "shell", "typescript", "javascript", "node", "react",
    "pydantic", "sqlmodel", "alembic", "prefect", "dagster", "dask",
}


def extract_jd_keywords(jd_text: str, tech_stack: list[str], top_n: int = 5) -> list[str]:
    """
    Extract the top `top_n` unique, meaningful keywords from a job description
    that should appear in the tailored resume.

    Strategy (no LLM required):
      1. Start with the tech_stack already identified by the evaluator LLM.
      2. Scan the JD for additional tech terms not in tech_stack.
      3. Use word-frequency counting on non-stop-words to find repeated terms.
      4. De-duplicate and return the most prominent N keywords.

    Args:
        jd_text:    Raw job description text.
        tech_stack: Keywords already extracted by the evaluator (list of strings).
        top_n:      Maximum number of keywords to return.

    Returns:
        List of keyword strings (original casing from JD when possible).
    """
    if not jd_text:
        return tech_stack[:top_n]

    # Start with evaluator-provided stack (already the best source)
    seen_lower: set[str] = set()
    result: list[str] = []

    for kw in tech_stack:
        kw_clean = kw.strip()
        if kw_clean and kw_clean.lower() not in seen_lower:
            result.append(kw_clean)
            seen_lower.add(kw_clean.lower())

    if len(result) >= top_n:
        return result[:top_n]

    # Tokenise JD — keep multi-word tech terms together
    text_lower = jd_text.lower()

    # Check for known multi-word tech terms first
    for tech in _TECH_PRIORITY:
        if tech in text_lower and tech not in seen_lower:
            # Find original casing in JD
            match = re.search(re.escape(tech), jd_text, re.IGNORECASE)
            original = match.group(0) if match else tech.title()
            result.append(original)
            seen_lower.add(tech)
            if len(result) >= top_n:
                return result[:top_n]

    # Fall back to high-frequency single words
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9+#\-.]{2,}\b", jd_text)
    freq  = Counter(
        w.lower() for w in words
        if w.lower() not in _STOP_WORDS and len(w) > 2
    )

    for word, count in freq.most_common(20):
        if count >= 2 and word not in seen_lower:
            # Preserve original casing (prefer capitalised if seen that way)
            match = re.search(r"\b" + re.escape(word) + r"\b", jd_text, re.IGNORECASE)
            original = match.group(0) if match else word
            result.append(original)
            seen_lower.add(word)
            if len(result) >= top_n:
                break

    logger.debug(f"extract_jd_keywords: {result[:top_n]}")
    return result[:top_n]


def _inject_keywords_into_summary(summary: str, keywords: list[str]) -> str:
    """
    Append a natural-sounding keyword sentence to the summary paragraph.
    Only adds keywords that aren't already present.

    Example output:
      "...passionate about building scalable systems.
       Particularly experienced with FastAPI, PostgreSQL, and Redis."
    """
    if not keywords:
        return summary

    summary_lower = summary.lower()
    missing = [kw for kw in keywords if kw.lower() not in summary_lower]

    if not missing:
        return summary

    if len(missing) == 1:
        kw_str = missing[0]
    elif len(missing) == 2:
        kw_str = f"{missing[0]} and {missing[1]}"
    else:
        kw_str = ", ".join(missing[:-1]) + f", and {missing[-1]}"

    suffix = f" Particularly experienced with {kw_str}."
    # Don't double-punctuate
    trimmed = summary.rstrip()
    if trimmed.endswith("."):
        return trimmed + suffix
    return trimmed + "." + suffix


# ─────────────────────────────────────────────────────────────────────────────
# Bullet scoring engine
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_keyword(kw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", kw.lower())


def score_bullet(bullet: dict, target_stack: list[str]) -> int:
    if not target_stack:
        return 1

    score = 0
    bullet_tags  = [_normalise_keyword(t) for t in bullet.get("tags", [])]
    bullet_text  = _normalise_keyword(bullet.get("text", ""))
    norm_targets = [_normalise_keyword(kw) for kw in target_stack]

    for norm_kw in norm_targets:
        if not norm_kw:
            continue
        if norm_kw in bullet_tags:
            score += 3
            continue
        if any(norm_kw in tag or tag in norm_kw for tag in bullet_tags):
            score += 1
        if norm_kw in bullet_text:
            score += 1

    return score


def select_bullets(
    job_bullets: list[dict],
    target_stack: list[str],
    max_bullets: int = MAX_BULLETS,
) -> list[str]:
    if not job_bullets:
        return []

    scored = [
        (score_bullet(b, target_stack), b)
        for b in job_bullets
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    take = max(min(3, len(scored)), min(max_bullets, len(scored)))
    selected = [b["text"] for _, b in scored[:take]]

    logger.debug(
        f"Bullet selection: {len(job_bullets)} candidates → {len(selected)} selected. "
        f"Top score: {scored[0][0] if scored else 0}"
    )
    return selected


def select_projects(
    projects: list[dict],
    target_stack: list[str],
    max_projects: int = MAX_PROJECTS,
) -> list[dict]:
    if not projects:
        return []

    def project_score(proj: dict) -> int:
        score = 0
        proj_tags    = [_normalise_keyword(t) for t in proj.get("tags", [])]
        norm_targets = [_normalise_keyword(kw) for kw in target_stack]
        for norm_kw in norm_targets:
            if norm_kw in proj_tags:
                score += 3
            elif any(norm_kw in t or t in norm_kw for t in proj_tags):
                score += 1
        return score

    scored = sorted(projects, key=project_score, reverse=True)
    return [
        {
            "name":        p["name"],
            "period":      p.get("period", ""),
            "tech":        p.get("tech", ""),
            "description": p.get("description", ""),
            "bullets":     p.get("bullets", []),
        }
        for p in scored[:max_projects]
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Jinja2 renderer
# ─────────────────────────────────────────────────────────────────────────────

def _build_jinja_env(templates_dir: Path = TEMPLATES_DIR) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_resume_html(
    master: dict,
    target_stack: list[str],
    company_name: str = "",
    job_title: str = "",
    jd_keywords: list[str] | None = None,
) -> str:
    """
    Assemble the template context and render resume.html.j2 → HTML string.

    Args:
        master:       Raw data from master_resume.json
        target_stack: Keywords from LLM evaluator (used for bullet scoring)
        company_name: Injected into header for personalisation
        job_title:    Injected into summary for personalisation
        jd_keywords:  Top 3-5 keywords extracted from JD — injected into summary
                      and highlighted skills for ATS matching. If None, derived
                      from target_stack automatically.
    """
    # Resolve keywords
    keywords = jd_keywords if jd_keywords is not None else target_stack[:5]

    # ── Personal block ───────────────────────────────────────────────────────
    personal = dict(master["personal"])
    if job_title:
        personal["title"] = job_title

    # FIX: inject keywords into summary
    original_summary = personal.get("summary", "")
    personal["summary"] = _inject_keywords_into_summary(original_summary, keywords)

    # FIX: add a highlighted_keywords field for the template to display
    # (hidden ATS-friendly line OR visible "Key Skills for this role" block)
    personal["highlighted_keywords"] = keywords

    # ── Experience: score + select bullets per job ────────────────────────────
    experience_rendered = []
    for job in master.get("experience", []):
        selected_bullets = select_bullets(
            job_bullets=job.get("bullets", []),
            target_stack=target_stack,
            max_bullets=master.get("meta", {}).get("max_bullets_per_job", MAX_BULLETS),
        )
        experience_rendered.append({
            "company":  job["company"],
            "title":    job["title"],
            "period":   job["period"],
            "location": job.get("location", ""),
            "bullets":  selected_bullets,
        })

    # ── Projects: score + select ─────────────────────────────────────────────
    projects_rendered = select_projects(
        projects=master.get("projects", []),
        target_stack=target_stack,
        max_projects=master.get("meta", {}).get("max_projects_shown", MAX_PROJECTS),
    )

    # ── Skills: full list + highlighted keywords merged in ───────────────────
    skills = dict(master.get("skills", {}))

    # Merge jd_keywords into the "languages_frameworks" skill list so ATS
    # sees the exact terms from the job posting in the skills section
    if keywords:
        existing_skills_lower = set()
        for v in skills.values():
            if isinstance(v, list):
                existing_skills_lower.update(s.lower() for s in v)
            elif isinstance(v, str):
                existing_skills_lower.update(s.strip().lower() for s in v.split(","))

        new_kws = [kw for kw in keywords if kw.lower() not in existing_skills_lower]
        if new_kws:
            # Append to the first list-type skill group found
            for key in ("languages_frameworks", "tools", "skills", "technologies"):
                if key in skills and isinstance(skills[key], list):
                    skills[key] = new_kws + skills[key]  # prepend = higher ATS weight
                    logger.debug(f"Injected {new_kws} into skills[{key!r}]")
                    break

    # ── Education ─────────────────────────────────────────────────────────────
    education = master.get("education", [])

    # ── Spoken languages ──────────────────────────────────────────────────────
    spoken_languages = master.get("spoken_languages", [])

    # ── Render ────────────────────────────────────────────────────────────────
    env      = _build_jinja_env()
    template = env.get_template("resume.html.j2")

    html = template.render(
        personal=personal,
        experience=experience_rendered,
        projects=projects_rendered,
        skills=skills,
        education=education,
        spoken_languages=spoken_languages,
        company_name=company_name,
        job_title=job_title,
        target_stack=target_stack,
        jd_keywords=keywords,      # available in template if needed
    )
    return html


# ─────────────────────────────────────────────────────────────────────────────
# PDF exporter
# ─────────────────────────────────────────────────────────────────────────────

_PAGE_CSS = CSS(string="""
    @page {
        size: A4;
        margin: 15mm 18mm 15mm 18mm;
    }
    body {
        padding: 0 !important;
    }
""")


def html_to_pdf(html: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        WeasyHTML(string=html).write_pdf(
            target=str(output_path),
            stylesheets=[_PAGE_CSS],
        )
        size_kb = output_path.stat().st_size // 1024
        logger.info(f"PDF written: {output_path.name} ({size_kb} KB)")
        return output_path

    except Exception as exc:
        logger.error(f"WeasyPrint failed: {exc}")
        raise RuntimeError(f"PDF generation failed: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Main public API — called from the Prefect flow
# ─────────────────────────────────────────────────────────────────────────────

def build_resume(
    company_name: str,
    job_title: str,
    tech_stack: list[str],
    output_dir: Path = RESUMES_OUTPUT,
    master_path: Path = RESUME_JSON_PATH,
    job_description: str = "",   # FIX: pass raw JD for keyword extraction
) -> Path:
    """
    Full pipeline: JSON → keyword extraction → bullet selection → HTML → PDF.

    Args:
        company_name:    Used in filename + template context
        job_title:       Used in filename + tweaks the title line
        tech_stack:      From LLM evaluator — drives bullet scoring
        output_dir:      Where to save the PDF
        master_path:     Path to master_resume.json
        job_description: Raw JD text — used to extract top 3-5 ATS keywords
                         that get injected into the resume summary & skills.
                         Falls back to tech_stack if empty.

    Returns:
        Path to the generated PDF.
    """
    logger.info(
        f"Building resume | company={company_name!r} | "
        f"role={job_title!r} | stack={tech_stack}"
    )

    # FIX: extract top keywords from the actual JD text
    jd_keywords = extract_jd_keywords(
        jd_text=job_description,
        tech_stack=tech_stack,
        top_n=5,
    )
    logger.info(f"JD keywords injected into resume: {jd_keywords}")

    # 1. Load master data
    master = load_master_resume(master_path)

    # 2. Render HTML (with keyword injection)
    html = render_resume_html(
        master=master,
        target_stack=tech_stack,
        company_name=company_name,
        job_title=job_title,
        jd_keywords=jd_keywords,
    )

    # 3. Build output filename
    safe_company = slugify(company_name or "Company", max_length=30)
    safe_role    = slugify(job_title    or "Role",    max_length=30)
    filename     = f"Resume_{safe_company}_{safe_role}.pdf"
    output_path  = output_dir / filename

    # 4. Export PDF
    return html_to_pdf(html, output_path)


# ─────────────────────────────────────────────────────────────────────────────
# CLI / smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("RESUME BUILDER — Smoke Tests")
    print("=" * 60)

    # ── Test 1: keyword extraction ─────────────────────────────────────────
    print("\n[1] JD keyword extraction")

    mock_jd = """
    Senior Python Backend Engineer – Remote
    We need a Python developer experienced with FastAPI and PostgreSQL.
    You will build data pipelines using Airflow and work with Redis for caching.
    Docker and Kubernetes experience preferred. REST API design is essential.
    Strong Python skills required. Experience with async/await patterns.
    """

    kws = extract_jd_keywords(mock_jd, ["Python", "FastAPI"], top_n=5)
    print(f"  ✅  Keywords: {kws}")
    assert "Python" in kws or "FastAPI" in kws, "Evaluator keywords must appear"
    assert len(kws) >= 3, f"Expected ≥3 keywords, got {len(kws)}"

    # ── Test 2: summary injection ──────────────────────────────────────────
    print("\n[2] Summary keyword injection")
    summary = "Experienced backend engineer passionate about building scalable systems."
    enriched = _inject_keywords_into_summary(summary, ["FastAPI", "PostgreSQL", "Redis"])
    print(f"  Original:  {summary}")
    print(f"  Enriched:  {enriched}")
    assert "FastAPI" in enriched
    assert "PostgreSQL" in enriched
    print("  ✅  Keywords injected correctly")

    # No-op test (keywords already in summary)
    summary2 = "I work with FastAPI and PostgreSQL daily."
    enriched2 = _inject_keywords_into_summary(summary2, ["FastAPI", "PostgreSQL"])
    assert enriched2 == summary2, "Should not duplicate existing keywords"
    print("  ✅  Existing keywords not duplicated")

    # ── Test 3: bullet scoring ─────────────────────────────────────────────
    print("\n[3] Bullet scoring engine")

    bullets = [
        {"text": "Built REST APIs with FastAPI",     "tags": ["Python", "FastAPI", "REST API"]},
        {"text": "Built ML pipeline with FAISS",     "tags": ["ML", "FAISS", "Python", "AI"]},
        {"text": "Led marketing campaigns",           "tags": ["marketing", "branding"]},
        {"text": "Wrote Celery automation scripts",   "tags": ["automation", "Celery", "Python"]},
    ]
    stack = ["Python", "FastAPI", "automation"]

    scored = [(score_bullet(b, stack), b["text"]) for b in bullets]
    scored.sort(reverse=True)
    for sc, txt in scored:
        print(f"  score={sc:2d}  {txt[:60]}")

    selected = select_bullets(bullets, stack, max_bullets=3)
    assert "Led marketing campaigns" not in selected
    print(f"  ✅  Selected bullets: {[s[:40] for s in selected]}")

    # ── Test 4: full build with JD keywords ────────────────────────────────
    print("\n[4] Full resume build with JD keyword injection")
    master_data = load_master_resume()

    test_output_dir = settings.base_dir / "output" / "resumes"
    pdf_path = build_resume(
        company_name="Acme Corp",
        job_title="Backend Engineer",
        tech_stack=["Python", "FastAPI", "PostgreSQL"],
        job_description=mock_jd,
        output_dir=test_output_dir,
    )
    assert pdf_path.exists()
    size_kb = pdf_path.stat().st_size // 1024
    assert size_kb > 5
    print(f"  ✅  PDF created: {pdf_path.name} ({size_kb} KB)")

    print("\n" + "=" * 60)
    print(f"All tests passed ✅  — PDFs in: {test_output_dir}")
    print("=" * 60)

    if "--open" in sys.argv:
        import subprocess
        subprocess.run(["xdg-open", str(pdf_path)])