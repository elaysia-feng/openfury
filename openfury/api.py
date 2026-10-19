"""The platform: a CRM or ATS places calls over HTTP and gets everything back by webhook —
result, transcript, turn-by-turn log, recording. Multi-tenant: each client gets its own API
keys, webhook, concurrency, calling hours and do-not-call list. Docs: /apidoc.

    python -m openfury.api tenant-add acme --name "Acme Staffing" \\
        --webhook-url https://acme.example/hooks/screening --company-name "Acme Talent"
    python -m openfury.api tenant-set acme --concurrency 3
    python -m openfury.api tenant-list

The server (`python -m openfury.server`) keeps no data longer than it must. The client stores
what the webhook delivers; here a finished call's transcript, record and audio live only until
its webhook is delivered AND the retention window (24 h default) has passed, then the files are
deleted and the row keeps only what billing needs (ids, timestamps, duration, outcome). A number
that asked to be removed is kept as a salted hash so it is never dialled again for that tenant.

Careful by construction, because these are real people:
- The agent must be IN the room before the phone rings. If no worker takes the dispatch in
  15 s, the call goes back in the queue — nobody ever answers to silence.
- Calling hours per tenant (09:30–20:30 local default); outside them calls wait, they don't drop.
- A global cap on simultaneous calls (MAX_CONCURRENT_CALLS) and one per tenant.
- Two answered calls in a row with a silent candidate channel pause all dialling for 15 min.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from openfury.compose import compose
from openfury.use_cases import VOICES, all_use_cases, normalize_phone, render

load_dotenv()
log = logging.getLogger("openfury.api")
HERE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", "data"))
RECORDS = DATA / "calls"
DB_PATH = Path(os.environ.get("DB_PATH", DATA / "platform.db"))
TENANTS = Path(os.environ.get("TENANTS_FILE", DATA / "tenants.json"))
PUBLIC = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8097").rstrip("/")
AGENT = os.environ.get("AGENT_NAME", "openfury")
TRUNK = os.environ.get("SIP_TRUNK_ID", "")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_CALLS", "10"))
SECRET = os.environ["LIVEKIT_API_SECRET"].encode()
DEFAULT_TZ = os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")
SILENT_DB = -70.0     # a real phone line, even quiet, sits well above this; -84 was no audio at all
RING_S, MAX_CALL_S, AGENT_JOIN_S = 45, 15 * 60, 15
FINALIZE_WAIT_S = 150
WEBHOOK_BACKOFF_S = [30, 120, 600, 1800, 3600, 7200, 14400, 28800]   # ~16 h of retries
ACTIVE = ("dialing", "ringing", "in_progress", "processing")

# One slug per kind of call, loaded from use_cases/. Each has its own prompt, record schema and
# time cap. `defaults` fill optional job fields with lines the agent can safely SAY — never
# "Not specified", which it would read out.
USE_CASES: dict[str, dict] = all_use_cases()

router = APIRouter()


# ------------------------------------------------------------------------------ storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
  outcome TEXT, external_ref TEXT, idem_key TEXT, phone TEXT, request TEXT,
  room TEXT, attempt INTEGER NOT NULL DEFAULT 0, retries_left INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, not_before REAL NOT NULL, dialed_at REAL, answered_at REAL,
  ended_at REAL, cancel_requested INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0,
  events TEXT NOT NULL DEFAULT '[]', payload TEXT, purged INTEGER NOT NULL DEFAULT 0,
  talk_s REAL, error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS calls_idem ON calls(tenant, idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS calls_status ON calls(status, not_before);
CREATE INDEX IF NOT EXISTS calls_tenant ON calls(tenant, created_at);
CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY, call_id TEXT NOT NULL, tenant TEXT NOT NULL, event TEXT NOT NULL,
  body TEXT, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
  last_error TEXT, created_at REAL NOT NULL, delivered_at REAL);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(state, next_at);
CREATE TABLE IF NOT EXISTS role_cache (h TEXT PRIMARY KEY, role_block TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS suppressions (tenant TEXT NOT NULL, phone_hash TEXT NOT NULL,
  reason TEXT, at REAL NOT NULL, PRIMARY KEY (tenant, phone_hash));
"""


@contextlib.contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    try:
        yield c
    finally:
        c.close()


def init_db() -> None:
    with db() as c:
        c.executescript(SCHEMA)


def _row(cid: str) -> sqlite3.Row | None:
    with db() as c:
        return c.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()


def _iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z") if ts else None


def _phone_hash(tenant: str, phone: str) -> str:
    return hmac.new(SECRET, f"dnc:{tenant}:{phone}".encode(), hashlib.sha256).hexdigest()


def _suppressed(tenant: str, phone: str) -> bool:
    with db() as c:
        return c.execute("SELECT 1 FROM suppressions WHERE tenant=? AND phone_hash=?",
                         (tenant, _phone_hash(tenant, phone))).fetchone() is not None


# ------------------------------------------------------------------------------ tenants

def _tenants() -> dict:
    """Read on every request: a tenant change needs no restart."""
    return json.loads(TENANTS.read_text("utf-8")) if TENANTS.exists() else {}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _auth(authorization: str | None) -> tuple[str, dict, str]:
    """-> (tenant id, tenant config, mode). Live and test keys differ only in mode."""
    key = (authorization or "").removeprefix("Bearer ").strip()
    if key.startswith(("of_live_", "of_test_")):
        h = _sha(key)
        for tid, t in _tenants().items():
            if t.get("disabled"):
                continue
            for mode in ("live", "test"):
                if hmac.compare_digest(t.get(f"{mode}_key_sha256", ""), h):
                    return tid, t, mode
    raise HTTPException(401, {"code": "unauthorized", "message": "missing or invalid API key"})


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


# ------------------------------------------------------------------------------ request model

class Candidate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=10, max_length=20)
    variables: dict[str, str] = Field(default_factory=dict)


