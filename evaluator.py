
"""
evaluator.py
============
NODE 3 of the Job Hunter pipeline.

Connects to the Groq API and uses the `instructor` library to extract
a strict Pydantic model from raw job description text.

Flow:
  raw JD text
      │
      ▼
  Groq (llama3-70b-8192)
      │
      ▼
  instructor (structured JSON output)
      │
      ▼
  JobEvaluation(fit_score=87, is_backend_role=True, ...)
      │
      ├── fit_score < 75  →  drop
      └── fit_score >= 75 →  pass to resume generator

Design notes:
  - instructor patches the Groq client for guaranteed Pydantic validation
    with automatic retry on bad JSON.
  - Two LLM calls per job:
      1. Fast "gate" check  (is this even a tech/backend role?)  ~0.3s
      2. Full evaluation    (score, stack, fit reasoning)        ~1-2s
  - Groq is free-tier with generous rate limits (~30 req/min on free).
    Add GROQ_API_KEY to your .env file.
    Get a key at: https://console.groq.com
"""
#V2
# import json
# import logging
# import time
# from enum import Enum
# from typing import Optional

# import instructor
# from groq import Groq
# from pydantic import BaseModel, Field, field_validator

# from config import settings

# logger = logging.getLogger("evaluator")


# # ─────────────────────────────────────────────────────────────────────────────
# # Pydantic models
# # ─────────────────────────────────────────────────────────────────────────────

# class SeniorityLevel(str, Enum):
#     INTERN     = "intern"
#     JUNIOR     = "junior"
#     MID        = "mid"
#     SENIOR     = "senior"
#     LEAD       = "lead"
#     UNKNOWN    = "unknown"


# class JobEvaluation(BaseModel):
#     """
#     Structured evaluation of a single job posting.
#     Returned by the LLM for every job that passes the gate check.
#     """

#     # ── Core decision fields ─────────────────────────────────────────────────
#     is_backend_role: bool = Field(
#         description=(
#             "True if the role primarily involves backend/server-side work: "
#             "APIs, data pipelines, automation, ML infrastructure, DevOps. "
#             "False for frontend-only, design, sales, or non-technical roles."
#         )
#     )

#     fit_score: int = Field(
#         ge=0, le=100,
#         description=(
#             "0–100 relevance score for a Python developer with 1.5 years experience "
#             "specialising in REST APIs, data pipelines, automation, and basic ML. "
#             "Score higher for: Python, FastAPI/Django/Flask, async, data engineering, "
#             "automation, LLM/AI tooling, startup culture, remote-friendly. "
#             "Score lower for: Java/C++/.NET only, 5+ years required, "
#             "pure frontend, enterprise legacy stack."
#         )
#     )

#     # ── Tech intelligence ────────────────────────────────────────────────────
#     core_tech_stack: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Explicit technologies, frameworks, and tools mentioned in the JD. "
#             "Examples: ['Python', 'FastAPI', 'PostgreSQL', 'Redis', 'Docker']. "
#             "Include only things actually stated — do not infer."
#         )
#     )

#     required_years_experience: Optional[int] = Field(
#         default=None,
#         description="Minimum years of experience explicitly required. None if not stated."
#     )

#     seniority_level: SeniorityLevel = Field(
#         default=SeniorityLevel.UNKNOWN,
#         description="Inferred seniority level of the role."
#     )

#     # ── Fit analysis ─────────────────────────────────────────────────────────
#     matching_strengths: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Specific reasons why this candidate IS a good fit. "
#             "E.g. ['Python required and candidate is strong in Python', "
#             "'startup culture matches candidate background']"
#         )
#     )

#     potential_gaps: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Honest gaps or risks. E.g. ['Role requires 3yr exp, candidate has 1.5', "
#             "'Kubernetes mentioned — candidate may need to upskill']"
#         )
#     )

#     rejection_reason: str = Field(
#         default="",
#         description=(
#             "If fit_score < 75, a one-sentence explanation of the main disqualifier. "
#             "Empty string if fit_score >= 75."
#         )
#     )

#     # ── Opportunity signals ──────────────────────────────────────────────────
#     is_remote_friendly: Optional[bool] = Field(
#         default=None,
#         description="True if remote/hybrid mentioned, False if on-site only, None if unclear."
#     )

#     has_equity: Optional[bool] = Field(
#         default=None,
#         description="True if equity/stock options mentioned."
#     )

#     # ── Validators ───────────────────────────────────────────────────────────
#     @field_validator("core_tech_stack", mode="before")
#     @classmethod
#     def normalise_stack(cls, v):
#         if isinstance(v, str):
#             try:
#                 v = json.loads(v)
#             except Exception:
#                 v = [x.strip() for x in v.split(",") if x.strip()]
#         return [str(item).strip() for item in (v or [])]

#     @field_validator("fit_score", mode="before")
#     @classmethod
#     def clamp_score(cls, v):
#         try:
#             return max(0, min(100, int(v)))
#         except (TypeError, ValueError):
#             return 0

#     @property
#     def should_apply(self) -> bool:
#         return self.fit_score >= settings.fit_score_threshold and self.is_backend_role

#     def summary(self) -> str:
#         """One-line human-readable summary for logging."""
#         stack = ", ".join(self.core_tech_stack[:5]) or "unknown stack"
#         return (
#             f"score={self.fit_score} | backend={self.is_backend_role} | "
#             f"seniority={self.seniority_level.value} | "
#             f"stack=[{stack}] | apply={self.should_apply}"
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Gate check model  (fast, cheap first-pass filter)
# # ─────────────────────────────────────────────────────────────────────────────

# class GateCheck(BaseModel):
#     """Quick first-pass: is this even a real job posting worth evaluating?"""
#     is_tech_job:           bool = Field(description="True if this is a software/data/engineering role.")
#     is_english:            bool = Field(description="True if the job description is in English.")
#     has_content:           bool = Field(description="True if the description has enough content to evaluate (>100 words).")
#     is_actual_job_posting: bool = Field(
#         description=(
#             "True ONLY if this is a real job opening for a specific hire. "
#             "False if it is a company service page, product description, agency offering, "
#             "capability page, or marketing content. "
#             "Look for: 'Apply', 'Requirements', 'Responsibilities', 'We are looking for', "
#             "'Join our team' — these signal real postings. "
#             "Service pages often say 'We offer', 'Our team provides', 'Hire us' — these are NOT jobs."
#         )
#     )

#     @property
#     def passes(self) -> bool:
#         return (
#             self.is_tech_job
#             and self.is_english
#             and self.has_content
#             and self.is_actual_job_posting
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Prompts
# # ─────────────────────────────────────────────────────────────────────────────

# GATE_SYSTEM = """You are a job classification assistant.
# Analyse the text and return a JSON object.
# Be strict: only return True for is_tech_job if this is a software engineering,
# data engineering, DevOps, ML, or automation role.
# Be especially strict for is_actual_job_posting: service pages, agency capability
# pages, and product descriptions must return False even if they mention tech."""

# GATE_USER = """Text (first 600 chars):
# {snippet}

# Return JSON with exactly these keys: is_tech_job, is_english, has_content, is_actual_job_posting."""


# EVAL_SYSTEM = """You are a senior technical recruiter evaluating job fit for a specific candidate.

# CANDIDATE PROFILE:
# - Python developer, 1.5 years professional experience
# - Startup background (built backend systems from scratch)
# - Strong: Python, REST APIs, FastAPI/Flask, data pipelines, automation, async programming
# - Familiar: LLMs, prompt engineering, AI agents, basic ML model development
# - Target: mid/junior-mid backend, automation, or data engineering roles
# - Location: Yerevan, Armenia (remote-first strongly preferred)

# SCORING RUBRIC:
# 90–100: Near-perfect fit. Python-first, matches seniority, remote ok, interesting tech
# 75–89:  Good fit. Backend role, Python present, manageable experience gap
# 60–74:  Possible but stretch. Wrong seniority or some missing stack items
# 0–59:   Poor fit. Wrong language, too senior, pure frontend, unrelated domain

# Return a complete JSON evaluation. Be honest about gaps — do not inflate scores."""


# EVAL_USER = """Evaluate this job for the candidate described above.

# JOB DESCRIPTION:
# {jd_text}

# Return a JSON object with ALL of these fields:
# - is_backend_role (bool)
# - fit_score (int 0-100)
# - core_tech_stack (list of strings)
# - required_years_experience (int or null)
# - seniority_level (one of: intern, junior, mid, senior, lead, unknown)
# - matching_strengths (list of strings)
# - potential_gaps (list of strings)
# - rejection_reason (string, empty if score >= 75)
# - is_remote_friendly (bool or null)
# - has_equity (bool or null)"""


# # ─────────────────────────────────────────────────────────────────────────────
# # Groq client factory
# # ─────────────────────────────────────────────────────────────────────────────

# def _make_instructor_client() -> instructor.Instructor:
#     """
#     Create an instructor-patched Groq client.

#     instructor wraps the client so that every .chat.completions.create()
#     call automatically:
#       1. Appends JSON schema instructions to the prompt
#       2. Validates the response against the Pydantic model
#       3. Retries up to `max_retries` times if validation fails
#     """
#     raw_client = Groq(api_key=settings.groq_api_key)
#     return instructor.from_groq(raw_client, mode=instructor.Mode.JSON)


# # Singleton client — created once, reused across all evaluations in a run
# _client: instructor.Instructor | None = None

# def get_client() -> instructor.Instructor:
#     global _client
#     if _client is None:
#         _client = _make_instructor_client()
#     return _client


# # ─────────────────────────────────────────────────────────────────────────────
# # Core evaluation functions
# # ─────────────────────────────────────────────────────────────────────────────

# def run_gate_check(jd_text: str) -> GateCheck:
#     """
#     Fast first-pass filter.
#     Rejects non-tech jobs, non-English, or empty descriptions before
#     wasting the slower full-eval call.
#     """
#     snippet = jd_text[:600].replace("\n", " ").strip()

#     try:
#         result = get_client().chat.completions.create(
#             model=settings.groq_model,
#             response_model=GateCheck,
#             max_retries=2,
#             messages=[
#                 {"role": "system", "content": GATE_SYSTEM},
#                 {"role": "user",   "content": GATE_USER.format(snippet=snippet)},
#             ],
#             temperature=0.0,
#         )
#         logger.debug(f"Gate check: tech={result.is_tech_job} en={result.is_english} content={result.has_content}")
#         return result

#     except Exception as exc:
#         logger.warning(f"Gate check LLM call failed ({exc}), defaulting to conservative reject")
#         # On error: let it through for tech/content, but flag as unknown posting
#         # This prevents service pages slipping through on Ollama timeout
#         return GateCheck(
#             is_tech_job=True,
#             is_english=True,
#             has_content=True,
#             is_actual_job_posting=False,
#         )


# def evaluate_job(jd_text: str, job_title: str = "") -> JobEvaluation:
#     """
#     Full structured evaluation of a job description.

#     Args:
#         jd_text:   Raw text of the job posting (max ~6000 chars from crawler)
#         job_title: Optional title hint to include in context

#     Returns:
#         JobEvaluation Pydantic model (always valid — instructor ensures this)

#     Raises:
#         RuntimeError if Ollama is unreachable after retries
#     """
#     if not jd_text or len(jd_text.strip()) < 50:
#         logger.warning("JD too short to evaluate, returning zero-score result")
#         return JobEvaluation(
#             is_backend_role=False,
#             fit_score=0,
#             rejection_reason="Job description too short or empty to evaluate.",
#         )

#     # Truncate to keep within context window (Llama 3.2 = 128k but be conservative)
#     jd_truncated = jd_text[:5000]

#     title_hint = f"Job Title (from crawler): {job_title}\n\n" if job_title else ""

#     start = time.perf_counter()

#     try:
#         result: JobEvaluation = get_client().chat.completions.create(
#             model=settings.groq_model,
#             response_model=JobEvaluation,
#             max_retries=3,              # instructor retries on validation failure
#             messages=[
#                 {"role": "system", "content": EVAL_SYSTEM},
#                 {"role": "user",   "content": EVAL_USER.format(
#                     jd_text=title_hint + jd_truncated
#                 )},
#             ],
#             temperature=0.1,            # low temp = consistent scoring
#         )

#         elapsed = time.perf_counter() - start
#         logger.info(f"Evaluation complete in {elapsed:.1f}s | {result.summary()}")
#         return result

#     except Exception as exc:
#         elapsed = time.perf_counter() - start
#         logger.error(f"Evaluation failed after {elapsed:.1f}s: {exc}")
#         raise RuntimeError(f"Groq evaluation failed: {exc}") from exc


# def evaluate_with_gate(jd_text: str, job_title: str = "") -> tuple[JobEvaluation | None, str]:
#     """
#     Two-stage evaluation pipeline:
#       1. Gate check  (fast, cheap)
#       2. Full eval   (only if gate passes)

#     Returns:
#         (JobEvaluation, "ok") on success
#         (None, reason_string) if gated out or error
#     """
#     # Stage 1: Gate
#     gate = run_gate_check(jd_text)
#     if not gate.passes:
#         reasons = []
#         if not gate.is_tech_job:           reasons.append("not a tech role")
#         if not gate.is_english:            reasons.append("non-English JD")
#         if not gate.has_content:           reasons.append("insufficient content")
#         if not gate.is_actual_job_posting: reasons.append("service/agency page, not a job posting")
#         reason = ", ".join(reasons)
#         logger.info(f"Gate check failed: {reason}")
#         return None, f"gate_failed:{reason}"

#     # Stage 2: Full evaluation
#     try:
#         evaluation = evaluate_job(jd_text, job_title)
#         return evaluation, "ok"
#     except RuntimeError as exc:
#         return None, f"eval_error:{exc}"


# # ─────────────────────────────────────────────────────────────────────────────
# # Groq health check  (replaces check_ollama_health)
# # ─────────────────────────────────────────────────────────────────────────────

# def check_groq_health() -> bool:
#     """
#     Verify the Groq API key is configured and the API is reachable.
#     Called at pipeline startup (preflight_task in main.py).
#     """
#     if settings.groq_api_key == "CHANGE_ME":
#         logger.error(
#             "GROQ_API_KEY not set in .env. "
#             "Get a free key at https://console.groq.com and add:\n"
#             "  GROQ_API_KEY=gsk_..."
#         )
#         return False

#     try:
#         client = Groq(api_key=settings.groq_api_key)
#         # Minimal test call — 1 token, returns almost instantly
#         client.chat.completions.create(
#             model=settings.groq_model,
#             messages=[{"role": "user", "content": "hi"}],
#             max_tokens=1,
#         )
#         logger.info(f"Groq health check OK | model={settings.groq_model}")
#         return True
#     except Exception as exc:
#         logger.error(f"Groq API unreachable: {exc}")
#         return False


# # Keep old name as alias so main.py import doesn't break
# check_ollama_health = check_groq_health
# # ─────────────────────────────────────────────────────────────────────────────

# def batch_evaluate(
#     jobs: list[dict],                   # each dict: {title, url, description}
#     threshold: int = None,
# ) -> list[dict]:
#     """
#     Evaluate a list of job dicts.

#     Args:
#         jobs:      List of dicts with keys: title, url, description
#         threshold: Override settings.fit_score_threshold for this batch

#     Returns:
#         List of dicts with original fields + evaluation results added.
#         Only jobs that pass the threshold are included.
#     """
#     cutoff = threshold if threshold is not None else settings.fit_score_threshold
#     passed: list[dict] = []

#     for i, job in enumerate(jobs, 1):
#         logger.info(f"Evaluating job {i}/{len(jobs)}: {job.get('title', 'unknown')!r}")

#         evaluation, status = evaluate_with_gate(
#             jd_text=job.get("description", ""),
#             job_title=job.get("title", ""),
#         )

#         if evaluation is None:
#             logger.info(f"  ✗ Dropped ({status})")
#             continue

#         if not evaluation.should_apply:
#             logger.info(f"  ✗ Score {evaluation.fit_score} < {cutoff} — dropped. Reason: {evaluation.rejection_reason}")
#             continue

#         logger.info(f"  ✓ PASSED — {evaluation.summary()}")

