"""The voice agent: a LiveKit worker that holds one screening call per job.

Two things exist here beyond "an LLM on a phone line":

  A HARD TIME CAP. Each use case sets one (12 minutes for a job screen), with a wrap-up nudge
  before it. A screening call that runs long is not thorough, it is expensive and it annoys a
  candidate who is usually taking it between meetings.

  A POST-CALL RECORD. The call is not the deliverable — the verdict, the summary and the
  filled table are. They are produced by a second LLM pass over the transcript rather than
  by asking the agent to narrate them mid-call, because a model reciting a table out loud
  is both slow and a worse table.

Which call it is comes from the dispatch metadata's `use_case` (see use_cases/). How it sounds
and takes turns is in `voice.py`.

    python -m openfury.worker dev          # local, with USE_CASE=engineering etc.
    python -m openfury.worker start        # production
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit import api
from livekit import rtc  # noqa: E402
from livekit.agents import (Agent, AgentSession, JobContext, JobProcess, RoomInputOptions,
                            RunContext, WorkerOptions, cli, function_tool,
                            get_job_context, metrics)
from livekit.plugins import noise_cancellation, silero

from livekit.plugins.google.beta import GeminiTTS

from openfury.use_cases import VOICES, load, render
from openfury.voice import (attach_ambience, make_llm, make_stt, make_tts, paced_tts_node,
                            turn_taking_kwargs)

load_dotenv()
log = logging.getLogger("openfury.worker")

# Calls are evidence a client reviews — transcript, record and audio — so they live somewhere
# a reboot does not wipe.
DATA = Path(os.environ.get("DATA_DIR", "data"))
RECORDS = DATA / "calls"

# Who she is when the brief doesn't say. A real call always carries these in job metadata.
DEFAULTS = {"agent_name": "Meera", "company_name": "Acme", "candidate_name": "there",
            "client_company": ""}


class Recruiter(Agent):
    def __init__(self, values: dict, tts, prompt: str, system_prompt: str | None = None,
                 role_block: str | None = None) -> None:
        # Precedence: a full override wins (local experiments), else the use case's prompt
        # with the role-specific block appended, else the bare prompt so a plain
        # `lk dispatch` still works.
        base = system_prompt or render(prompt, values)
        super().__init__(instructions=base + ("\n" + role_block if role_block else ""))
        self.values = values
        self._tts_ref = tts

    @function_tool
    async def end_call(self, ctx: RunContext, reason: str) -> str:
        """End the phone call. Call this ONLY after you have spoken your closing line.

        Use it when: the screen is complete and you have closed; the candidate is not
        interested and you have thanked them; they asked for a callback and you captured
        the time; it is the wrong person; they asked to be removed; or you have reached the
        twelve-minute cap.

        Never call it mid-sentence, never before saying goodbye, and never instead of
        answering a question the candidate just asked.

        Args:
            reason: why the call is ending — completed, not_interested, callback,
                    wrong_person, removed, time_cap, voicemail (an automated message
                    answered, not a person)
        """
        log.info("END_CALL requested reason=%s", reason)
        self.values["_end_reason"] = reason           # into the call log the platform delivers
        # The hang-up runs AFTER this tool returns. A tool cannot wait for the playout of the
        # speech it belongs to — LiveKit raises, the old suppress() swallowed it, and the room
        # was deleted 0.4 s later whether or not she had said a word. On a live call she made
        # end_call her entire reply to "is there any relocation assistance?" and the
        # candidate's question was met with a dead line.
        if self._hangup and not self._hangup.done():
            self._hangup.cancel()                     # a newer request replaces a stale one
        self._hangup = asyncio.create_task(self._hang_up_after_goodbye(reason))
        return None   # nothing to say about it; no follow-up generation

    # Times she STARTED speaking, from agent_state_changed (wired in entrypoint). Whether a
    # candidate message was answered is judged from these, not from chat history: history
    # gets her line only after LiveKit finalises the turn, and checking it too early refused
    # a hang-up after she HAD answered — twice, on a live call.
    _spoke_at: list[float]
    _hangup: asyncio.Task | None = None
    _refused_at: float = 0.0

    def _unanswered(self) -> str | None:
        """The candidate's last message, if she has not started speaking since it; else None."""
        users = [i for i in self.chat_ctx.items
                 if getattr(i, "type", "") == "message" and i.role == "user"]
        if not users:
            return None
        last = users[-1]
        if any(t >= last.created_at for t in getattr(self, "_spoke_at", [])):
            return None
        return (last.text_content or "").strip()

    _acks: list = []
    _ack_i: int = 0
    _ack_on: bool = False

    async def prerender_acks(self) -> None:
        """Render the short acknowledgements once, in her voice, before the call needs them."""
        out = []
        for word in ("Okay.", "Got it.", "Right.", "Sure."):
            frames = []
            with contextlib.suppress(Exception):
                async for ev in self._tts_ref.synthesize(word):
                    frames.append(ev.frame)
            if frames:
                out.append(frames)
        self._acks = out
        log.info("ACKS pre-rendered: %d", len(out))

    def _next_ack(self):
        """An acknowledgement for a reply to the candidate — not for the opener (spoken with
        its own audio), not before any reply exists, and never the same word twice in a row."""
        if not self._ack_on or not self._acks:
            return None
        a = self._acks[self._ack_i % len(self._acks)]
        self._ack_i += 1
        return a

    def _last_user_text(self, since: float) -> str | None:
        users = [i for i in self.chat_ctx.items
                 if getattr(i, "type", "") == "message" and i.role == "user"
                 and i.created_at >= since]
        return (users[-1].text_content or "").strip() if users else None

    def _speaking(self) -> bool:
        sp = self.session.current_speech
        return self.session.agent_state == "speaking" or (sp is not None and not sp.done())

    async def _hang_up_after_goodbye(self, reason: str) -> None:
        """Hang up once she has stopped talking, the candidate isn't, and nothing they said is
        left unanswered. Only one of these runs at a time — a stale one hung up a live call in
        the middle of her next sentence."""
        try:
            # "Please hang up, my boss is coming": refusing end_call as "unanswered" cost an
            # extra LLM turn and 10 s of a line they had begged to end (2026-10-07). The answer
            # to that request is a quick bye and a dead line, now.
            asked = self._last_user_text(since=time.time() - 20)
            if reason != "voicemail" and asked and _wants_hangup(asked):
                log.info("END_CALL fast — they asked to hang up: %r", asked[:80])
                with contextlib.suppress(Exception):
                    if not self._speaking() and self._unanswered() is not None:
                        await self.session.say("Sure, sorry — bye!", allow_interruptions=False)
                    else:
                        for _ in range(50):           # let her finish the bye she's saying
                            if not self._speaking():
                                break
                            await asyncio.sleep(0.1)
                ctx_job = get_job_context()
                with contextlib.suppress(Exception):
                    await ctx_job.api.room.delete_room(api.DeleteRoomRequest(room=ctx_job.room.name))
                return
            for _ in range(300):                      # up to ~60 s for her to finish
                if not self._speaking():
                    break
                await asyncio.sleep(0.2)
            # A real person waits a beat after "bye". On a test the candidate began "wait, one
            # more thing" ~0.5 s after her goodbye — before speech detection had flagged them —
            # and the line went dead mid-question. 1.8 s, watched in 100 ms steps.
            heard = False
            for _ in range(18):
                await asyncio.sleep(0.1)
                if self.session.user_state == "speaking":
                    heard = True
                    break
            if self._speaking():
                log.info("END_CALL held — she is speaking again; waiting for the next end_call")
                return
            if heard or self.session.user_state == "speaking":
                # Most of what people say after "goodbye" is goodbye. Holding for it made a
                # 34 s ping-pong on a live call ("Okay thank you" / "You're welcome. Goodbye."
                # / "Goodbye." / "Goodbye." / "Good."). Let them finish, read what they said:
                # a pleasantry ends the call without a reply; anything else holds it.
                said_at = time.time()
                for _ in range(60):                       # up to 6 s for them to finish
                    if self.session.user_state != "speaking":
                        break
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.9)                  # the final transcript lands
                last = self._last_user_text(since=said_at - 3)
                if _nothing_to_answer(last):
                    log.info("END_CALL — they only said %r; hanging up", last[:60])
                    with contextlib.suppress(Exception):
                        self.session.interrupt()          # no "you're welcome" round
                    ctx_job = get_job_context()
                    with contextlib.suppress(Exception):
                        await ctx_job.api.room.delete_room(
                            api.DeleteRoomRequest(room=ctx_job.room.name))
                    return
                log.info("END_CALL held — the candidate is talking; she will answer first")
                # Left alone she answers and then just stops, and the line sits open until the
                # candidate hangs up. Tell her the call was closing.
                c = self.chat_ctx.copy()
                c.add_message(role="system", content=(
                    "You were about to end the call when the candidate spoke again. Answer what "
                    "they just said. Then, unless they clearly want to keep talking, say one short "
                    "closing line and call end_call."))
                await self.update_chat_ctx(c)
                return
            # A voicemail greeting is always "unanswered", and answering it is how a recording
            # got "I'll remove your details" and a Not Interested record.
            pending = None if reason == "voicemail" else self._unanswered()
            # "Okay, thank you" after her goodbye is not a question. Refusing on it made her
            # say "Have a great day!", hear "Okay", and refuse again (2026-10-07).
            if pending is not None and _nothing_to_answer(pending):
                pending = None
            # One refusal is a nudge to answer; a second end_call means she has decided.
            if pending is not None and time.time() - self._refused_at < 30:
                log.info("END_CALL — second request after a refusal; hanging up")
                pending = None
            if pending is not None:
                log.warning("END_CALL refused — unanswered: %r", pending[:120])
                self._refused_at = time.time()
                self.session.generate_reply(instructions=(
                    "You tried to end the call without responding to the candidate. Their last "
                    f"words were: \"{pending}\". Respond to that properly first (if the answer "
                    "is not in your role details or extra context, say you'll note it and the "
                    "recruiter will confirm). Then say your closing line, then call end_call."))
                return
            log.info("END_CALL reason=%s — hanging up", reason)
            ctx_job = get_job_context()
            with contextlib.suppress(Exception):
                await ctx_job.api.room.delete_room(api.DeleteRoomRequest(room=ctx_job.room.name))
        except asyncio.CancelledError:
            pass

    async def tts_node(self, text, model_settings):
        """Speaking rate varies with what is being said.

        A recruiter slows down for a salary figure and speeds through the pitch. Reading
        "twenty eight lakhs per annum" at storytelling speed is the single most common
        reason a candidate says "sorry, how much?".
        """
        # Pacing is a nicety; audio is not. If anything in the pacing path raises, fall
        # straight through to the stock node — the call keeps its voice and we get a log
        # line instead of a silent agent. Learned the hard way: an unbound-method TypeError
        # in here was swallowed by the TTS task, and the only symptom was an agent that
        # typed and never spoke.
        # Instant acknowledgement: a pre-rendered "Okay." in her own voice goes out the moment
        # this reply starts, covering the 1.5–1.9 s the TTS takes to its first frame
        # (measured per turn, LiveKit metrics). The prompt tells her it has been said.
        ack = self._next_ack()
        if ack:
            for f in ack:
                yield f
        try:
            async for f in paced_tts_node(self, text, model_settings,
                                          tts=self._tts_ref, log=log):
                yield f
            return
        except Exception as e:
            log.error("pacing failed, falling back to default TTS: %s: %s",
                      type(e).__name__, e)
        async for f in Agent.default.tts_node(self, text, model_settings):
            yield f


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


