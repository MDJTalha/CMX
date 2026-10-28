"""Static contract checks for the dependency-free CMX browser workspace."""

from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class _DocumentInspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.labels = []
        self.scripts = []
        self.stylesheets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "label" and attrs.get("for"):
            self.labels.append(attrs["for"])
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs.get("href", ""))


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.js = (FRONTEND / "app.js").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        cls.document = _DocumentInspector()
        cls.document.feed(cls.html)

    def test_frontend_assets_are_present_and_referenced(self):
        self.assertIn("./app.js", self.document.scripts)
        self.assertIn("./styles.css", self.document.stylesheets)
        self.assertTrue((FRONTEND / "app.js").is_file())
        self.assertTrue((FRONTEND / "styles.css").is_file())

    def test_every_label_target_exists(self):
        missing = sorted(set(self.document.labels) - self.document.ids)
        self.assertEqual(missing, [])

    def test_frontend_uses_actual_decision_request_field_names(self):
        for field in (
            "problem_statement",
            "desired_outcome",
            "region",
            "max_first_year_cost_usd",
            "requirements",
            "candidates",
        ):
            with self.subTest(field=field):
                self.assertRegex(self.js, rf"\b{re.escape(field)}\b")
        self.assertIn("/api/v1/assessments/evaluate", self.js)

    def test_request_does_not_use_superseded_field_names(self):
        self.assertNotIn("target_region:", self.js)
        self.assertNotIn("first_year_budget_usd", self.js)

    def test_protected_api_access_is_not_misrepresented_as_public(self):
        self.assertIn("assessment requests still require a bearer credential", self.js)
        self.assertIn('headers.Authorization = "Bearer " + state.credential', self.js)
        self.assertIn("never stored in browser storage", self.js)
        self.assertNotIn("localStorage", self.js)
        self.assertNotIn("sessionStorage", self.js)
        self.assertIn("renderResults(payload)", self.js)
        self.assertIn("result-review", self.html)

    def test_accessibility_basics_remain_in_markup_and_styles(self):
        self.assertIn('href="#main"', self.html)
        self.assertIn('role="status"', self.html)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("prefers-reduced-motion", self.css)


if __name__ == "__main__":
    unittest.main()