#         passed.append({
#             **job,
#             # Evaluation fields
#             "fit_score":                 evaluation.fit_score,
#             "is_backend":                evaluation.is_backend_role,
#             "tech_stack":                json.dumps(evaluation.core_tech_stack),
#             "rejection_reason":          evaluation.rejection_reason,
#             "seniority":                 evaluation.seniority_level.value,
#             "required_years":            evaluation.required_years_experience,
#             "matching_strengths":        json.dumps(evaluation.matching_strengths),
#             "potential_gaps":            json.dumps(evaluation.potential_gaps),
#             "is_remote_friendly":        evaluation.is_remote_friendly,
#             "has_equity":                evaluation.has_equity,
#             # Raw object for downstream use
#             "_evaluation":               evaluation,
#         })

#     logger.info(f"Batch complete: {len(passed)}/{len(jobs)} jobs passed threshold {cutoff}")
#     return passed


# # ─────────────────────────────────────────────────────────────────────────────
# # Ollama health check
# # ─────────────────────────────────────────────────────────────────────────────

# def check_ollama_health() -> bool:
#     """
#     Verify Ollama is running and the configured model is available.
#     Called at pipeline startup.
#     """
#     import httpx
#     try:
#         resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
#         if resp.status_code != 200:
#             logger.error(f"Ollama API returned {resp.status_code}")
#             return False

#         models = [m["name"] for m in resp.json().get("models", [])]
#         model_base = settings.ollama_model.split(":")[0]

#         if not any(model_base in m for m in models):
#             logger.warning(
#                 f"Model '{settings.ollama_model}' not found in Ollama. "
#                 f"Available: {models}. "
#                 f"Run: ollama pull {settings.ollama_model}"
#             )
#             return False

#         logger.info(f"Ollama health check OK | model={settings.ollama_model} | available_models={models}")
#         return True

#     except Exception as exc:
#         logger.error(f"Ollama unreachable at {settings.ollama_base_url}: {exc}")
#         return False


# # ─────────────────────────────────────────────────────────────────────────────
# # CLI entry point — test with mock data (no Ollama needed for schema test)
# # ─────────────────────────────────────────────────────────────────────────────

# MOCK_JD_BACKEND = """
# Senior Python Backend Engineer – Remote
# Acme Data Inc.

# About the Role:
# We're looking for a backend engineer to join our small, fast-moving team building
# data infrastructure for our SaaS platform.

# Responsibilities:
# - Design and maintain REST APIs using FastAPI and async Python
# - Build and maintain data pipelines with Apache Airflow
# - Work with PostgreSQL, Redis, and S3-compatible object storage
# - Contribute to our internal LLM-powered automation tooling
# - Deploy and monitor services on Kubernetes (nice to have)

# Requirements:
# - 1-3 years of Python backend experience
# - Strong understanding of REST API design
# - Experience with SQL databases (PostgreSQL preferred)
# - Comfortable with Docker and basic DevOps
# - Startup experience preferred — we move fast

# Nice to Have:
# - FastAPI or Django REST Framework
# - Experience with LLM APIs (OpenAI, Anthropic)
# - Redis / Celery for async task queues

# We're remote-first with competitive salary and equity.
# """

# MOCK_JD_IRRELEVANT = """
# Senior Marketing Manager – New York (On-site)

# We are looking for an experienced marketing manager to lead our
# brand strategy and social media campaigns. Must have 5+ years in
# digital marketing, strong copywriting skills, and experience with
# Salesforce CRM. No technical background required.
# """


# if __name__ == "__main__":
#     import sys

#     print("=" * 60)
#     print("EVALUATOR — Schema & Logic Tests (no Ollama required)")
#     print("=" * 60)

#     # ── Test 1: Pydantic model validation ────────────────────────────────────
#     print("\n[1] Pydantic model validation")

#     eval_good = JobEvaluation(
#         is_backend_role=True,
#         fit_score=87,
#         core_tech_stack=["Python", "FastAPI", "PostgreSQL"],
#         seniority_level=SeniorityLevel.MID,
#         matching_strengths=["Python required", "remote friendly"],
#         potential_gaps=["Kubernetes is nice-to-have"],
#         is_remote_friendly=True,
#         has_equity=True,
#     )
#     assert eval_good.should_apply is True
#     assert eval_good.fit_score == 87
#     print(f"  ✅  Good fit model: {eval_good.summary()}")

#     eval_bad = JobEvaluation(
#         is_backend_role=False,
#         fit_score=30,
#         rejection_reason="Marketing role, no engineering component.",
#     )
#     assert eval_bad.should_apply is False
#     print(f"  ✅  Bad fit model:  {eval_bad.summary()}")

#     # ── Test 2: Score clamping ───────────────────────────────────────────────
#     print("\n[2] Score clamping validator")
#     e = JobEvaluation(is_backend_role=True, fit_score=150)
#     assert e.fit_score == 100, f"Expected 100, got {e.fit_score}"
#     e2 = JobEvaluation(is_backend_role=True, fit_score=-10)
#     assert e2.fit_score == 0
#     print("  ✅  Score clamping works (150→100, -10→0)")

#     # ── Test 3: Stack normalisation ──────────────────────────────────────────
#     print("\n[3] Tech stack normalisation")
#     e3 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack="Python, FastAPI, Redis")
#     assert "Python" in e3.core_tech_stack
#     assert len(e3.core_tech_stack) == 3
#     print(f"  ✅  String → list: {e3.core_tech_stack}")

#     e4 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack='["Go","gRPC"]')
#     assert e4.core_tech_stack == ["Go", "gRPC"]
#     print(f"  ✅  JSON string → list: {e4.core_tech_stack}")

#     # ── Test 4: GateCheck ────────────────────────────────────────────────────
#     print("\n[4] GateCheck logic")
#     gate_pass = GateCheck(is_tech_job=True, is_english=True, has_content=True, is_actual_job_posting=True)
#     assert gate_pass.passes
#     gate_fail = GateCheck(is_tech_job=False, is_english=True, has_content=True, is_actual_job_posting=True)
#     assert not gate_fail.passes
#     print("  ✅  GateCheck.passes logic correct")

#     # ── Test 5: Groq health check (needs GROQ_API_KEY in .env) ──────────────
#     print("\n[5] Groq API health check")
#     is_healthy = check_groq_health()
#     if is_healthy:
#         print(f"  ✅  Groq is reachable | model={settings.groq_model}")
#         print("\n[6] Live LLM evaluation (backend JD)")
#         eval_result, status = evaluate_with_gate(MOCK_JD_BACKEND, "Senior Python Backend Engineer")
#         if eval_result:
#             print(f"  ✅  {eval_result.summary()}")
#             print(f"     Strengths: {eval_result.matching_strengths}")
#             print(f"     Gaps:      {eval_result.potential_gaps}")
#         else:
#             print(f"  ❌  Evaluation failed: {status}")

#         print("\n[7] Live LLM evaluation (irrelevant JD)")
#         eval_result2, status2 = evaluate_with_gate(MOCK_JD_IRRELEVANT, "Senior Marketing Manager")
#         if eval_result2 is None:
#             print(f"  ✅  Correctly rejected: {status2}")
#         else:
#             print(f"  ⚠️   Unexpected pass with score {eval_result2.fit_score}")
#     else:
#         print("  ⚠️   Groq not available — skipping live LLM tests")
#         print("       Add to .env:  GROQ_API_KEY=gsk_...")
#         print("       Free key at:  https://console.groq.com")

#     print("\n" + "=" * 60)
#     print("All schema/logic tests passed ✅")
#     print("=" * 60)

# """
# evaluator.py
# ============
# NODE 3 of the Job Hunter pipeline.

# Connects to a locally-running Ollama instance and uses the `instructor`
# library to extract a strict Pydantic model from raw job description text.

# Flow:
#   raw JD text
#       │
#       ▼
#   Ollama (Llama 3.2 / Mistral)
#       │
#       ▼
#   instructor (structured output via JSON mode)
#       │
#       ▼
#   JobEvaluation(fit_score=87, is_backend_role=True, ...)
#       │
#       ├── fit_score < 75  →  drop
#       └── fit_score >= 75 →  pass to resume generator

# Design notes:
#   - instructor patches the OpenAI-compatible Ollama client so we get
#     guaranteed Pydantic validation with automatic retry on bad JSON.
#   - We run two LLM calls per job:
#       1. Fast "gate" check  (is this even a tech/backend role?)  ~0.5s
#       2. Full evaluation    (score, stack, fit reasoning)        ~2-4s
#     This avoids wasting the slower full-eval on marketing jobs.
#   - All calls are synchronous; the Prefect flow wraps them in threads
#     via asyncio.to_thread() so the event loop stays unblocked.
# """

# import json
# import logging
# import time
# from enum import Enum
# from typing import Optional

# import instructor
# from openai import OpenAI          # Ollama exposes an OpenAI-compatible API
# from pydantic import BaseModel, Field, field_validator

# from config import settings

# logger = logging.getLogger("evaluator")


# # ─────────────────────────────────────────────────────────────────────────────
# # Pydantic models
# # ─────────────────────────────────────────────────────────────────────────────

# class SeniorityLevel(str, Enum):
#     INTERN     = "intern"
#     JUNIOR     = "junior"
#     MID        = "mid"
#     SENIOR     = "senior"
#     LEAD       = "lead"
#     UNKNOWN    = "unknown"


# class JobEvaluation(BaseModel):
#     """
#     Structured evaluation of a single job posting.
#     Returned by the LLM for every job that passes the gate check.
#     """

#     # ── Core decision fields ─────────────────────────────────────────────────
#     is_backend_role: bool = Field(
#         description=(
#             "True if the role primarily involves backend/server-side work: "
#             "APIs, data pipelines, automation, ML infrastructure, DevOps. "
#             "False for frontend-only, design, sales, or non-technical roles."
#         )
#     )

#     fit_score: int = Field(
#         ge=0, le=100,
#         description=(
#             "0–100 relevance score for a Python developer with 1.5 years experience "
#             "specialising in REST APIs, data pipelines, automation, and basic ML. "
#             "Score higher for: Python, FastAPI/Django/Flask, async, data engineering, "
#             "automation, LLM/AI tooling, startup culture, remote-friendly. "
#             "Score lower for: Java/C++/.NET only, 5+ years required, "
#             "pure frontend, enterprise legacy stack."
#         )
#     )

#     # ── Tech intelligence ────────────────────────────────────────────────────
#     core_tech_stack: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Explicit technologies, frameworks, and tools mentioned in the JD. "
#             "Examples: ['Python', 'FastAPI', 'PostgreSQL', 'Redis', 'Docker']. "
#             "Include only things actually stated — do not infer."
#         )
#     )

#     required_years_experience: Optional[int] = Field(
#         default=None,
#         description="Minimum years of experience explicitly required. None if not stated."
#     )

#     seniority_level: SeniorityLevel = Field(
#         default=SeniorityLevel.UNKNOWN,
#         description="Inferred seniority level of the role."
#     )

#     # ── Fit analysis ─────────────────────────────────────────────────────────
#     matching_strengths: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Specific reasons why this candidate IS a good fit. "
#             "E.g. ['Python required and candidate is strong in Python', "
#             "'startup culture matches candidate background']"
#         )
#     )

#     potential_gaps: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Honest gaps or risks. E.g. ['Role requires 3yr exp, candidate has 1.5', "
#             "'Kubernetes mentioned — candidate may need to upskill']"
#         )
#     )

#     rejection_reason: str = Field(
#         default="",
#         description=(
#             "If fit_score < 75, a one-sentence explanation of the main disqualifier. "
#             "Empty string if fit_score >= 75."
#         )
#     )

#     # ── Opportunity signals ──────────────────────────────────────────────────
#     is_remote_friendly: Optional[bool] = Field(
#         default=None,
#         description="True if remote/hybrid mentioned, False if on-site only, None if unclear."
#     )

#     has_equity: Optional[bool] = Field(
#         default=None,
#         description="True if equity/stock options mentioned."
#     )

#     # ── Validators ───────────────────────────────────────────────────────────
#     @field_validator("core_tech_stack", mode="before")
#     @classmethod
#     def normalise_stack(cls, v):
#         if isinstance(v, str):
#             try:
#                 v = json.loads(v)
#             except Exception:
#                 v = [x.strip() for x in v.split(",") if x.strip()]
#         return [str(item).strip() for item in (v or [])]

#     @field_validator("fit_score", mode="before")
#     @classmethod
#     def clamp_score(cls, v):
#         try:
#             return max(0, min(100, int(v)))
#         except (TypeError, ValueError):
#             return 0

#     @property
#     def should_apply(self) -> bool:
#         return self.fit_score >= settings.fit_score_threshold and self.is_backend_role

#     def summary(self) -> str:
#         """One-line human-readable summary for logging."""
#         stack = ", ".join(self.core_tech_stack[:5]) or "unknown stack"
#         return (
#             f"score={self.fit_score} | backend={self.is_backend_role} | "
#             f"seniority={self.seniority_level.value} | "
#             f"stack=[{stack}] | apply={self.should_apply}"
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Gate check model  (fast, cheap first-pass filter)
# # ─────────────────────────────────────────────────────────────────────────────

# class GateCheck(BaseModel):
#     """Quick first-pass: is this even a real job posting worth evaluating?"""
#     is_tech_job:           bool = Field(description="True if this is a software/data/engineering role.")
#     is_english:            bool = Field(description="True if the job description is in English.")
#     has_content:           bool = Field(description="True if the description has enough content to evaluate (>100 words).")
#     is_actual_job_posting: bool = Field(
#         description=(
#             "True ONLY if this is a real job opening for a specific hire. "
#             "False if it is a company service page, product description, agency offering, "
#             "capability page, or marketing content. "
#             "Look for: 'Apply', 'Requirements', 'Responsibilities', 'We are looking for', "
#             "'Join our team' — these signal real postings. "
#             "Service pages often say 'We offer', 'Our team provides', 'Hire us' — these are NOT jobs."
#         )
#     )

#     @property
#     def passes(self) -> bool:
#         return (
#             self.is_tech_job
#             and self.is_english
#             and self.has_content
#             and self.is_actual_job_posting
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Prompts
# # ─────────────────────────────────────────────────────────────────────────────

# GATE_SYSTEM = """You are a job classification assistant.
# Analyse the text and return a JSON object.
# Be strict: only return True for is_tech_job if this is a software engineering,
# data engineering, DevOps, ML, or automation role.
# Be especially strict for is_actual_job_posting: service pages, agency capability
# pages, and product descriptions must return False even if they mention tech."""

# GATE_USER = """Text (first 600 chars):
# {snippet}

# Return JSON with exactly these keys: is_tech_job, is_english, has_content, is_actual_job_posting."""


# EVAL_SYSTEM = """You are a senior technical recruiter evaluating job fit for a specific candidate.

# CANDIDATE PROFILE:
# - Python developer, 1.5 years professional experience
# - Startup background (built backend systems from scratch)
# - Strong: Python, REST APIs, FastAPI/Flask, data pipelines, automation, async programming
# - Familiar: LLMs, prompt engineering, AI agents, basic ML model development
# - Target: mid/junior-mid backend, automation, or data engineering roles
# - Location: Yerevan, Armenia (remote-first strongly preferred)

# SCORING RUBRIC:
# 90–100: Near-perfect fit. Python-first, matches seniority, remote ok, interesting tech
# 75–89:  Good fit. Backend role, Python present, manageable experience gap
# 60–74:  Possible but stretch. Wrong seniority or some missing stack items
# 0–59:   Poor fit. Wrong language, too senior, pure frontend, unrelated domain

# Return a complete JSON evaluation. Be honest about gaps — do not inflate scores."""


# EVAL_USER = """Evaluate this job for the candidate described above.

# JOB DESCRIPTION:
# {jd_text}

# Return a JSON object with ALL of these fields:
# - is_backend_role (bool)
# - fit_score (int 0-100)
# - core_tech_stack (list of strings)
# - required_years_experience (int or null)
# - seniority_level (one of: intern, junior, mid, senior, lead, unknown)
# - matching_strengths (list of strings)
# - potential_gaps (list of strings)
# - rejection_reason (string, empty if score >= 75)
# - is_remote_friendly (bool or null)
# - has_equity (bool or null)"""


# # ─────────────────────────────────────────────────────────────────────────────
# # Ollama client factory
# # ─────────────────────────────────────────────────────────────────────────────

# def _make_instructor_client() -> instructor.Instructor:
#     """
#     Create an instructor-patched OpenAI client pointed at local Ollama.