def _speak_with_locale(tts, lang: str) -> None:
    """Indian English accent on Gemini TTS, without a style prompt.

    Measured 2026-10-03 on ten of her own short lines, accent judged by a second model from
    the audio: Aoede as-is 7/10 American; languageCode en-IN 9/10 Indian with 0/10 leaks; a
    director's-notes style block 6/10 Indian; notes + en-IN leaked the notes once. A style
    prompt is what she read aloud on live calls, so the locale is the lever. The LiveKit
    plugin builds SpeechConfig without language_code, so set it on the way out."""
    models = tts._client.aio.models
    orig = models.generate_content_stream

    async def with_locale(*, model, contents, config):
        if config is not None and config.speech_config is not None:
            config.speech_config.language_code = lang
        return await orig(model=model, contents=contents, config=config)

    models.generate_content_stream = with_locale


# --------------------------------------------------------------------- post-call record

async def write_record(session: AgentSession, values: dict, uc: dict, room: str,
                       started: float) -> None:
    """Second LLM pass over the transcript. Never raises into shutdown."""
    try:
        turns = []
        for item in session.history.items:
            role = getattr(item, "role", None)
            text = getattr(item, "text_content", None) or ""
            if role in ("user", "assistant") and text.strip():
                who = "CANDIDATE" if role == "user" else "RECRUITER"
                turns.append(f"{who}: {text.strip()}")
        transcript = "\n".join(turns)
        RECORDS.mkdir(parents=True, exist_ok=True)
        stem = RECORDS / f"{room}"
        (stem.with_suffix(".transcript.txt")).write_text(transcript, encoding="utf-8")
        _write_call_log(session, values, stem, started)

        if not transcript.strip():
            log.warning("no transcript — nothing to score")
            return

        from google import genai
        from google.genai import types as gtypes
        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY")
                              or os.environ["GEMINI_API_KEY"])
        custom = values.get("_custom_fields") or []
        extra = ("" if not custom else
                 "\n\nALSO return a top-level \"custom\" object with exactly these keys. Each "
                 "value is what the candidate said, or exactly \"Not captured\":\n"
                 + "\n".join(f'- "{f["key"]}": {f["question"]}' for f in custom))
        r = await asyncio.to_thread(
            client.models.generate_content,
            model=os.environ.get("RECORD_LLM", "gemini-2.5-flash"),
            contents=(render(uc["record"], values) + extra + "\n\n" + ("" if not uc.get("compose") else
                      f"ROLE: {values.get('role_name')} at "
                      f"{values.get('company_name')}, budget {values.get('ctc_min')} to "
                      f"{values.get('ctc_max')}, max notice {values.get('max_notice_period')}, "
                      f"{values.get('work_mode')} in {values.get('role_location')}.\n\n")
                      + f"TRANSCRIPT:\n{transcript}"),
            config=gtypes.GenerateContentConfig(
                temperature=0, response_mime_type="application/json",
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0)),
        )
        rec = json.loads(r.text)
        rec["_meta"] = {"room": room, "duration_s": round(time.time() - started, 1),
                        "use_case": uc["id"], "role": values.get("role_name"),
                        "company": values.get("company_name")}
        stem.with_suffix(".json").write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                                             encoding="utf-8")

        md = [f"# {rec.get('verdict','?')} — {values.get('candidate_name')}",
              "", f"**{values.get('role_name')}** at {values.get('company_name')} · "
              f"{rec['_meta']['duration_s']}s", "", rec.get("summary", ""), "",
              "| Field | Value |", "|---|---|"]
        for k, v in (rec.get("fields") or {}).items():
            md.append(f"| {k} | {v or 'Not captured'} |")
        mc = rec.get("must_capture") or {}
        if mc:
            md += ["", "## Must capture", ""]
            md += [f"- **{k}**: {v or 'Not captured'}" for k, v in mc.items()]
        stem.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
        log.info("RECORD verdict=%s -> %s", rec.get("verdict"), stem.with_suffix(".md"))
    except Exception as e:  # a failed record must not take the call down with it
        log.error("record failed: %s: %s", type(e).__name__, e)


