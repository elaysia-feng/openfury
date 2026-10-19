"""How the agent sounds and takes turns, and the measurements behind each setting.

Every number here was taken off real screening calls and sessions, not from a default. If you
change one, measure it the same way and update the comment — that is how this file stays honest.
"""
from __future__ import annotations

import os

from livekit.agents import AudioConfig, BackgroundAudioPlayer, BuiltinAudioClip
from livekit.agents.inference import TurnDetector
from livekit.plugins import sarvam


def make_stt():
    """saaras:v4 + codemix.

    The plugin's default MODE (`transcribe`) renders ENGLISH SPEECH IN DEVANAGARI —
    "आई एम हियर कॉलिंग यू" — measured on our own recordings. The LLM then cannot tell which
    language the caller used and answers in the same mangled form. `codemix` keeps each
    language in its own script. The default MODEL is fine; only the mode was wrong.
    """
    return sarvam.STT(model="saaras:v4", mode="codemix",
                      language=os.environ.get("STT_LANG", "en-IN"))


def make_tts(speaker: str | None = None):
    """bulbul:v3.

    `pitch` and `loudness` are v2-only and silently ignored here, and bulbul:v2 is
    deprecated — so SPEAKER and TEXT are the only two levers that exist.

    Measured on identical warm text: suhani 95 Hz F0 spread, tanya 68, priya 66, shreya 65.
    And on the same voice, flat phrasing gives 38 Hz against warm phrasing's 66 — which is
    why both agents' prompts teach prosody explicitly. The voice takes its delivery from
    the text and nothing else.
    """
    return sarvam.TTS(
        model="bulbul:v3",
        speaker=speaker or os.environ.get("SARVAM_SPEAKER", "suhani"),
        target_language_code=os.environ.get("SARVAM_LANG", "en-IN"),
        temperature=float(os.environ.get("SARVAM_TEMP", "0.7")),
        pace=float(os.environ.get("SARVAM_PACE", "1.15")),
        dict_id=os.environ.get("SARVAM_DICT_ID") or None,
    )


def make_llm(model_env: str = "LLM_MODEL", default: str = "gemini-2.5-flash",
             temperature: float = 0.3):
    """Gemini with THINKING OFF.

    Measured: LLM time-to-first-token p50 2.22s with thinking on, 1.16s with it off — the
    single dominant term in a turn. These agents follow a script; there is nothing to reason
    about.
    """
    from google.genai import types as gtypes
    from livekit.plugins import google
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("neither GOOGLE_API_KEY nor GEMINI_API_KEY is set")
    model = os.environ.get(model_env, default)
    # Gemini 3 takes a thinking LEVEL and answers a budget with 400; 2.x takes a budget.
    thinking = (gtypes.ThinkingConfig(thinking_level="MINIMAL") if model.startswith("gemini-3")
                else gtypes.ThinkingConfig(thinking_budget=0))
    return google.LLM(model=model, api_key=key, temperature=temperature,
                      thinking_config=thinking)


def turn_taking_kwargs() -> dict:
    """Turn-taking, measured on 363 real turn gaps in human recruiter call recordings:
    p10 0.40s, p50 0.58s, p75 0.84s, p90 1.34s, none under 0.30s.
    """
    return dict(
        # version="v1" is NOT the default outside dev mode — `start` would otherwise get
        # "v1-mini", the local fallback, whose backchannel_probability is None. Backchannel
        # detection is Cloud-only in practice.
        turn_detection=TurnDetector(version="v1"),
        # One word is a backchannel, two is an interruption. NOTE: CallGideon's docs argue
        # against this — with min_words=2 the SDK DROPS a sub-2-word turn entirely, so a
        # one-word barge-in ("sorry", "wait") is discarded along with "okay". Their
        # production value is 0. Ours is the untested bet; flip it first if a real
        # interruption gets swallowed.
        min_interruption_words=2,
        min_interruption_duration=0.4,      # p10 — nothing shorter was ever a real pause
        min_endpointing_delay=0.4,
        # MEASURED: end_of_utterance_delay sat at exactly 3.000s every turn — the detector
        # was not deciding, it was timing out on the old 3.0 ceiling, and that was the
        # whole of a 4.4s response. 1.3 is above the 1.34s p90 so a genuine long pause
        # still survives.
        max_endpointing_delay=1.3,
        resume_false_interruption=True,
        false_interruption_timeout=1.0,
        # OFF. It buys ~1s of latency and pays for it in double-speak, which is worse.
        #
        # Evidence from a real transcript: she began "Great. So," on a partial turn,
        # the candidate kept talking ("We can discuss."), the turn recommitted, and a
        # SECOND generation fired while the first was still in his ear — "Excellent. To
        # start, could you tell me...". Two replies to one intent, overlapping.
        #
        # CallGideon documented this exact failure as Bug #20: merging the late fragment
        # into the prior message does not un-trigger the LLM call that already went out,
        # so suppression has to happen at generation, not at text.
        #
        # The latency it was buying is affordable now: thinking_budget=0 took LLM
        # time-to-first-token 2.22s -> 1.16s, and capping endpointing took 3.0s -> 1.3s.
        preemptive_generation=False,
        allow_interruptions=True,
    )


