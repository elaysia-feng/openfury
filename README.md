<div align="center">

# openfury

### The open-source AI recruiter, by [HumanBit](https://humanbit.ai)

**It phones candidates, screens them like a good recruiter, and hands back a decision.**

[![CI](https://github.com/LazarusStack/openfury/actions/workflows/ci.yml/badge.svg)](https://github.com/LazarusStack/openfury/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[Listen](#listen) · [Use cases](#use-cases) · [Quickstart](#quickstart) · [Contribute](#help-make-it-the-best-recruiting-agent-there-is)

</div>

You send a candidate, a job brief and a use case (`engineering`, `sales`, `support`, …).
openfury calls the candidate, runs a structured screen in natural Indian English, and posts
back a verdict (`proceed` / `hold` / `reject` / …), the captured fields (notice period, current
and expected CTC, stack, quota, shift fit…), a timestamped transcript and a stereo recording.

It's built on [LiveKit Agents](https://github.com/livekit/agents). The tuning comes from real
screening calls: turn-taking delays measured on 363 real recruiter turn gaps, a pre-rendered
"Okay." that covers TTS latency, a slower speaking rate for salary figures, and hang-up rules
that never leave a candidate's question unanswered.

> **Don't want to run the phone lines yourself?** We host openfury, including SIP numbers,
> voices, scaling and uptime. Email **[shivam@humanbit.ai](mailto:shivam@humanbit.ai)** and
> **[apoorv@humanbit.ai](mailto:apoorv@humanbit.ai)**.

## Listen

Real phone calls from HumanBit's own deployment. The "candidates" are the two of us, Shivam
and Apoorv, playing candidates and doing our best to break it. Stereo: the agent is on one
channel, the candidate on the other. No real candidate's call is ever published here.

| Call | Use case | What to listen for |
|---|---|---|
| [▶ Budget disclosure](samples/job-screening-budget-disclosure.mp3) · 2:36 | `job-screening` | The candidate asks for 50 lakhs. The agent states the 28-lakh budget once, plainly, and moves on. It never haggles. |
| [▶ A clean qualified screen](samples/language-freelancer-qualified.mp3) · 2:28 | `language-freelancer` | A clean screen: laptop, languages, read/write check, hours, earnings, then the next step. |
| [▶ Hinglish answer](samples/language-freelancer-hinglish.mp3) · 1:31 | `language-freelancer` | The candidate switches into Hindi mid-answer and the agent carries on without missing the number. |
| [▶ "How did you get my number?"](samples/language-freelancer-how-did-you-get-my-number.mp3) · 2:07 | `language-freelancer` | A suspicious candidate, then a language mismatch. The agent answers, doesn't argue, and closes politely. |
| [▶ "My boss is coming"](samples/edge-case-boss-is-coming.mp3) · 0:58 | any | The candidate begs it to hang up. The agent offers a callback in one line and ends the call. |

These were recorded with HumanBit's voice ("Sanskriti") and an earlier version of the prompts.
Want a sample for a slug that isn't here yet? Record one with
[`examples/demo_call.py`](examples/demo_call.py) and open a PR.

## Use cases

Each use case (or "slug") is a folder under [`use_cases/`](use_cases). Pick one per call with
`"use_case": "<slug>"`.

| Slug | What the call does | Extra result fields |
|---|---|---|
| `job-screening` | Generic full-time screen. Deal-breakers first (notice, CTC, location/mode, competing offers, experience), then the pitch and role-specific questions written from your brief. | `structured.current_ctc_lpa`, `expected_ctc_lpa`, `notice_days` |
| `engineering` | `job-screening` plus a technical-depth check: stack, one system they built and its scale, *their* part, the reason behind one choice, on-call. Never quizzes trivia. | `primary_stack`, `system_built`, `technical_depth` (specific / mixed / vague) |
| `sales` | `job-screening` for sales roles: segment, inbound vs outbound, quota and attainment, deal size and cycle, fixed vs OTE pay. | `quota_attainment_pct`, `segment`, fixed/OTE split |
| `support` | Shift fit is treated as a deal-breaker. Covers channels, volume, tools, CSAT/AHT and a hard-customer example. The call itself doubles as the communication check. | `accepts_shift`, `communication`, `tools` |
| `language-freelancer` | A 3-minute call for remote freelance work in Indian languages (annotation, transcription, voice data). Checks for a laptop, English, which languages they speak and write, hours per day and earnings. | `has_laptop`, `languages_read_written`, `hours_per_day` |

Every use case keeps the same fixed rules. The agent asks one question at a time and accepts the
first "no". It says it's an AI if asked. It never asks for bank or OTP details, never states a
salary that isn't in the brief, and never asks about protected topics. A separate grading pass
applies mechanical verdict rules, so you never get an optimistic "Proceed" for someone who is
over budget.

**Adding a slug** takes three files. See [CONTRIBUTING.md](CONTRIBUTING.md#add-a-use-case).
Good first slugs to add: `nursing`, `delivery-partner`, `teacher`, `campus-hiring`.

## How it works

```
your ATS ──POST /v1/calls──▶ openfury.server ──dispatch──▶ LiveKit ◀──▶ openfury.worker (the voice)
    ▲                          │  queue, calling hours,          │ SIP
    └──── signed webhooks ─────┘  concurrency, DNC, retries      ▼
          call.status / call.transcript / call.completed      candidate's phone
```

- **`openfury/worker.py`** holds one call. It runs STT → LLM → TTS, enforces the time cap and
  the hang-up rules, records stereo audio (agent on one channel, candidate on the other), then
  grades the transcript with the use case's `record.md`.
- **`openfury/api.py`** is the multi-tenant platform. Each tenant gets live and test API keys,
  a signed webhook with retries, concurrency and calling-hour limits, a do-not-call list,
  idempotency, scheduled calls, retry on no-answer, a live transcript (SSE or webhooks), and
  deletion after a retention window. Test keys run the whole lifecycle with no phone call.
- **`openfury/voice.py`** holds turn-taking, pacing and pronunciation fixes, each with the
  measurement behind it.

Full API reference: run the server and open `/apidoc`.

## Quickstart

You need a [LiveKit Cloud](https://cloud.livekit.io) project (or self-hosted LiveKit) and API
keys for Gemini, ElevenLabs and Smallest. Live calls also need an outbound SIP trunk in LiveKit.
On self-hosted LiveKit, background-voice cancellation is switched off and a local turn model
is used, because both of those features only run on LiveKit Cloud.

```bash
git clone https://github.com/LazarusStack/openfury && cd openfury
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill in the keys

python -m openfury.worker start          # terminal 1: the voice agent
python -m openfury.server                # terminal 2: the API on :8097

python -m openfury.api tenant-add acme --name "Acme" --company-name "Acme Talent" \
    --webhook-url https://your-app.example/hooks/openfury
# prints a live key, a test key and the webhook secret, once
```

Place a test call. Nobody is dialled, and the full lifecycle and webhooks arrive in about 10 s:

```bash
curl localhost:8097/v1/calls -H "Authorization: Bearer $OF_TEST_KEY" \
  -H "Content-Type: application/json" -d '{
    "use_case": "engineering",
    "candidate": {"name": "Ravi Kumar", "phone": "+919876543210"},
    "job": {"role_name": "Backend Engineer", "role_location": "Bangalore", "work_mode": "hybrid",
            "experience_range": "4 to 7 years", "ctc_max": "30 lakhs per annum",
            "max_notice_period": "60 days", "role_description": "Go, Postgres, payments"}
  }'
```

Switch to the live key to make a real call.

**Try it without a phone.** [`examples/demo_call.py`](examples/demo_call.py) has the agent
screen a simulated candidate (another voice agent with a persona) in a LiveKit room.

```bash
AGENT_NAME=openfury-demo python -m openfury.worker start &
AGENT_NAME=openfury-demo python examples/demo_call.py sales
```

## Responsible use

These are real people's phones. The defaults are deliberately conservative: calls only happen
inside calling hours, one open call per number, a do-not-call list that fills itself when someone
asks to be removed, and dialling pauses after two silent calls in a row. Call only candidates
who applied or consented, tell them calls may be recorded and handled by AI, and keep a person
in charge of the hiring decision.

## Help make it the best recruiting agent there is

openfury is meant to be built in the open. Recruiters, engineers and anyone who has sat
through a bad screening call can make it better. Good places to start:

- **A new use case.** `nursing`, `delivery-partner`, `teacher`, `campus-hiring`, `bpo-voice`,
  `driver`. Three files each, no Python needed.
- **New languages and voices.** Hindi-first, Tamil, Telugu, Bengali and Marathi screens, and
  more STT/TTS providers.
- **Measured voice improvements.** Turn-taking, latency and interruptions. Bring a recording
  from before and after the change.
- **Platform.** Postgres storage, a Docker Compose setup with self-hosted LiveKit, ATS
  integrations (Greenhouse, Lever, Zoho Recruit) and a small review dashboard.

Read [CONTRIBUTING.md](CONTRIBUTING.md), pick an issue labelled
[`good first issue`](https://github.com/LazarusStack/openfury/labels/good%20first%20issue) or
open a [discussion](https://github.com/LazarusStack/openfury/discussions) with your idea.
Everyone taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE). Built at [HumanBit](https://humanbit.ai).