#     instructor wraps the client so that every .chat.completions.create()
#     call automatically:
#       1. Appends JSON schema instructions to the prompt
#       2. Validates the response against the Pydantic model
#       3. Retries up to `max_retries` times if validation fails
#     """
#     raw_client = OpenAI(
#         base_url=f"{settings.ollama_base_url}/v1",
#         api_key="ollama",           # Ollama ignores the key but OpenAI SDK requires it
#     )
#     return instructor.from_openai(
#         raw_client,
#         mode=instructor.Mode.JSON,  # JSON mode: most reliable with Ollama
#     )


# # Singleton client — created once, reused across all evaluations
# _client: instructor.Instructor | None = None

# def get_client() -> instructor.Instructor:
#     global _client
#     if _client is None:
#         _client = _make_instructor_client()
#     return _client


# # ─────────────────────────────────────────────────────────────────────────────
# # Core evaluation functions
# # ─────────────────────────────────────────────────────────────────────────────

# def run_gate_check(jd_text: str) -> GateCheck:
#     """
#     Fast first-pass filter.
#     Rejects non-tech jobs, non-English, or empty descriptions before
#     wasting the slower full-eval call.
#     """
#     snippet = jd_text[:600].replace("\n", " ").strip()

#     try:
#         result = get_client().chat.completions.create(
#             model=settings.ollama_model,
#             response_model=GateCheck,
#             max_retries=2,
#             messages=[
#                 {"role": "system", "content": GATE_SYSTEM},
#                 {"role": "user",   "content": GATE_USER.format(snippet=snippet)},
#             ],
#             temperature=0.0,
#         )
#         logger.debug(f"Gate check: tech={result.is_tech_job} en={result.is_english} content={result.has_content}")
#         return result

#     except Exception as exc:
#         logger.warning(f"Gate check LLM call failed ({exc}), defaulting to conservative reject")
#         # On error: let it through for tech/content, but flag as unknown posting
#         # This prevents service pages slipping through on Ollama timeout
#         return GateCheck(
#             is_tech_job=True,
#             is_english=True,
#             has_content=True,
#             is_actual_job_posting=False,
#         )


# def evaluate_job(jd_text: str, job_title: str = "") -> JobEvaluation:
#     """
#     Full structured evaluation of a job description.

#     Args:
#         jd_text:   Raw text of the job posting (max ~6000 chars from crawler)
#         job_title: Optional title hint to include in context

#     Returns:
#         JobEvaluation Pydantic model (always valid — instructor ensures this)

#     Raises:
#         RuntimeError if Ollama is unreachable after retries
#     """
#     if not jd_text or len(jd_text.strip()) < 50:
#         logger.warning("JD too short to evaluate, returning zero-score result")
#         return JobEvaluation(
#             is_backend_role=False,
#             fit_score=0,
#             rejection_reason="Job description too short or empty to evaluate.",
#         )

#     # Truncate to keep within context window (Llama 3.2 = 128k but be conservative)
#     jd_truncated = jd_text[:5000]

#     title_hint = f"Job Title (from crawler): {job_title}\n\n" if job_title else ""

#     start = time.perf_counter()

#     try:
#         result: JobEvaluation = get_client().chat.completions.create(
#             model=settings.ollama_model,
#             response_model=JobEvaluation,
#             max_retries=3,              # instructor retries on validation failure
#             messages=[
#                 {"role": "system", "content": EVAL_SYSTEM},
#                 {"role": "user",   "content": EVAL_USER.format(
#                     jd_text=title_hint + jd_truncated
#                 )},
#             ],
#             temperature=0.1,            # low temp = consistent scoring
#         )

#         elapsed = time.perf_counter() - start
#         logger.info(f"Evaluation complete in {elapsed:.1f}s | {result.summary()}")
#         return result

#     except Exception as exc:
#         elapsed = time.perf_counter() - start
#         logger.error(f"Evaluation failed after {elapsed:.1f}s: {exc}")
#         raise RuntimeError(f"Ollama evaluation failed: {exc}") from exc


# def evaluate_with_gate(jd_text: str, job_title: str = "") -> tuple[JobEvaluation | None, str]:
#     """
#     Two-stage evaluation pipeline:
#       1. Gate check  (fast, cheap)
#       2. Full eval   (only if gate passes)

#     Returns:
#         (JobEvaluation, "ok") on success
#         (None, reason_string) if gated out or error
#     """
#     # Stage 1: Gate
#     gate = run_gate_check(jd_text)
#     if not gate.passes:
#         reasons = []
#         if not gate.is_tech_job:           reasons.append("not a tech role")
#         if not gate.is_english:            reasons.append("non-English JD")
#         if not gate.has_content:           reasons.append("insufficient content")
#         if not gate.is_actual_job_posting: reasons.append("service/agency page, not a job posting")
#         reason = ", ".join(reasons)
#         logger.info(f"Gate check failed: {reason}")
#         return None, f"gate_failed:{reason}"

#     # Stage 2: Full evaluation
#     try:
#         evaluation = evaluate_job(jd_text, job_title)
#         return evaluation, "ok"
#     except RuntimeError as exc:
#         return None, f"eval_error:{exc}"


# # ─────────────────────────────────────────────────────────────────────────────
# # Batch evaluator — wraps single-job eval for use in the Prefect flow
# # ─────────────────────────────────────────────────────────────────────────────

# def batch_evaluate(
#     jobs: list[dict],                   # each dict: {title, url, description}
#     threshold: int = None,
# ) -> list[dict]:
#     """
#     Evaluate a list of job dicts.

#     Args:
#         jobs:      List of dicts with keys: title, url, description
#         threshold: Override settings.fit_score_threshold for this batch

#     Returns:
#         List of dicts with original fields + evaluation results added.
#         Only jobs that pass the threshold are included.
#     """
#     cutoff = threshold if threshold is not None else settings.fit_score_threshold
#     passed: list[dict] = []

#     for i, job in enumerate(jobs, 1):
#         logger.info(f"Evaluating job {i}/{len(jobs)}: {job.get('title', 'unknown')!r}")

#         evaluation, status = evaluate_with_gate(
#             jd_text=job.get("description", ""),
#             job_title=job.get("title", ""),
#         )

#         if evaluation is None:
#             logger.info(f"  ✗ Dropped ({status})")
#             continue

#         if not evaluation.should_apply:
#             logger.info(f"  ✗ Score {evaluation.fit_score} < {cutoff} — dropped. Reason: {evaluation.rejection_reason}")
#             continue

#         logger.info(f"  ✓ PASSED — {evaluation.summary()}")

#         passed.append({
#             **job,
#             # Evaluation fields
#             "fit_score":                 evaluation.fit_score,
#             "is_backend":                evaluation.is_backend_role,
#             "tech_stack":                json.dumps(evaluation.core_tech_stack),
#             "rejection_reason":          evaluation.rejection_reason,
#             "seniority":                 evaluation.seniority_level.value,
#             "required_years":            evaluation.required_years_experience,
#             "matching_strengths":        json.dumps(evaluation.matching_strengths),
#             "potential_gaps":            json.dumps(evaluation.potential_gaps),
#             "is_remote_friendly":        evaluation.is_remote_friendly,
#             "has_equity":                evaluation.has_equity,
#             # Raw object for downstream use
#             "_evaluation":               evaluation,
#         })

#     logger.info(f"Batch complete: {len(passed)}/{len(jobs)} jobs passed threshold {cutoff}")
#     return passed


# # ─────────────────────────────────────────────────────────────────────────────
# # Ollama health check
# # ─────────────────────────────────────────────────────────────────────────────

# def check_ollama_health() -> bool:
#     """
#     Verify Ollama is running and the configured model is available.
#     Called at pipeline startup.
#     """
#     import httpx
#     try:
#         resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
#         if resp.status_code != 200:
#             logger.error(f"Ollama API returned {resp.status_code}")
#             return False

#         models = [m["name"] for m in resp.json().get("models", [])]
#         model_base = settings.ollama_model.split(":")[0]

#         if not any(model_base in m for m in models):
#             logger.warning(
#                 f"Model '{settings.ollama_model}' not found in Ollama. "
#                 f"Available: {models}. "
#                 f"Run: ollama pull {settings.ollama_model}"
#             )
#             return False

#         logger.info(f"Ollama health check OK | model={settings.ollama_model} | available_models={models}")
#         return True

#     except Exception as exc:
#         logger.error(f"Ollama unreachable at {settings.ollama_base_url}: {exc}")
#         return False


# # ─────────────────────────────────────────────────────────────────────────────
# # CLI entry point — test with mock data (no Ollama needed for schema test)
# # ─────────────────────────────────────────────────────────────────────────────

# MOCK_JD_BACKEND = """
# Senior Python Backend Engineer – Remote
# Acme Data Inc.

# About the Role:
# We're looking for a backend engineer to join our small, fast-moving team building
# data infrastructure for our SaaS platform.

# Responsibilities:
# - Design and maintain REST APIs using FastAPI and async Python
# - Build and maintain data pipelines with Apache Airflow
# - Work with PostgreSQL, Redis, and S3-compatible object storage
# - Contribute to our internal LLM-powered automation tooling
# - Deploy and monitor services on Kubernetes (nice to have)

# Requirements:
# - 1-3 years of Python backend experience
# - Strong understanding of REST API design
# - Experience with SQL databases (PostgreSQL preferred)
# - Comfortable with Docker and basic DevOps
# - Startup experience preferred — we move fast

# Nice to Have:
# - FastAPI or Django REST Framework
# - Experience with LLM APIs (OpenAI, Anthropic)
# - Redis / Celery for async task queues

# We're remote-first with competitive salary and equity.
# """

# MOCK_JD_IRRELEVANT = """
# Senior Marketing Manager – New York (On-site)

# We are looking for an experienced marketing manager to lead our
# brand strategy and social media campaigns. Must have 5+ years in
# digital marketing, strong copywriting skills, and experience with
# Salesforce CRM. No technical background required.
# """


# if __name__ == "__main__":
#     import sys

#     print("=" * 60)
#     print("EVALUATOR — Schema & Logic Tests (no Ollama required)")
#     print("=" * 60)

#     # ── Test 1: Pydantic model validation ────────────────────────────────────
#     print("\n[1] Pydantic model validation")

#     eval_good = JobEvaluation(
#         is_backend_role=True,
#         fit_score=87,
#         core_tech_stack=["Python", "FastAPI", "PostgreSQL"],
#         seniority_level=SeniorityLevel.MID,
#         matching_strengths=["Python required", "remote friendly"],
#         potential_gaps=["Kubernetes is nice-to-have"],
#         is_remote_friendly=True,
#         has_equity=True,
#     )
#     assert eval_good.should_apply is True
#     assert eval_good.fit_score == 87
#     print(f"  ✅  Good fit model: {eval_good.summary()}")

#     eval_bad = JobEvaluation(
#         is_backend_role=False,
#         fit_score=30,
#         rejection_reason="Marketing role, no engineering component.",
#     )
#     assert eval_bad.should_apply is False
#     print(f"  ✅  Bad fit model:  {eval_bad.summary()}")

#     # ── Test 2: Score clamping ───────────────────────────────────────────────
#     print("\n[2] Score clamping validator")
#     e = JobEvaluation(is_backend_role=True, fit_score=150)
#     assert e.fit_score == 100, f"Expected 100, got {e.fit_score}"
#     e2 = JobEvaluation(is_backend_role=True, fit_score=-10)
#     assert e2.fit_score == 0
#     print("  ✅  Score clamping works (150→100, -10→0)")

#     # ── Test 3: Stack normalisation ──────────────────────────────────────────
#     print("\n[3] Tech stack normalisation")
#     e3 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack="Python, FastAPI, Redis")
#     assert "Python" in e3.core_tech_stack
#     assert len(e3.core_tech_stack) == 3
#     print(f"  ✅  String → list: {e3.core_tech_stack}")

#     e4 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack='["Go","gRPC"]')
#     assert e4.core_tech_stack == ["Go", "gRPC"]
#     print(f"  ✅  JSON string → list: {e4.core_tech_stack}")

#     # ── Test 4: GateCheck ────────────────────────────────────────────────────
#     print("\n[4] GateCheck logic")
#     gate_pass = GateCheck(is_tech_job=True, is_english=True, has_content=True)
#     assert gate_pass.passes
#     gate_fail = GateCheck(is_tech_job=False, is_english=True, has_content=True)
#     assert not gate_fail.passes
#     print("  ✅  GateCheck.passes logic correct")

#     # ── Test 5: Ollama health check (optional — needs running Ollama) ────────
#     print("\n[5] Ollama health check")
#     is_healthy = check_ollama_health()
#     if is_healthy:
#         print("  ✅  Ollama is running and model is available")
#         print("\n[6] Live LLM evaluation (backend JD)")
#         eval_result, status = evaluate_with_gate(MOCK_JD_BACKEND, "Senior Python Backend Engineer")
#         if eval_result:
#             print(f"  ✅  {eval_result.summary()}")
#             print(f"     Strengths: {eval_result.matching_strengths}")
#             print(f"     Gaps:      {eval_result.potential_gaps}")
#         else:
#             print(f"  ❌  Evaluation failed: {status}")

#         print("\n[7] Live LLM evaluation (irrelevant JD)")
#         eval_result2, status2 = evaluate_with_gate(MOCK_JD_IRRELEVANT, "Senior Marketing Manager")
#         if eval_result2 is None:
#             print(f"  ✅  Correctly rejected: {status2}")
#         else:
#             print(f"  ⚠️   Unexpected pass with score {eval_result2.fit_score}")
#     else:
#         print("  ⚠️   Ollama not running — skipping live LLM tests")
#         print("       Start Ollama: ollama serve")
#         print(f"       Pull model:   ollama pull {settings.ollama_model}")

#     print("\n" + "=" * 60)
#     print("All schema/logic tests passed ✅")
#     print("=" * 60)

# """
# evaluator.py
# ============
# NODE 3 of the Job Hunter pipeline.

# Connects to the Groq API and uses the `instructor` library to extract
# a strict Pydantic model from raw job description text.

# Flow:
#   raw JD text
#       │
#       ▼
#   Groq (llama3-70b-8192)
#       │
#       ▼
#   instructor (structured JSON output)
#       │
#       ▼
#   JobEvaluation(fit_score=87, is_backend_role=True, ...)
#       │
#       ├── fit_score < 75  →  drop
#       └── fit_score >= 75 →  pass to resume generator

# Design notes:
#   - instructor patches the Groq client for guaranteed Pydantic validation
#     with automatic retry on bad JSON.
#   - Two LLM calls per job:
#       1. Fast "gate" check  (is this even a tech/backend role?)  ~0.3s
#       2. Full evaluation    (score, stack, fit reasoning)        ~1-2s
#   - Groq is free-tier with generous rate limits (~30 req/min on free).
#     Add GROQ_API_KEY to your .env file.
#     Get a key at: https://console.groq.com
# """

# import json
# import logging
# import time
# from enum import Enum
# from typing import Optional

# import instructor
# from openai import OpenAI          # used for Ollama's OpenAI-compatible endpoint
# from pydantic import BaseModel, Field, field_validator

# from config import settings

# # Groq is optional — only imported if llm_backend == "groq"
# try:
#     from groq import Groq as _Groq
# except ImportError:
#     _Groq = None  # type: ignore

# logger = logging.getLogger("evaluator")


# # ─────────────────────────────────────────────────────────────────────────────
# # Pydantic models
# # ─────────────────────────────────────────────────────────────────────────────

# class SeniorityLevel(str, Enum):
#     INTERN     = "intern"
#     JUNIOR     = "junior"
#     MID        = "mid"
#     SENIOR     = "senior"
#     LEAD       = "lead"
#     UNKNOWN    = "unknown"


# class JobEvaluation(BaseModel):
#     """
#     Structured evaluation of a single job posting.
#     Returned by the LLM for every job that passes the gate check.
#     """

#     # ── Core decision fields ─────────────────────────────────────────────────
#     is_backend_role: bool = Field(
#         description=(
#             "True if the role primarily involves backend/server-side work: "
#             "APIs, data pipelines, automation, ML infrastructure, DevOps. "
#             "False for frontend-only, design, sales, or non-technical roles."
#         )
#     )

