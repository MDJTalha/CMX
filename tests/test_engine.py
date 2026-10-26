from cmx.engine import evaluate_decision
from cmx.models import CandidateSolution, DecisionRequest, EvidenceStatus, Requirement


def requirement(key: str, mandatory: bool = True, weight: int = 3) -> Requirement:
    return Requirement(key=key, label=key.replace("_", " ").title(), mandatory=mandatory, weight=weight)


def verified_candidate(**overrides) -> CandidateSolution:
    values = {
        "id": "candidate_a",
        "name": "Candidate A",
        "category": "automation",
        "summary": "Test catalogue record",
        "capability_keys": ["email_integration", "approval_workflow"],
        "unsupported_capability_keys": [],
        "supported_regions": ["global"],
        "unsupported_regions": [],
        "setup_cost_usd": 100,
        "monthly_cost_usd": 20,
        "pricing_status": EvidenceStatus.VERIFIED,
        "evidence_status": EvidenceStatus.VERIFIED,
        "source_urls": ["https://example.com/product"],
        "evidence_notes": [],
    }
    values.update(overrides)
    return CandidateSolution(**values)


def request_for(candidates, **overrides) -> DecisionRequest:
    values = {
        "problem_statement": "Reduce manual customer enquiry processing.",
        "desired_outcome": "Save time with a reliable workflow.",
        "region": "global",
        "max_first_year_cost_usd": 500,
        "requirements": [
            requirement("email_integration", mandatory=True, weight=5),
            requirement("approval_workflow", mandatory=False, weight=2),
        ],
        "candidates": candidates,
    }
    values.update(overrides)
    return DecisionRequest(**values)


def test_verified_candidate_with_constraints_is_eligible():
    result = evaluate_decision(request_for([verified_candidate()]))
    assert result.status == "recommendation_ready"
    assert result.recommended_candidate_id == "candidate_a"
    assert result.evaluations[0].eligibility == "eligible"
    assert result.evaluations[0].estimated_first_year_cost_usd == 340
    assert result.evaluations[0].documented_match_score == 100


def test_explicitly_unsupported_mandatory_requirement_excludes_candidate():
    candidate = verified_candidate(
        unsupported_capability_keys=["email_integration"],
        capability_keys=["approval_workflow"],
    )
    result = evaluate_decision(request_for([candidate]))
    assert result.status == "no_viable_candidates"
    assert result.evaluations[0].eligibility == "excluded"
    assert result.evaluations[0].failed_requirements == ["email_integration"]


def test_unknown_mandatory_requirement_requires_validation_not_exclusion():
    candidate = verified_candidate(capability_keys=["approval_workflow"])
    result = evaluate_decision(request_for([candidate]))
    assert result.status == "provisional_only"
    assert result.recommended_candidate_id is None
    assert result.provisional_candidate_id == "candidate_a"
    assert "email_integration" in result.evaluations[0].unknown_requirements


def test_verified_cost_over_budget_excludes_candidate():
    candidate = verified_candidate(setup_cost_usd=1000, monthly_cost_usd=100)
    result = evaluate_decision(request_for([candidate], max_first_year_cost_usd=1500))
    assert result.evaluations[0].eligibility == "excluded"
    assert result.evaluations[0].budget_status == "over_budget"


def test_unverified_cost_does_not_create_false_budget_certainty():
    candidate = verified_candidate(
        setup_cost_usd=1000,
        monthly_cost_usd=100,
        pricing_status=EvidenceStatus.ESTIMATED,
    )
    result = evaluate_decision(request_for([candidate], max_first_year_cost_usd=1500))
    assert result.evaluations[0].budget_status == "unknown"
    assert result.evaluations[0].eligibility == "needs_validation"


def test_explicitly_unsupported_region_excludes_candidate():
    candidate = verified_candidate(supported_regions=["GB"], unsupported_regions=["PK"])
    result = evaluate_decision(request_for([candidate], region="PK"))
    assert result.evaluations[0].eligibility == "excluded"
    assert result.evaluations[0].region_status == "unsupported"


def test_unverified_region_requires_validation():
    candidate = verified_candidate(supported_regions=["GB"])
    result = evaluate_decision(request_for([candidate], region="PK"))
    assert result.evaluations[0].eligibility == "needs_validation"
    assert result.evaluations[0].region_status == "unknown"


def test_missing_candidates_returns_more_information_not_a_fake_winner():
    result = evaluate_decision(request_for([]))
    assert result.status == "needs_more_information"
    assert result.recommended_candidate_id is None
    assert result.evaluated_count == 0


def test_candidates_are_ranked_by_eligibility_before_score():
    low = verified_candidate(
        id="candidate_b",
        name="Low score",
        capability_keys=["email_integration"],
    )
    high = verified_candidate(
        id="candidate_c",
        name="Higher score but unverified evidence",
        evidence_status=EvidenceStatus.UNKNOWN,
        capability_keys=["email_integration", "approval_workflow"],
    )
    result = evaluate_decision(request_for([high, low]))
    assert result.evaluations[0].candidate_id == "candidate_b"
    assert result.evaluations[0].eligibility == "eligible"
    assert result.evaluations[1].eligibility == "needs_validation"


def test_no_requirements_cannot_produce_a_confident_recommendation():
    candidate = verified_candidate()
    result = evaluate_decision(request_for([candidate], requirements=[]))
    assert result.status == "provisional_only"
    assert result.recommended_candidate_id is None


def test_conflicting_capability_claims_are_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        verified_candidate(unsupported_capability_keys=["email_integration"])


def test_duplicate_requirement_keys_are_rejected():
    import pytest
    from pydantic import ValidationError

    candidate = verified_candidate()
    with pytest.raises(ValidationError):
        request_for([candidate], requirements=[requirement("email_integration"), requirement("email_integration")])
