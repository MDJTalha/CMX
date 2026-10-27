from cmx.assessment_templates import (
    get_template,
    list_templates,
    validate_template_reference,
)


def test_template_catalog_is_versioned_and_marked_unvalidated():
    templates = list_templates()
    assert len(templates) >= 3
    assert all(item["version"] >= 1 for item in templates)
    assert all(item["market_validation"] == "not_validated" for item in templates)


def test_exact_version_lookup_does_not_silently_upgrade():
    current = get_template("sme-back-office-workflow", 1)
    assert current is not None
    assert current["requirements"]
    assert get_template("sme-back-office-workflow", 2) is None
    assert not validate_template_reference("sme-back-office-workflow", 2)


def test_template_return_values_cannot_mutate_catalog():
    first = get_template("general-assessment")
    first["title"] = "tampered"
    assert get_template("general-assessment")["title"] == "General decision assessment"


def test_unknown_template_is_not_replaced_by_default():
    assert get_template("does-not-exist") is None
