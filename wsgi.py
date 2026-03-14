"""
ASGI entry point for ContactSwap (FastAPI rewrite).

FastAPI is an ASGI framework, not WSGI. Use an ASGI server:

    uvicorn  wsgi:app --workers 4 --host 0.0.0.0 --port 8080
    gunicorn wsgi:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8080

Why ASGI instead of WSGI?
--------------------------
WSGI is synchronous — each request occupies a thread from start to finish.
ASGI (Asynchronous Server Gateway Interface) is the async successor: a single
worker can handle thousands of concurrent connections because coroutines
yield the event loop while waiting (e.g. during the 20-second long-poll),
rather than blocking a thread.

Worker scaling notes
--------------------
Unlike the original http.server version, you CAN safely use multiple
worker *processes* here as long as state is kept in-process (the default).
Each worker has its own `rooms` dict, so members must be in the same worker.
For a small group app on one machine this is fine. For multi-server deployments,
replace the in-memory store with Redis (see README).

Nginx proxy_read_timeout
------------------------
If sitting behind Nginx, set proxy_read_timeout to at least 25s to allow
the 20-second long-poll to complete without being cut off:

    proxy_read_timeout 25s;
"""

from server import app  # re-export the ASGI callable

__all__ = ["app"]
