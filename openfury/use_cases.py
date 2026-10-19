"""Use cases ("slugs"): one folder per kind of call under use_cases/.

    use_cases/<slug>/use_case.json   what the API accepts and returns for this call
    use_cases/<slug>/prompt.md       the agent's system prompt  (or "extends" + addendum.md)
    use_cases/<slug>/record.md       the post-call grader's instructions

A slug that "extends" another takes that one's prompt.md and appends its own addendum.md, so a
fix to the base screen (voice rules, safety rules) reaches every slug built on it.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(os.environ.get("USE_CASES_DIR", Path(__file__).resolve().parent.parent / "use_cases"))

# Voices a call may pick. Only voices that have been listened to on real phone calls.
# Ids are ElevenLabs shared-library voices; add your own here.
VOICES = {"saavi": {"eleven_id": "H8bdWZHK2OgZwTN7ponr", "gender": "female",
                    "accent": "Indian English", "tone": "warm, professional"},
          "monika": {"eleven_id": "EaBs7G1VibMrNAuz2Na7", "gender": "female",
                     "accent": "Indian English", "tone": "casual, young"}}


def render(template: str, values: dict) -> str:
    """Fill {{name}} placeholders.

    Deliberately not str.format: the prompt is authored with handlebars and contains other
    braces, and a stray one would raise mid-call. An unknown placeholder is left visible
    rather than silently blanked — a prompt that still says {{ctc_max}} on screen is a
    configuration bug you can see, while an empty string is one you cannot.
    """
    def sub(m: re.Match) -> str:
        return str(values.get(m.group(1).strip(), m.group(0)))
    return re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", sub, template)


@lru_cache(maxsize=None)
def load(slug: str) -> dict:
    """The use case as a dict, with `prompt` and `record` resolved to text."""
    if not re.match(r"^[a-z0-9-]{2,40}$", slug) or not (ROOT / slug / "use_case.json").exists():
        raise KeyError(slug)
    d = ROOT / slug
    uc = json.loads((d / "use_case.json").read_text("utf-8"))
    uc["id"] = slug
    if uc.get("extends"):
        uc["prompt"] = (load(uc["extends"])["prompt"].rstrip() + "\n\n"
                        + (d / "addendum.md").read_text("utf-8").strip() + "\n")
    else:
        uc["prompt"] = (d / "prompt.md").read_text("utf-8")
    uc["record"] = (d / "record.md").read_text("utf-8")
    return uc


def all_use_cases() -> dict[str, dict]:
    return {p.name: load(p.name) for p in sorted(ROOT.iterdir())
            if (p / "use_case.json").exists()}


def normalize_phone(raw: str) -> str | None:
    """E.164, or None. A bare national number gets DEFAULT_COUNTRY_CODE (91 by default)."""
    s = (raw or "").strip()
    d = re.sub(r"\D", "", s)
    cc = os.environ.get("DEFAULT_COUNTRY_CODE", "91")
    if not s.startswith("+") and cc == "91" and len(d) == 10:
        d = "91" + d
    elif not s.startswith("+") and not d.startswith(cc):
        d = cc + d
    if d.startswith("91"):                  # Indian mobiles: 10 digits starting 6-9
        return "+" + d if len(d) == 12 and d[2] in "6789" else None
    return "+" + d if 8 <= len(d) <= 15 else None
