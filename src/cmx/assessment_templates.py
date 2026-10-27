"""Versioned assessment templates; templates are discovery aids, not market evidence."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


TEMPLATE_SCHEMA_VERSION = 1

_TEMPLATES: dict[str, dict[str, Any]] = {
    "general-assessment": {
        "version": 1,
        "title": "General decision assessment",
        "status": "discovery",
        "problem_prompt": "Describe the current problem and who experiences it.",
        "outcome_prompt": "Describe the measurable outcome you want to improve.",
        "requirements": [],
        "market_validation": "not_validated",
    },
    "sme-back-office-workflow": {
        "version": 1,
        "title": "SME back-office workflow discovery",
        "status": "discovery",
        "problem_prompt": "Describe one recurring SME back-office workflow, its owner, and current failure points.",
        "outcome_prompt": "State a measurable baseline and target, such as time per case, error rate, or cost per month.",
        "requirements": [
            {"key": "current_process_documented", "label": "Current process and baseline documented", "mandatory": True, "weight": 5},
            {"key": "data_access_defined", "label": "Required data access and ownership are clear", "mandatory": True, "weight": 5},
            {"key": "audit_trail", "label": "Changes and decisions can be audited", "mandatory": True, "weight": 4},
            {"key": "human_approval", "label": "Human approval is available for consequential changes", "mandatory": True, "weight": 5},
            {"key": "exportable_data", "label": "Data can be exported in a usable format", "mandatory": False, "weight": 3},
        ],
        "discovery_questions": [
            "How often does the workflow occur and who owns it?",
            "What is the measured baseline for time, cost, error, and rework?",
            "What workaround is used today and what has already been tried?",
            "Who approves a purchase and what budget authority exists?",
            "Would the organization pay for a small, time-bounded pilot? At what price and under what success criteria?",
        ],
        "market_validation": "not_validated",
    },
    "software-or-service-selection": {
        "version": 1,
        "title": "Software or service selection",
        "status": "discovery",
        "problem_prompt": "Describe the job to be done and why current tools or services are insufficient.",
        "outcome_prompt": "Define the measurable result and acceptable implementation risk.",
        "requirements": [
            {"key": "security_review", "label": "Security and privacy requirements can be met", "mandatory": True, "weight": 5},
            {"key": "total_cost_known", "label": "First-year and ongoing costs are documented", "mandatory": True, "weight": 5},
            {"key": "data_export", "label": "Data export and exit options are documented", "mandatory": True, "weight": 4},
            {"key": "support_model", "label": "Support and escalation expectations are clear", "mandatory": False, "weight": 3},
        ],
        "market_validation": "not_validated",
    },
}


def list_templates() -> list[dict[str, Any]]:
    """Return templates in stable key order without exposing mutable internals."""
    return [
        {"template_id": key, **deepcopy(value)}
        for key, value in sorted(_TEMPLATES.items())
    ]


def get_template(template_id: str, version: int | None = None) -> dict[str, Any] | None:
    """Return an exact template version; never silently substitute a newer version."""
    template = _TEMPLATES.get(template_id)
    if template is None or (version is not None and template["version"] != version):
        return None
    return {"template_id": template_id, **deepcopy(template)}


def validate_template_reference(template_id: str, version: int) -> bool:
    """Check whether a persisted assessment references a supported template version."""
    return get_template(template_id, version) is not None
