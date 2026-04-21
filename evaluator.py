"""
evaluator.py
============

Replaces the previous LLM-based evaluator with a sentence-transformer
similarity approach.

This module compares the candidate's master resume against extracted job
sections using the `all-MiniLM-L6-v2` model.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator
from sentence_transformers import SentenceTransformer, util

from config import settings

logger = logging.getLogger("evaluator")

MODEL_NAME = "all-MiniLM-L6-v2"
_model: Optional[SentenceTransformer] = None
_resume_embedding: Optional[Any] = None
_resume_text: Optional[str] = None

BACKEND_KEYWORDS = [
    "backend",
    "api",
    "server",
    "database",
    "data pipeline",
    "data engineering",
    "cloud",
    "microservice",
    "container",
    "docker",
    "kubernetes",
    "devops",
    "etl",
    "pipeline",
    "automation",
    "python",
    "fastapi",
    "django",
    "flask",
    "sql",
    "postgres",
    "postgresql",
    "redis",
    "sre",
    "infrastructure",
]

TECH_TERMS = [
    "Python",
    "FastAPI",
    "Django",
    "Flask",
    "SQL",
    "PostgreSQL",
    "Redis",
    "Docker",
    "Kubernetes",
    "AWS",
    "GCP",
    "Azure",
    "Kafka",
    "Celery",
    "RabbitMQ",
    "MongoDB",
    "ElasticSearch",
    "Kafka",
    "REST",
    "GraphQL",
    "CI/CD",
    "Linux",
    "API",
    "Data Engineering",
]


class JobEvaluation(BaseModel):
    is_backend_role: bool = Field(
        description="True if the job appears to be a backend/technical role."
    )
    fit_score: int = Field(
        ge=0,
        le=100,
        description="Similarity-based fit score between resume and job description."
    )
    core_tech_stack: List[str] = Field(
        default_factory=list,
        description="Tech keywords extracted from the job description."
    )
    rejection_reason: str = Field(
        default="",
        description="Why the job was considered lower fit or gated out."
    )
    matching_strengths: List[str] = Field(
        default_factory=list,
        description="Short explanation of strengths found in the match."
    )
    potential_gaps: List[str] = Field(
        default_factory=list,
        description="Potential weaknesses or gaps relative to the candidate profile."
    )

    @field_validator("core_tech_stack", mode="before")
    @classmethod
    def normalize_stack(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return [item.strip() for item in value.split(",") if item.strip()]
        return value or []

    @property
    def should_apply(self) -> bool:
        return self.fit_score >= settings.fit_score_threshold and self.is_backend_role

    def summary(self) -> str:
        stack = ", ".join(self.core_tech_stack[:5]) or "unknown stack"
        return (
            f"score={self.fit_score} | backend={self.is_backend_role} | "
            f"stack=[{stack}] | apply={self.should_apply}"
        )


def _load_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _load_resume_text() -> str:
    global _resume_text
    if _resume_text is None:
        resume_path = Path(__file__).parent / "data" / "master_resume.json"
        if not resume_path.exists():
            raise FileNotFoundError(f"Missing resume data: {resume_path}")
        data = json.loads(resume_path.read_text(encoding="utf-8"))
        _resume_text = _flatten_resume_data(data)
    return _resume_text


def _flatten_resume_data(data: Any) -> str:
    pieces: List[str] = []
    if isinstance(data, dict):
        for value in data.values():
            pieces.append(_flatten_resume_data(value))
    elif isinstance(data, list):
        for item in data:
            pieces.append(_flatten_resume_data(item))
    elif data is not None:
        pieces.append(str(data))
    return "\n".join([line.strip() for line in "\n".join(pieces).splitlines() if line.strip()])


def _get_resume_embedding() -> Any:
    global _resume_embedding
    if _resume_embedding is None:
        model = _load_model()
        _resume_embedding = model.encode(
            _load_resume_text(),
            convert_to_tensor=True,
            normalize_embeddings=True,
        )
    return _resume_embedding


def _extract_sections(jd_text: str) -> List[str]:
    lines = [line.strip() for line in jd_text.splitlines()]
    sections: List[str] = []
    current: List[str] = []

    for line in lines:
        if not line:
            if current:
                sections.append(" ".join(current))
                current = []
            continue

        if len(current) > 0 and re.match(r"^[A-Z][A-Za-z0-9 \-/&]+$", line):
            sections.append(" ".join(current))
            current = [line]
        else:
            current.append(line)

    if current:
        sections.append(" ".join(current))

    sections = [section for section in sections if len(section) > 30]
    return sections or [jd_text.strip()]


def _job_similarity_score(jd_text: str) -> float:
    model = _load_model()
    resume_emb = _get_resume_embedding()
    sections = _extract_sections(jd_text)
    section_embeddings = model.encode(
        sections,
        convert_to_tensor=True,
        normalize_embeddings=True,
    )
    similarity_scores = util.cos_sim(resume_emb, section_embeddings)[0]
    max_similarity = float(similarity_scores.max()) if len(similarity_scores) else 0.0
    return max_similarity


def _extract_tech_stack(jd_text: str) -> List[str]:
    text = jd_text.lower()
    found: List[str] = []
    for term in TECH_TERMS:
        if term.lower() in text and term not in found:
            found.append(term)
    return found[:10]


def _is_backend_role(jd_text: str) -> bool:
    text = jd_text.lower()
    score = sum(1 for keyword in BACKEND_KEYWORDS if keyword in text)
    return score >= 2


def _build_strengths_and_gaps(evaluation: JobEvaluation, jd_text: str) -> None:
    if evaluation.is_backend_role:
        evaluation.matching_strengths.append("Job appears to target backend or technical work.")
    else:
        evaluation.potential_gaps.append("Job does not strongly mention backend or engineering responsibilities.")

    if evaluation.fit_score >= settings.fit_score_threshold:
        evaluation.matching_strengths.append("Strong semantic similarity to the master resume.")
    else:
        evaluation.potential_gaps.append("Resume similarity is below the configured threshold.")

    if evaluation.core_tech_stack:
        evaluation.matching_strengths.append(
            f"Extracted keywords: {', '.join(evaluation.core_tech_stack[:5])}."
        )


def evaluate_job(jd_text: str, job_title: str = "") -> JobEvaluation:
    job_text = f"{job_title}\n{jd_text}".strip()
    similarity = _job_similarity_score(job_text)
    fit_score = int(round(min(100.0, max(0.0, similarity * 100))))
    is_backend = _is_backend_role(job_text)

    if not is_backend:
        fit_score = max(0, fit_score - 20)

    tech_stack = _extract_tech_stack(job_text)
    rejection_reason = ""
    if fit_score < settings.fit_score_threshold:
        rejection_reason = "Job similarity below threshold or lacking clear backend signals."

    evaluation = JobEvaluation(
        is_backend_role=is_backend,
        fit_score=fit_score,
        core_tech_stack=tech_stack,
        rejection_reason=rejection_reason,
    )
    _build_strengths_and_gaps(evaluation, job_text)
    return evaluation


def evaluate_with_gate(jd_text: str, job_title: str = "") -> Tuple[Optional[JobEvaluation], str]:
    if len(jd_text.strip()) < 80:
        return None, "gated_out_short_description"

    if not _is_backend_role(jd_text) and len(_extract_tech_stack(jd_text)) < 2:
        return None, "gated_out_non_backend"

    try:
        evaluation = evaluate_job(jd_text, job_title)
        return evaluation, "ok"
    except Exception as exc:
        logger.exception("Job evaluation failed")
        return None, "error"


def _build_job_result(job: Dict[str, Any]) -> Dict[str, Any]:
    evaluation, status = evaluate_with_gate(job.get("description", ""), job.get("title", ""))
    if evaluation is None:
        return {
            "job_id": job.get("job_id"),
            "url": job.get("url"),
            "title": job.get("title"),
            "fit_score": 0,
            "is_backend": False,
            "tech_stack": [],
            "rejection_reason": status,
            "matching_strengths": [],
            "potential_gaps": [],
            "_evaluation": None,
        }

    return {
        "job_id": job.get("job_id"),
        "url": job.get("url"),
        "title": job.get("title"),
        "fit_score": evaluation.fit_score,
        "is_backend": int(evaluation.is_backend_role),
        "tech_stack": evaluation.core_tech_stack,
        "rejection_reason": evaluation.rejection_reason,
        "matching_strengths": evaluation.matching_strengths,
        "potential_gaps": evaluation.potential_gaps,
        "_evaluation": evaluation,
    }


def batch_evaluate_parallel(
    raw_jobs: List[Dict[str, Any]],
    max_workers: int = 4,
) -> List[Dict[str, Any]]:
    _load_model()
    _get_resume_embedding()

    if not raw_jobs:
        return []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(_build_job_result, raw_jobs))

    return results


def check_evaluator_health() -> bool:
    try:
        _load_model()
        _load_resume_text()
        return True
    except Exception as exc:
        logger.error(f"Evaluator health check failed: {exc}")
        return False
