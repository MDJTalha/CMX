"""Validated request and response contracts for the CMX decision engine."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class EvidenceStatus(StrEnum):
    """Provenance label supplied by a trusted catalogue workflow."""

    VERIFIED = "verified"
    USER_PROVIDED = "user_provided"
    ESTIMATED = "estimated"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


class Requirement(BaseModel):
    """A normalized capability requirement.

    Use stable machine keys (for example, email_integration) for matching.
    Free-text semantic matching is intentionally not performed by the core engine.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    label: str = Field(min_length=2, max_length=180)
    mandatory: bool = True
    weight: int = Field(default=3, ge=1, le=5)


class CandidateSolution(BaseModel):
    """A candidate solution provided by a trusted catalogue or assessment flow."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=180)
    category: str = Field(min_length=2, max_length=100)
    summary: str = Field(default="", max_length=1200)
    capability_keys: list[str] = Field(default_factory=list, max_length=100)
    unsupported_capability_keys: list[str] = Field(default_factory=list, max_length=100)
    supported_regions: list[str] = Field(default_factory=list, max_length=100)
    unsupported_regions: list[str] = Field(default_factory=list, max_length=100)
    setup_cost_usd: float | None = Field(default=None, ge=0)
    monthly_cost_usd: float | None = Field(default=None, ge=0)
    pricing_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    evidence_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    source_urls: list[HttpUrl] = Field(default_factory=list, max_length=30)
    evidence_notes: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def capability_claims_must_not_conflict(self) -> "CandidateSolution":
        overlap = set(self.capability_keys) & set(self.unsupported_capability_keys)
        if overlap:
            raise ValueError(
                "A capability cannot be both supported and unsupported: "
                + ", ".join(sorted(overlap))
            )
        region_overlap = set(self.supported_regions) & set(self.unsupported_regions)
        if region_overlap:
            raise ValueError(
                "A region cannot be both supported and unsupported: "
                + ", ".join(sorted(region_overlap))
            )
        return self


class DecisionRequest(BaseModel):
    """A bounded decision assessment request."""

    model_config = ConfigDict(extra="forbid")

    problem_statement: str = Field(min_length=10, max_length=4000)
    desired_outcome: str = Field(min_length=3, max_length=1000)
    region: str = Field(default="global", min_length=2, max_length=80)
    max_first_year_cost_usd: float | None = Field(default=None, ge=0)
    requirements: list[Requirement] = Field(default_factory=list, max_length=50)
    candidates: list[CandidateSolution] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def assessment_keys_must_be_unique(self) -> "DecisionRequest":
        requirement_keys = [item.key for item in self.requirements]
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("Requirement keys must be unique.")
        candidate_ids = [item.id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Candidate IDs must be unique.")
        return self


class CandidateEvaluation(BaseModel):
    """Explainable evaluation of one candidate."""

    candidate_id: str
    name: str
    category: str
    eligibility: Literal["eligible", "needs_validation", "excluded"]
    documented_match_score: float | None = Field(default=None, ge=0, le=100)
    matched_requirements: list[str] = Field(default_factory=list)
    unknown_requirements: list[str] = Field(default_factory=list)
    failed_requirements: list[str] = Field(default_factory=list)
    estimated_first_year_cost_usd: float | None = None
    budget_status: Literal["within_budget", "over_budget", "unknown", "not_applicable"]
    region_status: Literal["supported", "unsupported", "unknown", "not_applicable"]
    evidence_status: EvidenceStatus
    pricing_status: EvidenceStatus
    source_urls: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DecisionResponse(BaseModel):
    """A decision result, with a clear distinction between recommendations and leads."""

    status: Literal[
        "recommendation_ready",
        "provisional_only",
        "no_viable_candidates",
        "needs_more_information",
    ]
    summary: str
    recommended_candidate_id: str | None = None
    provisional_candidate_id: str | None = None
    evaluated_count: int = Field(ge=0)
    evaluations: list[CandidateEvaluation] = Field(default_factory=list)
    methodology_note: str
