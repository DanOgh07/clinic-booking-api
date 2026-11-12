"""Clinic Booking API: levels 1-5 of the Koya assessment spec."""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db

log = logging.getLogger("clinic")

API_KEY = os.environ.get("API_KEY", "")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RATE_LIMIT = 5          # attempts per email ...
RATE_WINDOW_S = 60      # ... per this many seconds
STATIC = Path(__file__).parent / "static"


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        self.status, self.message, self.headers = status, message, headers or {}


def reply(status: int, body, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers)


def err(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return reply(status, {"error": message}, headers)


# ---------------------------------------------------------------- app setup

@asynccontextmanager
async def lifespan(_: FastAPI):
    if not API_KEY:
        raise RuntimeError("API_KEY environment variable is not set")
    db.open_pool()
    if os.environ.get("SEED_DEMO", "1") == "1":
        seed_demo_slots()
    yield
    db.close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(ApiError)
async def _api_error(_, exc: ApiError):
    return err(exc.status, exc.message, exc.headers)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_, exc: StarletteHTTPException):
    return err(exc.status_code, str(exc.detail))


@app.exception_handler(Exception)
async def _unhandled(_, exc: Exception):
    log.exception("unhandled error", exc_info=exc)
    return err(500, "internal server error")


def require_key(request: Request) -> None:
    given = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(given.encode(), API_KEY.encode()):
        raise ApiError(401, "missing or invalid API key")


# ---------------------------------------------------------------- helpers

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def booking_json(row: dict) -> dict:
    return {k: row[k] for k in ("id", "slot_id", "name", "email", "status", "created_at")}


async def json_body(request: Request):
    try:
        return json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "request body must be valid JSON")


