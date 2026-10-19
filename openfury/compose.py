"""The role-specific half of the prompt, written once per brief.

The call flow, voice rules, CTC disclosure rule and safety rules live in the use case's prompt
and are never touched here. This only writes what a generic screen would miss for THIS role:
probes, a ten-second pitch, green and red flags. The API caches it per brief.
"""
from __future__ import annotations

import json
import os

COMPOSER = """You are preparing a phone-screening brief for a recruiter who will call
one candidate about one role. You are NOT writing the script — the call flow, voice rules,
CTC disclosure rule and safety rules already exist and are not yours to touch. Write only
the role-specific part.

Return STRICT JSON, no prose, no markdown fence:
{
  "probes": ["3-6 short questions THIS role needs that a generic screen would miss. Concrete, answerable on a phone call. No 'tell me about yourself'."],
  "pitch": "The spoken pitch: TWO short sentences, under ten seconds — what the company does and why this role is interesting. Spoken English with contractions, no bullets, no buzzwords, no funding history. Sounds like a person who likes working there, not a careers page.",
  "green_flags": ["2-4 things in an answer that mean this candidate is worth progressing"],
  "red_flags": ["2-4 things that mean they are not, for THIS role specifically"],
  "seniority_note": "One line on what a candidate at this experience band actually cares about."
}

Never invent a salary, benefit, technology or team detail that is not in the brief. If the
brief is thin, write fewer probes rather than padding with generic ones."""


def compose(brief: dict) -> str:
    from google import genai
    from google.genai import types as gt
    text = "\n".join(f"- {k}: {v}" for k, v in brief.items() if v)
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"])
    r = client.models.generate_content(
        model=os.environ.get("COMPOSER_LLM", "gemini-2.5-flash"),
        contents=f"{COMPOSER}\n\nROLE BRIEF:\n{text}",
        # thinking off: this is a formatting job, and it is the difference between a
        # one-second compose and a six-second one
        config=gt.GenerateContentConfig(temperature=0.4, response_mime_type="application/json",
                                        thinking_config=gt.ThinkingConfig(thinking_budget=0)))
    c = json.loads(r.text)
    L: list[str] = []
    if c.get("seniority_note"):
        L += [f"**Pitch at their level.** {c['seniority_note']}", ""]
    if c.get("pitch"):
        L += ["**Your pitch for this role** (say it in your own words, never read it):", f"> {c['pitch']}", ""]
    if c.get("probes"):
        L += ["**Ask these, not generic questions.** Work them into the flow naturally:", *[f"- {p}" for p in c["probes"]], ""]
    if c.get("green_flags"):
        L += ["**Worth progressing if you hear:**", *[f"- {g}" for g in c["green_flags"]], ""]
    if c.get("red_flags"):
        L += ["**Not a fit for THIS role if you hear:**", *[f"- {x}" for x in c["red_flags"]], "",
              "Those are signals to note, not verdicts to say out loud. Never tell a candidate they failed a screen.", ""]
    return "\n".join(["", "# THIS ROLE — written for this brief, use it", "", *L])