async def attach_ambience(ctx, session, log):
    """Room tone, plus keys only while thinking.

    office-ambience measures 0.5 sharp onsets/sec against keyboard-typing's 1.9 — genuine
    room tone, not keys. A real line is never silent, and that is what stops a pause
    reading as a dropped call.

    Traced event order on a live session:
        USER_STATE     speaking -> listening     <- KEYS START (she is writing you down)
        SPEECH_CREATED source=generate_reply
        AGENT_STATE    listening -> thinking
        AGENT_STATE    thinking  -> speaking     <- KEYS STOP (she looks up to talk)

    Keys start on USER_STATE, not on "thinking". Starting at "thinking" meant they only
    began after STT and endpointing had already finished, so they missed the first and
    longest part of the gap — the bit that actually needs filling. A person starts typing
    the moment you stop talking, not a second later.

    The player buffers ~200ms, so stop() can leave a short tail; volume is set low enough
    that a tail sits under the voice rather than competing with it.
    """
    audio = BackgroundAudioPlayer(
        ambient_sound=AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=0.3))
    await audio.start(room=ctx.room, agent_session=session)
    typing: list = [None]

    def _stop(why: str) -> None:
        h = typing[0]
        typing[0] = None
        if h is not None and not h.done():
            h.stop()
            log.info("TYPING stop (%s)", why)

    def _start() -> None:
        if typing[0] is None or typing[0].done():
            typing[0] = audio.play(
                AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.28))
            log.info("TYPING play")

    @session.on("user_state_changed")
    def _on_user(ev) -> None:
        old, new = getattr(ev, "old_state", "?"), getattr(ev, "new_state", "?")
        log.info("USER_STATE %s -> %s", old, new)
        if old == "speaking" and new == "listening":
            _start()                      # you stopped; she starts writing
        elif new == "speaking":
            _stop("user speaking")        # you cut back in; she stops to listen

    @session.on("user_input_transcribed")
    def _on_heard(ev) -> None:
        # What the STT thought the caller said. Absent from the default logs, and without
        # it a wrong name in the agent's mouth is untraceable — you cannot tell a
        # mis-hearing from a hallucination.
        if getattr(ev, "is_final", False):
            log.info("HEARD %r", (getattr(ev, "transcript", "") or "")[:120])

    @session.on("agent_state_changed")
    def _on_state(ev) -> None:
        new = getattr(ev, "new_state", "?")
        log.info("AGENT_STATE %s -> %s", getattr(ev, "old_state", "?"), new)
        if new == "speaking":
            _stop("agent speaking")       # she looks up to talk
        elif new == "listening":
            _stop("idle")                 # turn over, nothing to write

    return audio


# --------------------------------------------------------------------- how words sound

# Acronyms a recruiter SAYS AS A WORD, which the TTS otherwise spells out letter by letter.
# "ESOP" came back as "E-S-O-P" on a live call; no recruiter in India has ever said that.
#
# The inverse list is longer and deliberately absent: CTC, LPA, HR, API, SQL, PF, SDE are
# all correctly spelled out, so touching them would break what already works. Only the
# ones that are wrong are in here.
#
# This is a front-end lexicon, and it is the right layer: the fix belongs before synthesis,
# not in a prompt asking the model to please spell things phonetically — the model would
# comply unevenly and the transcript would fill with "ee-sop", which is wrong in the record.
SAY_AS_WORD = {
    "ESOPs": "ee-sops", "ESOP": "ee-sop", "esops": "ee-sops", "esop": "ee-sop",
    "SaaS": "sass", "saas": "sass",
    "JIRA": "jeera", "Jira": "jeera",
    "PaaS": "pass", "IaaS": "eye-ass",
    "OOPs": "oops", "OOP": "oh-oh-pee",
    "CI/CD": "C I C D",
    "IIT": "I I T", "IIM": "I I M",           # already right; pinned so a change is visible
}
_SAY_RE = _re_pron = None


def say_aloud(text: str) -> str:
    """Rewrite what gets synthesised, never what gets recorded.

    Applied in the TTS node only. The chat history, the transcript and the hiring record
    keep the real spelling — "ee-sop" in a record a human reads would be a defect, and the
    post-call table has to say ESOP.
    """
    global _SAY_RE
    import re
    if _SAY_RE is None:
        keys = sorted(SAY_AS_WORD, key=len, reverse=True)
        _SAY_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b")
    return _SAY_RE.sub(lambda m: SAY_AS_WORD[m.group(1)], text)


# --------------------------------------------------------------------- variable pace

import re as _re