#     fit_score: int = Field(
#         ge=0, le=100,
#         description=(
#             "0–100 relevance score for a Python developer with 1.5 years experience "
#             "specialising in REST APIs, data pipelines, automation, and basic ML. "
#             "Score higher for: Python, FastAPI/Django/Flask, async, data engineering, "
#             "automation, LLM/AI tooling, startup culture, remote-friendly. "
#             "Score lower for: Java/C++/.NET only, 5+ years required, "
#             "pure frontend, enterprise legacy stack."
#         )
#     )

#     # ── Tech intelligence ────────────────────────────────────────────────────
#     core_tech_stack: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Explicit technologies, frameworks, and tools mentioned in the JD. "
#             "Examples: ['Python', 'FastAPI', 'PostgreSQL', 'Redis', 'Docker']. "
#             "Include only things actually stated — do not infer."
#         )
#     )

#     required_years_experience: Optional[int] = Field(
#         default=None,
#         description="Minimum years of experience explicitly required. None if not stated."
#     )

#     seniority_level: SeniorityLevel = Field(
#         default=SeniorityLevel.UNKNOWN,
#         description="Inferred seniority level of the role."
#     )

#     # ── Fit analysis ─────────────────────────────────────────────────────────
#     matching_strengths: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Specific reasons why this candidate IS a good fit. "
#             "E.g. ['Python required and candidate is strong in Python', "
#             "'startup culture matches candidate background']"
#         )
#     )

#     potential_gaps: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Honest gaps or risks. E.g. ['Role requires 3yr exp, candidate has 1.5', "
#             "'Kubernetes mentioned — candidate may need to upskill']"
#         )
#     )

#     rejection_reason: str = Field(
#         default="",
#         description=(
#             "If fit_score < 75, a one-sentence explanation of the main disqualifier. "
#             "Empty string if fit_score >= 75."
#         )
#     )

#     # ── Opportunity signals ──────────────────────────────────────────────────
#     is_remote_friendly: Optional[bool] = Field(
#         default=None,
#         description="True if remote/hybrid mentioned, False if on-site only, None if unclear."
#     )

#     has_equity: Optional[bool] = Field(
#         default=None,
#         description="True if equity/stock options mentioned."
#     )

#     # ── Validators ───────────────────────────────────────────────────────────
#     @field_validator("core_tech_stack", mode="before")
#     @classmethod
#     def normalise_stack(cls, v):
#         if isinstance(v, str):
#             try:
#                 v = json.loads(v)
#             except Exception:
#                 v = [x.strip() for x in v.split(",") if x.strip()]
#         return [str(item).strip() for item in (v or [])]

#     @field_validator("fit_score", mode="before")
#     @classmethod
#     def clamp_score(cls, v):
#         try:
#             return max(0, min(100, int(v)))
#         except (TypeError, ValueError):
#             return 0

#     @property
#     def should_apply(self) -> bool:
#         return self.fit_score >= settings.fit_score_threshold and self.is_backend_role

#     def summary(self) -> str:
#         """One-line human-readable summary for logging."""
#         stack = ", ".join(self.core_tech_stack[:5]) or "unknown stack"
#         return (
#             f"score={self.fit_score} | backend={self.is_backend_role} | "
#             f"seniority={self.seniority_level.value} | "
#             f"stack=[{stack}] | apply={self.should_apply}"
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Gate check model  (fast, cheap first-pass filter)
# # ─────────────────────────────────────────────────────────────────────────────

# class GateCheck(BaseModel):
#     """Quick first-pass: is this even a real job posting worth evaluating?"""
#     is_tech_job:           bool = Field(description="True if this is a software/data/engineering role.")
#     is_english:            bool = Field(description="True if the job description is in English.")
#     has_content:           bool = Field(description="True if the description has enough content to evaluate (>100 words).")
#     is_actual_job_posting: bool = Field(
#         description=(
#             "True ONLY if this is a real job opening for a specific hire. "
#             "False if it is a company service page, product description, agency offering, "
#             "capability page, or marketing content. "
#             "Look for: 'Apply', 'Requirements', 'Responsibilities', 'We are looking for', "
#             "'Join our team' — these signal real postings. "
#             "Service pages often say 'We offer', 'Our team provides', 'Hire us' — these are NOT jobs."
#         )
#     )

#     @property
#     def passes(self) -> bool:
#         return (
#             self.is_tech_job
#             and self.is_english
#             and self.has_content
#             and self.is_actual_job_posting
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Prompts
# # ─────────────────────────────────────────────────────────────────────────────

# # ── Gate prompts — kept minimal so small models don't hallucinate extra fields ──

# GATE_SYSTEM = """You are a job classifier. Output only valid JSON, no explanation.
# Rules:
# - is_tech_job: true only for software/data/ML/DevOps/automation engineering roles
# - is_actual_job_posting: true only if the text describes an open position to apply to.
#   False for service pages, outsourcing ads, capability pages, or "hire our team" content."""

# GATE_USER = """Classify this text. Return ONLY this JSON with no other text:
# {{"is_tech_job": bool, "is_english": bool, "has_content": bool, "is_actual_job_posting": bool}}

# TEXT:
# {snippet}"""


# # ── Eval prompts — explicit anchors + worked examples help small models stay calibrated ──

# EVAL_SYSTEM = """You are a technical recruiter. Output only valid JSON, no explanation or markdown.

# CANDIDATE:
# - Python developer, 1.5 years professional experience, startup background
# - Strong: Python, FastAPI, Flask, REST APIs, async, data pipelines, automation
# - Knows: LLMs, prompt engineering, basic ML, PostgreSQL, Redis, Docker
# - Target: junior-mid backend / data-engineering / automation roles
# - Location: Yerevan, Armenia — needs remote or remote-friendly

# SCORING — be precise, do not round to 80 or 90 by default:
# 95: Python-first, 1-3yr experience required, fully remote, interesting tech stack
# 85: Backend role, Python listed, seniority gap ≤1yr, hybrid ok
# 75: Python present but not primary, OR 3-5yr required, OR on-site only
# 60: Python barely mentioned, OR 5+yr required, OR wrong language primary
# 40: Java/.NET/Go only, pure frontend, non-technical, or 7+yr senior role
# 20: Completely unrelated domain or language

# WORKED EXAMPLES (use as anchors):
# - "Junior Python Backend Engineer, FastAPI, remote" → score 92
# - "Backend Engineer, Python/Go, 2-4yr, hybrid" → score 80
# - "Senior Python Engineer, 5yr required, on-site SF" → score 62
# - "Full Stack Engineer, React + Node.js, 3yr" → score 45
# - "Java Spring Boot Senior Developer, 7yr" → score 22"""


# EVAL_USER = """Evaluate this job posting for the candidate above.

# JOB TITLE: {job_title}

# JOB DESCRIPTION:
# {jd_text}

# Return ONLY this JSON object with no other text:
# {{
#   "is_backend_role": bool,
#   "fit_score": integer 0-100,
#   "core_tech_stack": ["list", "of", "tech", "from", "JD"],
#   "required_years_experience": integer or null,
#   "seniority_level": "intern|junior|mid|senior|lead|unknown",
#   "matching_strengths": ["reason1", "reason2"],
#   "potential_gaps": ["gap1", "gap2"],
#   "rejection_reason": "one sentence if score < 75, else empty string",
#   "is_remote_friendly": true/false/null,
#   "has_equity": true/false/null
# }}"""


# # ─────────────────────────────────────────────────────────────────────────────
# # Backend-aware client factory  (Groq OR local Ollama)
# # ─────────────────────────────────────────────────────────────────────────────

# def _make_instructor_client() -> instructor.Instructor:
#     """
#     Create an instructor-patched LLM client based on settings.llm_backend.

#     "groq"   → Groq cloud API  (fast, precise, rate-limited)
#     "ollama" → Local Ollama     (unlimited, slightly less precise on small models)

#     instructor guarantees Pydantic-validated JSON output with automatic retries.
#     """
#     backend = settings.llm_backend.lower()

#     if backend == "groq":
#         if _Groq is None:
#             raise RuntimeError("groq package not installed. Run: pip install groq")
#         raw = _Groq(api_key=settings.groq_api_key)
#         return instructor.from_groq(raw, mode=instructor.Mode.JSON)

#     # Ollama — uses the OpenAI-compatible /v1 endpoint
#     raw = OpenAI(
#         base_url=f"{settings.ollama_base_url}/v1",
#         api_key="ollama",   # Ollama ignores this but openai SDK requires it
#     )
#     return instructor.from_openai(raw, mode=instructor.Mode.JSON)


# def _active_model() -> str:
#     """Return the model name string for the currently active backend."""
#     return (
#         settings.groq_model
#         if settings.llm_backend.lower() == "groq"
#         else settings.ollama_model
#     )


# # Singleton — recreated if backend changes between calls (shouldn't happen in prod)
# _client: instructor.Instructor | None = None
# _client_backend: str = ""

# def get_client() -> instructor.Instructor:
#     global _client, _client_backend
#     backend = settings.llm_backend.lower()
#     if _client is None or _client_backend != backend:
#         _client = _make_instructor_client()
#         _client_backend = backend
#         logger.info(f"LLM client initialised | backend={backend} | model={_active_model()}")
#     return _client


# # ─────────────────────────────────────────────────────────────────────────────
# # Core evaluation functions
# # ─────────────────────────────────────────────────────────────────────────────

# def run_gate_check(jd_text: str) -> GateCheck:
#     """
#     Fast first-pass filter.
#     Rejects non-tech jobs, non-English, or empty descriptions before
#     wasting the slower full-eval call.
#     """
#     snippet = jd_text[:800].replace("\n", " ").strip()

#     try:
#         result = get_client().chat.completions.create(
#             model=_active_model(),
#             response_model=GateCheck,
#             max_retries=3,           # local models sometimes need an extra retry
#             messages=[
#                 {"role": "system", "content": GATE_SYSTEM},
#                 {"role": "user",   "content": GATE_USER.format(snippet=snippet)},
#             ],
#             temperature=0.0,         # deterministic — gate is binary
#         )
#         logger.debug(
#             f"Gate check [{settings.llm_backend}]: "
#             f"tech={result.is_tech_job} en={result.is_english} "
#             f"content={result.has_content} posting={result.is_actual_job_posting}"
#         )
#         return result

#     except Exception as exc:
#         logger.warning(f"Gate check LLM call failed ({exc}), defaulting to conservative reject")
#         return GateCheck(
#             is_tech_job=True,
#             is_english=True,
#             has_content=True,
#             is_actual_job_posting=False,
#         )


# def evaluate_job(jd_text: str, job_title: str = "") -> JobEvaluation:
#     """
#     Full structured evaluation of a job description.

#     Args:
#         jd_text:   Raw text of the job posting (max ~6000 chars from crawler)
#         job_title: Optional title hint — passed directly into the prompt so the
#                    model can use it as a strong signal before reading the full JD.

#     Returns:
#         JobEvaluation Pydantic model (always valid — instructor ensures this)

#     Raises:
#         RuntimeError if the LLM backend is unreachable after retries
#     """
#     if not jd_text or len(jd_text.strip()) < 50:
#         logger.warning("JD too short to evaluate, returning zero-score result")
#         return JobEvaluation(
#             is_backend_role=False,
#             fit_score=0,
#             rejection_reason="Job description too short or empty to evaluate.",
#         )

#     # Local 8b models handle ~4k chars well; 70b cloud models handle 6k+
#     max_chars = 4000 if settings.llm_backend.lower() == "ollama" else 5500
#     jd_truncated = jd_text[:max_chars]

#     start = time.perf_counter()

#     try:
#         result: JobEvaluation = get_client().chat.completions.create(
#             model=_active_model(),
#             response_model=JobEvaluation,
#             max_retries=4,              # local models need more patience
#             messages=[
#                 {"role": "system", "content": EVAL_SYSTEM},
#                 {"role": "user",   "content": EVAL_USER.format(
#                     job_title=job_title or "Not specified",
#                     jd_text=jd_truncated,
#                 )},
#             ],
#             temperature=0.1,            # low temp = consistent, comparable scores
#         )

#         elapsed = time.perf_counter() - start
#         logger.info(
#             f"Evaluation complete in {elapsed:.1f}s "
#             f"[{settings.llm_backend}/{_active_model()}] | {result.summary()}"
#         )
#         return result

#     except Exception as exc:
#         elapsed = time.perf_counter() - start
#         logger.error(f"Evaluation failed after {elapsed:.1f}s: {exc}")
#         raise RuntimeError(f"LLM evaluation failed ({settings.llm_backend}): {exc}") from exc


# def evaluate_with_gate(jd_text: str, job_title: str = "") -> tuple[JobEvaluation | None, str]:
#     """
#     Two-stage evaluation pipeline:
#       1. Gate check  (fast, cheap)
#       2. Full eval   (only if gate passes)

#     Returns:
#         (JobEvaluation, "ok") on success
#         (None, reason_string) if gated out or error
#     """
#     # Stage 1: Gate
#     gate = run_gate_check(jd_text)
#     if not gate.passes:
#         reasons = []
#         if not gate.is_tech_job:           reasons.append("not a tech role")
#         if not gate.is_english:            reasons.append("non-English JD")
#         if not gate.has_content:           reasons.append("insufficient content")
#         if not gate.is_actual_job_posting: reasons.append("service/agency page, not a job posting")
#         reason = ", ".join(reasons)
#         logger.info(f"Gate check failed: {reason}")
#         return None, f"gate_failed:{reason}"

#     # Stage 2: Full evaluation
#     try:
#         evaluation = evaluate_job(jd_text, job_title)
#         return evaluation, "ok"
#     except RuntimeError as exc:
#         return None, f"eval_error:{exc}"


# # ─────────────────────────────────────────────────────────────────────────────
# # Health checks — backend-aware, used by main.py preflight
# # ─────────────────────────────────────────────────────────────────────────────

# def check_groq_health() -> bool:
#     """Verify Groq API key is set and the API responds."""
#     if _Groq is None:
#         logger.error("groq package not installed. Run: pip install groq")
#         return False
#     if not settings.groq_api_key or settings.groq_api_key == "CHANGE_ME":
#         logger.error("GROQ_API_KEY not set in .env — get a free key at https://console.groq.com")
#         return False
#     try:
#         client = _Groq(api_key=settings.groq_api_key)
#         client.chat.completions.create(
#             model=settings.groq_model,
#             messages=[{"role": "user", "content": "hi"}],
#             max_tokens=1,
#         )
#         logger.info(f"Groq health check OK | model={settings.groq_model}")
#         return True
#     except Exception as exc:
#         logger.error(f"Groq API unreachable: {exc}")
#         return False


# def check_ollama_health() -> bool:
#     """Verify Ollama is running locally and the configured model is pulled."""
#     import httpx
#     try:
#         resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
#         if resp.status_code != 200:
#             logger.error(f"Ollama API returned {resp.status_code}")
#             return False
#         models = [m["name"] for m in resp.json().get("models", [])]
#         model_base = settings.ollama_model.split(":")[0]
#         if not any(model_base in m for m in models):
#             logger.warning(
#                 f"Model '{settings.ollama_model}' not found in Ollama. "
#                 f"Available: {models}. Run: ollama pull {settings.ollama_model}"
#             )
#             return False
#         logger.info(f"Ollama health OK | model={settings.ollama_model}")
#         return True
#     except Exception as exc:
#         logger.error(f"Ollama unreachable at {settings.ollama_base_url}: {exc}")
#         return False


# def check_llm_health() -> bool:
#     """
#     Check whichever backend is currently active (settings.llm_backend).
#     This is what main.py preflight_task should call — it handles both backends
#     without needing to know which one is in use.
#     """
#     backend = settings.llm_backend.lower()
#     if backend == "groq":
#         return check_groq_health()
#     if backend == "ollama":
#         return check_ollama_health()
#     logger.error(f"Unknown llm_backend: {backend!r} — must be 'groq' or 'ollama'")
#     return False

# # ─────────────────────────────────────────────────────────────────────────────

# def batch_evaluate(
#     jobs: list[dict],                   # each dict: {title, url, description}
#     threshold: int = None,
# ) -> list[dict]:
#     """
#     Evaluate a list of job dicts.

#     Args:
#         jobs:      List of dicts with keys: title, url, description
#         threshold: Override settings.fit_score_threshold for this batch

