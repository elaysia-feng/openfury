"""A demo call with no phone and no real person: the agent screens a simulated candidate.

The recruiter side is the real worker, unchanged. The candidate is a second voice agent with a
persona, joining the room as participant "candidate" the way a phone leg would. The worker
records the call as usual, so this is how to record a sample for a new use case.

    python -m openfury.worker dev                       # terminal 1 (AGENT_NAME=openfury-demo)
    python examples/demo_call.py engineering            # terminal 2

The recording lands in $DATA_DIR/calls/<room>.mp3, next to the graded record (<room>.json).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from livekit import api, rtc  # noqa: E402
from livekit.agents import Agent, AgentSession, RoomInputOptions  # noqa: E402
from livekit.plugins import silero, smallestai  # noqa: E402
from livekit.plugins.google.beta import GeminiTTS  # noqa: E402

from openfury.use_cases import load  # noqa: E402
from openfury.voice import make_llm  # noqa: E402
from openfury.worker import _speak_with_locale  # noqa: E402

load_dotenv()

STYLE = """You are a job candidate in India taking an unexpected phone call from a recruiter.
You are a real person, not an assistant: answer only what you are asked, in one or two short
spoken sentences, casual Indian English, with the odd "umm" or "actually". Never list things.
Never ask "how can I help". Don't volunteer your whole story; let the recruiter pull it out.
When the recruiter closes the call, say a short bye and nothing else.

WHO YOU ARE:
"""

PERSONAS = {
    "job-screening": """Ravi, 5 years as a backend developer at a mid-size logistics company in Pune, Java and
Spring. Notice period 60 days, can negotiate to 45. Current CTC 19 lakhs, expecting around
30 lakhs fixed — if told the budget is lower, say you could consider 28 if the work is good.
Open to hybrid but would need a month to move to Bangalore. You have one other interview in
progress, early stage. Ask once what the team size is.""",
    "engineering": """Ravi, 6 years as a backend engineer. Right now at a fintech in Bangalore, mostly Go and
Postgres on AWS. You rebuilt the payments reconciliation service last year: about two million
transactions a day; you designed the matching logic and the retry queue yourself, two others
did the dashboards. You chose Postgres over Mongo because reconciliation needs transactions
across ledgers. You are on-call one week a month. Notice 30 days. Current CTC 26 lakhs,
expecting 34 — if told the budget, say it's a bit low but you're open to talking. Fine with
hybrid in Bangalore. No other offers.""",
    "sales": """Ravi, 4 years in B2B SaaS sales, now an account executive selling HR software to
mid-market companies, mostly outbound. Annual quota 1.2 crore, you closed about 1.4 crore last
year. Typical deal 8 to 10 lakhs, takes two to three months. Biggest deal: a 40 lakh contract
with a hospital chain that took five months — it closed because you got their CFO on a call
early. Current pay 14 lakhs fixed, 20 with incentives; expecting 18 fixed. Notice 60 days.
Fine with Bangalore, hybrid. When asked about CTC, first just say "twenty" and let them ask
whether that's fixed.""",
    "support": """Ravi, 2 years in customer support at a food-delivery company, chat and email, around 60
tickets a day on Freshdesk. CSAT around 92 percent, you don't remember your handle time. A
recent tough customer: a refund that failed twice; you escalated to payments and called the
customer back yourself the next day. Already on rotational shifts, weekends are fine but you
would prefer not to do night shifts permanently — say so if asked. Current CTC 3.6 lakhs,
expecting 4.5. Notice 30 days. You speak Hindi, English and some Marathi.""",
    "nursing": """Nisha, 5 years as a registered nurse at Sample Hospital in Bangalore. Your registration with
the Karnataka State Nursing Council is active; do not share a registration number or any
patient details. You have 2 years in adult emergency triage and 3 years on a medical-surgical
ward. You are comfortable with rotating night and weekend shifts. Current CTC is 6 lakhs,
expected is 7 lakhs, notice is 30 days, and you are open to a hybrid role in Bangalore. No
other offers.""",
    "language-freelancer": """Ravi, a final-year B.Com student in Hubli. You have a laptop (an old Lenovo). You speak
Kannada and Hindi at home and can read and write Kannada well; your English is okay. You
could give three to four hours a day, more on weekends. You don't earn anything right now
apart from tuition, maybe eight thousand a month. You are a little suspicious at first —
ask once "is this real or some scam?" — and relax when it sounds specific.""",
}


async def main(slug: str) -> None:
    uc = load(slug)
    room_name = f"demo-{slug}-{int(time.time())}"
    brief = {**uc.get("defaults", {}), **uc["demo"], "use_case": slug, "candidate_name": "Ravi",
             "agent_name": "Meera", "company_name": "Acme"}
    lk = api.LiveKitAPI()
    await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
        agent_name=os.environ.get("AGENT_NAME", "openfury-demo"), room=room_name,
        metadata=json.dumps(brief)))
    token = (api.AccessToken().with_identity("candidate").with_name("Ravi")
             .with_grants(api.VideoGrants(room_join=True, room=room_name)).to_jwt())
    room = rtc.Room()
    await room.connect(os.environ["LIVEKIT_URL"], token)
    gone = asyncio.Event()
    room.on("disconnected", lambda *_: gone.set())

    tts = GeminiTTS(model=os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts"),
                    voice_name="Charon", api_key=os.environ["GEMINI_API_KEY"], instructions=None)
    _speak_with_locale(tts, "en-IN")
    session = AgentSession(stt=smallestai.STT(language="en"), llm=make_llm(model_env="DEMO_LLM"),
                           tts=tts, vad=silero.VAD.load(), turn_detection="vad",
                           min_endpointing_delay=0.6)
    await session.start(room=room, agent=Agent(instructions=STYLE + PERSONAS[slug]),
                        room_input_options=RoomInputOptions(close_on_disconnect=False))
    print(f"room {room_name}: waiting for the call to end…", flush=True)
    try:
        await asyncio.wait_for(gone.wait(), timeout=uc.get("max_minutes", 12) * 60 + 60)
    except asyncio.TimeoutError:
        pass
    await session.aclose()
    await room.disconnect()
    await lk.aclose()
    print(f"done: {room_name}  (recording: $DATA_DIR/calls/{room_name}.mp3)")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in PERSONAS:
        sys.exit(f"usage: demo_call.py {{{'|'.join(PERSONAS)}}}")
    asyncio.run(main(sys.argv[1]))
