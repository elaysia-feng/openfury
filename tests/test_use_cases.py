import json
import re

import pytest

from openfury.use_cases import all_use_cases, normalize_phone, render

SLUGS = ["engineering", "job-screening", "language-freelancer", "sales", "support"]


def test_every_slug_loads():
    assert sorted(all_use_cases()) == SLUGS


@pytest.mark.parametrize("slug", SLUGS)
def test_record_labels_match_fields(slug):
    """The grader's labels and the API's field keys must not drift apart."""
    uc = all_use_cases()[slug]
    block = re.search(r'"fields": \{(.*?)\n  \}', uc["record"], re.S).group(1)
    assert re.findall(r'"([^"]+)": ""', block) == list(uc["fields"])


@pytest.mark.parametrize("slug", SLUGS)
def test_prompt_renders_with_no_leftover_placeholders(slug):
    uc = all_use_cases()[slug]
    values = {"agent_name": "Meera", "company_name": "Acme", "candidate_name": "Ravi",
              "client_company": "", **uc["defaults"], **uc["demo"]}
    values.update({k: values.get(k, "x") for k in (*uc["required"], *uc["optional"])})
    assert re.findall(r"\{\{\w+\}\}", render(uc["prompt"], values)) == []


@pytest.mark.parametrize("slug", SLUGS)
def test_sample_result_uses_declared_fields(slug):
    uc = all_use_cases()[slug]
    assert set(uc["sample"]["result"]["fields"]) <= set(uc["fields"].values())
    assert uc["sample"]["result"]["verdict"] in uc["verdicts"]
    json.dumps(uc)


def test_extends_appends_addendum():
    ucs = all_use_cases()
    assert ucs["engineering"]["prompt"].startswith(ucs["job-screening"]["prompt"].rstrip())
    assert "ENGINEERING SCREEN" in ucs["engineering"]["prompt"]


@pytest.mark.parametrize("raw,want", [
    ("98765 43210", "+919876543210"), ("+91 98765 43210", "+919876543210"),
    ("09876543210", None), ("+1 415 555 0100", "+14155550100"), ("12345", None),
    ("+91 5876543210", None),
])
def test_normalize_phone(raw, want):
    assert normalize_phone(raw) == want


def test_render_leaves_unknown_placeholders_visible():
    assert render("{{a}} {{b}}", {"a": 1}) == "1 {{b}}"