#     Returns:
#         List of dicts with original fields + evaluation results added.
#         Only jobs that pass the threshold are included.
#     """
#     cutoff = threshold if threshold is not None else settings.fit_score_threshold
#     passed: list[dict] = []

#     for i, job in enumerate(jobs, 1):
#         logger.info(f"Evaluating job {i}/{len(jobs)}: {job.get('title', 'unknown')!r}")

#         evaluation, status = evaluate_with_gate(
#             jd_text=job.get("description", ""),
#             job_title=job.get("title", ""),
#         )

#         if evaluation is None:
#             logger.info(f"  ✗ Dropped ({status})")
#             continue

#         if not evaluation.should_apply:
#             logger.info(f"  ✗ Score {evaluation.fit_score} < {cutoff} — dropped. Reason: {evaluation.rejection_reason}")
#             continue

#         logger.info(f"  ✓ PASSED — {evaluation.summary()}")

#         passed.append({
#             **job,
#             # Evaluation fields
#             "fit_score":                 evaluation.fit_score,
#             "is_backend":                evaluation.is_backend_role,
#             "tech_stack":                json.dumps(evaluation.core_tech_stack),
#             "rejection_reason":          evaluation.rejection_reason,
#             "seniority":                 evaluation.seniority_level.value,
#             "required_years":            evaluation.required_years_experience,
#             "matching_strengths":        json.dumps(evaluation.matching_strengths),
#             "potential_gaps":            json.dumps(evaluation.potential_gaps),
#             "is_remote_friendly":        evaluation.is_remote_friendly,
#             "has_equity":                evaluation.has_equity,
#             # Raw object for downstream use
#             "_evaluation":               evaluation,
#         })

#     logger.info(f"Batch complete: {len(passed)}/{len(jobs)} jobs passed threshold {cutoff}")
#     return passed


# # ─────────────────────────────────────────────────────────────────────────────
# # CLI entry point — test with mock data (no Ollama needed for schema test)
# # ─────────────────────────────────────────────────────────────────────────────

# MOCK_JD_BACKEND = """
# Senior Python Backend Engineer – Remote
# Acme Data Inc.

# About the Role:
# We're looking for a backend engineer to join our small, fast-moving team building
# data infrastructure for our SaaS platform.

# Responsibilities:
# - Design and maintain REST APIs using FastAPI and async Python
# - Build and maintain data pipelines with Apache Airflow
# - Work with PostgreSQL, Redis, and S3-compatible object storage
# - Contribute to our internal LLM-powered automation tooling
# - Deploy and monitor services on Kubernetes (nice to have)

# Requirements:
# - 1-3 years of Python backend experience
# - Strong understanding of REST API design
# - Experience with SQL databases (PostgreSQL preferred)
# - Comfortable with Docker and basic DevOps
# - Startup experience preferred — we move fast

# Nice to Have:
# - FastAPI or Django REST Framework
# - Experience with LLM APIs (OpenAI, Anthropic)
# - Redis / Celery for async task queues

# We're remote-first with competitive salary and equity.
# """

# MOCK_JD_IRRELEVANT = """
# Senior Marketing Manager – New York (On-site)

# We are looking for an experienced marketing manager to lead our
# brand strategy and social media campaigns. Must have 5+ years in
# digital marketing, strong copywriting skills, and experience with
# Salesforce CRM. No technical background required.
# """


# if __name__ == "__main__":
#     import sys

#     print("=" * 60)
#     print("EVALUATOR — Schema & Logic Tests (no Ollama required)")
#     print("=" * 60)

#     # ── Test 1: Pydantic model validation ────────────────────────────────────
#     print("\n[1] Pydantic model validation")

#     eval_good = JobEvaluation(
#         is_backend_role=True,
#         fit_score=87,
#         core_tech_stack=["Python", "FastAPI", "PostgreSQL"],
#         seniority_level=SeniorityLevel.MID,
#         matching_strengths=["Python required", "remote friendly"],
#         potential_gaps=["Kubernetes is nice-to-have"],
#         is_remote_friendly=True,
#         has_equity=True,
#     )
#     assert eval_good.should_apply is True
#     assert eval_good.fit_score == 87
#     print(f"  ✅  Good fit model: {eval_good.summary()}")

#     eval_bad = JobEvaluation(
#         is_backend_role=False,
#         fit_score=30,
#         rejection_reason="Marketing role, no engineering component.",
#     )
#     assert eval_bad.should_apply is False
#     print(f"  ✅  Bad fit model:  {eval_bad.summary()}")

#     # ── Test 2: Score clamping ───────────────────────────────────────────────
#     print("\n[2] Score clamping validator")
#     e = JobEvaluation(is_backend_role=True, fit_score=150)
#     assert e.fit_score == 100, f"Expected 100, got {e.fit_score}"
#     e2 = JobEvaluation(is_backend_role=True, fit_score=-10)
#     assert e2.fit_score == 0
#     print("  ✅  Score clamping works (150→100, -10→0)")

#     # ── Test 3: Stack normalisation ──────────────────────────────────────────
#     print("\n[3] Tech stack normalisation")
#     e3 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack="Python, FastAPI, Redis")
#     assert "Python" in e3.core_tech_stack
#     assert len(e3.core_tech_stack) == 3
#     print(f"  ✅  String → list: {e3.core_tech_stack}")

#     e4 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack='["Go","gRPC"]')
#     assert e4.core_tech_stack == ["Go", "gRPC"]
#     print(f"  ✅  JSON string → list: {e4.core_tech_stack}")

#     # ── Test 4: GateCheck ────────────────────────────────────────────────────
#     print("\n[4] GateCheck logic")
#     gate_pass = GateCheck(is_tech_job=True, is_english=True, has_content=True, is_actual_job_posting=True)
#     assert gate_pass.passes
#     gate_fail = GateCheck(is_tech_job=False, is_english=True, has_content=True, is_actual_job_posting=True)
#     assert not gate_fail.passes
#     print("  ✅  GateCheck.passes logic correct")

#     # ── Test 5: Groq health check (needs GROQ_API_KEY in .env) ──────────────
#     print("\n[5] Groq API health check")
#     is_healthy = check_groq_health()
#     if is_healthy:
#         print(f"  ✅  Groq is reachable | model={settings.groq_model}")
#         print("\n[6] Live LLM evaluation (backend JD)")
#         eval_result, status = evaluate_with_gate(MOCK_JD_BACKEND, "Senior Python Backend Engineer")
#         if eval_result:
#             print(f"  ✅  {eval_result.summary()}")
#             print(f"     Strengths: {eval_result.matching_strengths}")
#             print(f"     Gaps:      {eval_result.potential_gaps}")
#         else:
#             print(f"  ❌  Evaluation failed: {status}")

#         print("\n[7] Live LLM evaluation (irrelevant JD)")
#         eval_result2, status2 = evaluate_with_gate(MOCK_JD_IRRELEVANT, "Senior Marketing Manager")
#         if eval_result2 is None:
#             print(f"  ✅  Correctly rejected: {status2}")
#         else:
#             print(f"  ⚠️   Unexpected pass with score {eval_result2.fit_score}")
#     else:
#         print("  ⚠️   Groq not available — skipping live LLM tests")
#         print("       Add to .env:  GROQ_API_KEY=gsk_...")
#         print("       Free key at:  https://console.groq.com")

#     print("\n" + "=" * 60)
#     print("All schema/logic tests passed ✅")
#     print("=" * 60)


# """
# evaluator.py
# ============
# NODE 3 of the Job Hunter pipeline.

# Connects to a locally-running Ollama instance and uses the `instructor`
# library to extract a strict Pydantic model from raw job description text.

# Flow:
#   raw JD text
#       │
#       ▼
#   Ollama (Llama 3.2 / Mistral)
#       │
#       ▼
#   instructor (structured output via JSON mode)
#       │
#       ▼
#   JobEvaluation(fit_score=87, is_backend_role=True, ...)
#       │
#       ├── fit_score < 75  →  drop
#       └── fit_score >= 75 →  pass to resume generator

# Design notes:
#   - instructor patches the OpenAI-compatible Ollama client so we get
#     guaranteed Pydantic validation with automatic retry on bad JSON.
#   - We run two LLM calls per job:
#       1. Fast "gate" check  (is this even a tech/backend role?)  ~0.5s
#       2. Full evaluation    (score, stack, fit reasoning)        ~2-4s
#     This avoids wasting the slower full-eval on marketing jobs.
#   - All calls are synchronous; the Prefect flow wraps them in threads
#     via asyncio.to_thread() so the event loop stays unblocked.
# """

# import json
# import logging
# import time
# from enum import Enum
# from typing import Optional

# import instructor
# from openai import OpenAI          # Ollama exposes an OpenAI-compatible API
# from pydantic import BaseModel, Field, field_validator

# from config import settings

# logger = logging.getLogger("evaluator")


# # ─────────────────────────────────────────────────────────────────────────────
# # Pydantic models
# # ─────────────────────────────────────────────────────────────────────────────

# class SeniorityLevel(str, Enum):
#     INTERN     = "intern"
#     JUNIOR     = "junior"
#     MID        = "mid"
#     SENIOR     = "senior"
#     LEAD       = "lead"
#     UNKNOWN    = "unknown"


# class JobEvaluation(BaseModel):
#     """
#     Structured evaluation of a single job posting.
#     Returned by the LLM for every job that passes the gate check.
#     """

#     # ── Core decision fields ─────────────────────────────────────────────────
#     is_backend_role: bool = Field(
#         description=(
#             "True if the role primarily involves backend/server-side work: "
#             "APIs, data pipelines, automation, ML infrastructure, DevOps. "
#             "False for frontend-only, design, sales, or non-technical roles."
#         )
#     )

#     fit_score: int = Field(
#         ge=0, le=100,
#         description=(
#             "0–100 relevance score for a Python developer with 1.5 years experience "
#             "specialising in REST APIs, data pipelines, automation, and basic ML. "
#             "Score higher for: Python, FastAPI/Django/Flask, async, data engineering, "
#             "automation, LLM/AI tooling, startup culture, remote-friendly. "
#             "Score lower for: Java/C++/.NET only, 5+ years required, "
#             "pure frontend, enterprise legacy stack."
#         )
#     )

#     # ── Tech intelligence ────────────────────────────────────────────────────
#     core_tech_stack: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Explicit technologies, frameworks, and tools mentioned in the JD. "
#             "Examples: ['Python', 'FastAPI', 'PostgreSQL', 'Redis', 'Docker']. "
#             "Include only things actually stated — do not infer."
#         )
#     )

#     required_years_experience: Optional[int] = Field(
#         default=None,
#         description="Minimum years of experience explicitly required. None if not stated."
#     )

#     seniority_level: SeniorityLevel = Field(
#         default=SeniorityLevel.UNKNOWN,
#         description="Inferred seniority level of the role."
#     )

#     # ── Fit analysis ─────────────────────────────────────────────────────────
#     matching_strengths: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Specific reasons why this candidate IS a good fit. "
#             "E.g. ['Python required and candidate is strong in Python', "
#             "'startup culture matches candidate background']"
#         )
#     )

#     potential_gaps: list[str] = Field(
#         default_factory=list,
#         description=(
#             "Honest gaps or risks. E.g. ['Role requires 3yr exp, candidate has 1.5', "
#             "'Kubernetes mentioned — candidate may need to upskill']"
#         )
#     )

#     rejection_reason: str = Field(
#         default="",
#         description=(
#             "If fit_score < 75, a one-sentence explanation of the main disqualifier. "
#             "Empty string if fit_score >= 75."
#         )
#     )

#     # ── Opportunity signals ──────────────────────────────────────────────────
#     is_remote_friendly: Optional[bool] = Field(
#         default=None,
#         description="True if remote/hybrid mentioned, False if on-site only, None if unclear."
#     )

#     has_equity: Optional[bool] = Field(
#         default=None,
#         description="True if equity/stock options mentioned."
#     )

#     # ── Validators ───────────────────────────────────────────────────────────
#     @field_validator("core_tech_stack", mode="before")
#     @classmethod
#     def normalise_stack(cls, v):
#         if isinstance(v, str):
#             try:
#                 v = json.loads(v)
#             except Exception:
#                 v = [x.strip() for x in v.split(",") if x.strip()]
#         return [str(item).strip() for item in (v or [])]

#     @field_validator("fit_score", mode="before")
#     @classmethod
#     def clamp_score(cls, v):
#         try:
#             return max(0, min(100, int(v)))
#         except (TypeError, ValueError):
#             return 0

#     @property
#     def should_apply(self) -> bool:
#         return self.fit_score >= settings.fit_score_threshold and self.is_backend_role

#     def summary(self) -> str:
#         """One-line human-readable summary for logging."""
#         stack = ", ".join(self.core_tech_stack[:5]) or "unknown stack"
#         return (
#             f"score={self.fit_score} | backend={self.is_backend_role} | "
#             f"seniority={self.seniority_level.value} | "
#             f"stack=[{stack}] | apply={self.should_apply}"
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Gate check model  (fast, cheap first-pass filter)
# # ─────────────────────────────────────────────────────────────────────────────

# class GateCheck(BaseModel):
#     """Quick first-pass: is this even a real job posting worth evaluating?"""
#     is_tech_job:           bool = Field(description="True if this is a software/data/engineering role.")
#     is_english:            bool = Field(description="True if the job description is in English.")
#     has_content:           bool = Field(description="True if the description has enough content to evaluate (>100 words).")
#     is_actual_job_posting: bool = Field(
#         description=(
#             "True ONLY if this is a real job opening for a specific hire. "
#             "False if it is a company service page, product description, agency offering, "
#             "capability page, or marketing content. "
#             "Look for: 'Apply', 'Requirements', 'Responsibilities', 'We are looking for', "
#             "'Join our team' — these signal real postings. "
#             "Service pages often say 'We offer', 'Our team provides', 'Hire us' — these are NOT jobs."
#         )
#     )

#     @property
#     def passes(self) -> bool:
#         return (
#             self.is_tech_job
#             and self.is_english
#             and self.has_content
#             and self.is_actual_job_posting
#         )


# # ─────────────────────────────────────────────────────────────────────────────
# # Prompts
# # ─────────────────────────────────────────────────────────────────────────────

# GATE_SYSTEM = """You are a job classification assistant.
# Analyse the text and return a JSON object.
# Be strict: only return True for is_tech_job if this is a software engineering,
# data engineering, DevOps, ML, or automation role.
# Be especially strict for is_actual_job_posting: service pages, agency capability
# pages, and product descriptions must return False even if they mention tech."""

# GATE_USER = """Text (first 600 chars):
# {snippet}

# Return JSON with exactly these keys: is_tech_job, is_english, has_content, is_actual_job_posting."""


# EVAL_SYSTEM = """You are a senior technical recruiter evaluating job fit for a specific candidate.

# CANDIDATE PROFILE:
# - Python developer, 1.5 years professional experience
# - Startup background (built backend systems from scratch)
# - Strong: Python, REST APIs, FastAPI/Flask, data pipelines, automation, async programming
# - Familiar: LLMs, prompt engineering, AI agents, basic ML model development
# - Target: mid/junior-mid backend, automation, or data engineering roles
# - Location: Yerevan, Armenia (remote-first strongly preferred)

# SCORING RUBRIC:
# 90–100: Near-perfect fit. Python-first, matches seniority, remote ok, interesting tech
# 75–89:  Good fit. Backend role, Python present, manageable experience gap
# 60–74:  Possible but stretch. Wrong seniority or some missing stack items
# 0–59:   Poor fit. Wrong language, too senior, pure frontend, unrelated domain

# Return a complete JSON evaluation. Be honest about gaps — do not inflate scores."""


# EVAL_USER = """Evaluate this job for the candidate described above.

# JOB DESCRIPTION:
# {jd_text}

# Return a JSON object with ALL of these fields:
# - is_backend_role (bool)
# - fit_score (int 0-100)
# - core_tech_stack (list of strings)
# - required_years_experience (int or null)
# - seniority_level (one of: intern, junior, mid, senior, lead, unknown)
# - matching_strengths (list of strings)
# - potential_gaps (list of strings)
# - rejection_reason (string, empty if score >= 75)
# - is_remote_friendly (bool or null)
# - has_equity (bool or null)"""


# # ─────────────────────────────────────────────────────────────────────────────
# # Ollama client factory
# # ─────────────────────────────────────────────────────────────────────────────