def _write_call_log(session: AgentSession, values: dict, stem: Path, started: float) -> None:
    """Everything that happened on the call, as data: each turn with its time and latency, why
    it ended, what it cost. The platform API hands this to the client, who keeps it."""
    try:
        turns = []
        for item in session.history.items:
            role = getattr(item, "role", None)
            text = (getattr(item, "text_content", None) or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            t = {"speaker": "candidate" if role == "user" else "agent", "text": text,
                 "at_s": round(max(0.0, (getattr(item, "created_at", started) or started) - started), 2),
                 "interrupted": bool(getattr(item, "interrupted", False))}
            if role == "user" and getattr(item, "transcript_confidence", None) is not None:
                t["stt_confidence"] = round(item.transcript_confidence, 3)
            m = getattr(item, "metrics", None) or {}
            lat = {k: round(m[k], 3) for k in ("e2e_latency", "end_of_turn_delay",
                                                 "llm_node_ttft", "tts_node_ttfb")
                   if isinstance(m.get(k), (int, float))}
            if lat:
                t["latency_s"] = lat
            turns.append(t)
        out = {"turns": turns, "end_reason": values.get("_end_reason"),
               "duration_s": round(time.time() - started, 1),
               "usage": values.get("_usage") or {}}
        stem.with_suffix(".log.json").write_text(json.dumps(out, indent=2, ensure_ascii=False),
                                                 encoding="utf-8")
    except Exception as e:  # the log is a convenience; never let it cost the record
        log.error("call log failed: %s: %s", type(e).__name__, e)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    meta = ctx.job.metadata or ""
    try:
        supplied = json.loads(meta) if meta.strip().startswith("{") else {}
    except json.JSONDecodeError:
        supplied = {}
    # The use case picks the prompt, the record, the time cap and the voice.
    uc = load(supplied.pop("use_case", None) or os.environ.get("USE_CASE", "job-screening"))
    # `role_block` is the role-specific half, appended to the prompt that lives HERE. That is
    # the safer shape — the voice rules, the CTC disclosure rule and the safety rules never
    # leave the worker, so nothing upstream can replace them, not even by accident.
    # `system_prompt` replaces the lot and exists for local experiments only.
    system_prompt = supplied.pop("system_prompt", None)
    role_block = supplied.pop("role_block", None)
    # Inbound: someone phones her number, and the SIP dispatch rule hands her a brief as if
    # SHE had placed the call. The caller's identity is then sip_<number>, not "candidate",
    # so she links to the SIP participant instead.
    inbound = bool(supplied.pop("inbound", False))
    values = {**DEFAULTS, **uc.get("defaults", {}), **uc.get("demo", {}), **supplied}
    log.info("BRIEF use_case=%s role=%r candidate=%r role_block=%s override=%s",
             uc["id"], values.get("role_name"), values.get("candidate_name"),
             f"{len(role_block)} chars" if role_block else "none",
             "yes" if system_prompt else "no")

    # ElevenLabs by default; TTS=gemini or TTS=sarvam switch. Neither ElevenLabs nor Gemini
    # has a rate knob, so only Sarvam slows down for money (see voice.paced_tts_node).
    which = os.environ.get("TTS", "eleven").lower()
    tts_cfg = uc.get("tts", {})
    if which == "gemini":
        # Gemini first audio p50 1474 ms on gemini-3.8-flash-tts, 1046 ms on -lite.
        tts = GeminiTTS(model=os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts"),
                        voice_name=os.environ.get("GEMINI_TTS_VOICE", "Aoede"),
                        api_key=os.environ["GEMINI_API_KEY"],
                        # None, not a style prompt: the plugin prepends instructions to every
                        # line, and on short turns Gemini SPEAKS them — candidates heard "say
                        # in a natural Indian English accent" on live calls. Leaving it unset
                        # would apply the plugin's own default prompt, which can leak the same way.
                        instructions=None)
        _speak_with_locale(tts, os.environ.get("GEMINI_TTS_LANG", "en-IN"))
    elif which == "eleven":
        from livekit.agents import tts as lk_tts
        from livekit.plugins import elevenlabs
        # One HTTP request per whole sentence, no language code. Saavi on v3 Conversational
        # sounded Indian rendered that way and British on a live call that fed the websocket
        # word by word with language="en". First audio ~250 ms per sentence.
        voice = VOICES.get(values.get("_voice") or uc.get("voice") or "saavi", VOICES["saavi"])
        lang = os.environ.get("ELEVEN_LANG") or tts_cfg.get("language")
        tts = lk_tts.StreamAdapter(tts=elevenlabs.TTS(
            voice_id=os.environ.get("ELEVEN_VOICE_ID") or voice["eleven_id"],
            model=os.environ.get("ELEVEN_MODEL") or tts_cfg.get("model", "eleven_v3_conversational"),
            # "hi" on Flash/Turbo makes the English desi; Saavi without it read as London.
            **({"language": lang} if lang else {}),
            api_key=os.environ["ELEVENLABS_API_KEY"]))
    else:
        tts = make_tts()
    log.info("TTS %s", type(tts).__module__)
    # Smallest Pulse by default for cost; STT=sarvam is the saaras:v4 codemix baseline.
    # Language "hi", measured on real candidate channels: "en" turns Hinglish into nonsense
    # ("Expected me boocha … Achachaduna"), "hi" keeps English words in Latin and Hindi in
    # Devanagari and caught "forty LPA" that "en" lost.
    which_stt = (os.environ.get("STT") or uc.get("stt") or "smallest").lower()
    if which_stt == "sarvam" and uc.get("stt") == "sarvam":
        # A use case asking for Sarvam gets auto-detect. Measured on Telugu, Kannada,
        # Malayalam and Hindi replies: "unknown" writes each in its own script with English
        # words left in Latin ("నా దగ్గర laptop ఉంది"); en-IN translated some and returned
        # "Out." for a Telugu sentence.
        from livekit.plugins import sarvam as _sarvam
        stt = _sarvam.STT(model="saaras:v4", mode="codemix",
                          language=uc.get("stt_lang") or "unknown")
    elif which_stt == "smallest":
        from livekit.plugins import smallestai
        stt = smallestai.STT(language=os.environ.get("SMALLEST_LANG")
                             or uc.get("stt_lang") or "hi")
    else:
        stt = make_stt()
    log.info("STT %s", type(stt).__module__)
    session: AgentSession = AgentSession(
        stt=stt, llm=make_llm(model_env="LLM_MODEL",
                              # first token p50 841 ms vs 1041 for 2.5-flash, same prompt and turn
                              default="gemini-3.5-flash-lite"), tts=tts,
        vad=ctx.proc.userdata["vad"], **turn_taking_kwargs())

    started = time.time()
    usage = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _m(ev):
        usage.collect(ev.metrics)

    await attach_ambience(ctx, session, log)
    # BACKGROUND VOICE CANCELLATION. Not noise suppression — voice suppression. Without
    # it, a colleague talking behind the candidate lands in the transcript as if the
    # candidate said it, and the agent answers a question nobody asked her. On a screening
    # call people take from a desk, a corridor, a cab; there is almost always a second
    # voice.
    #
    # Phone calls get the 8 kHz telephony model. The wideband BVC on a phone line read line
    # noise as 11.7 s of "candidate speaking" through her whole opener.
    phone = bool(inbound or values.get("candidate_phone") or values.get("campaign")
                 or os.environ.get("TELEPHONY"))
    nc = noise_cancellation.BVCTelephony() if phone else noise_cancellation.BVC()
    link = ({"participant_kinds": [rtc.ParticipantKind.PARTICIPANT_KIND_SIP]} if inbound
            # Pinned: an observer (a dashboard showing the live transcript) may join the room.
            # Unpinned, the agent links to whoever joins first — which can be the observer,
            # before the phone answers.
            else {"participant_identity": "candidate"})
    agent = Recruiter(values, tts, uc["prompt"], system_prompt, role_block)
    agent._spoke_at = []

    # Live transcript for calls placed through the platform API: one JSON line per finished
    # turn, which api.py tails into webhooks and its event stream while the call runs.
    live = (RECORDS / f"{ctx.room.name}.live.jsonl"
            if str(values.get("campaign", "")).startswith("api:") else None)
    if live:
        RECORDS.mkdir(parents=True, exist_ok=True)

    @session.on("conversation_item_added")
    def _turn_metrics(ev) -> None:
        # Where each reply's wait goes. ChatMessage.metrics carries LiveKit's own split.
        item = getattr(ev, "item", None)
        text = (getattr(item, "text_content", None) or "").strip()
        if live and text and getattr(item, "role", "") in ("user", "assistant"):
            with contextlib.suppress(Exception), open(live, "a", encoding="utf-8") as f:
                f.write(json.dumps({"speaker": "candidate" if item.role == "user" else "agent",
                                    "text": text, "at_s": round(time.time() - started, 2),
                                    "interrupted": bool(getattr(item, "interrupted", False))},
                                   ensure_ascii=False) + "\n")
        m = getattr(item, "metrics", None) or {}
        if getattr(item, "role", "") == "assistant" and m:
            log.info("LATENCY e2e=%s eot=%s llm_ttft=%s tts_ttfb=%s | %s",
                     *(f"{m[k]:.2f}" if isinstance(m.get(k), (int, float)) else "-"
                       for k in ("e2e_latency", "end_of_turn_delay", "llm_node_ttft", "tts_node_ttfb")),
                     (item.text_content or "")[:50])
        # She says goodbye and then never calls end_call — on a live call (2026-10-07) the
        # candidate begged "please hang up, my boss is coming", she said "No problem — I'll
        # call you back later. Thanks for your time!" and the line sat open 20 s until they
        # hung up. A spoken sign-off is the hang-up; the usual guards still apply.
        if (getattr(item, "role", "") == "assistant"
                and _is_signoff(item.text_content or "")
                and (agent._hangup is None or agent._hangup.done())):
            async def _hang_up_if_she_forgot() -> None:
                await asyncio.sleep(1.5)              # a real end_call may still be in flight
                if agent._hangup is None or agent._hangup.done():
                    log.info("END_CALL auto — she signed off without calling end_call")
                    agent.values.setdefault("_end_reason", "completed")
                    agent._hangup = asyncio.create_task(agent._hang_up_after_goodbye("signoff"))
            asyncio.create_task(_hang_up_if_she_forgot())

    # Silence. LiveKit marks the candidate "away" after 15 s in which nobody has spoken, and
    # nothing listened for it: on a test call (2026-10-16) the candidate's answer never
    # reached the transcript, she waited for it, and the line sat open 66 s until they hung
    # up. One check-in; a second silence ends the call. After her goodbye, no check-in.
    silences = {"n": 0}

    @session.on("user_state_changed")
    def _user_state(ev) -> None:
        if ev.new_state == "speaking":
            silences["n"] = 0
            return
        if ev.new_state != "away" or (agent._hangup is not None and not agent._hangup.done()):
            return
        if not agent._ack_on:            # still ringing or mid-opener — not their silence
            return
        silences["n"] += 1
        said = [i for i in agent.chat_ctx.items
                if getattr(i, "type", "") == "message" and i.role == "assistant"]
        signed_off = bool(said) and _is_signoff(said[-1].text_content or "")
        if silences["n"] == 1 and not signed_off:
            log.info("SILENCE 15s — checking they're still there")
            session.generate_reply(instructions=(
                "The candidate has said nothing for fifteen seconds. Their answer may have been "
                "lost on the line. In one short line, say you didn't catch that and ask your last "
                "question again in fewer words, or ask if they're still there. Do not call end_call."))
            return
        log.info("SILENCE — no response after %s; ending the call",
                 "her goodbye" if signed_off else "a check-in")
        agent.values.setdefault("_end_reason", "no_response")

        async def _bye() -> None:
            with contextlib.suppress(Exception):
                if not signed_off:
                    await session.say("I think we've lost the line — the team will reach out "
                                      "to you. Bye!", allow_interruptions=False)
            with contextlib.suppress(Exception):
                await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        agent._hangup = asyncio.create_task(_bye())

    @session.on("agent_state_changed")
    def _agent_state(ev) -> None:
        if ev.new_state == "speaking":
            agent._spoke_at.append(time.time())
    await session.start(room=ctx.room, agent=agent,
                        room_input_options=RoomInputOptions(noise_cancellation=nc, **link))

    # The opening is fixed text, said not generated — the prompt says SAY EXACTLY THIS, and
    # generate_reply would paraphrase it.
    # No claim about WHERE the profile came from: the source differs per campaign (LinkedIn,
    # Naukri Resdex…) and the brief's extra context says what to answer when asked. An opener
    # that names the wrong source contradicts her own answer thirty seconds later.
    client = (values.get("client_company") or "").strip()
    # Five seconds, not thirteen: on a cold call the opener is the window in which they
    # decide to stay. Said uninterruptibly — a "hello?" on picking up used to cut it off
    # after one sentence and leave them in silence.
    opener = render(uc["opener"], values) if uc.get("opener") else render(
        'Hi {{candidate_name}}, this is {{agent_name}} from {{company_name}}. I\'m calling about a '
        '{{role_name}} role' + (f' at {client}' if client else '') +
        '. Do you have two minutes?', values)

    # Render the opener while the phone rings, and say it only once the call is ANSWERED.
    # The dial does not wait for the answer (so the console can show "ringing"), which means
    # the phone joins the room while still ringing. On the first campaign call she spoke the
    # whole opener into the ring tone and the candidate picked up to silence; the old
    # wait-for-answer dial had hidden this, and so had answering fast.
    async def render_opener() -> list:
        frames = []
        with contextlib.suppress(Exception):
            async for ev in tts.synthesize(opener):
                frames.append(ev.frame)
        return frames

    pre = asyncio.create_task(render_opener())
    acks = asyncio.create_task(agent.prerender_acks())
    answered = await _wait_for_answer(ctx, inbound, started)
    if not answered:
        log.warning("CALL never answered — not speaking")
        pre.cancel()
        return
    frames = await pre

    async def play():
        for f in frames:
            yield f

    log.info("OPENER at %.1fs after dispatch (%d frames pre-rendered)", time.time() - started, len(frames))
    await session.say(opener, audio=play() if frames else None, allow_interruptions=False)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(acks, timeout=10)
    agent._ack_on = True

    wrap_up_at = float(os.environ.get("WRAP_UP_S") or uc.get("wrap_up_minutes", 10) * 60)
    hard_cap_at = float(os.environ.get("HARD_CAP_S") or uc.get("max_minutes", 12) * 60)

    async def clock() -> None:
        """Wrap-up nudge, then a hard stop at the use case's max_minutes.

        The nudge is a hint to the model rather than a spoken line, so it wraps up in its
        own words and still lands the missing must-capture item. The cap is hard: at the cap
        the call ends whether or not the screen is complete, and the record simply
        says what was not captured.
        """
        try:
            await asyncio.sleep(wrap_up_at)
            log.info("CLOCK wrap-up at %.0fs", time.time() - started)
            session.generate_reply(instructions=(
                "You are near the time limit. Begin wrapping up now. Ask only the single most "
                "important thing you still have not captured, then close with the standard "
                "closing line. Do not start a new topic."))
            await asyncio.sleep(max(0.0, hard_cap_at - wrap_up_at))
            log.info("CLOCK hard cap at %.0fs — ending", time.time() - started)
            await session.aclose()          # hard stop; the record still gets written
        except asyncio.CancelledError:
            pass

    # When the conversation is over, close the room ourselves. Before the console's hidden
    # observer existed, the candidate hanging up emptied the room and that ended the job; now
    # an open console tab keeps the room alive, the job never ends, and the record — written
    # at shutdown — never gets written. Found on a test room still open a day later.
    @session.on("close")
    def _closed(ev) -> None:
        log.info("SESSION closed (%s) — closing room", getattr(ev, "reason", ""))
        async def _close() -> None:
            with contextlib.suppress(Exception):
                await ctx.delete_room()
            ctx.shutdown(reason="session closed")
        asyncio.create_task(_close())

    task = asyncio.create_task(clock())
    ctx.add_shutdown_callback(lambda: _shutdown(task, session, values, uc, ctx.room.name,
                                                started, usage))


_PLEASANTRY = re.compile(
    r"^(?:(?:ok(?:ay)?|thanks?|thank you|thank you so much|bye|bye bye|goodbye|good ?bye|good|"
    r"sure|alright|all right|fine|yes|yeah|ya|haan|ji|theek hai|accha|great|cool|perfect|"
    r"you too|same to you|have a good day|take care|ma'?am|sir|"
    # Devanagari: STT writes Hindi words in script. "हाँ, okay, thank you" was refused as an
    # unanswered question on a live call (2026-10-16) and the candidate hung up on her.
    r"हाँ|हां|हा|जी|ठीक है|ठीक|अच्छा|धन्यवाद|शुक्रिया|ओके|थैंक यू|बाय|नमस्ते)[\s,.!?।]*)+$", re.I)


_SIGNOFF = re.compile(
    r"(thanks?(?: you)?(?: so much)? for (?:your time|letting me know|talking|speaking|your interest)|"
    r"good ?bye|\bbye\b|take care|talk (?:to you )?soon|"
    r"have a (?:good|great|nice|lovely|wonderful) (?:day|evening|one|weekend)|i'?ll call you back)", re.I)


_WANTS_HANGUP = re.compile(
    r"(hang ?up|cut (?:the )?call|disconnect|call (?:me )?later|call (?:me )?back later|"
    r"can'?t talk|cannot talk|not (?:able|free) to talk|busy right now|boss is)", re.I)


def _wants_hangup(text: str) -> bool:
    """The candidate asked her to end the call — the answer to that IS hanging up."""
    return bool(_WANTS_HANGUP.search(text or ""))


def _is_signoff(text: str) -> bool:
    """Her line closes the call: a sign-off phrase, and it doesn't end on a question."""
    t = text.strip()
    return bool(t) and not t.endswith("?") and bool(_SIGNOFF.search(t))


def _nothing_to_answer(text: str | None) -> bool:
    """A pleasantry, or a fragment too short to be a question ("Hell", "o  Hello").
    Holding the line for those looped end_call five times on a live call (2026-10-07)."""
    t = (text or "").strip()
    words = len(re.findall(r"[^\W\d_]{2,}", t))
    if not t or _is_pleasantry(t):
        return True
    return words < (2 if "?" in t else 3)


def _is_pleasantry(text: str) -> bool:
    """'Okay thank you', 'Goodbye.', 'Bye bye ma'am' — words that close a call, nothing else."""
    t = text.strip()
    return bool(t) and len(t.split()) <= 6 and bool(_PLEASANTRY.match(t))


async def _wait_for_answer(ctx: JobContext, inbound: bool, started: float,
                           timeout: float = 75.0) -> bool:
    """True once the person is actually on the line.

    A phone leg joins the room while still RINGING; its sip.callStatus attribute moves
    dialing -> ringing -> active (answered) -> hangup. Inbound calls and browser/test
    participants are live the moment they join."""
    if inbound:
        return True
    while time.time() - started < timeout:
        p = ctx.room.remote_participants.get("candidate")
        if p is not None:
            if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                return True                     # browser rehearsal / scripted candidate
            st = (p.attributes or {}).get("sip.callStatus", "")
            if st == "active":
                return True
            if st == "hangup":
                return False
        await asyncio.sleep(0.1)
    return False


async def _shutdown(task, session, values, uc, room, started, usage) -> None:
    task.cancel()
    summary = usage.get_summary()
    log.info("usage: %s", summary)
    with contextlib.suppress(Exception):
        import dataclasses
        values["_usage"] = dataclasses.asdict(summary)
    await write_record(session, values, uc, room, started)
    await _keep_audio(session, room)


async def _keep_audio(session, room: str) -> None:
    """Copy the call's stereo recording out of the job's temp dir before it is deleted.

    AgentSession records by default (RecorderIO): the agent on one channel, the candidate on
    the other, on one aligned timeline — but into a TemporaryDirectory that goes when the job
    ends. Closing the session finalises the file."""
    try:
        with contextlib.suppress(Exception):
            await session.aclose()
        rio = getattr(session, "_recorder_io", None)
        src = rio.output_path if rio else None
        if not src or not Path(src).exists():
            log.warning("no recording for %s", room)
            return
        RECORDS.mkdir(parents=True, exist_ok=True)
        dst = RECORDS / f"{room}.ogg"
        shutil.copyfile(src, dst)
        log.info("AUDIO kept -> %s (%d KB)", dst, dst.stat().st_size // 1024)
        # MP3 beside it: the client opens these links on whatever they have, and older
        # iPhone Safari will not play Ogg Opus. Stereo kept — one voice per side.
        mp3 = dst.with_suffix(".mp3")
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-y", "-i", str(dst), "-ac", "2", "-b:a", "96k",
            str(mp3))
        if await p.wait() == 0:
            log.info("AUDIO mp3 -> %s (%d KB)", mp3, mp3.stat().st_size // 1024)
    except Exception as e:  # a lost recording must not take the record down with it
        log.error("keeping audio failed: %s: %s", type(e).__name__, e)


def main() -> None:
    # Two workers on one box need separate health ports, and the collision fails with a
    # bare "address already in use" that looks nothing like the real cause.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm,
                              # a second name lets a test worker (another TTS, another LLM)
                              # take only the calls dispatched to it, not live ones
                              agent_name=os.environ.get("AGENT_NAME", "openfury"),
                              port=int(os.environ.get("WORKER_PORT", "8082"))))


if __name__ == "__main__":
    main()
