# Clinic Booking API

FastAPI + Postgres. Implements levels 1-5 of the Koya assessment spec, plus a one-page web UI at `/`.

## Layout
- `app/main.py`: all endpoints
- `app/db.py`: connection pool + schema (created automatically at startup)
- `app/static/index.html`: the web page (served at `/`, no key needed to load it; it asks for the API key)
- `scripts/smoke_test.py`: black-box checks for every level, including races
- `render.yaml`: Render blueprint

## How each level is met
| Level | Mechanism |
|---|---|
| 1 | `/reset`, `/slots`, `POST /bookings`, `GET /bookings/{id}` |
| 2 | `X-API-Key` checked on every API route (constant-time compare); field validation; name/email trimmed, email lower-cased |
| 3 | Partial unique index `one_confirmed_per_slot` on `bookings(slot_id) where status='confirmed'`; the database decides races. Cancel is a single atomic `UPDATE ... WHERE status='confirmed'` |
| 4 | `Idempotency-Key`: stored with a hash of the body, guarded by a Postgres advisory lock so simultaneous retries take turns. Cursor pagination on an ordered sequence column |
| 5 | Sliding 60 s window, 5 attempts per email, counted in Postgres under a per-email advisory lock, so it holds under concurrency and across restarts. `Retry-After` = seconds until the oldest attempt leaves the window |

## Deploy (Neon + Render, both free)
1. **Neon**: create a project, copy the connection string (`postgresql://...?sslmode=require`).
2. **Render**: push this folder to a GitHub repo, then New > Blueprint (or New > Web Service) pointing at it.
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Env vars: `DATABASE_URL` = the Neon string, `API_KEY` = a long random secret, `PYTHON_VERSION` = `3.12.8`
3. Generate a key with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
4. Test the deployed URL **before submitting** (the script wipes data):
   `BASE_URL=https://<your-app>.onrender.com API_KEY=<key> python scripts/smoke_test.py`
5. Submit the URL and the key. After submitting, do not run the smoke test or `/reset` again.

## Run locally
```
pip install -r requirements.txt httpx
export DATABASE_URL=postgresql://user:pass@localhost:5432/clinic API_KEY=dev-key
uvicorn app.main:app --reload
```
Set `SEED_DEMO=0` to skip the demo slots that are created on an empty database.
