"""Who may see and do what. Off on a laptop with no settings (every page open, as before); on as soon as
ADMIN_PASSWORD or POD_LAN_CODE is set, which every deployment does.

  admin     signs in at /login with ADMIN_PASSWORD. Sees every org; the admin page (/admin) issues and revokes access
            codes, flips the fault switches, reads the audit log.
  operator  enters an access code at /join. Sees only the org of that code (or every org for an all-org code, such
            as the team code POD_LAN_CODE that serve.py --lan prints); runs stages, uploads photos, reviews, overrides.
  laptop    a request from this machine itself that no proxy forwarded counts as admin (the presenter's screen).

The JSON API takes the same secrets as `Authorization: Bearer <access code or admin password>`.

Tenancy: `current().sees(org)` decides every read and write. Another org's case, workflow, record or photo answers 404,
exactly as if it did not exist, so a code for one org cannot even learn the other org's unit ids.

A session is a cookie signed with SESSION_SECRET (HMAC-SHA256). It stops working when its code is revoked, the team
code or admin password changes, or after 12 hours. Issued codes are stored only as SHA-256. Wrong codes are counted per
visitor (10) and in total (50); past either limit, signing in is closed until the server restarts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

COOKIE = "pod12_code"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-real-ip", "forwarded")
SESSION_S = 12 * 3600
MAX_WRONG = 10
MAX_WRONG_TOTAL = 50  # from anywhere: a forwarded address can be faked, so the total is capped too
PUBLIC = ("/join", "/login", "/health", "/favicon.ico")
_WRONG: dict[str, int] = defaultdict(int)
_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)  # no setting: sessions end when the server stops
_CODES: dict[int, dict] = {}  # issued codes when there is no database (tests, a laptop)
_AUDIT: deque = deque(maxlen=1000)
_LOCK = threading.Lock()
_REVOKED_CACHE: dict[int, tuple[float, bool]] = {}


@dataclass(frozen=True)
class Access:
    role: str  # "admin" | "operator" | "anon"
    orgs: frozenset | None = None  # None: every org
    actor: str = "anonymous"
    team: bool = False  # may see the team code and its QR (the presenter)
    cid: str = ""  # what the session rests on: "admin", "team:<hash>", or an issued code's id
    extra: dict = field(default_factory=dict, compare=False)

    def sees(self, org: str | None) -> bool:
        return self.role != "anon" and (self.orgs is None or org in self.orgs)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def scope(self) -> set[str] | None:
        return None if self.orgs is None else set(self.orgs)


ANON = Access("anon")
OPEN = Access("admin", None, "local", team=True, cid="open")
_CURRENT: ContextVar[Access] = ContextVar("pod12_access", default=OPEN)


def current() -> Access:
    return _CURRENT.get()


def guard_on() -> bool:
    return bool(admin_password() or lan_code())


def admin_password() -> str | None:
    return os.environ.get("ADMIN_PASSWORD") or None


def lan_code() -> str | None:
    return os.environ.get("POD_LAN_CODE") or None


def _h8(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- who is asking
def is_local(request: Request) -> bool:
    """The laptop itself: a loopback connection that no proxy forwarded. A Cloudflare tunnel or a cloud load balancer
    also connects from a private address, so without the header check every visitor would count as local."""
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    return (request.client.host if request.client else "") in LOOPBACK


def visitor(request: Request) -> str:
    """Who is asking, for the wrong-code lockout: the address the tunnel or proxy reports, else the socket's."""
    for h in ("cf-connecting-ip", "x-real-ip"):
        if request.headers.get(h):
            return request.headers[h].strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def locked(request: Request) -> bool:
    return _WRONG[visitor(request)] >= MAX_WRONG or sum(_WRONG.values()) >= MAX_WRONG_TOTAL


def wrong(request: Request) -> None:
    _WRONG[visitor(request)] += 1


# ---------------------------------------------------------------- secrets -> access
def check_code(code: str) -> Access | None:
    """An access code (the team code or one the admin issued) -> what it allows, or None."""
    code = (code or "").strip()
    if not code:
        return None
    team = lan_code()
    if team and hmac.compare_digest(code, team):
        return Access("operator", None, "team code", team=True, cid=f"team:{_h8(team)}")
    row = _find_code(hashlib.sha256(code.encode()).hexdigest())
    if row is None:
        return None
    orgs = None if row["org_id"] is None else frozenset({row["org_id"]})
    return Access(row["role"], orgs, f"code:{row['label']}", team=False, cid=str(row["id"]))


