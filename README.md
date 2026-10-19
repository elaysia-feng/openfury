# openfury

**An open-source AI recruiter that phones candidates, screens them, and hands back a decision.**

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

<!-- SAMPLES -->

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
screen a simulated candidate (another voice agent with a persona) in a LiveKit room. The
samples above were recorded that way.

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

## Contributing

Bug reports, new use cases, new voices and languages, other STT/TTS providers, and measured
improvements to the voice are all welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). Built at [HumanBit](https://humanbit.ai).
