"""Postgres connection pool and schema."""
import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Neon/Render hand out postgres:// or postgresql:// URLs; psycopg accepts both.
    return url


pool: ConnectionPool | None = None

SCHEMA = """
create table if not exists slots (
    id        text primary key,
    provider  text        not null,
    start_at  text        not null,   -- echoed back exactly as sent
    end_at    text        not null,
    start_ts  timestamptz not null    -- parsed, used for ordering and the "already started" check
);
create index if not exists slots_order on slots (start_ts, id);
create index if not exists slots_provider on slots (provider);

create table if not exists bookings (
    seq        bigserial   not null unique,   -- stable ordering + cursor
    id         text        primary key,
    slot_id    text        not null references slots (id) on delete cascade,
    name       text        not null,
    email      text        not null,          -- stored lower-case
    status     text        not null check (status in ('confirmed', 'cancelled')),
    created_at text        not null
);
-- The guarantee behind level 3: the database itself refuses a second confirmed
-- booking for a slot, no matter how many requests arrive at once.
create unique index if not exists one_confirmed_per_slot
    on bookings (slot_id) where status = 'confirmed';
create index if not exists bookings_email_seq on bookings (email, seq);

create table if not exists idempotency (
    key       text primary key,
    body_hash text        not null,
    status    int         not null,
    response  jsonb       not null,
    created   timestamptz not null default now()
);

create table if not exists attempts (
    email text        not null,
    at    timestamptz not null default clock_timestamp()
);
create index if not exists attempts_email_at on attempts (email, at);
"""


def open_pool() -> None:
    global pool
    pool = ConnectionPool(
        _dsn(),
        min_size=1,
        max_size=int(os.environ.get("DB_POOL_MAX", "10")),
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
        check=ConnectionPool.check_connection,  # drop connections Neon closed while idle
        open=False,
    )
    pool.open(wait=True, timeout=30)
    with pool.connection() as conn:
        conn.execute("select pg_advisory_xact_lock(42)")  # one worker creates the schema
        conn.execute(SCHEMA)


def close_pool() -> None:
    if pool is not None:
        pool.close()