# def _make_instructor_client() -> instructor.Instructor:
#     """
#     Create an instructor-patched OpenAI client pointed at local Ollama.

#     instructor wraps the client so that every .chat.completions.create()
#     call automatically:
#       1. Appends JSON schema instructions to the prompt
#       2. Validates the response against the Pydantic model
#       3. Retries up to `max_retries` times if validation fails
#     """
#     raw_client = OpenAI(
#         base_url=f"{settings.ollama_base_url}/v1",
#         api_key="ollama",           # Ollama ignores the key but OpenAI SDK requires it
#     )
#     return instructor.from_openai(
#         raw_client,
#         mode=instructor.Mode.JSON,  # JSON mode: most reliable with Ollama
#     )


# # Singleton client — created once, reused across all evaluations
# _client: instructor.Instructor | None = None

# def get_client() -> instructor.Instructor:
#     global _client
#     if _client is None:
#         _client = _make_instructor_client()
#     return _client


# # ─────────────────────────────────────────────────────────────────────────────
# # Core evaluation functions
# # ─────────────────────────────────────────────────────────────────────────────

# def run_gate_check(jd_text: str) -> GateCheck:
#     """
#     Fast first-pass filter.
#     Rejects non-tech jobs, non-English, or empty descriptions before
#     wasting the slower full-eval call.
#     """
#     snippet = jd_text[:600].replace("\n", " ").strip()

#     try:
#         result = get_client().chat.completions.create(
#             model=settings.ollama_model,
#             response_model=GateCheck,
#             max_retries=2,
#             messages=[
#                 {"role": "system", "content": GATE_SYSTEM},
#                 {"role": "user",   "content": GATE_USER.format(snippet=snippet)},
#             ],
#             temperature=0.0,
#         )
#         logger.debug(f"Gate check: tech={result.is_tech_job} en={result.is_english} content={result.has_content}")
#         return result

#     except Exception as exc:
#         logger.warning(f"Gate check LLM call failed ({exc}), defaulting to conservative reject")
#         # On error: let it through for tech/content, but flag as unknown posting
#         # This prevents service pages slipping through on Ollama timeout
#         return GateCheck(
#             is_tech_job=True,
#             is_english=True,
#             has_content=True,
#             is_actual_job_posting=False,
#         )


# def evaluate_job(jd_text: str, job_title: str = "") -> JobEvaluation:
#     """
#     Full structured evaluation of a job description.

#     Args:
#         jd_text:   Raw text of the job posting (max ~6000 chars from crawler)
#         job_title: Optional title hint to include in context

#     Returns:
#         JobEvaluation Pydantic model (always valid — instructor ensures this)

#     Raises:
#         RuntimeError if Ollama is unreachable after retries
#     """
#     if not jd_text or len(jd_text.strip()) < 50:
#         logger.warning("JD too short to evaluate, returning zero-score result")
#         return JobEvaluation(
#             is_backend_role=False,
#             fit_score=0,
#             rejection_reason="Job description too short or empty to evaluate.",
#         )

#     # Truncate to keep within context window (Llama 3.2 = 128k but be conservative)
#     jd_truncated = jd_text[:5000]

#     title_hint = f"Job Title (from crawler): {job_title}\n\n" if job_title else ""

#     start = time.perf_counter()

#     try:
#         result: JobEvaluation = get_client().chat.completions.create(
#             model=settings.ollama_model,
#             response_model=JobEvaluation,
#             max_retries=3,              # instructor retries on validation failure
#             messages=[
#                 {"role": "system", "content": EVAL_SYSTEM},
#                 {"role": "user",   "content": EVAL_USER.format(
#                     jd_text=title_hint + jd_truncated
#                 )},
#             ],
#             temperature=0.1,            # low temp = consistent scoring
#         )

#         elapsed = time.perf_counter() - start
#         logger.info(f"Evaluation complete in {elapsed:.1f}s | {result.summary()}")
#         return result

#     except Exception as exc:
#         elapsed = time.perf_counter() - start
#         logger.error(f"Evaluation failed after {elapsed:.1f}s: {exc}")
#         raise RuntimeError(f"Ollama evaluation failed: {exc}") from exc


# def evaluate_with_gate(jd_text: str, job_title: str = "") -> tuple[JobEvaluation | None, str]:
#     """
#     Two-stage evaluation pipeline:
#       1. Gate check  (fast, cheap)
#       2. Full eval   (only if gate passes)

#     Returns:
#         (JobEvaluation, "ok") on success
#         (None, reason_string) if gated out or error
#     """
#     # Stage 1: Gate
#     gate = run_gate_check(jd_text)
#     if not gate.passes:
#         reasons = []
#         if not gate.is_tech_job:           reasons.append("not a tech role")
#         if not gate.is_english:            reasons.append("non-English JD")
#         if not gate.has_content:           reasons.append("insufficient content")
#         if not gate.is_actual_job_posting: reasons.append("service/agency page, not a job posting")
#         reason = ", ".join(reasons)
#         logger.info(f"Gate check failed: {reason}")
#         return None, f"gate_failed:{reason}"

#     # Stage 2: Full evaluation
#     try:
#         evaluation = evaluate_job(jd_text, job_title)
#         return evaluation, "ok"
#     except RuntimeError as exc:
#         return None, f"eval_error:{exc}"


# # ─────────────────────────────────────────────────────────────────────────────
# # Batch evaluator — wraps single-job eval for use in the Prefect flow
# # ─────────────────────────────────────────────────────────────────────────────

# def batch_evaluate(
#     jobs: list[dict],                   # each dict: {title, url, description}
#     threshold: int = None,
# ) -> list[dict]:
#     """
#     Evaluate a list of job dicts.

#     Args:
#         jobs:      List of dicts with keys: title, url, description
#         threshold: Override settings.fit_score_threshold for this batch

#     Returns:
#         List of dicts with original fields + evaluation results added.
#         Only jobs that pass the threshold are included.
#     """
#     cutoff = threshold if threshold is not None else settings.fit_score_threshold
#     passed: list[dict] = []

#     for i, job in enumerate(jobs, 1):
#         logger.info(f"Evaluating job {i}/{len(jobs)}: {job.get('title', 'unknown')!r}")

#         evaluation, status = evaluate_with_gate(
#             jd_text=job.get("description", ""),
#             job_title=job.get("title", ""),
#         )

#         if evaluation is None:
#             logger.info(f"  ✗ Dropped ({status})")
#             continue

#         if not evaluation.should_apply:
#             logger.info(f"  ✗ Score {evaluation.fit_score} < {cutoff} — dropped. Reason: {evaluation.rejection_reason}")
#             continue

#         logger.info(f"  ✓ PASSED — {evaluation.summary()}")

#         passed.append({
#             **job,
#             # Evaluation fields
#             "fit_score":                 evaluation.fit_score,
#             "is_backend":                evaluation.is_backend_role,
#             "tech_stack":                json.dumps(evaluation.core_tech_stack),
#             "rejection_reason":          evaluation.rejection_reason,
#             "seniority":                 evaluation.seniority_level.value,
#             "required_years":            evaluation.required_years_experience,
#             "matching_strengths":        json.dumps(evaluation.matching_strengths),
#             "potential_gaps":            json.dumps(evaluation.potential_gaps),
#             "is_remote_friendly":        evaluation.is_remote_friendly,
#             "has_equity":                evaluation.has_equity,
#             # Raw object for downstream use
#             "_evaluation":               evaluation,
#         })

#     logger.info(f"Batch complete: {len(passed)}/{len(jobs)} jobs passed threshold {cutoff}")
#     return passed


# # ─────────────────────────────────────────────────────────────────────────────
# # Ollama health check
# # ─────────────────────────────────────────────────────────────────────────────

# def check_ollama_health() -> bool:
#     """
#     Verify Ollama is running and the configured model is available.
#     Called at pipeline startup.
#     """
#     import httpx
#     try:
#         resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
#         if resp.status_code != 200:
#             logger.error(f"Ollama API returned {resp.status_code}")
#             return False

#         models = [m["name"] for m in resp.json().get("models", [])]
#         model_base = settings.ollama_model.split(":")[0]

#         if not any(model_base in m for m in models):
#             logger.warning(
#                 f"Model '{settings.ollama_model}' not found in Ollama. "
#                 f"Available: {models}. "
#                 f"Run: ollama pull {settings.ollama_model}"
#             )
#             return False

#         logger.info(f"Ollama health check OK | model={settings.ollama_model} | available_models={models}")
#         return True

#     except Exception as exc:
#         logger.error(f"Ollama unreachable at {settings.ollama_base_url}: {exc}")
#         return False


# # ─────────────────────────────────────────────────────────────────────────────
# # CLI entry point — test with mock data (no Ollama needed for schema test)
# # ─────────────────────────────────────────────────────────────────────────────

# MOCK_JD_BACKEND = """
# Senior Python Backend Engineer – Remote
# Acme Data Inc.

# About the Role:
# We're looking for a backend engineer to join our small, fast-moving team building
# data infrastructure for our SaaS platform.

# Responsibilities:
# - Design and maintain REST APIs using FastAPI and async Python
# - Build and maintain data pipelines with Apache Airflow
# - Work with PostgreSQL, Redis, and S3-compatible object storage
# - Contribute to our internal LLM-powered automation tooling
# - Deploy and monitor services on Kubernetes (nice to have)

# Requirements:
# - 1-3 years of Python backend experience
# - Strong understanding of REST API design
# - Experience with SQL databases (PostgreSQL preferred)
# - Comfortable with Docker and basic DevOps
# - Startup experience preferred — we move fast

# Nice to Have:
# - FastAPI or Django REST Framework
# - Experience with LLM APIs (OpenAI, Anthropic)
# - Redis / Celery for async task queues

# We're remote-first with competitive salary and equity.
# """

# MOCK_JD_IRRELEVANT = """
# Senior Marketing Manager – New York (On-site)

# We are looking for an experienced marketing manager to lead our
# brand strategy and social media campaigns. Must have 5+ years in
# digital marketing, strong copywriting skills, and experience with
# Salesforce CRM. No technical background required.
# """


# if __name__ == "__main__":
#     import sys

#     print("=" * 60)
#     print("EVALUATOR — Schema & Logic Tests (no Ollama required)")
#     print("=" * 60)

#     # ── Test 1: Pydantic model validation ────────────────────────────────────
#     print("\n[1] Pydantic model validation")

#     eval_good = JobEvaluation(
#         is_backend_role=True,
#         fit_score=87,
#         core_tech_stack=["Python", "FastAPI", "PostgreSQL"],
#         seniority_level=SeniorityLevel.MID,
#         matching_strengths=["Python required", "remote friendly"],
#         potential_gaps=["Kubernetes is nice-to-have"],
#         is_remote_friendly=True,
#         has_equity=True,
#     )
#     assert eval_good.should_apply is True
#     assert eval_good.fit_score == 87
#     print(f"  ✅  Good fit model: {eval_good.summary()}")

#     eval_bad = JobEvaluation(
#         is_backend_role=False,
#         fit_score=30,
#         rejection_reason="Marketing role, no engineering component.",
#     )
#     assert eval_bad.should_apply is False
#     print(f"  ✅  Bad fit model:  {eval_bad.summary()}")

#     # ── Test 2: Score clamping ───────────────────────────────────────────────
#     print("\n[2] Score clamping validator")
#     e = JobEvaluation(is_backend_role=True, fit_score=150)
#     assert e.fit_score == 100, f"Expected 100, got {e.fit_score}"
#     e2 = JobEvaluation(is_backend_role=True, fit_score=-10)
#     assert e2.fit_score == 0
#     print("  ✅  Score clamping works (150→100, -10→0)")

#     # ── Test 3: Stack normalisation ──────────────────────────────────────────
#     print("\n[3] Tech stack normalisation")
#     e3 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack="Python, FastAPI, Redis")
#     assert "Python" in e3.core_tech_stack
#     assert len(e3.core_tech_stack) == 3
#     print(f"  ✅  String → list: {e3.core_tech_stack}")

#     e4 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack='["Go","gRPC"]')
#     assert e4.core_tech_stack == ["Go", "gRPC"]
#     print(f"  ✅  JSON string → list: {e4.core_tech_stack}")

#     # ── Test 4: GateCheck ────────────────────────────────────────────────────
#     print("\n[4] GateCheck logic")
#     gate_pass = GateCheck(is_tech_job=True, is_english=True, has_content=True)
#     assert gate_pass.passes
#     gate_fail = GateCheck(is_tech_job=False, is_english=True, has_content=True)
#     assert not gate_fail.passes
#     print("  ✅  GateCheck.passes logic correct")

#     # ── Test 5: Ollama health check (optional — needs running Ollama) ────────
#     print("\n[5] Ollama health check")
#     is_healthy = check_ollama_health()
#     if is_healthy:
#         print("  ✅  Ollama is running and model is available")
#         print("\n[6] Live LLM evaluation (backend JD)")
#         eval_result, status = evaluate_with_gate(MOCK_JD_BACKEND, "Senior Python Backend Engineer")
#         if eval_result:
#             print(f"  ✅  {eval_result.summary()}")
#             print(f"     Strengths: {eval_result.matching_strengths}")
#             print(f"     Gaps:      {eval_result.potential_gaps}")
#         else:
#             print(f"  ❌  Evaluation failed: {status}")

#         print("\n[7] Live LLM evaluation (irrelevant JD)")
#         eval_result2, status2 = evaluate_with_gate(MOCK_JD_IRRELEVANT, "Senior Marketing Manager")
#         if eval_result2 is None:
#             print(f"  ✅  Correctly rejected: {status2}")
#         else:
#             print(f"  ⚠️   Unexpected pass with score {eval_result2.fit_score}")
#     else:
#         print("  ⚠️   Ollama not running — skipping live LLM tests")
#         print("       Start Ollama: ollama serve")
#         print(f"       Pull model:   ollama pull {settings.ollama_model}")

#     print("\n" + "=" * 60)
#     print("All schema/logic tests passed ✅")
#     print("=" * 60)

"""
evaluator.py
============
NODE 3 of the Job Hunter pipeline.

Connects to the Groq API and uses the `instructor` library to extract
a strict Pydantic model from raw job description text.

Flow:
  raw JD text
      │
      ▼
  Groq (llama3-70b-8192)
      │
      ▼
  instructor (structured JSON output)
      │
      ▼
  JobEvaluation(fit_score=87, is_backend_role=True, ...)
      │
      ├── fit_score < 75  →  drop
      └── fit_score >= 75 →  pass to resume generator

Design notes:
  - instructor patches the Groq client for guaranteed Pydantic validation
    with automatic retry on bad JSON.
  - Two LLM calls per job:
      1. Fast "gate" check  (is this even a tech/backend role?)  ~0.3s
      2. Full evaluation    (score, stack, fit reasoning)        ~1-2s
  - Groq is free-tier with generous rate limits (~30 req/min on free).
    Add GROQ_API_KEY to your .env file.
    Get a key at: https://console.groq.com
"""

import asyncio
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Optional

import instructor
from openai import OpenAI          # used for Ollama's OpenAI-compatible endpoint
from pydantic import BaseModel, Field, field_validator

from config import settings

# Groq is optional — only imported if llm_backend == "groq"
try:
    from groq import Groq as _Groq
except ImportError:
    _Groq = None  # type: ignore

logger = logging.getLogger("evaluator")


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class SeniorityLevel(str, Enum):
    INTERN     = "intern"
    JUNIOR     = "junior"
    MID        = "mid"
    SENIOR     = "senior"
    LEAD       = "lead"
    UNKNOWN    = "unknown"