def check_admin(password: str) -> Access | None:
    want = admin_password()
    if want and hmac.compare_digest((password or "").encode(), want.encode()):
        return Access("admin", None, "admin", team=True, cid=f"admin:{_h8(want)}")
    return None


def _find_code(sha: str) -> dict | None:
    from shared.utils import db

    if db.enabled():
        r = db.fetchone(f"select id, label, org_id, role from {db.SCHEMA}.access_codes where code_sha256=%s "
                        f"and revoked_at is null", (sha,))
        return {"id": r[0], "label": r[1], "org_id": r[2], "role": r[3]} if r else None
    with _LOCK:
        return next((dict(c) for c in _CODES.values() if c["sha"] == sha and not c["revoked_at"]), None)


def _code_alive(cid: int) -> bool:
    hit = _REVOKED_CACHE.get(cid)
    if hit and time.monotonic() - hit[0] < 10:
        return hit[1]
    from shared.utils import db

    if db.enabled():
        r = db.fetchone(f"select revoked_at is null from {db.SCHEMA}.access_codes where id=%s", (cid,))
        alive = bool(r and r[0])
    else:
        with _LOCK:
            alive = cid in _CODES and not _CODES[cid]["revoked_at"]
    _REVOKED_CACHE[cid] = (time.monotonic(), alive)
    return alive


# ---------------------------------------------------------------- issued codes (admin page)
def issue_code(label: str, org_id: str | None, role: str = "operator") -> str:
    """Make a new 8-digit code for `org_id` (None: every org). Returns the code; only its hash is kept."""
    from shared.utils import db

    if role not in ("operator", "admin"):
        raise ValueError("role must be operator or admin")
    label = " ".join((label or "").split())[:60] or "unnamed"
    for _ in range(20):
        code = f"{secrets.randbelow(10**8):08d}"
        sha = hashlib.sha256(code.encode()).hexdigest()
        if _find_code(sha) is not None or code == lan_code():
            continue
        if db.enabled():
            db.execute(f"insert into {db.SCHEMA}.access_codes (label, org_id, role, code_sha256, created_by) "
                       f"values (%s,%s,%s,%s,%s)", (label, org_id, role, sha, current().actor))
        else:
            with _LOCK:
                cid = max(_CODES, default=0) + 1
                _CODES[cid] = {"id": cid, "label": label, "org_id": org_id, "role": role, "sha": sha,
                               "created_by": current().actor, "created_at": db.now(), "revoked_at": None}
        return code
    raise RuntimeError("could not find a free code")


def list_codes() -> list[dict]:
    from shared.utils import db

    if db.enabled():
        rows = db.fetchall(f"select id, label, org_id, role, created_by, created_at, revoked_at from "
                           f"{db.SCHEMA}.access_codes order by id desc")
        return [{"id": i, "label": lb, "org_id": o, "role": r, "created_by": by,
                 "created_at": at.strftime("%Y-%m-%d %H:%M UTC"),
                 "revoked_at": rv.strftime("%Y-%m-%d %H:%M UTC") if rv else None} for i, lb, o, r, by, at, rv in rows]
    with _LOCK:
        return [{k: v for k, v in c.items() if k != "sha"} for c in sorted(_CODES.values(), key=lambda c: -c["id"])]


def revoke_code(cid: int) -> bool:
    from shared.utils import db

    _REVOKED_CACHE.pop(cid, None)
    if db.enabled():
        r = db.fetchone(f"update {db.SCHEMA}.access_codes set revoked_at=now() where id=%s and revoked_at is null "
                        f"returning id", (cid,))
        return r is not None
    with _LOCK:
        if cid in _CODES and not _CODES[cid]["revoked_at"]:
            _CODES[cid]["revoked_at"] = db.now()
            return True
    return False


# ---------------------------------------------------------------- sessions
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def session_token(acc: Access) -> str:
    body = _b64(json.dumps({"r": acc.role, "o": None if acc.orgs is None else sorted(acc.orgs), "a": acc.actor,
                            "t": acc.team, "c": acc.cid, "x": int(time.time()) + SESSION_S}).encode())
    return f"{body}.{_b64(hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).digest())}"


