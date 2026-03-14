#!/usr/bin/env python3
"""
ContactSwap — Redis-backed rewrite.

Replaces the in-memory `rooms` dict with Leapcell Redis so that room state
survives restarts and is visible to every instance regardless of how the
platform routes requests.

Storage layout (Redis keys)
---------------------------
  room:{code}              — Hash  {code, created_at, last_activity, connected,
                                     member_ids (JSON list)}
  room:{code}:member:{id}  — Hash  {id, name, url}

Pub/Sub
-------
  channel: room:{code}     — Published to whenever a member joins, leaves, or
                             the room is connected. Wakes up waiting polls
                             immediately instead of sleeping in a loop.

Environment variables
---------------------
  REDIS_URL   — Redis connection string, e.g.
                redis://default:password@host:6379
                Set this in Leapcell → Service → Environment.

Dev server:   uvicorn server:app --reload --port 8080
Production:   see wsgi.py / README
"""

import asyncio
import json
import os
import random
import string
import time
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl, field_validator

# ── Constants ─────────────────────────────────────────────────────────────────
ROOM_TTL = 3600       # seconds; applied as Redis key TTL
POLL_TIMEOUT = 20     # seconds a long-poll waits for a pub/sub message
BASE_DIR = Path(__file__).parent

# ── Redis client (initialised in lifespan) ────────────────────────────────────
_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised")
    return _redis


# ── Key helpers ───────────────────────────────────────────────────────────────
def _new_id(k: int = 12) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=k))


def _room_key(code: str) -> str:
    return f"room:{code}"


def _member_key(code: str, member_id: str) -> str:
    return f"room:{code}:member:{member_id}"


def _channel(code: str) -> str:
    return f"room:{code}"


# ── Redis helpers ─────────────────────────────────────────────────────────────
async def _get_room_meta(r: aioredis.Redis, code: str) -> dict | None:
    data = await r.hgetall(_room_key(code))
    if not data:
        return None
    return {k.decode(): v.decode() for k, v in data.items()}


async def _get_members(r: aioredis.Redis, code: str, meta: dict) -> list[dict]:
    member_ids = json.loads(meta.get("member_ids", "[]"))
    members = []
    for mid in member_ids:
        raw = await r.hgetall(_member_key(code, mid))
        if raw:
            members.append({k.decode(): v.decode() for k, v in raw.items()})
    return members


async def _room_state(r: aioredis.Redis, code: str) -> dict:
    meta = await _get_room_meta(r, code)
    if not meta:
        return {}
    connected = meta.get("connected") == "1"
    members = await _get_members(r, code, meta)
    members_out = (
        members
        if connected
        else [{"id": m["id"], "name": m["name"]} for m in members]
    )
    return {
        "connected": connected,
        "member_count": len(members),
        "members": members_out,
    }


async def _new_room_code(r: aioredis.Redis) -> str:
    for _ in range(200):
        code = "".join(random.choices(string.digits, k=4))
        if not await r.exists(_room_key(code)):
            return code
    raise RuntimeError("Could not generate a unique room code")


async def _touch_room(r: aioredis.Redis, code: str):
    """Reset TTL and update last_activity timestamp."""
    await r.hset(_room_key(code), "last_activity", str(time.time()))
    await r.expire(_room_key(code), ROOM_TTL)


# ── App lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    _redis = aioredis.from_url(redis_url, decode_responses=False)
    try:
        await _redis.ping()
    except Exception as e:
        raise RuntimeError(f"Could not connect to Redis at {redis_url}: {e}")
    print(f"Connected to Redis at {redis_url}")
    yield
    await _redis.aclose()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="ContactSwap", lifespan=lifespan)


# ── Request schemas ───────────────────────────────────────────────────────────
class CreateRoomRequest(BaseModel):
    name: str
    url: HttpUrl

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()[:60]
        if not v:
            raise ValueError("name must not be empty")
        return v