class JobEvaluation(BaseModel):
    """
    Structured evaluation of a single job posting.
    Returned by the LLM for every job that passes the gate check.
    """

    # ── Core decision fields ─────────────────────────────────────────────────
    is_backend_role: bool = Field(
        description=(
            "True if the role primarily involves backend/server-side work: "
            "APIs, data pipelines, automation, ML infrastructure, DevOps. "
            "False for frontend-only, design, sales, or non-technical roles."
        )
    )

    fit_score: int = Field(
        ge=0, le=100,
        description=(
            "0–100 relevance score for a Python developer with 1.5 years experience "
            "specialising in REST APIs, data pipelines, automation, and basic ML. "
            "Score higher for: Python, FastAPI/Django/Flask, async, data engineering, "
            "automation, LLM/AI tooling, startup culture, remote-friendly. "
            "Score lower for: Java/C++/.NET only, 5+ years required, "
            "pure frontend, enterprise legacy stack."
        )
    )

    # ── Tech intelligence ────────────────────────────────────────────────────
    core_tech_stack: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit technologies, frameworks, and tools mentioned in the JD. "
            "Examples: ['Python', 'FastAPI', 'PostgreSQL', 'Redis', 'Docker']. "
            "Include only things actually stated — do not infer."
        )
    )

    required_years_experience: Optional[int] = Field(
        default=None,
        description="Minimum years of experience explicitly required. None if not stated."
    )

    seniority_level: SeniorityLevel = Field(
        default=SeniorityLevel.UNKNOWN,
        description="Inferred seniority level of the role."
    )

    # ── Fit analysis ─────────────────────────────────────────────────────────
    matching_strengths: list[str] = Field(
        default_factory=list,
        description=(
            "Specific reasons why this candidate IS a good fit. "
            "E.g. ['Python required and candidate is strong in Python', "
            "'startup culture matches candidate background']"
        )
    )

    potential_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "Honest gaps or risks. E.g. ['Role requires 3yr exp, candidate has 1.5', "
            "'Kubernetes mentioned — candidate may need to upskill']"
        )
    )

    rejection_reason: str = Field(
        default="",
        description=(
            "If fit_score < 75, a one-sentence explanation of the main disqualifier. "
            "Empty string if fit_score >= 75."
        )
    )

    # ── Opportunity signals ──────────────────────────────────────────────────
    is_remote_friendly: Optional[bool] = Field(
        default=None,
        description="True if remote/hybrid mentioned, False if on-site only, None if unclear."
    )

    has_equity: Optional[bool] = Field(
        default=None,
        description="True if equity/stock options mentioned."
    )

    # ── Validators ───────────────────────────────────────────────────────────
    @field_validator("core_tech_stack", mode="before")
    @classmethod
    def normalise_stack(cls, v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = [x.strip() for x in v.split(",") if x.strip()]
        return [str(item).strip() for item in (v or [])]

    @field_validator("fit_score", mode="before")
    @classmethod
    def clamp_score(cls, v):
        try:
            return max(0, min(100, int(v)))
        except (TypeError, ValueError):
            return 0

    @property
    def should_apply(self) -> bool:
        return self.fit_score >= settings.fit_score_threshold and self.is_backend_role

    def summary(self) -> str:
        """One-line human-readable summary for logging."""
        stack = ", ".join(self.core_tech_stack[:5]) or "unknown stack"
        return (
            f"score={self.fit_score} | backend={self.is_backend_role} | "
            f"seniority={self.seniority_level.value} | "
            f"stack=[{stack}] | apply={self.should_apply}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Gate check model  (fast, cheap first-pass filter)
# ─────────────────────────────────────────────────────────────────────────────

class GateCheck(BaseModel):
    """Quick first-pass: is this even a real job posting worth evaluating?"""
    is_tech_job:           bool = Field(description="True if this is a software/data/engineering role.")
    is_english:            bool = Field(description="True if the job description is in English.")
    has_content:           bool = Field(description="True if the description has enough content to evaluate (>100 words).")
    is_actual_job_posting: bool = Field(
        description=(
            "True ONLY if this is a real job opening for a specific hire. "
            "False if it is a company service page, product description, agency offering, "
            "capability page, or marketing content. "
            "Look for: 'Apply', 'Requirements', 'Responsibilities', 'We are looking for', "
            "'Join our team' — these signal real postings. "
            "Service pages often say 'We offer', 'Our team provides', 'Hire us' — these are NOT jobs."
        )
    )

    @property
    def passes(self) -> bool:
        return (
            self.is_tech_job
            and self.is_english
            and self.has_content
            and self.is_actual_job_posting
        )


class CombinedEvaluation(BaseModel):
    """
    Single-call model used by the merged gate+eval prompt (Ollama path).
    If pass_gate is False, only gate_reason is populated.
    """
    pass_gate: bool = Field(description="False = reject immediately, skip all other fields.")
    gate_reason: str = Field(default="", description="Why gated out, if pass_gate is false.")

    # These mirror JobEvaluation exactly so we can convert easily
    is_backend_role: bool = Field(default=False)
    fit_score: int = Field(default=0, ge=0, le=100)
    core_tech_stack: list[str] = Field(default_factory=list)
    required_years_experience: Optional[int] = Field(default=None)
    seniority_level: SeniorityLevel = Field(default=SeniorityLevel.UNKNOWN)
    matching_strengths: list[str] = Field(default_factory=list)
    potential_gaps: list[str] = Field(default_factory=list)
    rejection_reason: str = Field(default="")
    is_remote_friendly: Optional[bool] = Field(default=None)
    has_equity: Optional[bool] = Field(default=None)

    @field_validator("core_tech_stack", mode="before")
    @classmethod
    def normalise_stack(cls, v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = [x.strip() for x in v.split(",") if x.strip()]
        return [str(item).strip() for item in (v or [])]

    @field_validator("fit_score", mode="before")
    @classmethod
    def clamp_score(cls, v):
        try:
            return max(0, min(100, int(v)))
        except (TypeError, ValueError):
            return 0

    def to_job_evaluation(self) -> "JobEvaluation":
        """Convert to JobEvaluation for downstream compatibility."""
        return JobEvaluation(
            is_backend_role=self.is_backend_role,
            fit_score=self.fit_score,
            core_tech_stack=self.core_tech_stack,
            required_years_experience=self.required_years_experience,
            seniority_level=self.seniority_level,
            matching_strengths=self.matching_strengths,
            potential_gaps=self.potential_gaps,
            rejection_reason=self.rejection_reason,
            is_remote_friendly=self.is_remote_friendly,
            has_equity=self.has_equity,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Regex-based pre-gate (zero LLM cost — runs before any model call)
# ─────────────────────────────────────────────────────────────────────────────

import re as _re

_TECH_SIGNALS = _re.compile(
    r"\b(python|backend|fastapi|django|flask|node|java|golang|rust|devops|"
    r"data\s+engineer|ml\s+engineer|software\s+engineer|software\s+developer|"
    r"full[\s-]?stack|automation|api|rest|grpc|microservice|kubernetes|docker)\b",
    _re.IGNORECASE,
)
_SERVICE_PAGE_SIGNALS = _re.compile(
    r"\b(we\s+offer|our\s+services|hire\s+(our|us)|outsourc|staff\s+augmentation|"
    r"dedicated\s+team|custom\s+software\s+development)\b",
    _re.IGNORECASE,
)
_JOB_POSTING_SIGNALS = _re.compile(
    r"\b(we\s+(are|re)\s+(hiring|looking|seeking)|join\s+(our|the)\s+team|"
    r"you\s+will|responsibilities|requirements|apply\s+(now|today|here)|"
    r"about\s+the\s+role|competitive\s+salary)\b",
    _re.IGNORECASE,
)

def regex_prefilter(jd_text: str) -> tuple[bool, str]:
    """
    Zero-cost pre-gate using regex only. Runs before any LLM call.
    Returns (passes, reason).

    Catches the easy rejects immediately:
      - Too short to be a real job
      - No tech keywords at all
      - Looks like a service/outsourcing page
    Borderline cases are passed through to the LLM gate.
    """
    text = jd_text[:1200]

    if len(jd_text.strip()) < 120:
        return False, "description too short"

    if not _TECH_SIGNALS.search(text):
        return False, "no tech keywords found"

    service_hits = len(_SERVICE_PAGE_SIGNALS.findall(text))
    job_hits     = len(_JOB_POSTING_SIGNALS.findall(text))
    if service_hits >= 2 and job_hits == 0:
        return False, "service/agency page (regex)"

    return True, "ok"


# ── Single merged prompt — one LLM call does gate + eval together ─────────────
# Splitting into two calls doubles latency on local models. A single prompt with
# an explicit "pass: false" short-circuit path is faster and equally accurate.

COMBINED_SYSTEM = """You are a technical recruiter AI. Output only valid JSON, no explanation or markdown.

First, decide if this is worth evaluating:
- pass_gate: true if this is a real tech/engineering job posting in English with enough content.
  false for service pages, outsourcing ads, non-tech roles, or non-English text.

If pass_gate is false, return ONLY:
{"pass_gate": false, "gate_reason": "one-sentence reason"}

If pass_gate is true, also return the full evaluation:

CANDIDATE:
- Python developer, 1.5 years experience, startup background
- Strong: Python, FastAPI, Flask, REST APIs, async, data pipelines, automation
- Knows: LLMs, prompt engineering, basic ML, PostgreSQL, Redis, Docker
- Target: junior-mid backend / data-engineering / automation roles
- Location: Yerevan, Armenia — needs remote or remote-friendly

SCORING — use the full range, do not default to 75-80:
95: Python-first, 1-3yr required, fully remote, modern stack
85: Backend role, Python listed, seniority gap ≤1yr, hybrid ok
75: Python present but secondary, OR 3-5yr required, OR on-site only
60: Python barely mentioned OR 5+yr required OR wrong primary language
40: Java/.NET/Go only, pure frontend, non-technical, 7+yr senior
20: Completely unrelated domain or language

ANCHORS (use these to calibrate your score):
- "Junior Python Backend, FastAPI, remote" → 92
- "Backend Engineer, Python/Go, 2-4yr, hybrid" → 80
- "Senior Python Engineer, 5yr required, on-site SF" → 62
- "Full Stack, React + Node.js, 3yr" → 45
- "Java Spring Boot Senior, 7yr" → 22"""

COMBINED_USER = """JOB TITLE: {job_title}

JOB DESCRIPTION (first 1500 chars):
{jd_text}

Return ONLY valid JSON:
{{
  "pass_gate": true,
  "is_backend_role": bool,
  "fit_score": integer 0-100,
  "core_tech_stack": ["tech1", "tech2"],
  "required_years_experience": integer or null,
  "seniority_level": "intern|junior|mid|senior|lead|unknown",
  "matching_strengths": ["strength1"],
  "potential_gaps": ["gap1"],
  "rejection_reason": "one sentence if score < 75, else empty string",
  "is_remote_friendly": true/false/null,
  "has_equity": true/false/null
}}"""

# Legacy separate prompts kept for Groq (two-call path is fine on cloud):
GATE_SYSTEM = """You are a job classifier. Output only valid JSON, no explanation.
Rules:
- is_tech_job: true only for software/data/ML/DevOps/automation engineering roles
- is_actual_job_posting: true only if the text describes an open position to apply to.
  False for service pages, outsourcing ads, capability pages, or "hire our team" content."""

GATE_USER = """Classify this text. Return ONLY this JSON with no other text:
{{"is_tech_job": bool, "is_english": bool, "has_content": bool, "is_actual_job_posting": bool}}

TEXT:
{snippet}"""

EVAL_SYSTEM = """You are a technical recruiter. Output only valid JSON, no explanation or markdown.

CANDIDATE:
- Python developer, 1.5 years professional experience, startup background
- Strong: Python, FastAPI, Flask, REST APIs, async, data pipelines, automation
- Knows: LLMs, prompt engineering, basic ML, PostgreSQL, Redis, Docker
- Target: junior-mid backend / data-engineering / automation roles
- Location: Yerevan, Armenia — needs remote or remote-friendly

SCORING — be precise, do not round to 80 or 90 by default:
95: Python-first, 1-3yr experience required, fully remote, interesting tech stack
85: Backend role, Python listed, seniority gap ≤1yr, hybrid ok
75: Python present but not primary, OR 3-5yr required, OR on-site only
60: Python barely mentioned, OR 5+yr required, OR wrong language primary
40: Java/.NET/Go only, pure frontend, non-technical, or 7+yr senior role
20: Completely unrelated domain or language

WORKED EXAMPLES (use as anchors):
- "Junior Python Backend Engineer, FastAPI, remote" → score 92
- "Backend Engineer, Python/Go, 2-4yr, hybrid" → score 80
- "Senior Python Engineer, 5yr required, on-site SF" → score 62
- "Full Stack Engineer, React + Node.js, 3yr" → score 45
- "Java Spring Boot Senior Developer, 7yr" → score 22"""

EVAL_USER = """Evaluate this job posting for the candidate above.

JOB TITLE: {job_title}

JOB DESCRIPTION:
{jd_text}

Return ONLY this JSON object with no other text:
{{
  "is_backend_role": bool,
  "fit_score": integer 0-100,
  "core_tech_stack": ["list", "of", "tech", "from", "JD"],
  "required_years_experience": integer or null,
  "seniority_level": "intern|junior|mid|senior|lead|unknown",
  "matching_strengths": ["reason1", "reason2"],
  "potential_gaps": ["gap1", "gap2"],
  "rejection_reason": "one sentence if score < 75, else empty string",
  "is_remote_friendly": true/false/null,
  "has_equity": true/false/null
}}"""


# ─────────────────────────────────────────────────────────────────────────────
# Backend-aware client factory  (Groq OR local Ollama)
# ─────────────────────────────────────────────────────────────────────────────

def _make_instructor_client() -> instructor.Instructor:
    """
    Create an instructor-patched LLM client based on settings.llm_backend.

    "groq"   → Groq cloud API  (fast, precise, rate-limited)
    "ollama" → Local Ollama     (unlimited, slightly less precise on small models)

    instructor guarantees Pydantic-validated JSON output with automatic retries.
    """
    backend = settings.llm_backend.lower()

    if backend == "groq":
        if _Groq is None:
            raise RuntimeError("groq package not installed. Run: pip install groq")
        raw = _Groq(api_key=settings.groq_api_key)
        return instructor.from_groq(raw, mode=instructor.Mode.JSON)

    # Ollama — uses the OpenAI-compatible /v1 endpoint
    raw = OpenAI(
        base_url=f"{settings.ollama_base_url}/v1",
        api_key="ollama",   # Ollama ignores this but openai SDK requires it
    )
    return instructor.from_openai(raw, mode=instructor.Mode.JSON)


def _active_model() -> str:
    """Return the model name string for the currently active backend."""
    return (
        settings.groq_model
        if settings.llm_backend.lower() == "groq"
        else settings.ollama_model
    )


# Singleton — recreated if backend changes between calls (shouldn't happen in prod)
_client: instructor.Instructor | None = None
_client_backend: str = ""

def get_client() -> instructor.Instructor:
    global _client, _client_backend
    backend = settings.llm_backend.lower()
    if _client is None or _client_backend != backend:
        _client = _make_instructor_client()
        _client_backend = backend
        logger.info(f"LLM client initialised | backend={backend} | model={_active_model()}")
    return _client


# ─────────────────────────────────────────────────────────────────────────────
# Core evaluation functions
# ─────────────────────────────────────────────────────────────────────────────

def run_gate_check(jd_text: str) -> GateCheck:
    """
    Fast first-pass filter.
    Rejects non-tech jobs, non-English, or empty descriptions before
    wasting the slower full-eval call.
    """
    snippet = jd_text[:800].replace("\n", " ").strip()

    try:
        result = get_client().chat.completions.create(
            model=_active_model(),
            response_model=GateCheck,
            max_retries=3,           # local models sometimes need an extra retry
            messages=[
                {"role": "system", "content": GATE_SYSTEM},
                {"role": "user",   "content": GATE_USER.format(snippet=snippet)},
            ],
            temperature=0.0,         # deterministic — gate is binary
        )
        logger.debug(
            f"Gate check [{settings.llm_backend}]: "
            f"tech={result.is_tech_job} en={result.is_english} "
            f"content={result.has_content} posting={result.is_actual_job_posting}"
        )
        return result

    except Exception as exc:
        logger.warning(f"Gate check LLM call failed ({exc}), defaulting to conservative reject")
        return GateCheck(
            is_tech_job=True,
            is_english=True,
            has_content=True,
            is_actual_job_posting=False,
        )


def evaluate_job(jd_text: str, job_title: str = "") -> JobEvaluation:
    """
    Full structured evaluation of a job description.

    Args:
        jd_text:   Raw text of the job posting (max ~6000 chars from crawler)
        job_title: Optional title hint — passed directly into the prompt so the
                   model can use it as a strong signal before reading the full JD.

    Returns:
        JobEvaluation Pydantic model (always valid — instructor ensures this)

    Raises:
        RuntimeError if the LLM backend is unreachable after retries
    """
    if not jd_text or len(jd_text.strip()) < 50:
        logger.warning("JD too short to evaluate, returning zero-score result")
        return JobEvaluation(
            is_backend_role=False,
            fit_score=0,
            rejection_reason="Job description too short or empty to evaluate.",
        )

    # Local 8b models handle ~4k chars well; 70b cloud models handle 6k+
    max_chars = 4000 if settings.llm_backend.lower() == "ollama" else 5500
    jd_truncated = jd_text[:max_chars]

    start = time.perf_counter()

    try:
        result: JobEvaluation = get_client().chat.completions.create(
            model=_active_model(),
            response_model=JobEvaluation,
            max_retries=4,              # local models need more patience
            messages=[
                {"role": "system", "content": EVAL_SYSTEM},
                {"role": "user",   "content": EVAL_USER.format(
                    job_title=job_title or "Not specified",
                    jd_text=jd_truncated,
                )},
            ],
            temperature=0.1,            # low temp = consistent, comparable scores
        )

        elapsed = time.perf_counter() - start
        logger.info(
            f"Evaluation complete in {elapsed:.1f}s "
            f"[{settings.llm_backend}/{_active_model()}] | {result.summary()}"
        )
        return result

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.error(f"Evaluation failed after {elapsed:.1f}s: {exc}")
        raise RuntimeError(f"LLM evaluation failed ({settings.llm_backend}): {exc}") from exc


def evaluate_with_gate(jd_text: str, job_title: str = "") -> tuple[JobEvaluation | None, str]:
    """
    Two-stage evaluation pipeline:
      1. Gate check  (fast, cheap)
      2. Full eval   (only if gate passes)

    Returns:
        (JobEvaluation, "ok") on success
        (None, reason_string) if gated out or error
    """
    # Stage 1: Gate
    gate = run_gate_check(jd_text)
    if not gate.passes:
        reasons = []
        if not gate.is_tech_job:           reasons.append("not a tech role")
        if not gate.is_english:            reasons.append("non-English JD")
        if not gate.has_content:           reasons.append("insufficient content")
        if not gate.is_actual_job_posting: reasons.append("service/agency page, not a job posting")
        reason = ", ".join(reasons)
        logger.info(f"Gate check failed: {reason}")
        return None, f"gate_failed:{reason}"

    # Stage 2: Full evaluation
    try:
        evaluation = evaluate_job(jd_text, job_title)
        return evaluation, "ok"
    except RuntimeError as exc:
        return None, f"eval_error:{exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation cache  (skip re-evaluating identical JDs)
# ─────────────────────────────────────────────────────────────────────────────

_eval_cache: dict[str, tuple[JobEvaluation | None, str]] = {}

def _jd_hash(jd_text: str, job_title: str) -> str:
    """MD5 of the first 1500 chars of JD + title — cheap cache key."""
    key = f"{job_title}||{jd_text[:1500]}"
    return hashlib.md5(key.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Merged single-call path (Ollama) + parallel batch evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_with_gate_ollama(jd_text: str, job_title: str = "") -> tuple[JobEvaluation | None, str]:
    """
    Single-call gate+eval for local Ollama.

    Flow:
      1. Regex pre-filter (zero cost) — drops obvious non-jobs instantly
      2. One LLM call that does gate check AND scoring together
      3. Cache result by JD hash — identical jobs never re-evaluated

    Returns same signature as evaluate_with_gate() for drop-in compatibility.
    """
    # Fast regex pre-filter — no LLM cost at all
    passes, reason = regex_prefilter(jd_text)
    if not passes:
        logger.info(f"Regex prefilter rejected: {reason}")
        return None, f"gate_failed:{reason}"

    # Cache lookup
    cache_key = _jd_hash(jd_text, job_title)
    if cache_key in _eval_cache:
        logger.debug(f"Cache hit for job: {job_title!r}")
        return _eval_cache[cache_key]

    # Truncate to 1500 chars — captures requirements/responsibilities,
    # skips boilerplate benefits text at the end which adds no scoring signal
    jd_short = jd_text[:1500]

    start = time.perf_counter()
    try:
        result: CombinedEvaluation = get_client().chat.completions.create(
            model=_active_model(),
            response_model=CombinedEvaluation,
            max_retries=4,
            messages=[
                {"role": "system", "content": COMBINED_SYSTEM},
                {"role": "user",   "content": COMBINED_USER.format(
                    job_title=job_title or "Not specified",
                    jd_text=jd_short,
                )},
            ],
            temperature=0.1,
        )
        elapsed = time.perf_counter() - start
        logger.info(f"Ollama eval in {elapsed:.1f}s | pass={result.pass_gate} score={result.fit_score}")

        if not result.pass_gate:
            out = (None, f"gate_failed:{result.gate_reason or 'LLM rejected'}")
        else:
            job_eval = result.to_job_evaluation()
            if not job_eval.should_apply:
                out = (job_eval, "ok")   # keep result for DB even if below threshold
            else:
                out = (job_eval, "ok")

        _eval_cache[cache_key] = out
        return out

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.error(f"Ollama eval failed after {elapsed:.1f}s: {exc}")
        return None, f"eval_error:{exc}"


def evaluate_with_gate(jd_text: str, job_title: str = "") -> tuple[JobEvaluation | None, str]:
    """
    Backend-aware dispatcher.
    - ollama → merged single-call path (fast, cached, regex pre-filtered)
    - groq   → original two-call path (gate then full eval)
    """
    if settings.llm_backend.lower() == "ollama":
        return evaluate_with_gate_ollama(jd_text, job_title)

    # ── Groq two-call path ────────────────────────────────────────────────────
    # Cache check (applies to both backends)
    cache_key = _jd_hash(jd_text, job_title)
    if cache_key in _eval_cache:
        logger.debug(f"Cache hit (groq): {job_title!r}")
        return _eval_cache[cache_key]

    gate = run_gate_check(jd_text)
    if not gate.passes:
        reasons = []
        if not gate.is_tech_job:           reasons.append("not a tech role")
        if not gate.is_english:            reasons.append("non-English JD")
        if not gate.has_content:           reasons.append("insufficient content")
        if not gate.is_actual_job_posting: reasons.append("service/agency page")
        reason = ", ".join(reasons)
        logger.info(f"Gate check failed: {reason}")
        out = (None, f"gate_failed:{reason}")
        _eval_cache[cache_key] = out
        return out

    try:
        evaluation = evaluate_job(jd_text, job_title)
        out = (evaluation, "ok")
        _eval_cache[cache_key] = out
        return out
    except RuntimeError as exc:
        return None, f"eval_error:{exc}"


def batch_evaluate_parallel(
    jobs: list[dict],
    threshold: int | None = None,
    max_workers: int = 3,
) -> list[dict]:
    """
    Parallel batch evaluator — runs up to `max_workers` evaluations concurrently
    using a thread pool. Safe for both Groq and Ollama.

    For Ollama: max_workers=2 is recommended (limited by VRAM/CPU).
    For Groq:   max_workers=3-5 is fine (limited by rate limit ~30 req/min).

    Returns same format as batch_evaluate().
    """
    cutoff   = threshold if threshold is not None else settings.fit_score_threshold
    passed:  list[dict] = []
    lock     = __import__("threading").Lock()

    def _eval_one(job: dict) -> dict | None:
        title = job.get("title", "unknown")
        logger.info(f"Evaluating: {title!r}")
        evaluation, status = evaluate_with_gate(
            jd_text=job.get("description", ""),
            job_title=title,
        )
        if evaluation is None:
            logger.info(f"  ✗ Dropped ({status})")
            return None
        if not evaluation.should_apply:
            logger.info(f"  ✗ Score {evaluation.fit_score} < {cutoff} — {evaluation.rejection_reason}")
            return None
        logger.info(f"  ✓ PASSED — {evaluation.summary()}")
        return {
            **job,
            "fit_score":          evaluation.fit_score,
            "is_backend":         evaluation.is_backend_role,
            "tech_stack":         json.dumps(evaluation.core_tech_stack),
            "rejection_reason":   evaluation.rejection_reason,
            "seniority":          evaluation.seniority_level.value,
            "required_years":     evaluation.required_years_experience,
            "matching_strengths": json.dumps(evaluation.matching_strengths),
            "potential_gaps":     json.dumps(evaluation.potential_gaps),
            "is_remote_friendly": evaluation.is_remote_friendly,
            "has_equity":         evaluation.has_equity,
            "_evaluation":        evaluation,
        }

    workers = max_workers if settings.llm_backend.lower() == "groq" else min(max_workers, 2)
    logger.info(f"Batch evaluating {len(jobs)} jobs | backend={settings.llm_backend} | workers={workers}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_eval_one, jobs))

    passed = [r for r in results if r is not None]
    logger.info(f"Batch complete: {len(passed)}/{len(jobs)} passed threshold {cutoff}")
    return passed


# ─────────────────────────────────────────────────────────────────────────────
# Health checks — backend-aware, used by main.py preflight
# ─────────────────────────────────────────────────────────────────────────────

def check_groq_health() -> bool:
    """Verify Groq API key is set and the API responds."""
    if _Groq is None:
        logger.error("groq package not installed. Run: pip install groq")
        return False
    if not settings.groq_api_key or settings.groq_api_key == "CHANGE_ME":
        logger.error("GROQ_API_KEY not set in .env — get a free key at https://console.groq.com")
        return False
    try:
        client = _Groq(api_key=settings.groq_api_key)
        client.chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        logger.info(f"Groq health check OK | model={settings.groq_model}")
        return True
    except Exception as exc:
        logger.error(f"Groq API unreachable: {exc}")
        return False


def check_ollama_health() -> bool:
    """Verify Ollama is running locally and the configured model is pulled."""
    import httpx
    try:
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        if resp.status_code != 200:
            logger.error(f"Ollama API returned {resp.status_code}")
            return False
        models = [m["name"] for m in resp.json().get("models", [])]
        model_base = settings.ollama_model.split(":")[0]
        if not any(model_base in m for m in models):
            logger.warning(
                f"Model '{settings.ollama_model}' not found in Ollama. "
                f"Available: {models}. Run: ollama pull {settings.ollama_model}"
            )
            return False
        logger.info(f"Ollama health OK | model={settings.ollama_model}")
        return True
    except Exception as exc:
        logger.error(f"Ollama unreachable at {settings.ollama_base_url}: {exc}")
        return False


def check_llm_health() -> bool:
    """
    Check whichever backend is currently active (settings.llm_backend).
    This is what main.py preflight_task should call — it handles both backends
    without needing to know which one is in use.
    """
    backend = settings.llm_backend.lower()
    if backend == "groq":
        return check_groq_health()
    if backend == "ollama":
        return check_ollama_health()
    logger.error(f"Unknown llm_backend: {backend!r} — must be 'groq' or 'ollama'")
    return False

# ─────────────────────────────────────────────────────────────────────────────

def batch_evaluate(
    jobs: list[dict],
    threshold: int = None,
) -> list[dict]:
    """Legacy alias → now calls the parallel version. Drop-in compatible."""
    return batch_evaluate_parallel(jobs, threshold=threshold)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point — test with mock data (no Ollama needed for schema test)
# ─────────────────────────────────────────────────────────────────────────────

MOCK_JD_BACKEND = """
Senior Python Backend Engineer – Remote
Acme Data Inc.

About the Role:
We're looking for a backend engineer to join our small, fast-moving team building
data infrastructure for our SaaS platform.

Responsibilities:
- Design and maintain REST APIs using FastAPI and async Python
- Build and maintain data pipelines with Apache Airflow
- Work with PostgreSQL, Redis, and S3-compatible object storage
- Contribute to our internal LLM-powered automation tooling
- Deploy and monitor services on Kubernetes (nice to have)

Requirements:
- 1-3 years of Python backend experience
- Strong understanding of REST API design
- Experience with SQL databases (PostgreSQL preferred)
- Comfortable with Docker and basic DevOps
- Startup experience preferred — we move fast

Nice to Have:
- FastAPI or Django REST Framework
- Experience with LLM APIs (OpenAI, Anthropic)
- Redis / Celery for async task queues

We're remote-first with competitive salary and equity.
"""

MOCK_JD_IRRELEVANT = """
Senior Marketing Manager – New York (On-site)

We are looking for an experienced marketing manager to lead our
brand strategy and social media campaigns. Must have 5+ years in
digital marketing, strong copywriting skills, and experience with
Salesforce CRM. No technical background required.
"""


if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("EVALUATOR — Schema & Logic Tests (no Ollama required)")
    print("=" * 60)

    # ── Test 1: Pydantic model validation ────────────────────────────────────
    print("\n[1] Pydantic model validation")

    eval_good = JobEvaluation(
        is_backend_role=True,
        fit_score=87,
        core_tech_stack=["Python", "FastAPI", "PostgreSQL"],
        seniority_level=SeniorityLevel.MID,
        matching_strengths=["Python required", "remote friendly"],
        potential_gaps=["Kubernetes is nice-to-have"],
        is_remote_friendly=True,
        has_equity=True,
    )
    assert eval_good.should_apply is True
    assert eval_good.fit_score == 87
    print(f"  ✅  Good fit model: {eval_good.summary()}")

    eval_bad = JobEvaluation(
        is_backend_role=False,
        fit_score=30,
        rejection_reason="Marketing role, no engineering component.",
    )
    assert eval_bad.should_apply is False
    print(f"  ✅  Bad fit model:  {eval_bad.summary()}")

    # ── Test 2: Score clamping ───────────────────────────────────────────────
    print("\n[2] Score clamping validator")
    e = JobEvaluation(is_backend_role=True, fit_score=150)
    assert e.fit_score == 100, f"Expected 100, got {e.fit_score}"
    e2 = JobEvaluation(is_backend_role=True, fit_score=-10)
    assert e2.fit_score == 0
    print("  ✅  Score clamping works (150→100, -10→0)")

    # ── Test 3: Stack normalisation ──────────────────────────────────────────
    print("\n[3] Tech stack normalisation")
    e3 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack="Python, FastAPI, Redis")
    assert "Python" in e3.core_tech_stack
    assert len(e3.core_tech_stack) == 3
    print(f"  ✅  String → list: {e3.core_tech_stack}")

    e4 = JobEvaluation(is_backend_role=True, fit_score=80, core_tech_stack='["Go","gRPC"]')
    assert e4.core_tech_stack == ["Go", "gRPC"]
    print(f"  ✅  JSON string → list: {e4.core_tech_stack}")

    # ── Test 4: GateCheck ────────────────────────────────────────────────────
    print("\n[4] GateCheck logic")
    gate_pass = GateCheck(is_tech_job=True, is_english=True, has_content=True, is_actual_job_posting=True)
    assert gate_pass.passes
    gate_fail = GateCheck(is_tech_job=False, is_english=True, has_content=True, is_actual_job_posting=True)
    assert not gate_fail.passes
    print("  ✅  GateCheck.passes logic correct")

    # ── Test 5: Groq health check (needs GROQ_API_KEY in .env) ──────────────
    print("\n[5] Groq API health check")
    is_healthy = check_groq_health()
    if is_healthy:
        print(f"  ✅  Groq is reachable | model={settings.groq_model}")
        print("\n[6] Live LLM evaluation (backend JD)")
        eval_result, status = evaluate_with_gate(MOCK_JD_BACKEND, "Senior Python Backend Engineer")
        if eval_result:
            print(f"  ✅  {eval_result.summary()}")
            print(f"     Strengths: {eval_result.matching_strengths}")
            print(f"     Gaps:      {eval_result.potential_gaps}")
        else:
            print(f"  ❌  Evaluation failed: {status}")

        print("\n[7] Live LLM evaluation (irrelevant JD)")
        eval_result2, status2 = evaluate_with_gate(MOCK_JD_IRRELEVANT, "Senior Marketing Manager")
        if eval_result2 is None:
            print(f"  ✅  Correctly rejected: {status2}")
        else:
            print(f"  ⚠️   Unexpected pass with score {eval_result2.fit_score}")
    else:
        print("  ⚠️   Groq not available — skipping live LLM tests")
        print("       Add to .env:  GROQ_API_KEY=gsk_...")
        print("       Free key at:  https://console.groq.com")

    print("\n" + "=" * 60)
    print("All schema/logic tests passed ✅")
    print("=" * 60)