class CustomField(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    question: str = Field(min_length=3, max_length=240)


class AgentCfg(BaseModel):
    name: str | None = Field(None, max_length=30, pattern=r"^[A-Za-z][A-Za-z .'-]*$")
    company_name: str | None = Field(None, max_length=80)
    voice: str | None = None


class CallReq(BaseModel):
    use_case: str = "job-screening"
    candidate: Candidate
    job: dict[str, str]
    realtime_transcript: bool = False
    agent: AgentCfg = Field(default_factory=AgentCfg)
    custom_fields: list[CustomField] = Field(default_factory=list, max_length=8)
    external_ref: str | None = Field(None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    schedule_at: dt.datetime | None = None
    retry_no_answer: int = Field(0, ge=0, le=3)
    retry_after_minutes: int = Field(120, ge=10, le=1440)

    @field_validator("job")
    @classmethod
    def _job(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if len(str(val)) > 3000:
                raise ValueError(f"job.{k} is longer than 3000 characters")
        return {k: str(val).strip() for k, val in v.items()}

    @field_validator("metadata")
    @classmethod
    def _meta(cls, v: dict) -> dict:
        if len(json.dumps(v)) > 4000:
            raise ValueError("metadata is larger than 4 KB")
        return v


# ------------------------------------------------------------------------------ events

def _set_status(cid: str, status: str, **cols: Any) -> None:
    """Move a call on, record the transition, queue its webhook."""
    now = time.time()
    with db() as c:
        r = c.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
        ev = json.loads(r["events"])
        ev.append({"status": status, "at": _iso(now), **({"outcome": cols["outcome"]} if cols.get("outcome") else {})})
        seq = r["seq"] + 1
        sets = {"status": status, "events": json.dumps(ev), "seq": seq, **cols}
        c.execute(f"UPDATE calls SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                  (*sets.values(), cid))
    log.info("CALL %s -> %s %s", cid, status, cols.get("outcome") or "")
    if status != "ended":                              # the final event carries the payload
        _enqueue(cid, r["tenant"], "call.status", {
            "id": cid, "object": "call", "status": status, "sequence": seq,
            "external_ref": r["external_ref"], "at": _iso(now),
            "metadata": json.loads(r["request"] or "{}").get("metadata", {})})


def _enqueue(cid: str, tenant: str, event: str, data: dict) -> None:
    did = "dlv_" + secrets.token_hex(10)
    body = json.dumps({"id": did, "type": event, "created_at": _iso(time.time()), "data": data},
                      ensure_ascii=False)
    with db() as c:
        c.execute("INSERT INTO deliveries (id, call_id, tenant, event, body, state, next_at, created_at)"
                  " VALUES (?,?,?,?,?,'pending',?,?)", (did, cid, tenant, event, body, time.time(), time.time()))


def sign(secret: str, ts: int, body: str) -> str:
    return hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------------------ the call object

def _summary(r: sqlite3.Row) -> dict:
    req = json.loads(r["request"] or "{}")
    out = {"id": r["id"], "object": "call", "use_case": req.get("use_case", "job-screening"),
           "mode": r["mode"], "status": r["status"],
           "outcome": r["outcome"], "external_ref": r["external_ref"],
           "attempt": r["attempt"], "created_at": _iso(r["created_at"]),
           "scheduled_for": _iso(r["not_before"]), "dialed_at": _iso(r["dialed_at"]),
           "answered_at": _iso(r["answered_at"]), "ended_at": _iso(r["ended_at"]),
           "events": json.loads(r["events"]), "metadata": req.get("metadata", {}),
           "purged": bool(r["purged"])}
    if r["error"]:
        out["error"] = r["error"]
    if r["status"] in ("in_progress", "processing") and not r["purged"]:
        out["live_transcript"] = _live_turns(r["room"])
    return out


def _live_turns(room: str | None) -> list[dict]:
    """Finished turns so far, from the worker's live file. Final text only, no partials."""
    f = RECORDS / f"{room}.live.jsonl" if room else None
    if not f or not f.exists():
        return []
    out = []
    for line in f.read_text("utf-8").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return out


def _call_obj(r: sqlite3.Row) -> dict:
    if r["status"] == "ended" and r["payload"]:
        p = json.loads(r["payload"])
        p["events"] = json.loads(r["events"])
        if p.get("recording"):                         # fresh signature on every read
            p["recording"].update(_recording_link(r["id"], r["ended_at"]))
        return p
    return _summary(r)


def _retention_h(t: dict) -> float:
    return float(min(max(t.get("retention_hours", 24), 1), 72))


def _recording_link(cid: str, ended_at: float | None) -> dict:
    t = _tenants().get(_row(cid)["tenant"], {})
    exp = int((ended_at or time.time()) + _retention_h(t) * 3600)
    sig = hmac.new(SECRET, f"rec:{cid}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return {"url": f"{PUBLIC}/v1/calls/{cid}/recording?exp={exp}&sig={sig}", "expires_at": _iso(exp)}


def _verdict(v: str | None) -> str | None:
    return re.sub(r"[^a-z]+", "_", v.lower()).strip("_") if v else None


def _build_payload(r: sqlite3.Row, level: float | None) -> dict:
    req = json.loads(r["request"])
    stem = RECORDS / r["room"] if r["room"] else None
    rec = _read_json(stem.with_suffix(".json")) if stem else None
    clog = _read_json(stem.with_suffix(".log.json")) if stem else None
    talk = (r["ended_at"] - r["answered_at"]) if r["answered_at"] and r["ended_at"] else 0.0
    end_reason = (clog or {}).get("end_reason") or ("candidate_hung_up" if r["answered_at"] else None)
    result = None
    keys = _use_case(r)["fields"]
    if rec:
        fields = {keys.get(k, re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")): v
                  for k, v in (rec.get("fields") or {}).items()}
        dnc = (str(fields.get("asked_to_be_removed", "")).lower().startswith("yes")
               or end_reason == "removed")
        result = {"verdict": _verdict(rec.get("verdict")), "summary": rec.get("summary"),
                  "fields": fields, "structured": rec.get("structured") or {},
                  "risk_flags": rec.get("risk_flags") or [],
                  "must_capture": rec.get("must_capture") or {},
                  "custom": rec.get("custom") or {}, "do_not_call": dnc}
    turns = (clog or {}).get("turns") or []
    lat = [t["latency_s"]["e2e_latency"] for t in turns
           if t.get("speaker") == "agent" and "e2e_latency" in (t.get("latency_s") or {})]
    has_audio = bool(stem and stem.with_suffix(".mp3").exists())
    return {
        "id": r["id"], "object": "call", "use_case": req.get("use_case", "job-screening"),
        "mode": r["mode"], "status": "ended",
        "outcome": r["outcome"], "external_ref": r["external_ref"], "attempt": r["attempt"],
        "metadata": req.get("metadata", {}),
        "candidate": {"name": req["candidate"]["name"], "phone": r["phone"]},
        "job": req["job"], "agent": req.get("agent_resolved", {}),
        "timeline": {"created_at": _iso(r["created_at"]), "dialed_at": _iso(r["dialed_at"]),
                     "answered_at": _iso(r["answered_at"]), "ended_at": _iso(r["ended_at"])},
        "duration_s": round(talk, 1),
        "billable_minutes": math.ceil(talk / 60) if talk else 0,
        "end_reason": end_reason if r["answered_at"] else r["outcome"],
        "result": result,
        "transcript": turns,
        "transcript_text": "\n".join(f"{'Candidate' if t['speaker'] == 'candidate' else 'Agent'}: {t['text']}"
                                     for t in turns),
        "recording": ({"format": "mp3", "channels": {"left": "candidate", "right": "agent"},
                       **_recording_link(r["id"], r["ended_at"])} if has_audio else None),
        "quality": {"candidate_audio_mean_db": level,
                    "candidate_audio_silent": level is not None and level < SILENT_DB,
                    "agent_reply_latency_p50_s": round(sorted(lat)[len(lat) // 2], 2) if lat else None,
                    "agent_turns": sum(t["speaker"] == "agent" for t in turns),
                    "candidate_turns": sum(t["speaker"] == "candidate" for t in turns),
                    "interruptions": sum(bool(t.get("interrupted")) for t in turns)},
        "usage": (clog or {}).get("usage") or {},
        "retention": {"delete_after": _iso((r["ended_at"] or time.time())
                                           + _retention_h(_tenants().get(r["tenant"], {})) * 3600)},
        "error": r["error"],
    }


def _sample(req: dict, agent: dict, uc: dict) -> tuple[list[dict], dict]:
    """Test mode: the use case's sample turns and result, filled with this request's values."""
    v = {**req["job"], "candidate_name": req["candidate"]["name"].split()[0],
         "agent_name": agent["name"], "company_name": agent["company_name"]}
    turns = [{"speaker": spk, "text": render(text, v), "at_s": round(i * 3.1, 1), "interrupted": False,
              **({"latency_s": {"e2e_latency": 1.1}} if spk == "agent" and i else {})}
             for i, (spk, text) in enumerate(uc["sample"]["turns"])]
    res = json.loads(render(json.dumps(uc["sample"]["result"], ensure_ascii=False), v))
    res["fields"] = ({k: "Not captured" for k in uc["fields"].values()}
                     | {"candidate_name": req["candidate"]["name"]} | res.get("fields", {}))
    res["custom"] = {f["key"]: "Not captured" for f in req.get("custom_fields", [])}
    res["do_not_call"] = False
    return turns, res


def _read_json(p: Path) -> dict | None:
    with contextlib.suppress(Exception):
        return json.loads(p.read_text("utf-8"))
    return None


# ------------------------------------------------------------------------------ dialling

def _brief(r: sqlite3.Row, t: dict) -> tuple[dict, dict]:
    """-> (worker metadata, agent as resolved). Every brief key is sent explicitly: the worker's
    own DEFAULTS are a demo role, and a missing ctc_max must never become someone else's."""
    req = json.loads(r["request"])
    uc = USE_CASES[req.get("use_case", "job-screening")]
    d = t.get("defaults", {})
    agent = {"name": req["agent"].get("name") or d.get("agent_name") or "Meera",
             "company_name": req["agent"].get("company_name") or d.get("company_name") or t.get("name"),
             "voice": req["agent"].get("voice") or uc["voice"]}
    job = {k: req["job"].get(k) or uc["defaults"].get(k, "Not specified")
           for k in (*uc["required"], *uc["optional"])}
    meta = {**job, **{k: v for k, v in req["candidate"].get("variables", {}).items()
                      if k not in job and not k.startswith("_")},
            "candidate_name": req["candidate"]["name"].split()[0],
            "agent_name": agent["name"], "company_name": agent["company_name"],
            "use_case": uc["id"], "campaign": f"api:{r['tenant']}", "_voice": agent["voice"]}
    if req.get("custom_fields"):
        meta["_custom_fields"] = req["custom_fields"]
    return meta, agent


def _use_case(r: sqlite3.Row) -> dict:
    return USE_CASES[json.loads(r["request"] or "{}").get("use_case", "job-screening")]


async def _role_block(meta: dict, custom: list[dict], uc: dict) -> str:
    brief = {k: meta[k] for k in (*uc["required"], *uc["optional"])
             if meta.get(k) not in (None, "", "None.", "Not specified")}
    brief["company_name"] = meta["company_name"]
    h = _sha(json.dumps(brief, sort_keys=True))
    with db() as c:
        hit = c.execute("SELECT role_block FROM role_cache WHERE h=?", (h,)).fetchone()
    block = hit["role_block"] if hit else ""
    if uc.get("compose") and not hit:
        try:
            block = await asyncio.to_thread(compose, brief)
            with db() as c:
                c.execute("INSERT OR REPLACE INTO role_cache VALUES (?,?,?)", (h, block, time.time()))
        except Exception as e:                     # the base prompt still screens; log, go on
            log.error("compose failed (%s) — dialling with the base prompt", e)
    if custom:
        block += ("\n\n**The hiring team also needs these answered.** Ask each one naturally once "
                  "the deal-breakers are done, one at a time, in your own words:\n"
                  + "\n".join(f"- {f['question']}" for f in custom))
    return block


async def _agent_joined(lk, room: str) -> bool:
    from livekit import api
    t0 = time.time()
    while time.time() - t0 < AGENT_JOIN_S:
        with contextlib.suppress(Exception):
            parts = (await lk.room.list_participants(api.ListParticipantsRequest(room=room))).participants
            if any(p.kind == api.ParticipantInfo.AGENT for p in parts):
                return True
        await asyncio.sleep(1)
    return False


async def _candidate_level(room: str) -> float | None:
    """Mean level of the candidate's channel (left) of the kept recording, in dB."""
    ogg = RECORDS / f"{room}.ogg"
    for _ in range(30):                               # the worker keeps it a few seconds after
        if ogg.exists():
            break
        await asyncio.sleep(1)
    if not ogg.exists():
        return None
    await asyncio.sleep(2)
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(ogg), "-af", "pan=mono|c0=c0,volumedetect", "-f", "null", "-",
        stderr=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL)
    _, err = await p.communicate()
    m = re.search(r"mean_volume: (-?[0-9.]+) dB", err.decode())
    return float(m.group(1)) if m else None


class Dispatcher:
    def __init__(self) -> None:
        self.running: dict[str, asyncio.Task] = {}
        self.owner: dict[str, tuple[str, str]] = {}     # call id -> (tenant, mode), while running
        self.paused_until = 0.0
        self.silent_run = 0
        self.lk = None

    async def start(self) -> None:
        from livekit import api
        init_db()
        self.lk = api.LiveKitAPI()
        with db() as c:                                # resume whatever a restart interrupted
            for r in c.execute(f"SELECT id, tenant, mode FROM calls WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
                self._spawn(r["id"], r["tenant"], r["mode"])
        for loop in (self._dispatch_loop, self._delivery_loop, self._purge_loop):
            asyncio.create_task(self._guard(loop))

    async def _guard(self, fn) -> None:
        while True:                                    # a loop that dies takes the platform with it
            try:
                await fn()
            except Exception as e:
                log.exception("loop %s crashed: %s — restarting in 5 s", fn.__name__, e)
                await asyncio.sleep(5)

    def _spawn(self, cid: str, tenant: str, mode: str) -> None:
        if cid in self.running:
            return
        coro = self._simulate(cid) if mode == "test" else self._call(cid)
        task = asyncio.create_task(coro)
        self.running[cid] = task
        self.owner[cid] = (tenant, mode)

        def _done(_):
            self.running.pop(cid, None)
            self.owner.pop(cid, None)
        task.add_done_callback(_done)

    async def _dispatch_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            now = time.time()
            tenants = _tenants()
            with db() as c:
                due = c.execute("SELECT id, tenant, mode, phone FROM calls WHERE status='queued' "
                                "AND not_before<=? ORDER BY not_before LIMIT 200", (now,)).fetchall()
            # What is running is what is on (or about to be on) a line — including a call still
            # composing its brief or waiting for the agent, which the DB still calls queued.
            active: dict[str, int] = {}
            for tenant, mode in self.owner.values():
                if mode == "live":
                    active[tenant] = active.get(tenant, 0) + 1
            total = sum(active.values())
            for r in due:
                if r["id"] in self.running:
                    continue
                if r["mode"] == "test":
                    self._spawn(r["id"], r["tenant"], "test")
                    continue
                t = tenants.get(r["tenant"])
                if not t or t.get("disabled"):
                    continue
                if now < self.paused_until or total >= MAX_CONCURRENT:
                    continue
                if active.get(r["tenant"], 0) >= int(t.get("concurrency", 10)):
                    continue
                h0, h1 = (dt.time.fromisoformat(x) for x in t.get("calling_hours", ["09:30", "20:30"]))
                if not (h0 <= dt.datetime.now(ZoneInfo(t.get("timezone", DEFAULT_TZ))).time() <= h1):
                    continue
                if _suppressed(r["tenant"], r["phone"]):
                    _set_status(r["id"], "ended", outcome="suppressed", ended_at=now)
                    self._final(r["id"], None)
                    continue
                active[r["tenant"]] = active.get(r["tenant"], 0) + 1
                total += 1
                self._spawn(r["id"], r["tenant"], "live")

    async def _call(self, cid: str) -> None:
        from livekit import api
        lk = self.lk
        r = _row(cid)
        t = _tenants().get(r["tenant"], {})
        try:
            if r["status"] == "queued":
                attempt = r["attempt"] + 1
                room = f"api-{cid}-{attempt}"
                meta, agent = _brief(r, t)
                uc = _use_case(r)
                meta["role_block"] = await _role_block(meta, meta.get("_custom_fields") or [], uc)
                req = json.loads(r["request"])
                req["agent_resolved"] = agent
                with db() as c:
                    c.execute("UPDATE calls SET room=?, attempt=?, request=? WHERE id=?",
                              (room, attempt, json.dumps(req, ensure_ascii=False), cid))
                await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
                    agent_name=uc.get("agent") or AGENT, room=room, metadata=json.dumps(meta, ensure_ascii=False)))
                if not await _agent_joined(lk, room):
                    log.warning("CALL %s: no agent took the dispatch in %ss — requeued", cid, AGENT_JOIN_S)
                    with contextlib.suppress(Exception):
                        await lk.room.delete_room(api.DeleteRoomRequest(room=room))
                    with db() as c:
                        c.execute("UPDATE calls SET not_before=?, attempt=?, room=NULL WHERE id=?",
                                  (time.time() + 60, attempt - 1, cid))
                    return
                _set_status(cid, "dialing", dialed_at=time.time())
                try:
                    await lk.sip.create_sip_participant(api.CreateSIPParticipantRequest(
                        sip_trunk_id=t.get("sip_trunk_id") or TRUNK, sip_call_to=r["phone"],
                        room_name=room, participant_identity="candidate",
                        participant_name=meta["candidate_name"], wait_until_answered=False))
                except Exception as e:
                    with contextlib.suppress(Exception):
                        await lk.room.delete_room(api.DeleteRoomRequest(room=room))
                    _set_status(cid, "ended", outcome="failed", ended_at=time.time(), error=f"dial failed: {str(e)[:200]}")
                    self._final(cid, None)
                    return
            r = _row(cid)
            room = r["room"]
            if r["status"] != "processing":
                await self._watch(cid, room)
            r = _row(cid)
            if not r["answered_at"]:
                if r["cancel_requested"]:
                    _set_status(cid, "ended", outcome="cancelled", ended_at=time.time())
                elif r["retries_left"] > 0:
                    req = json.loads(r["request"])
                    nb = time.time() + req.get("retry_after_minutes", 120) * 60
                    with db() as c:
                        c.execute("UPDATE calls SET retries_left=retries_left-1, not_before=?, "
                                  "dialed_at=NULL WHERE id=?", (nb, cid))
                    _set_status(cid, "queued")            # retry_scheduled shows in events
                    return
                else:
                    _set_status(cid, "ended", outcome="no_answer", ended_at=time.time())
                self._final(cid, None)
                return
            await self._finish_answered(cid, room)
        except Exception as e:
            log.exception("CALL %s crashed: %s", cid, e)
            r = _row(cid)
            if r and r["status"] != "ended":
                _set_status(cid, "ended", outcome="failed", ended_at=time.time(),
                            error=f"internal error: {type(e).__name__}")
                self._final(cid, None)

    async def _watch(self, cid: str, room: str) -> None:
        from livekit import api
        lk = self.lk
        r = _row(cid)
        dialed = r["dialed_at"] or time.time()
        status = r["status"]
        realtime = json.loads(r["request"]).get("realtime_transcript")
        sent = len(_live_turns(room)) if realtime else 0   # a restart resumes, never re-sends
        while time.time() - dialed < MAX_CALL_S:
            await asyncio.sleep(2)
            if realtime:
                turns = _live_turns(room)
                for i, turn in enumerate(turns[sent:], start=sent):
                    self._transcript_event(cid, r, i, turn)
                sent = len(turns)
            if _row(cid)["cancel_requested"]:
                with contextlib.suppress(Exception):
                    await lk.room.delete_room(api.DeleteRoomRequest(room=room))
                break
            try:
                if not (await lk.room.list_rooms(api.ListRoomsRequest(names=[room]))).rooms:
                    break
                parts = (await lk.room.list_participants(api.ListParticipantsRequest(room=room))).participants
            except Exception:
                continue                                 # a LiveKit blip is not a hang-up
            cand = next((p for p in parts if p.identity == "candidate"), None)
            st = (cand.attributes.get("sip.callStatus") if cand else None) or ""
            if st == "active" and status != "in_progress":
                status = "in_progress"
                _set_status(cid, status, answered_at=time.time())
            elif status == "dialing" and st == "ringing":
                status = "ringing"
                _set_status(cid, status)
            if status != "in_progress":
                if (cand is None or st == "hangup") and time.time() - dialed > 8:
                    break
                if time.time() - dialed > RING_S:
                    with contextlib.suppress(Exception):
                        await lk.room.delete_room(api.DeleteRoomRequest(room=room))
                    break
        else:
            with contextlib.suppress(Exception):
                await lk.room.delete_room(api.DeleteRoomRequest(room=room))
        if _row(cid)["answered_at"]:
            _set_status(cid, "processing", ended_at=time.time())

    async def _finish_answered(self, cid: str, room: str) -> None:
        mp3 = RECORDS / f"{room}.mp3"
        t0 = time.time()
        while time.time() - t0 < FINALIZE_WAIT_S and not mp3.exists():
            await asyncio.sleep(2)                       # the worker writes the mp3 last
        await asyncio.sleep(2)
        level = await _candidate_level(room)
        if level is not None and level < SILENT_DB:
            self.silent_run += 1
            if self.silent_run >= 2:
                self.paused_until = time.time() + 900
                log.error("PAUSED dialling 15 min: two answered calls in a row with a silent "
                          "candidate channel — the audio path may be broken")
        else:
            self.silent_run = 0
        clog = _read_json(RECORDS / f"{room}.log.json") or {}
        outcome = "voicemail" if clog.get("end_reason") == "voicemail" else (
            "cancelled" if _row(cid)["cancel_requested"] else "answered")
        r = _row(cid)
        _set_status(cid, "ended", outcome=outcome,
                    talk_s=(r["ended_at"] - r["answered_at"]) if r["ended_at"] else None)
        self._final(cid, level)

    def _transcript_event(self, cid: str, r: sqlite3.Row, i: int, turn: dict) -> None:
        _enqueue(cid, r["tenant"], "call.transcript", {
            "id": cid, "object": "call.transcript", "external_ref": r["external_ref"],
            "index": i, "turn": turn,
            "metadata": json.loads(r["request"] or "{}").get("metadata", {})})

    def _final(self, cid: str, level: float | None) -> None:
        r = _row(cid)
        p = _build_payload(r, level)
        if (p.get("result") or {}).get("do_not_call"):
            with db() as c:
                c.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?,?,?)",
                          (r["tenant"], _phone_hash(r["tenant"], r["phone"]), "asked on call", time.time()))
        with db() as c:
            c.execute("UPDATE calls SET payload=? WHERE id=?", (json.dumps(p, ensure_ascii=False), cid))
        p["events"] = json.loads(_row(cid)["events"])
        _enqueue(cid, r["tenant"], "call.completed", p)

    async def _simulate(self, cid: str) -> None:
        """Test keys: the whole lifecycle, every webhook, no phone call."""
        _set_status(cid, "dialing", dialed_at=time.time(), attempt=1)
        await asyncio.sleep(2)
        if _row(cid)["cancel_requested"]:
            _set_status(cid, "ended", outcome="cancelled", ended_at=time.time())
            self._final(cid, None)
            return
        _set_status(cid, "ringing")
        await asyncio.sleep(2)
        _set_status(cid, "in_progress", answered_at=time.time())
        r = _row(cid)
        req = json.loads(r["request"])
        meta, agent = _brief(r, _tenants().get(r["tenant"], {}))
        room = f"api-{cid}-1"
        with db() as c:
            c.execute("UPDATE calls SET room=? WHERE id=?", (room, cid))
        uc = _use_case(r)
        script, result = _sample(req, agent, uc)
        RECORDS.mkdir(parents=True, exist_ok=True)
        for i, turn in enumerate(script):
            await asyncio.sleep(1.2)
            with open(RECORDS / f"{room}.live.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(turn, ensure_ascii=False) + "\n")
            if req.get("realtime_transcript"):
                self._transcript_event(cid, _row(cid), i, turn)
        _set_status(cid, "processing", ended_at=time.time())
        await asyncio.sleep(1)
        r = _row(cid)
        p = _build_payload(r, None)
        p["agent"] = agent
        p["outcome"], p["end_reason"], p["duration_s"], p["billable_minutes"] = "answered", "completed", 4.0, 1
        p["result"] = result
        p["transcript"] = script
        p["transcript_text"] = "\n".join(f"{'Candidate' if t['speaker'] == 'candidate' else 'Agent'}: {t['text']}" for t in p["transcript"])
        p["quality"].update(agent_turns=2, candidate_turns=1, agent_reply_latency_p50_s=1.1)
        p["test"] = True
        _set_status(cid, "ended", outcome="answered", talk_s=4.0)
        with db() as c:
            c.execute("UPDATE calls SET payload=? WHERE id=?", (json.dumps(p, ensure_ascii=False), cid))
        p["events"] = json.loads(_row(cid)["events"])
        _enqueue(cid, r["tenant"], "call.completed", p)

    async def _delivery_loop(self) -> None:
        import httpx
        async with httpx.AsyncClient(timeout=15) as http:
            while True:
                await asyncio.sleep(2)
                with db() as c:
                    due = c.execute("SELECT * FROM deliveries WHERE state='pending' AND next_at<=? "
                                    "ORDER BY created_at LIMIT 25", (time.time(),)).fetchall()
                tenants = _tenants()
                for d in due:
                    t = tenants.get(d["tenant"], {})
                    url = t.get("webhook_url")
                    if not url:
                        with db() as c:
                            c.execute("UPDATE deliveries SET state='skipped', last_error='no webhook_url' WHERE id=?", (d["id"],))
                        continue
                    ts = int(time.time())
                    headers = {"content-type": "application/json", "user-agent": "openfury-webhooks/1",
                               "x-webhook-id": d["id"], "x-webhook-event": d["event"],
                               "x-webhook-signature": f"t={ts},v1={sign(t['webhook_secret'], ts, d['body'])}"}
                    err = None
                    try:
                        resp = await http.post(url, content=d["body"].encode(), headers=headers)
                        if not 200 <= resp.status_code < 300:
                            err = f"HTTP {resp.status_code}"
                    except Exception as e:
                        err = f"{type(e).__name__}: {str(e)[:150]}"
                    n = d["attempts"] + 1
                    with db() as c:
                        if err is None:
                            c.execute("UPDATE deliveries SET state='delivered', attempts=?, delivered_at=?, "
                                      "last_error=NULL WHERE id=?", (n, time.time(), d["id"]))
                        elif n > len(WEBHOOK_BACKOFF_S):
                            c.execute("UPDATE deliveries SET state='failed', attempts=?, last_error=? WHERE id=?",
                                      (n, err, d["id"]))
                            log.error("WEBHOOK %s %s gave up after %d attempts: %s", d["tenant"], d["event"], n, err)
                        else:
                            c.execute("UPDATE deliveries SET attempts=?, next_at=?, last_error=? WHERE id=?",
                                      (n, time.time() + WEBHOOK_BACKOFF_S[n - 1], err, d["id"]))

    async def _purge_loop(self) -> None:
        """Delete what we promised not to keep: transcript, record, audio, request and payload."""
        while True:
            await asyncio.sleep(300)
            tenants = _tenants()
            with db() as c:
                rows = c.execute("SELECT id, tenant, room, attempt, ended_at FROM calls WHERE status='ended' AND purged=0").fetchall()
            for r in rows:
                if time.time() < (r["ended_at"] or 0) + _retention_h(tenants.get(r["tenant"], {})) * 3600:
                    continue
                with db() as c:
                    if c.execute("SELECT 1 FROM deliveries WHERE call_id=? AND state='pending'", (r["id"],)).fetchone():
                        continue                         # still trying to hand it over
                for a in range(1, (r["attempt"] or 0) + 1):
                    stem = RECORDS / f"api-{r['id']}-{a}"
                    for ext in (".json", ".md", ".transcript.txt", ".log.json", ".live.jsonl", ".ogg", ".mp3"):
                        stem.with_suffix(ext).unlink(missing_ok=True)
                with db() as c:
                    c.execute("UPDATE calls SET purged=1, payload=NULL, request=NULL, phone=NULL WHERE id=?", (r["id"],))
                    c.execute("UPDATE deliveries SET body=NULL WHERE call_id=?", (r["id"],))
                log.info("PURGED %s", r["id"])


DISPATCHER = Dispatcher()


# ------------------------------------------------------------------------------ endpoints

@router.get("/apidoc", response_class=HTMLResponse)
@router.get("/apidoc/", response_class=HTMLResponse, include_in_schema=False)
def apidoc() -> str:
    return (HERE / "static" / "apidoc.html").read_text("utf-8").replace("{{PUBLIC_BASE_URL}}", PUBLIC)


@router.get("/v1/health")
async def health() -> dict:
    return {"ok": True, "time": _iso(time.time())}


@router.get("/v1/account")
async def account(authorization: str | None = Header(None)) -> dict:
    tid, t, mode = _auth(authorization)
    with db() as c:
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM calls WHERE tenant=? AND mode=? AND status!='ended' GROUP BY status", (tid, mode))}
    return {"tenant": tid, "name": t.get("name"), "mode": mode,
            "webhook_url": t.get("webhook_url"), "concurrency": int(t.get("concurrency", 10)),
            "calling_hours": t.get("calling_hours", ["09:30", "20:30"]),
            "timezone": t.get("timezone", DEFAULT_TZ),
            "retention_hours": _retention_h(t), "defaults": t.get("defaults", {}),
            "open_calls": counts}


@router.get("/v1/voices")
async def voices(authorization: str | None = Header(None)) -> dict:
    _auth(authorization)
    return {"data": [{"id": k, **{x: v[x] for x in ("gender", "accent", "tone")}} for k, v in VOICES.items()]}


@router.get("/v1/use-cases")
async def use_cases(authorization: str | None = Header(None)) -> dict:
    _auth(authorization)
    return {"data": [{"id": k, "title": u["title"], "summary": u["summary"],
                      "max_minutes": u["max_minutes"], "default_voice": u["voice"],
                      "job_required": list(u["required"]), "job_optional": list(u["optional"]),
                      "result_fields": list(u["fields"].values()), "verdicts": u["verdicts"]}
                     for k, u in USE_CASES.items()]}


@router.get("/v1/calls/{cid}/stream")
async def stream(cid: str, authorization: str | None = Header(None)):
    """Server-sent events: `status` on every move, `transcript` for each finished turn, then one
    `completed` with the full call object. The connection closes after `completed`."""
    from fastapi.responses import StreamingResponse
    tid, _, mode = _auth(authorization)
    _own(cid, tid, mode)

    async def gen():
        last_status, sent, t0 = None, 0, time.time()
        while time.time() - t0 < MAX_CALL_S + 600:
            r = _row(cid)
            if r["status"] != last_status:
                last_status = r["status"]
                yield f"event: status\ndata: {json.dumps({'id': cid, 'status': last_status, 'sequence': r['seq']})}\n\n"
            turns = _live_turns(r["room"]) if not r["purged"] else []
            for i, turn in enumerate(turns[sent:], start=sent):
                yield f"event: transcript\ndata: {json.dumps({'id': cid, 'index': i, 'turn': turn}, ensure_ascii=False)}\n\n"
            sent = max(sent, len(turns))
            if r["status"] == "ended" and r["payload"]:
                yield f"event: completed\ndata: {json.dumps(_call_obj(r), ensure_ascii=False)}\n\n"
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.7)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache", "x-accel-buffering": "no"})


@router.post("/v1/calls", status_code=202)
async def create_call(req: CallReq, authorization: str | None = Header(None),
                      idempotency_key: str | None = Header(None)) -> JSONResponse:
    tid, t, mode = _auth(authorization)
    if idempotency_key:
        if len(idempotency_key) > 200:
            raise _err(400, "invalid_request", "Idempotency-Key is longer than 200 characters")
        with db() as c:
            old = c.execute("SELECT * FROM calls WHERE tenant=? AND idem_key=?", (tid, idempotency_key)).fetchone()
        if old:
            return JSONResponse(_call_obj(old), status_code=200)
    uc = USE_CASES.get(req.use_case)
    if not uc:
        raise _err(422, "invalid_use_case", f"unknown use_case {req.use_case!r}; see GET /v1/use-cases")
    missing = [k for k in uc["required"] if not req.job.get(k)]
    unknown = [k for k in req.job if k not in (*uc["required"], *uc["optional"])]
    if missing or unknown:
        raise _err(422, "invalid_request", "; ".join(
            ([f"job is missing required fields for {req.use_case}: {', '.join(missing)}"] if missing else [])
            + ([f"job fields not used by {req.use_case}: {', '.join(unknown)}"] if unknown else [])))
    phone = normalize_phone(req.candidate.phone)
    if not phone:
        raise _err(422, "invalid_phone", "candidate.phone must be a mobile number in E.164 (+<country><number>)")
    voice = req.agent.voice or uc["voice"]
    if voice not in VOICES:
        raise _err(422, "invalid_voice", f"unknown voice {voice!r}; see GET /v1/voices")
    if not (req.agent.company_name or t.get("defaults", {}).get("company_name") or t.get("name")):
        raise _err(422, "invalid_request", "agent.company_name is required (no default set for your account)")
    if len({f.key for f in req.custom_fields}) != len(req.custom_fields):
        raise _err(422, "invalid_request", "custom_fields keys must be unique")
    if _suppressed(tid, phone):
        raise _err(409, "do_not_call", "this number asked not to be called again")
    with db() as c:
        dup = c.execute("SELECT id FROM calls WHERE tenant=? AND phone=? AND mode=? AND status!='ended'",
                        (tid, phone, mode)).fetchone()
    if dup:
        raise _err(409, "call_in_progress", f"this number already has an open call: {dup['id']}")
    if req.schedule_at and req.schedule_at.tzinfo is None:
        raise _err(422, "invalid_request", "schedule_at needs a timezone, e.g. 2026-10-17T10:00:00+05:30")
    if not TRUNK and mode == "live" and not t.get("sip_trunk_id"):
        raise _err(503, "not_configured", "no SIP trunk is configured on this server (SIP_TRUNK_ID)")
    now = time.time()
    nb = req.schedule_at.timestamp() if req.schedule_at else now
    if nb > now + 30 * 86400:
        raise _err(422, "invalid_request", "schedule_at is more than 30 days ahead")
    cid = "call_" + secrets.token_hex(10)
    body = req.model_dump(mode="json")
    body["agent"]["voice"] = voice
    events = [{"status": "queued", "at": _iso(now)}]
    with db() as c:
        try:
            c.execute("INSERT INTO calls (id, tenant, mode, status, external_ref, idem_key, phone, request, "
                      "retries_left, created_at, not_before, events, seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                      (cid, tid, mode, "queued", req.external_ref, idempotency_key, phone,
                       json.dumps(body, ensure_ascii=False), req.retry_no_answer, now, max(nb, now),
                       json.dumps(events)))
        except sqlite3.IntegrityError:                   # a concurrent retry with the same key
            old = c.execute("SELECT * FROM calls WHERE tenant=? AND idem_key=?", (tid, idempotency_key)).fetchone()
            return JSONResponse(_call_obj(old), status_code=200)
    log.info("CALL %s queued tenant=%s mode=%s", cid, tid, mode)
    return JSONResponse(_call_obj(_row(cid)), status_code=202)


def _own(cid: str, tid: str, mode: str) -> sqlite3.Row:
    r = _row(cid) if re.match(r"^call_[0-9a-f]{20}$", cid) else None
    if not r or r["tenant"] != tid or r["mode"] != mode:
        raise _err(404, "not_found", "no such call")
    return r


@router.get("/v1/calls/{cid}")
async def get_call(cid: str, authorization: str | None = Header(None)) -> dict:
    tid, _, mode = _auth(authorization)
    return _call_obj(_own(cid, tid, mode))


@router.get("/v1/calls")
async def list_calls(authorization: str | None = Header(None), status: str | None = None,
                     external_ref: str | None = None, limit: int = Query(50, ge=1, le=200),
                     before: str | None = None) -> dict:
    tid, _, mode = _auth(authorization)
    q, args = "SELECT * FROM calls WHERE tenant=? AND mode=?", [tid, mode]
    if status:
        q += " AND status=?"; args.append(status)
    if external_ref:
        q += " AND external_ref=?"; args.append(external_ref)
    if before:
        b = _row(before)
        if b:
            q += " AND created_at<?"; args.append(b["created_at"])
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit + 1)
    with db() as c:
        rows = c.execute(q, args).fetchall()
    data = [_summary(r) for r in rows[:limit]]
    return {"data": data, "has_more": len(rows) > limit,
            "next_before": data[-1]["id"] if len(rows) > limit else None}


@router.post("/v1/calls/{cid}/cancel")
async def cancel_call(cid: str, authorization: str | None = Header(None)) -> dict:
    tid, _, mode = _auth(authorization)
    r = _own(cid, tid, mode)
    if r["status"] == "ended":
        raise _err(409, "already_ended", "the call has already ended")
    with db() as c:
        c.execute("UPDATE calls SET cancel_requested=1 WHERE id=?", (cid,))
    if r["status"] == "queued" and cid not in DISPATCHER.running:
        _set_status(cid, "ended", outcome="cancelled", ended_at=time.time())
        DISPATCHER._final(cid, None)
    return _call_obj(_row(cid))


@router.post("/v1/calls/{cid}/redeliver")
async def redeliver(cid: str, authorization: str | None = Header(None)) -> dict:
    tid, _, mode = _auth(authorization)
    r = _own(cid, tid, mode)
    if r["status"] != "ended" or not r["payload"]:
        raise _err(409, "not_available", "only an ended call that has not been deleted can be redelivered")
    p = _call_obj(r)
    _enqueue(cid, tid, "call.completed", p)
    return {"queued": True}


@router.get("/v1/calls/{cid}/recording")
async def recording(cid: str, exp: int = 0, sig: str = "") -> FileResponse:
    """Signed link, no API key: a CRM can fetch it with a plain GET until it expires."""
    want = hmac.new(SECRET, f"rec:{cid}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(sig, want) or not re.match(r"^call_[0-9a-f]{20}$", cid):
        raise _err(404, "not_found", "no such recording")
    if time.time() > exp:
        raise _err(410, "expired", "this recording link has expired and the file has been deleted")
    r = _row(cid)
    f = RECORDS / f"{r['room']}.mp3" if r and r["room"] else None
    if not f or not f.exists() or r["purged"]:
        raise _err(410, "deleted", "the recording has been deleted")
    return FileResponse(f, media_type="audio/mpeg", filename=f"{cid}.mp3")


class SuppressReq(BaseModel):
    phone: str
    reason: str | None = Field(None, max_length=200)


@router.post("/v1/suppressions")
async def add_suppression(req: SuppressReq, authorization: str | None = Header(None)) -> dict:
    tid, _, _ = _auth(authorization)
    phone = normalize_phone(req.phone)
    if not phone:
        raise _err(422, "invalid_phone", "not a valid phone number")
    with db() as c:
        c.execute("INSERT OR REPLACE INTO suppressions VALUES (?,?,?,?)",
                  (tid, _phone_hash(tid, phone), req.reason, time.time()))
    return {"suppressed": True}


@router.delete("/v1/suppressions/{phone}")
async def del_suppression(phone: str, authorization: str | None = Header(None)) -> dict:
    tid, _, _ = _auth(authorization)
    p = normalize_phone(phone)
    if not p:
        raise _err(422, "invalid_phone", "not a valid phone number")
    with db() as c:
        n = c.execute("DELETE FROM suppressions WHERE tenant=? AND phone_hash=?", (tid, _phone_hash(tid, p))).rowcount
    return {"removed": bool(n)}


@router.post("/v1/webhooks/test")
async def webhook_test(authorization: str | None = Header(None)) -> dict:
    """Sends a signed `webhook.test` event now and reports what the endpoint answered."""
    import httpx
    tid, t, _ = _auth(authorization)
    if not t.get("webhook_url"):
        raise _err(409, "no_webhook", "no webhook_url is set for this account")
    ts = int(time.time())
    body = json.dumps({"id": "dlv_test_" + secrets.token_hex(6), "type": "webhook.test",
                       "created_at": _iso(ts), "data": {"hello": "world"}})
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(t["webhook_url"], content=body.encode(), headers={
                "content-type": "application/json", "x-webhook-event": "webhook.test",
                "x-webhook-signature": f"t={ts},v1={sign(t['webhook_secret'], ts, body)}"})
        return {"delivered": 200 <= resp.status_code < 300, "status_code": resp.status_code}
    except Exception as e:
        return {"delivered": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.get("/v1/deliveries")
async def deliveries(authorization: str | None = Header(None), state: str | None = None,
                     limit: int = Query(50, ge=1, le=200)) -> dict:
    """Webhook delivery log — what we sent, when, and what your endpoint answered."""
    tid, _, _ = _auth(authorization)
    q, args = "SELECT id, call_id, event, state, attempts, last_error, created_at, delivered_at, next_at FROM deliveries WHERE tenant=?", [tid]
    if state:
        q += " AND state=?"; args.append(state)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        rows = c.execute(q, args).fetchall()
    return {"data": [{"id": r["id"], "call_id": r["call_id"], "event": r["event"], "state": r["state"],
                      "attempts": r["attempts"], "last_error": r["last_error"],
                      "created_at": _iso(r["created_at"]), "delivered_at": _iso(r["delivered_at"]),
                      "next_attempt_at": _iso(r["next_at"]) if r["state"] == "pending" else None}
                     for r in rows]}


def install_error_handlers(app) -> None:
    """Errors as {"error": {"code", "message"}} on /v1 — FastAPI's default shapes are not a contract."""
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        msgs = [f"{'.'.join(str(x) for x in e['loc'] if x != 'body')}: {e['msg'].removeprefix('Value error, ')}"
                for e in exc.errors()]
        return JSONResponse({"error": {"code": "invalid_request", "message": "; ".join(msgs)}}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        d = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        if not request.url.path.startswith("/v1"):
            return JSONResponse({"detail": d.get("message")}, status_code=exc.status_code)
        return JSONResponse({"error": d}, status_code=exc.status_code)


# ------------------------------------------------------------------------------ tenant admin (CLI)

def _save_tenants(ts: dict) -> None:
    TENANTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = TENANTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(ts, indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(TENANTS)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("tenant-add", "tenant-set"):
        a = sub.add_parser(name)
        a.add_argument("id")
        a.add_argument("--name"); a.add_argument("--webhook-url")
        a.add_argument("--company-name"); a.add_argument("--agent-name"); a.add_argument("--voice")
        a.add_argument("--concurrency", type=int); a.add_argument("--hours", help="09:30-20:30")
        a.add_argument("--timezone", help="IANA name, e.g. Asia/Kolkata")
        a.add_argument("--retention-hours", type=int); a.add_argument("--sip-trunk-id")
        a.add_argument("--disable", action="store_true"); a.add_argument("--enable", action="store_true")
        a.add_argument("--rotate-keys", action="store_true"); a.add_argument("--rotate-webhook-secret", action="store_true")
    sub.add_parser("tenant-list")
    x = ap.parse_args()
    ts = _tenants()
    if x.cmd == "tenant-list":
        for tid, t in ts.items():
            print(f"{tid:16} {t.get('name','')!r:30} webhook={t.get('webhook_url')} conc={t.get('concurrency',10)}"
                  f"{' DISABLED' if t.get('disabled') else ''}")
        return
    if not re.match(r"^[a-z0-9-]{2,40}$", x.id):
        sys.exit("tenant id: lowercase letters, digits, dashes")
    if x.cmd == "tenant-add" and x.id in ts:
        sys.exit(f"{x.id} exists — use tenant-set")
    if x.cmd == "tenant-set" and x.id not in ts:
        sys.exit(f"no tenant {x.id}")
    t = ts.get(x.id, {"concurrency": 10, "calling_hours": ["09:30", "20:30"], "retention_hours": 24, "defaults": {}})
    shown = {}
    if x.cmd == "tenant-add" or x.rotate_keys:
        for mode in ("live", "test"):
            k = f"of_{mode}_" + secrets.token_hex(24)
            t[f"{mode}_key_sha256"] = _sha(k)
            shown[f"{mode}_key"] = k
    if x.cmd == "tenant-add" or x.rotate_webhook_secret:
        t["webhook_secret"] = shown["webhook_secret"] = "whsec_" + secrets.token_hex(24)
    for arg, key in (("name", "name"), ("webhook_url", "webhook_url"), ("concurrency", "concurrency"),
                     ("retention_hours", "retention_hours"), ("sip_trunk_id", "sip_trunk_id"),
                     ("timezone", "timezone")):
        if getattr(x, arg) is not None:
            t[key] = getattr(x, arg)
    for arg, key in (("company_name", "company_name"), ("agent_name", "agent_name"), ("voice", "voice")):
        if getattr(x, arg) is not None:
            t.setdefault("defaults", {})[key] = getattr(x, arg)
    if x.hours:
        t["calling_hours"] = x.hours.split("-")
    if x.disable:
        t["disabled"] = True
    if x.enable:
        t.pop("disabled", None)
    ts[x.id] = t
    _save_tenants(ts)
    print(json.dumps({"tenant": x.id, **{k: v for k, v in t.items() if not k.endswith(("_sha256", "secret"))}}, indent=2))
    if shown:
        print("\nShown ONCE — send to the client over a secure channel:")
        for k, v in shown.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