class JoinRoomRequest(BaseModel):
    code: str
    name: str
    url: HttpUrl

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()[:60]
        if not v:
            raise ValueError("name must not be empty")
        return v

    @field_validator("code")
    @classmethod
    def code_is_digits(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 4:
            raise ValueError("code must be exactly 4 digits")
        return v


class RoomMemberRequest(BaseModel):
    code: str
    member_id: str


# ── API routes ────────────────────────────────────────────────────────────────
@app.post("/api/create_room")
async def create_room(req: CreateRoomRequest):
    r = get_redis()
    code = await _new_room_code(r)
    member_id = _new_id()
    now = time.time()

    await r.hset(_room_key(code), mapping={
        "code":          code,
        "created_at":    str(now),
        "last_activity": str(now),
        "connected":     "0",
        "member_ids":    json.dumps([member_id]),
    })
    await r.expire(_room_key(code), ROOM_TTL)

    await r.hset(_member_key(code, member_id), mapping={
        "id":   member_id,
        "name": req.name,
        "url":  str(req.url),
    })
    await r.expire(_member_key(code, member_id), ROOM_TTL)

    return {"room_code": code, "member_id": member_id}


@app.post("/api/join_room")
async def join_room(req: JoinRoomRequest):
    r = get_redis()
    meta = await _get_room_meta(r, req.code)

    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Room not found. Check the code and try again.")
    if meta.get("connected") == "1":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This room has already been connected. "
                            "Ask your group to start a new room.")

    member_id = _new_id()
    member_ids = json.loads(meta.get("member_ids", "[]"))
    member_ids.append(member_id)

    await r.hset(_room_key(req.code), "member_ids", json.dumps(member_ids))
    await _touch_room(r, req.code)

    await r.hset(_member_key(req.code, member_id), mapping={
        "id":   member_id,
        "name": req.name,
        "url":  str(req.url),
    })
    await r.expire(_member_key(req.code, member_id), ROOM_TTL)

    # Wake up all waiting polls for this room immediately
    await r.publish(_channel(req.code), "join")

    return {"room_code": req.code, "member_id": member_id}


@app.post("/api/connect")
async def connect(req: RoomMemberRequest):
    r = get_redis()
    meta = await _get_room_meta(r, req.code)

    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")

    member_ids = json.loads(meta.get("member_ids", "[]"))
    if req.member_id not in member_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this room")

    await r.hset(_room_key(req.code), "connected", "1")
    await _touch_room(r, req.code)

    # Wake all waiting polls — they will all see connected=True
    await r.publish(_channel(req.code), "connected")

    return {"ok": True}


@app.get("/api/poll")
async def poll(code: str, member_id: str, count: int = 0):
    """
    Async long-poll backed by Redis Pub/Sub.

    Subscribes to the room's channel. Any publish (join/connect/leave)
    wakes this coroutine immediately so the client sees the change
    without waiting up to 1 second as in the old sleep-loop design.
    """
    r = get_redis()
    meta = await _get_room_meta(r, code)

    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")

    member_ids = json.loads(meta.get("member_ids", "[]"))
    if member_id not in member_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member")

    # Return immediately if state already changed since last poll
    if len(member_ids) != count or meta.get("connected") == "1":
        return await _room_state(r, code)

    # Subscribe and block until a message arrives or timeout
    pubsub = r.pubsub()
    await pubsub.subscribe(_channel(code))
    try:
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                    timeout=remaining + 0.1,
                )
            except asyncio.TimeoutError:
                break
            if msg is not None:
                break
            await asyncio.sleep(0.05)
    finally:
        await pubsub.unsubscribe(_channel(code))
        await pubsub.aclose()

    return await _room_state(r, code)


@app.post("/api/leave_room")
async def leave_room(req: RoomMemberRequest):
    r = get_redis()
    meta = await _get_room_meta(r, req.code)
    if meta:
        member_ids = json.loads(meta.get("member_ids", "[]"))
        if req.member_id in member_ids:
            member_ids.remove(req.member_id)
            await r.hset(_room_key(req.code), "member_ids", json.dumps(member_ids))
            await r.delete(_member_key(req.code, req.member_id))
            await _touch_room(r, req.code)
            await r.publish(_channel(req.code), "leave")
    return {"ok": True}


# ── Debug endpoint ────────────────────────────────────────────────────────────
# Useful for confirming Redis connectivity and verifying room keys exist.
# Remove or restrict this before making the app publicly accessible.
@app.get("/api/debug")
async def debug():
    r = get_redis()
    keys = [k.decode() async for k in r.scan_iter("room:????")]
    return {"worker_pid": os.getpid(), "room_keys": keys, "room_count": len(keys)}


# ── Serve frontend ────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)
