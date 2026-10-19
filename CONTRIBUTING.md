# Contributing to openfury

Thanks for helping make openfury the best open recruiting agent there is. The project has one rule that matters more than style: **a change to how
the agent sounds or behaves on a call should come with evidence**: a recording, a transcript,
or a number measured before and after. Most of the comments in `voice.py` and `worker.py` are
exactly that, and they are why the agent sounds like a person. Keep that habit.

## Setup

```bash
pip install -e ".[dev]"
python -m pytest -q          # no LiveKit or API keys needed
python -m pyflakes openfury tests examples
```

The tests run the whole API lifecycle with test keys, check that every use case loads, and
check that each record's field labels match its `use_case.json`.

## Add a use case

A use case is a folder in `use_cases/<slug>/`. The slug is lowercase letters, digits and dashes.

**A variant of the job screen** (most new roles) needs three files:

```
use_cases/nursing/
  use_case.json   {"extends": "job-screening", "title", "summary", "fields", "demo", "sample", …}
  addendum.md     the role-specific section, appended to job-screening/prompt.md
  record.md       the grader's instructions and the exact JSON it returns
```

Copy `use_cases/engineering/` and edit it. The addendum should say what changes compared with
the base screen (extra deal-breakers, what to probe in PART 2), plus the rules for this role.

**A different kind of call** (not a job screen) has its own `prompt.md` instead of
`extends` + `addendum.md`. See `language-freelancer/`.

`use_case.json` keys:

| Key | Meaning |
|---|---|
| `required`, `optional` | the `job` fields the API accepts; unknown fields are rejected |
| `defaults` | values for missing optional fields; the agent may say them aloud, so write sentences, not "N/A" |
| `fields` | record label → API key. Must list `record.md`'s `"fields"` labels in the same order (a test checks this) |
| `verdicts` | the snake_case verdicts the grader can return |
| `max_minutes`, `wrap_up_minutes` | hard cap, and when the agent starts wrapping up |
| `voice`, `tts`, `stt`, `stt_lang`, `opener` | voice stack overrides for this call |
| `compose` | write a role-specific block from the brief before dialling (job screens: yes) |
| `demo` | a brief for local runs and `examples/demo_call.py` |
| `sample` | the transcript and result that test keys return |

If you can, add a persona for the new slug to `examples/demo_call.py` and attach a demo
recording to your PR.

## Changing prompts or voice behaviour

- The prompt's voice rules (two sentences, then stop; no written-English connectives; accept
  the first no) come from failures on real calls. Don't loosen them without a recording that
  shows the problem.
- Never add anything that lets the agent ask about protected characteristics, collect payment
  details, or claim to be human.
- Post a short before-and-after recording or transcript in the PR.

## Don't commit real people

Never commit a recording, transcript or phone number of a real candidate. Sample audio has to
be simulated (`examples/demo_call.py`), or come from someone who explicitly agreed to have it
published, like the team calls in `samples/`.

## Reporting security issues

Don't open a public issue. See [SECURITY.md](SECURITY.md).
