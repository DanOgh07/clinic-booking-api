"""Black-box checks for levels 1-5.

    BASE_URL=https://your-app.onrender.com API_KEY=... python scripts/smoke_test.py

WARNING: calls POST /reset, which wipes all bookings and slots. Never point this at the
deployed service after you have submitted it.
"""
import concurrent.futures as cf
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
KEY = os.environ["API_KEY"]
H = {"X-API-Key": KEY}
client = httpx.Client(base_url=BASE, headers=H, timeout=30)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def email():
    return f"t{uuid.uuid4().hex[:8]}@example.com"


now = datetime.now(timezone.utc)
fut = lambda h: now + timedelta(days=30, hours=h)
SLOTS = [
    {"id": "s1", "provider": "Dr Ade", "start": iso(fut(2)), "end": iso(fut(3))},
    {"id": "s2", "provider": "Dr Bello", "start": iso(fut(0)), "end": iso(fut(1))},
    {"id": "s3", "provider": "Dr Ade", "start": iso(fut(1)), "end": iso(fut(2))},
    {"id": "past", "provider": "Dr Ade", "start": iso(now - timedelta(hours=2)), "end": iso(now - timedelta(hours=1))},
]
for i in range(4, 14):
    SLOTS.append({"id": f"p{i}", "provider": "Dr Page", "start": iso(fut(10 + i)), "end": iso(fut(11 + i))})


def reset():
    r = client.post("/reset", json={"slots": SLOTS})
    assert r.status_code == 200, r.text


def book(slot="s1", name="Ada Obi", mail=None, **kw):
    return client.post("/bookings", json={"slot_id": slot, "name": name, "email": mail or email()}, **kw)


print("level 2: api key")
r = httpx.get(BASE + "/slots")
check("no key -> 401", r.status_code == 401 and "error" in r.json())
check("wrong key -> 401", httpx.get(BASE + "/slots", headers={"X-API-Key": "nope"}).status_code == 401)
check("wrong key on POST -> 401", httpx.post(BASE + "/bookings", json={}, headers={"X-API-Key": "x"}).status_code == 401)

print("level 1: slots, book, lookup")
reset()
r = client.get("/slots")
slots = r.json()["slots"]
check("lists all slots", r.status_code == 200 and len(slots) == len(SLOTS))
check("earliest start first", [s["start"] for s in slots] == sorted(s["start"] for s in slots))
check("all available", all(s["available"] for s in slots))
r = client.get("/slots", params={"provider": "Dr Bello"})
check("provider filter", [s["id"] for s in r.json()["slots"]] == ["s2"])
check("unknown provider -> empty", client.get("/slots", params={"provider": "Nobody"}).json() == {"slots": []})

mail = email()
r = book("s1", "  Ada Obi  ", f"  {mail.upper()}  ")
b = r.json()
check("book -> 201 confirmed", r.status_code == 201 and b["status"] == "confirmed", r.text)
check("name trimmed, email lower-cased", b["name"] == "Ada Obi" and b["email"] == mail)
check("booking shape", set(b) == {"id", "slot_id", "name", "email", "status", "created_at"})
r = client.get(f"/bookings/{b['id']}")
check("lookup booking", r.status_code == 200 and r.json() == b)
check("lookup unknown -> 404", client.get("/bookings/nope").status_code == 404)
check("slot now unavailable", {s["id"]: s["available"] for s in client.get("/slots").json()["slots"]}["s1"] is False)

print("level 2: validation")
check("missing slot_id -> 400", client.post("/bookings", json={"name": "A", "email": email()}).status_code == 400)
check("missing name -> 400", client.post("/bookings", json={"slot_id": "s2", "email": email()}).status_code == 400)
check("blank name -> 400", book("s2", "   ").status_code == 400)
check("missing email -> 400", client.post("/bookings", json={"slot_id": "s2", "name": "A"}).status_code == 400)
check("bad email -> 400", book("s2", "A", "not-an-email").status_code == 400)
check("non-string fields -> 400", client.post("/bookings", json={"slot_id": 1, "name": 2, "email": 3}).status_code == 400)
check("bad JSON -> 400", client.post("/bookings", content=b"{oops", headers={"Content-Type": "application/json"}).status_code == 400)
r = book("ghost")
check("unknown slot -> 404", r.status_code == 404 and "error" in r.json())
r = book("past")
check("slot already started -> 400", r.status_code == 400 and "error" in r.json())

print("level 3: no double booking, cancel")
r = book("s1")
check("already booked -> 409", r.status_code == 409 and "error" in r.json())
reset()
with cf.ThreadPoolExecutor(40) as ex:
    results = list(ex.map(lambda _: book("s2").status_code, range(40)))
