"""Deterministic, explainable decision evaluation.

This module deliberately does not call an LLM, browse the web, or invent catalogue data.
It evaluates only the structured claims supplied to it.
"""

from __future__ import annotations

from cmx.models import (
    CandidateEvaluation,
    CandidateSolution,
    DecisionRequest,
    DecisionResponse,
    EvidenceStatus,
)

_METHOD_NOTE = (
    "The documented match score is weighted requirement coverage, not a probability "
    "of success. Unlisted capability claims are treated as unknown, not as facts. "
    "Evidence labels and URLs are supplied by the caller and are not independently "
    "verified by this engine; only a trusted, controlled catalogue workflow should "
    "mark evidence or pricing as verified."
)


def _first_year_cost(candidate: CandidateSolution) -> float | None:
    """Return a cost estimate only when both cost components and a status exist."""
    if (
        candidate.setup_cost_usd is None
        or candidate.monthly_cost_usd is None
        or candidate.pricing_status == EvidenceStatus.UNKNOWN
    ):
        return None
    return round(candidate.setup_cost_usd + 12 * candidate.monthly_cost_usd, 2)


def _evaluate_candidate(
    candidate: CandidateSolution,
    request: DecisionRequest,
) -> CandidateEvaluation:
    supported = set(candidate.capability_keys)
    unsupported = set(candidate.unsupported_capability_keys)
    matched: list[str] = []
    unknown: list[str] = []
    failed: list[str] = []
    weighted_matches = 0
    total_weight = 0

    for requirement in request.requirements:
        total_weight += requirement.weight
        if requirement.key in supported:
            matched.append(requirement.key)
            weighted_matches += requirement.weight
        elif requirement.key in unsupported:
            if requirement.mandatory:
                failed.append(requirement.key)
        else:
            unknown.append(requirement.key)

    score = (
        round(weighted_matches / total_weight * 100, 2)
        if total_weight > 0
        else None
    )
    first_year_cost = _first_year_cost(candidate)

    if request.max_first_year_cost_usd is None:
        budget_status = "not_applicable"
    elif first_year_cost is None:
        budget_status = "unknown"
    elif first_year_cost > request.max_first_year_cost_usd:
        if candidate.pricing_status == EvidenceStatus.VERIFIED:
            budget_status = "over_budget"
        else:
            budget_status = "unknown"
    else:
        budget_status = "within_budget"

    requested_region = request.region.strip().casefold()
    if requested_region in {"global", "unspecified"}:
        region_status = "not_applicable"
    else:
        supported_regions = {region.casefold() for region in candidate.supported_regions}
        unsupported_regions = {region.casefold() for region in candidate.unsupported_regions}
        if requested_region in unsupported_regions:
            region_status = "unsupported"
        elif "global" in supported_regions or requested_region in supported_regions:
            region_status = "supported"
        else:
            region_status = "unknown"

    reasons: list[str] = []
    warnings: list[str] = []
    if matched:
        reasons.append(f"Explicitly claims {len(matched)} matching capability/capabilities.")
    if failed:
        reasons.append("Fails mandatory requirements explicitly marked unsupported.")
    if unknown:
        warnings.append("Unverified capability coverage: " + ", ".join(unknown) + ".")
    if budget_status == "over_budget":
        reasons.append("Verified first-year cost exceeds the stated budget.")
    elif budget_status == "unknown" and request.max_first_year_cost_usd is not None:
        warnings.append("Budget fit cannot be established from verified first-year pricing.")
    if region_status == "unsupported":
        reasons.append("The requested region is explicitly marked unsupported.")
    elif region_status == "unknown":
        warnings.append("Regional availability has not been established.")
    if candidate.evidence_status != EvidenceStatus.VERIFIED:
        warnings.append("Solution evidence is not marked verified by a trusted catalogue.")
    if not candidate.source_urls:
        warnings.append("No source URLs were provided for independent review.")
    if candidate.pricing_status != EvidenceStatus.VERIFIED:
        warnings.append("Pricing is not marked verified by a trusted catalogue.")
    if candidate.setup_cost_usd is None or candidate.monthly_cost_usd is None:
        warnings.append(
            "First-year cost is unavailable because setup and monthly costs "
            "must both be supplied."
        )
    warnings.extend(candidate.evidence_notes)

    excluded = (
        bool(failed)
        or budget_status == "over_budget"
        or region_status == "unsupported"
    )
    missing_mandatory = any(
        requirement.mandatory and requirement.key in unknown
        for requirement in request.requirements
    )
    cost_is_complete_and_verified = (
        candidate.setup_cost_usd is not None
        and candidate.monthly_cost_usd is not None
        and candidate.pricing_status == EvidenceStatus.VERIFIED
    )
    evidence_is_reviewable = (
        candidate.evidence_status == EvidenceStatus.VERIFIED
        and bool(candidate.source_urls)
    )
    budget_is_resolved = (
        request.max_first_year_cost_usd is None
        or (budget_status == "within_budget" and cost_is_complete_and_verified)
    )
    region_is_resolved = region_status in {"supported", "not_applicable"}

    if excluded:
        eligibility = "excluded"
    elif (
        missing_mandatory
        or not evidence_is_reviewable
        or not cost_is_complete_and_verified
        or not budget_is_resolved
        or not region_is_resolved
    ):
        eligibility = "needs_validation"
    else:
        eligibility = "eligible"

    if not request.requirements:
        warnings.append("No requirements were supplied; a meaningful fit comparison cannot be made.")
        if eligibility == "eligible":
            eligibility = "needs_validation"

    return CandidateEvaluation(
        candidate_id=candidate.id,
        name=candidate.name,
        category=candidate.category,
        eligibility=eligibility,
        documented_match_score=score,
        matched_requirements=matched,
        unknown_requirements=unknown,
        failed_requirements=failed,
        estimated_first_year_cost_usd=first_year_cost,
        budget_status=budget_status,
        region_status=region_status,
        evidence_status=candidate.evidence_status,
        pricing_status=candidate.pricing_status,
        source_urls=[str(url) for url in candidate.source_urls],
        reasons=reasons,
        warnings=warnings,
    )