def from_token(token: str) -> Access | None:
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(_unb64(sig), hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).digest()):
            return None
        p = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if p.get("x", 0) < time.time():
        return None
    cid = str(p.get("c", ""))
    if cid.startswith("team:"):  # a new team code (a restart) ends the old sessions
        if not lan_code() or cid != f"team:{_h8(lan_code())}":
            return None
    elif cid.startswith("admin:"):
        if not admin_password() or cid != f"admin:{_h8(admin_password())}":
            return None
    elif not (cid.isdigit() and _code_alive(int(cid))):
        return None
    return Access(p["r"], None if p.get("o") is None else frozenset(p["o"]), p.get("a", "?"), bool(p.get("t")), cid)


def set_session(resp: Response, acc: Access, request: Request) -> None:
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(COOKIE, session_token(acc), httponly=True, samesite="lax", secure=secure, max_age=SESSION_S)


def resolve(request: Request) -> Access:
    if not guard_on():
        return OPEN
    if is_local(request):
        return Access("admin", None, "laptop", team=True, cid="local")
    tok = request.cookies.get(COOKIE)
    if tok:
        acc = from_token(tok)
        if acc:
            return acc
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and not locked(request):
        secret = auth[7:].strip()
        acc = check_admin(secret) or check_code(secret)
        if acc:
            return acc
        wrong(request)
    return ANON


# ---------------------------------------------------------------- audit log
def audit(action: str, target: str | None = None, *, status: int | None = None, org: str | None = None,
          acc: Access | None = None, **detail) -> None:
    """Append one line to the audit log (the database when there is one). Never raises: a full audit table must not
    stop the warehouse, and the failure is logged."""
    from shared.utils import db
    from shared.utils.log import get_logger

    acc = acc or current()
    row = {"at": db.now(), "actor": acc.actor, "role": acc.role, "org_id": org, "action": action, "target": target,
           "status": status, "detail": detail or None}
    try:
        if db.enabled():
            from psycopg.types.json import Jsonb

            db.execute(f"insert into {db.SCHEMA}.audit (actor, role, org_id, action, target, status, detail) "
                       f"values (%s,%s,%s,%s,%s,%s,%s)", (row["actor"], row["role"], org, action, target, status,
                                                          Jsonb(detail) if detail else None))
        else:
            _AUDIT.appendleft(row)
    except Exception as exc:
        get_logger("audit").warning("audit line not written", extra={"ctx": {"action": action, "error": type(exc).__name__}})


def audit_log(limit: int = 200, org: str | None = None) -> list[dict]:
    from shared.utils import db

    if db.enabled():
        q = (f"select at, actor, role, org_id, action, target, status, detail from {db.SCHEMA}.audit "
             + ("where org_id=%s " if org else "") + "order by id desc limit %s")
        rows = db.fetchall(q, (org, limit) if org else (limit,))
        return [{"at": a.strftime("%Y-%m-%d %H:%M:%S"), "actor": ac, "role": r, "org_id": o, "action": act, "target": t,
                 "status": s, "detail": d} for a, ac, r, o, act, t, s, d in rows]
    return [r for r in list(_AUDIT) if not org or r["org_id"] == org][:limit]


# ---------------------------------------------------------------- the middleware
def _org_in(path: str) -> str | None:
    from . import _all_cases

    for org in sorted({c["org_id"] for c in _all_cases()}, key=len, reverse=True):
        if org in path:
            return org
    return None


async def guard(request: Request, call_next):
    """Decide who is asking, keep it for the request (current()), refuse the anonymous, and audit every change."""
    acc = await run_in_threadpool(resolve, request)  # may ask the database whether a code was revoked
    token = _CURRENT.set(acc)
    try:
        path = request.url.path
        if acc.role == "anon" and not (path in PUBLIC or path.startswith("/ui/static/")):
            api = not (path == "/" or path.startswith(("/ui", "/admin")))
            if request.method == "GET" and not api:
                return RedirectResponse(f"/join?next={quote(path)}", status_code=303)
            if api:
                return JSONResponse({"detail": "sign in: Authorization: Bearer <access code>"}, status_code=401)
            return Response("access code required", status_code=403)
        if path.startswith("/admin") and not acc.is_admin and path != "/admin/login":
            if request.method == "GET":
                return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
            return Response("admin only", status_code=403)
        resp = await call_next(request)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and path not in ("/join", "/login") and guard_on():
            await run_in_threadpool(lambda: audit("request", f"{request.method} {path}", status=resp.status_code,
                                                  org=_org_in(path), acc=acc))
        return resp
    finally:
        _CURRENT.reset(token)
