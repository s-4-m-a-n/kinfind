"""Request rate limits and request-size limits, in one place.

Every request is matched against RULES (first match wins) to get a rate-limit bucket and
the largest body it may send. Limits are per client IP address and kept in memory, so they
reset when the server restarts and aren't shared between several server processes.

Behind a reverse proxy every request arrives from the proxy, so everyone would share one set of
limits. Set TRUSTED_PROXY_HOPS to the number of proxies in front of the app (Cloud Run: 1) and
the client address is read from X-Forwarded-For instead; see client_ip().
"""
from __future__ import annotations

import os
import re
import time

MB = 1024 * 1024
KB = 1024
MAX_PHOTO_BYTES = 8 * MB      # each photo
MAX_RECORD_PHOTOS = 10        # photos per missing report or found record (general + feature photos)
MAX_FEATURES = 20             # distinctive-feature rows per record
MAX_TIP_PHOTOS = 4            # photos per community tip
UPLOAD_SLACK = 1 * MB         # form fields and multipart overhead on top of the photos

# Bucket name -> (requests, seconds), per IP address.
BUCKETS = {
    "all": (600, 60),               # any request, including pages and photos: stops floods
    "login": (10, 15 * 60),         # responder and family sign-in: stops password guessing
    "signup": (5, 60 * 60),         # new family accounts and responder access requests
    "report": (10, 60 * 60),        # new missing-person reports
    "found": (60, 10 * 60),         # responders logging found people
    "tip": (5, 10 * 60),            # community sightings
    "zone_suggestion": (5, 60 * 60),
    "checkin": (10, 10 * 60),       # "I'm safe" check-ins
    "safe_search": (20, 10 * 60),   # stops guessing phone numbers to find someone
    "family_action": (60, 10 * 60),  # verification requests, appeal changes, hiding tips, check-in updates
    "ai_check": (10, 10 * 60),      # re-running the paid AI photo check
    "gdacs_sync": (5, 10 * 60),
    "responder_action": (300, 10 * 60),
    "post": (60, 10 * 60),          # any other form
}

# (method, path pattern, bucket, largest body in bytes). First match wins.
RULES = [
    ("POST", r"/(responder|account)/(login|google)", "login", 8 * KB),  # a Google credential is ~1-2 KB
    ("POST", r"/(responder|account)/signup", "signup", 8 * KB),
    ("POST", r"/missing", "report", MAX_RECORD_PHOTOS * MAX_PHOTO_BYTES + UPLOAD_SLACK),
    ("POST", r"/responder/found", "found", MAX_RECORD_PHOTOS * MAX_PHOTO_BYTES + UPLOAD_SLACK),
    ("POST", r"/appeal/[^/]+/tips", "tip", MAX_TIP_PHOTOS * MAX_PHOTO_BYTES + UPLOAD_SLACK),
    ("POST", r"/zones", "zone_suggestion", 16 * KB),
    ("POST", r"/safe", "checkin", 32 * KB),
    ("POST", r"/safe/find", "safe_search", 4 * KB),
    ("POST", r"/case/.+|/safe/c/[^/]+", "family_action", 16 * KB),
    ("POST", r"/responder/(missing|found)/[^/]+/ai-check", "ai_check", 4 * KB),
    ("POST", r"/responder/zones/sync", "gdacs_sync", 4 * KB),
    ("POST", r"/responder/.+", "responder_action", 64 * KB),
    ("POST", r".*", "post", 64 * KB),
    (".*", r".*", None, 64 * KB),  # GET and anything else: only the "all" bucket
]
_COMPILED = [(re.compile(m), re.compile(p), bucket, size) for m, p, bucket, size in RULES]

_hits: dict[tuple[str, str], list[float]] = {}
_last_prune = 0.0
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))


def client_ip(scope) -> str:
    """The visitor's address.

    Each trusted proxy appends the address it received the request from to X-Forwarded-For, so
    with N proxies the client is the Nth entry from the right. Entries further left are whatever
    the client itself sent, and could be faked to dodge the limits, so they are ignored.
    """
    ip = (scope.get("client") or ("unknown", 0))[0]
    if TRUSTED_PROXY_HOPS:
        header = dict(scope.get("headers") or []).get(b"x-forwarded-for", b"").decode("latin-1")
        hops = [h.strip() for h in header.split(",") if h.strip()]
        if len(hops) >= TRUSTED_PROXY_HOPS:
            ip = hops[-TRUSTED_PROXY_HOPS]
    return ip


def rule_for(method: str, path: str) -> tuple[str | None, int]:
    for m, p, bucket, size in _COMPILED:
        if m.fullmatch(method) and p.fullmatch(path):
            return bucket, size
    return None, 64 * KB


def hit(bucket: str, ip: str, now: float | None = None) -> int:
    """Record one request. Returns 0 if allowed, else seconds until the client may retry."""
    global _last_prune
    limit, window = BUCKETS[bucket]
    now = now or time.time()
    if now - _last_prune > 300:  # drop idle addresses so memory doesn't grow forever
        for key in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
            del _hits[key]
        _last_prune = now
    recent = [t for t in _hits.get((bucket, ip), []) if now - t < window]
    if len(recent) >= limit:
        _hits[(bucket, ip)] = recent
        return int(window - (now - recent[0])) + 1
    recent.append(now)
    _hits[(bucket, ip)] = recent
    return 0


def human_size(n: int) -> str:
    return f"{n // MB} MB" if n >= MB else f"{n // KB} KB"


def reset() -> None:
    _hits.clear()


class BodyTooLarge(Exception):
    pass


class RequestLimits:
    """ASGI middleware: rate limits first (cheap), then a cap on the request body size.

    `reject(scope, receive, send, status, message, headers)` renders the error response.
    """

    def __init__(self, app, reject):
        self.app, self.reject = app, reject

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith("/static/"):
            return await self.app(scope, receive, send)
        ip = client_ip(scope)
        bucket, max_body = rule_for(scope["method"], scope["path"])
        for b in ("all", bucket) if bucket else ("all",):
            wait = hit(b, ip)
            if wait:
                return await self.reject(scope, receive, send, 429,
                                         "Too many requests from your connection. Please wait a few minutes and try again.",
                                         {"Retry-After": str(wait)})

        too_big = f"That's more than this form accepts. The limit here is {human_size(max_body)} in total."
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length and length.isdigit() and int(length) > max_body:
            return await self.reject(scope, receive, send, 413, too_big, {})

        received, started = 0, False

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_body:  # no or false Content-Length: stop reading here
                    raise BodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except BodyTooLarge:
            if not started:
                await self.reject(scope, receive, send, 413, too_big, {})