def evaluate_decision(request: DecisionRequest) -> DecisionResponse:
    """Evaluate candidate solutions against explicit constraints and requirements."""
    if not request.candidates:
        return DecisionResponse(
            status="needs_more_information",
            summary=(
                "No candidate solutions were supplied. Define requirements and add "
                "candidate records or connect a trusted catalogue before ranking options."
            ),
            evaluated_count=0,
            evaluations=[],
            methodology_note=_METHOD_NOTE,
        )

    evaluations = [_evaluate_candidate(candidate, request) for candidate in request.candidates]
    order = {"eligible": 0, "needs_validation": 1, "excluded": 2}
    evaluations.sort(
        key=lambda item: (
            order[item.eligibility],
            -(item.documented_match_score or 0.0),
            item.name.casefold(),
            item.candidate_id,
        )
    )

    eligible = [item for item in evaluations if item.eligibility == "eligible"]
    provisional = [item for item in evaluations if item.eligibility == "needs_validation"]

    if eligible:
        winner = eligible[0]
        summary = (
            f"{len(eligible)} candidate(s) passed the configured eligibility checks. "
            f"{winner.name} ranks first on documented requirement coverage; review "
            "the evidence and implementation fit before committing."
        )
        status = "recommendation_ready"
        recommended_id = winner.candidate_id
        provisional_id = None
    elif provisional:
        leader = provisional[0]
        summary = (
            "No candidate is fully cleared on the supplied evidence. "
            f"{leader.name} is a provisional lead only; resolve the listed unknowns "
            "before making a purchase or implementation decision."
        )
        status = "provisional_only"
        recommended_id = None
        provisional_id = leader.candidate_id
    else:
        summary = (
            "No candidate passed the explicit mandatory constraints. Review the "
            "requirements or supply different candidates; do not silently weaken "
            "mandatory constraints."
        )
        status = "no_viable_candidates"
        recommended_id = None
        provisional_id = None

    return DecisionResponse(
        status=status,
        summary=summary,
        recommended_candidate_id=recommended_id,
        provisional_candidate_id=provisional_id,
        evaluated_count=len(evaluations),
        evaluations=evaluations,
        methodology_note=_METHOD_NOTE,
    )