# A person does not read a salary figure at the speed they tell a story. They slow down for
# anything the listener has to WRITE DOWN or get exactly right, and speed up through the
# part they have said two hundred times.
_SLOW = _re.compile(
    r"\d|lakh|crore|per annum|\bLPA\b|\bCTC\b|notice period|last working day|"
    r"@|\bdot\b|e-?mail|phone|number|spell|budget|salary|"
    r"[A-Z]-[A-Z]-[A-Z]", _re.I)

PACE_SLOW = 0.93     # numbers, money, dates, email, anything spelt out
PACE_NORMAL = 1.15   # the default — brisk, a recruiter on a clock
PACE_BRISK = 1.22    # boilerplate with no data in it


def pace_for(text: str, *, first: bool = False) -> float:
    """Pick a speaking rate for ONE SENTENCE.

    Per sentence, not per utterance. Measured on a real call: classifying on the first 60
    characters gave 1.15 to eight of nine turns, because a sentence that opens
    conversationally and delivers the number at character 120 never showed its number to
    the classifier. Worse, real speech does not hold one rate across a whole paragraph —
    it slows for the figure and resumes afterwards.

    `first` never gets the brisk rate. The opening line is where a candidate decides
    whether this is a real call or a scam, and rushing it is the worst possible read. The
    length heuristic used to fire on it precisely because it is long.
    """
    if _SLOW.search(text):
        return PACE_SLOW
    if first:
        return PACE_NORMAL
    if len(text) > 160:
        return PACE_BRISK
    return PACE_NORMAL


_SENT_END = _re.compile(r"[.!?।॥]\s")


async def paced_tts_node(agent, text, model_settings, *, tts, log=None):
    """`Agent.tts_node` that re-picks the rate only where the rate actually changes.

    Sentence-by-sentence synthesis was splitting one utterance into N separate TTS streams,
    and every join between streams is a place audio can clip — which is what a clipped
    half-word at a sentence boundary is. Worse, most splits bought nothing: "Got it." and
    "And how many years of experience do you have?" are both 1.15, so they were cut apart
    and restitched for no gain at all.

    So consecutive sentences at the SAME rate are batched into one synthesis. A typical
    utterance is one rate throughout and therefore one stream, exactly as before the
    pacing existed. We only pay for a join where the rate genuinely changes — going into a
    salary figure and coming out of it — which is the one place a small seam is worth it.
    """
    from livekit.agents import Agent as _Agent

    # Only Sarvam has a rate knob. Any other TTS still gets sentence batching and the
    # lexicon — calling update_options(pace=) on it would raise, and the caller's fallback
    # speaks WITHOUT the lexicon, so "E-S-O-P" would come back.
    paced = isinstance(tts, sarvam.TTS)
    if not paced:
        # No rate knob, so nothing to batch for — and batching was the bug: every sentence
        # waited for the next until the LLM finished, so she said nothing until the WHOLE
        # reply existed, then waited again for the whole reply's audio. Measured on real
        # calls: 3.4–6.6 s from the candidate going quiet to her first word. Now each
        # sentence goes to the TTS the moment it is complete (lexicon applied per sentence,
        # so an acronym is never split across chunks).
        async def sentences():
            pending = ""
            async for chunk in text:
                pending += chunk
                while (m := _SENT_END.search(pending)):
                    sentence, pending = pending[:m.end()].strip(), pending[m.end():]
                    if sentence:
                        yield say_aloud(sentence) + " "
            if pending.strip():
                yield say_aloud(pending.strip())

        async for fr in _Agent.default.tts_node(agent, sentences(), model_settings):
            yield fr
        return

    buf = ""
    pending: list[str] = []      # sentences waiting to be spoken at `cur`
    cur: float | None = None
    first = [True]

    async def _iter(s: str):
        yield s

    async def _flush():
        nonlocal pending
        if not pending:
            return
        chunk = say_aloud(" ".join(pending))
        pending = []
        if paced:
            tts.update_options(pace=cur)
        if log:
            log.info("PACE %.2f | %s", cur, chunk[:70])
        # Agent.default.tts_node is UNBOUND — it takes the agent explicitly. Calling it as
        # a bound method costs all audio: the TypeError is swallowed by the TTS task and
        # the agent simply never speaks.
        async for fr in _Agent.default.tts_node(agent, _iter(chunk), model_settings):
            yield fr

    async def _add(sentence: str):
        nonlocal cur
        p = pace_for(sentence, first=first[0]) if paced else 1.0
        first[0] = False
        if cur is not None and p != cur:
            async for fr in _flush():       # rate changed — seam here, on purpose
                yield fr
        cur = p
        pending.append(sentence)

    async for chunk in text:
        buf += chunk
        while True:
            m = _SENT_END.search(buf)
            if not m:
                break
            sentence, buf = buf[:m.end()].strip(), buf[m.end():]
            if sentence:
                async for fr in _add(sentence):
                    yield fr
    if buf.strip():
        async for fr in _add(buf.strip()):
            yield fr
    async for fr in _flush():
        yield fr
