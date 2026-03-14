# roundtable

Simple exchange of contact info of groups with no sign-ups using FastAPI.

Share contact links with a group instantly — no accounts, no app install, no pairwise sending.

## How it works

1. **One person** goes to the site, enters their name + contact URL, and clicks **"Create room"** → gets a 4-digit code.
2. **Everyone else** goes to the same site, enters their info, clicks **"Join room"**, and types the code.
3. When everyone is in, **one person** clicks **"⚡ Connect Everyone"** → the full list of contact URLs appears for everyone at once.

## Files

| File | Purpose |
|------|---------|
| `server.py` | FastAPI application — routes, models, async long-poll logic |
| `wsgi.py` | ASGI entry point — re-exports `app` for production servers |
| `index.html` | Single-page frontend (served by FastAPI) |
| `requirements.txt` | Python dependencies (`fastapi`, `uvicorn`) |

## Setup

```bash
pip install -r requirements.txt
```

## Development

```bash
uvicorn server:app --reload --port 8080
# → http://localhost:8080
# --reload restarts the server automatically when you edit server.py
```

## Production (ASGI)

FastAPI is an **ASGI** framework (not WSGI). Use an ASGI-compatible server.

### Uvicorn (simplest)

```bash
uvicorn wsgi:app --host 0.0.0.0 --port 8080 --workers 4
```

### Gunicorn + Uvicorn workers (recommended for production)

```bash
pip install gunicorn
gunicorn wsgi:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 4 \
  --bind 0.0.0.0:8080
```

### Nginx (reverse proxy)

```nginx
location / {
    proxy_pass         http://127.0.0.1:8080;
    proxy_read_timeout 25s;   # must exceed the 20s long-poll timeout
}
```

## Performance improvements over v1

| Issue | v1 (http.server) | v2 (FastAPI) |
|---|---|---|
| Long-poll blocks a thread | `time.sleep(1)` loop holds thread for up to 20s | `await asyncio.sleep()` suspends coroutine, thread is free |
| Poll wake-up latency | Up to 1s delay when someone joins | Instant — `asyncio.Event.set()` wakes waiting polls immediately |
| Lock contention | Single global lock for all rooms | Per-room `asyncio.Lock` — rooms never block each other |
| Request validation | Manual `str(body.get(...))` checks | Pydantic models with automatic type coercion and error messages |
| Cleanup | Called on every poll request | Dedicated background task running every 60s |

## Automatic API docs

FastAPI generates interactive API docs automatically:

- **Swagger UI**: http://localhost:8080/docs
- **ReDoc**: http://localhost:8080/redoc

## Multi-server note

Room state lives in memory, so members must hit the same server process.
For multi-server deployments, swap the `rooms` dict for Redis and replace
`asyncio.Event` with Redis Pub/Sub — the rest of the code stays the same.