check("40 simultaneous -> exactly one 201", results.count(201) == 1 and results.count(409) == 39, str(sorted(set(results))))
r = book("s3")
bid = r.json()["id"]
r = client.delete(f"/bookings/{bid}")
check("cancel -> 200 cancelled", r.status_code == 200 and r.json()["status"] == "cancelled")
check("cancel persisted", client.get(f"/bookings/{bid}").json()["status"] == "cancelled")
check("cancel again -> 409", client.delete(f"/bookings/{bid}").status_code == 409)
check("cancel unknown -> 404", client.delete("/bookings/nope").status_code == 404)
check("slot free again", {s["id"]: s["available"] for s in client.get("/slots").json()["slots"]}["s3"] is True)
check("can rebook freed slot", book("s3").status_code == 201)
r = book("p4")
bid = r.json()["id"]
with cf.ThreadPoolExecutor(10) as ex:
    codes = list(ex.map(lambda _: client.delete(f"/bookings/{bid}").status_code, range(10)))
check("10 simultaneous cancels -> one 200", codes.count(200) == 1 and codes.count(409) == 9, str(codes))

print("level 4: idempotency")
reset()
mail, key = email(), uuid.uuid4().hex
body = {"slot_id": "s1", "name": "Ada", "email": mail}
r1 = client.post("/bookings", json=body, headers={"Idempotency-Key": key})
r2 = client.post("/bookings", json=body, headers={"Idempotency-Key": key})
check("same key+body -> same status and id", r1.status_code == r2.status_code == 201 and r1.json() == r2.json())
r3 = client.post("/bookings", json={**body, "name": "Different"}, headers={"Idempotency-Key": key})
check("same key, other body -> 422", r3.status_code == 422 and "error" in r3.json())
check("only one booking exists", len(client.get("/bookings", params={"email": mail}).json()["bookings"]) == 1)
key2 = uuid.uuid4().hex
with cf.ThreadPoolExecutor(20) as ex:
    rs = list(ex.map(lambda _: client.post("/bookings", json={"slot_id": "s2", "name": "B", "email": "race@example.com"}, headers={"Idempotency-Key": key2}), range(20)))
check("20 simultaneous retries -> one booking id", len({r.json().get("id") for r in rs if r.status_code == 201}) == 1 and all(r.status_code == 201 for r in rs), str(sorted(r.status_code for r in rs)))

print("level 4: pagination")
reset()
mail = email()  # level 5 allows only 5 attempts per email per minute, so 5 bookings is the max here
ids = [book(f"p{i}", "Pat", mail).json()["id"] for i in range(4, 9)]
page = client.get("/bookings", params={"email": mail.upper(), "limit": 2}).json()
check("page 1 has 2, oldest first", [b["id"] for b in page["bookings"]] == ids[:2] and page["next_cursor"])
seen, pages = [b["id"] for b in page["bookings"]], 1
while page["next_cursor"]:
    page = client.get("/bookings", params={"email": mail, "limit": 2, "cursor": page["next_cursor"]}).json()
    seen += [b["id"] for b in page["bookings"]]
    pages += 1
check("walking cursors returns all 5 once, in order", seen == ids and pages == 3, f"{pages} pages")
check("last page next_cursor is null", page["next_cursor"] is None)
check("default limit returns all 5, no next page", client.get("/bookings", params={"email": mail}).json()["next_cursor"] is None)
check("exact fit has no next page", client.get("/bookings", params={"email": mail, "limit": 5}).json()["next_cursor"] is None)
for bad in ("0", "51", "abc", "-1", "1.5", ""):
    check(f"limit={bad!r} -> 400", client.get("/bookings", params={"email": mail, "limit": bad}).status_code == 400)
check("limit=50 ok", client.get("/bookings", params={"email": mail, "limit": 50}).status_code == 200)
check("bad cursor -> 400", client.get("/bookings", params={"email": mail, "cursor": "!!!"}).status_code == 400)
check("no email -> 400", client.get("/bookings").status_code == 400)
check("unknown email -> empty page", client.get("/bookings", params={"email": email()}).json() == {"bookings": [], "next_cursor": None})
check("raw + in email works", client.get("/bookings?email=a+b@example.com").status_code == 200)
check("cancelled bookings stay in the list", client.delete(f"/bookings/{ids[0]}").status_code == 200
      and any(b["status"] == "cancelled" for b in client.get("/bookings", params={"email": mail, "limit": 50}).json()["bookings"]))

print("level 5: rate limit")
reset()
mail, other = email(), email()
codes = [book("s1" if i == 0 else "ghost", "Spam", mail).status_code for i in range(5)]
check("first 5 attempts processed (201 then 404s)", codes == [201, 404, 404, 404, 404], str(codes))
r = book("s2", "Spam", mail.upper())
retry = r.headers.get("Retry-After", "")
check("6th -> 429 with whole-second Retry-After", r.status_code == 429 and retry.isdigit() and 1 <= int(retry) <= 60, f"{r.status_code} {retry!r}")
check("other email unaffected", book("s2", "Fine", other).status_code == 201)
mail3 = email()
codes = [client.post("/bookings", json={"slot_id": "s3", "name": "", "email": mail3}).status_code for _ in range(6)]
check("5 x 400 then 429", codes == [400] * 5 + [429], str(codes))
mail4 = email()
with cf.ThreadPoolExecutor(30) as ex:
    codes = list(ex.map(lambda _: book("ghost", "Z", mail4).status_code, range(30)))
check("30 simultaneous -> exactly 5 processed", codes.count(404) == 5 and codes.count(429) == 25, str(sorted(set(codes))))
reset()  # leave a clean slate

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