def clean_str(value) -> str | None:
    """A trimmed, non-empty string, or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def valid_email(value) -> str | None:
    e = clean_str(value)
    if e and len(e) <= 254 and EMAIL_RE.match(e):
        return e.lower()
    return None


# ---------------------------------------------------------------- level 1: reset + slots

def _reset(slots: list[dict]) -> None:
    with db.pool.connection() as conn:
        # Wait for in-flight bookings, and hold new ones back until the swap is done.
        conn.execute("lock table slots in access exclusive mode")
        conn.execute("delete from bookings")
        conn.execute("delete from idempotency")
        conn.execute("delete from attempts")
        conn.execute("delete from slots")
        with conn.cursor() as cur:
            cur.executemany(
                "insert into slots (id, provider, start_at, end_at, start_ts) values (%s,%s,%s,%s,%s)",
                slots,
            )


@app.post("/reset", dependencies=[Depends(require_key)])
async def reset(request: Request):
    data = await json_body(request)
    raw = data.get("slots") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ApiError(400, "body must be an object with a 'slots' array")
    rows, seen = [], set()
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ApiError(400, f"slots[{i}] must be an object")
        sid, provider = clean_str(s.get("id")), clean_str(s.get("provider"))
        if not sid or not provider:
            raise ApiError(400, f"slots[{i}] needs a non-empty id and provider")
        if sid in seen:
            raise ApiError(400, f"duplicate slot id '{sid}'")
        seen.add(sid)
        try:
            start_ts = parse_ts(s.get("start"))
            parse_ts(s.get("end"))
        except ValueError:
            raise ApiError(400, f"slots[{i}] needs ISO 8601 start and end times")
        rows.append((sid, provider, s["start"], s["end"], start_ts))
    await run_in_threadpool(_reset, rows)
    return reply(200, {"ok": True, "slots": len(rows)})


def _list_slots(provider: str | None) -> list[dict]:
    sql = """
        select s.id, s.provider, s.start_at as start, s.end_at as "end",
               not exists (select 1 from bookings b
                           where b.slot_id = s.id and b.status = 'confirmed') as available
        from slots s
        {where}
        order by s.start_ts, s.id
    """
    where, args = ("where s.provider = %s", (provider,)) if provider is not None else ("", ())
    with db.pool.connection() as conn:
        return conn.execute(sql.format(where=where), args).fetchall()


@app.get("/slots", dependencies=[Depends(require_key)])
async def list_slots(request: Request):
    provider = request.query_params.get("provider")
    return reply(200, {"slots": await run_in_threadpool(_list_slots, provider)})


# ---------------------------------------------------------------- levels 1-5: create booking

def _create_booking(data, idem_key: str | None):
    body_hash = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()

    with db.pool.connection() as conn:   # one transaction; commits when we return
        cur = conn.cursor()

        # Level 4: idempotency. The advisory lock makes two simultaneous requests with the
        # same key take turns, so the second sees the first one's stored result.
        if idem_key:
            cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("idem:" + idem_key,))
            row = cur.execute(
                "select body_hash, status, response from idempotency where key = %s", (idem_key,)
            ).fetchone()
            if row:
                if row["body_hash"] != body_hash:
                    return err(422, "Idempotency-Key was already used with a different request body")
                return reply(row["status"], row["response"])

        # Level 5: at most 5 attempts per email per minute, failed attempts included.
        # Any request carrying a usable email counts, whatever happens to it afterwards.
        email = valid_email(data.get("email")) if isinstance(data, dict) else None
        if email:
            cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("rate:" + email,))
            cur.execute(
                "delete from attempts where email = %s "
                "and at <= clock_timestamp() - make_interval(secs => %s)",
                (email, RATE_WINDOW_S),
            )
            row = cur.execute(
                "select count(*) as n, "
                "ceil(extract(epoch from (min(at) + make_interval(secs => %s) - clock_timestamp()))) as wait "
                "from attempts where email = %s",
                (RATE_WINDOW_S, email),
            ).fetchone()
            if row["n"] >= RATE_LIMIT:
                wait = max(1, int(row["wait"] or 1))
                return err(429, "too many booking attempts for this email, try again later",
                           {"Retry-After": str(wait)})
            cur.execute("insert into attempts (email) values (%s)", (email,))

        # Level 2: validation
        if not isinstance(data, dict):
            return err(400, "body must be a JSON object")
        slot_id, name = clean_str(data.get("slot_id")), clean_str(data.get("name"))
        if not slot_id:
            return err(400, "slot_id is required")
        if not name:
            return err(400, "name is required")
        if len(name) > 200:
            return err(400, "name is too long")
        if not email:
            return err(400, "a valid email is required")

        slot = cur.execute(
            "select start_ts <= clock_timestamp() as started from slots where id = %s", (slot_id,)
        ).fetchone()
        if not slot:
            return err(404, "slot not found")
        if slot["started"]:
            return err(400, "slot has already started")

        # Level 3: the unique index on confirmed bookings decides who wins a race.
        booking = cur.execute(
            """
            insert into bookings (id, slot_id, name, email, status, created_at)
            values (%s, %s, %s, %s, 'confirmed', %s)
            on conflict (slot_id) where status = 'confirmed' do nothing
            returning id, slot_id, name, email, status, created_at
            """,
            (uuid.uuid4().hex, slot_id, name, email, now_iso()),
        ).fetchone()
        if not booking:
            return err(409, "slot is already booked")

        body = booking_json(booking)
        if idem_key:
            cur.execute(
                "insert into idempotency (key, body_hash, status, response) values (%s,%s,201,%s)",
                (idem_key, body_hash, Jsonb(body)),
            )
        return reply(201, body)


@app.post("/bookings", dependencies=[Depends(require_key)])
async def create_booking(request: Request):
    data = await json_body(request)
    idem_key = (request.headers.get("idempotency-key") or "").strip() or None
    if idem_key and len(idem_key) > 255:
        raise ApiError(400, "Idempotency-Key is too long")
    return await run_in_threadpool(_create_booking, data, idem_key)


# ---------------------------------------------------------------- level 4: list a person's bookings

def _list_bookings(email: str, limit: int, after_seq: int):
    with db.pool.connection() as conn:
        rows = conn.execute(
            """
            select seq, id, slot_id, name, email, status, created_at
            from bookings
            where email = %s and seq > %s
            order by seq
            limit %s
            """,
            (email, after_seq, limit + 1),
        ).fetchall()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = base64.urlsafe_b64encode(str(page[-1]["seq"]).encode()).decode().rstrip("=")
    return {"bookings": [booking_json(r) for r in page], "next_cursor": next_cursor}


@app.get("/bookings", dependencies=[Depends(require_key)])
async def list_bookings(request: Request):
    q = request.query_params
    # A raw '+' in a query string arrives as a space; real emails can't contain spaces.
    email = valid_email((q.get("email") or "").strip().replace(" ", "+"))
    if not email:
        raise ApiError(400, "a valid email query parameter is required")

    limit = 10
    if "limit" in q:
        raw = q["limit"]
        if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= 50:
            raise ApiError(400, "limit must be an integer from 1 to 50")
        limit = int(raw)

    after_seq = 0
    if q.get("cursor"):
        try:
            padded = q["cursor"] + "=" * (-len(q["cursor"]) % 4)
            after_seq = int(base64.urlsafe_b64decode(padded.encode()).decode())
            if after_seq < 0:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "invalid cursor")

    return reply(200, await run_in_threadpool(_list_bookings, email, limit, after_seq))


# ---------------------------------------------------------------- get / cancel one booking

def _get_booking(booking_id: str):
    with db.pool.connection() as conn:
        return conn.execute(
            "select id, slot_id, name, email, status, created_at from bookings where id = %s",
            (booking_id,),
        ).fetchone()


@app.get("/bookings/{booking_id}", dependencies=[Depends(require_key)])
async def get_booking(booking_id: str):
    row = await run_in_threadpool(_get_booking, booking_id)
    if not row:
        raise ApiError(404, "booking not found")
    return reply(200, booking_json(row))


def _cancel(booking_id: str):
    with db.pool.connection() as conn:
        # Atomic: of two simultaneous cancels, exactly one matches status = 'confirmed'.
        row = conn.execute(
            """
            update bookings set status = 'cancelled'
            where id = %s and status = 'confirmed'
            returning id, slot_id, name, email, status, created_at
            """,
            (booking_id,),
        ).fetchone()
        if row:
            return reply(200, booking_json(row))
        exists = conn.execute("select 1 from bookings where id = %s", (booking_id,)).fetchone()
        return err(409, "booking is already cancelled") if exists else err(404, "booking not found")


@app.delete("/bookings/{booking_id}", dependencies=[Depends(require_key)])
async def cancel_booking(booking_id: str):
    return await run_in_threadpool(_cancel, booking_id)


# ---------------------------------------------------------------- web page + demo data

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html", media_type="text/html")


def seed_demo_slots() -> None:
    """Give the web page something to show on a fresh database. /reset replaces these."""
    with db.pool.connection() as conn:
        conn.execute("select pg_advisory_xact_lock(43)")
        if conn.execute("select 1 from slots limit 1").fetchone():
            return
        day = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
        rows = []
        for d in range(1, 4):
            for provider in ("Dr Ade", "Dr Bello"):
                for h in range(3):
                    start = day + timedelta(days=d, hours=h)
                    end = start + timedelta(minutes=30)
                    iso = lambda t: t.isoformat().replace("+00:00", "Z")
                    rows.append((f"demo-{provider[3:].lower()}-{d}-{h}", provider, iso(start), iso(end), start))
        with conn.cursor() as cur:
            cur.executemany(
                "insert into slots (id, provider, start_at, end_at, start_ts) values (%s,%s,%s,%s,%s)",
                rows,
            )
