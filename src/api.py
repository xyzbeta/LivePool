"""FastAPI application: REST API + Web dashboard."""

import asyncio
import hashlib
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from . import ChannelRecord, Stats, StreamStatus
from .auth import (
    AuthRedirectMiddleware,
    COOKIE_NAME,
    create_access_token,
    create_temp_token,
    ensure_default_admin,
    get_current_user,
    get_jwt_secret,
    get_logo_token,
    get_token_expire_hours,
    hash_password,
    optional_user,
    register_user,
    require_admin,
    validate_invite_code,
    verify_backup_code,
    verify_password,
    verify_temp_token,
)
from .collector import _migrate_sources_from_config
from .config import PROJECT_ROOT, get_web_config, get_generator_config
from .filter import resolution_score
from .generator import load_state, _render_m3u8
from .scheduler import create_scheduler, run_pipeline
from .store import get_sources_store, get_users_store

logger = logging.getLogger(__name__)


def _generate_qr_svg(uri: str) -> str:
    """Generate a clean QR code SVG for inline HTML embedding (SvgPathImage, no XML decl)."""
    import re as _re
    import qrcode
    import qrcode.image.svg
    import io
    qr = qrcode.QRCode(border=2)
    qr.add_data(uri)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    # Strip XML declaration for inline HTML embedding
    if svg.startswith("<?xml"):
        svg = svg.split("?>", 1)[-1].strip()
    # Force display size to 220px (viewBox handles aspect ratio)
    svg = _re.sub(r'width="[^"]*"', 'width="220px"', svg)
    svg = _re.sub(r'height="[^"]*"', 'height="220px"', svg)
    return svg


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

web_cfg = get_web_config()
_docs_enabled = web_cfg.get("api_docs", True)
app = FastAPI(
    title="LivePool",
    description="IPTV stream collector, validator & m3u8 generator",
    version="1.0.0",
    docs_url=None,  # Use custom admin-guarded route instead
    redoc_url=None,
    openapi_url=None,  # Use custom admin-guarded route instead
)

# CORS — wildcard origin + credentials is technically a spec violation
# (the browser ignores Access-Control-Allow-Credentials when origin is "*").
# For the built-in dashboard (same-origin) this is fine; for cross-origin
# API clients, set web.cors_origins in config.yaml to an explicit list.
if web_cfg.get("cors", True):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Auth redirect middleware (401 → /login for page requests)
app.add_middleware(AuthRedirectMiddleware)

# Static files & templates
static_dir = PROJECT_ROOT / "static"
templates_dir = PROJECT_ROOT / "src" / "templates"

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))


def _render(name: str, context: dict) -> HTMLResponse:
    tmpl = jinja_env.get_template(name)
    return HTMLResponse(tmpl.render(context))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def api_health():
    return {"status": "ok", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# EPG endpoint
# ---------------------------------------------------------------------------


@app.get("/api/epg.xml", response_class=PlainTextResponse)
async def api_epg():
    """Serve EPG (XMLTV) data from configured source."""
    from .epg import get_epg_async

    content = await get_epg_async()
    if not content:
        raise HTTPException(status_code=404, detail="EPG not configured or unavailable")

    return PlainTextResponse(
        content=content,
        media_type="application/xml; charset=utf-8",
    )


@app.get("/api/subscribe/{token}/epg", response_class=PlainTextResponse)
@app.get("/api/subscribe/{token}/epg.xml", response_class=PlainTextResponse)
async def api_subscribe_epg(token: str):
    """Token-protected EPG endpoint. Validates subscription token like m3u8 URLs."""
    _validate_subscription_token(token)
    from .epg import get_epg_async

    content = await get_epg_async()
    if not content:
        raise HTTPException(status_code=404, detail="EPG not configured or unavailable")

    return PlainTextResponse(
        content=content,
        media_type="application/xml; charset=utf-8",
    )


@app.get("/tv/{token}/epg", response_class=PlainTextResponse)
@app.get("/tv/{token}/epg.xml", response_class=PlainTextResponse)
async def tv_subscribe_epg(token: str):
    """Short URL alias for token-protected EPG."""
    return await api_subscribe_epg(token)


# ---------------------------------------------------------------------------
# API docs — admin only
# ---------------------------------------------------------------------------


@app.get("/api/docs", include_in_schema=False)
async def api_docs_swagger(admin: dict = Depends(require_admin)):
    """Swagger UI — admin only."""
    if not _docs_enabled:
        raise HTTPException(status_code=404, detail="API docs disabled")
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="LivePool API Docs",
        swagger_ui_parameters={
            "defaultModelsExpandDepth": -1,
            "displayRequestDuration": True,
        },
    )


@app.get("/openapi.json", include_in_schema=False)
async def api_openapi_schema(admin: dict = Depends(require_admin)):
    """OpenAPI schema — admin only."""
    if not _docs_enabled:
        raise HTTPException(status_code=404, detail="API docs disabled")
    return app.openapi()


@app.get("/api/backup")
async def api_backup(admin: dict = Depends(require_admin)):
    """Export all data as JSON for backup."""
    store = get_users_store()
    users = store.all()
    for u in users:
        u.pop("password_hash", None)
        u.pop("totp_secret", None)
        u.pop("totp_backup_codes", None)
    srcs = get_sources_store().all()
    chs = [c.to_dict() for c in load_state()]
    return {"users": users, "sources": srcs, "channels": chs}


@app.on_event("startup")
async def startup():
    global _scheduler
    ensure_default_admin()
    _migrate_sources_from_config()

    # Start scheduler
    _scheduler = create_scheduler()
    _scheduler.start()
    logger.info("Scheduler started")

    # Run pipeline once on startup (async, don't block)
    asyncio.create_task(_initial_pipeline_run())


async def _initial_pipeline_run():
    """Run one pipeline on startup to populate data."""
    try:
        await run_pipeline()
    except Exception as e:
        logger.error(f"Initial pipeline run failed: {e}")


# ---------------------------------------------------------------------------
# In-memory task tracking
# ---------------------------------------------------------------------------

_tasks: Dict[str, dict] = {}
_scheduler = None  # APScheduler instance, set on startup



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_channels_cache: Optional[List[ChannelRecord]] = None
_cache_time: float = 0.0
CACHE_TTL = 60  # seconds
_RE_GROUP_TITLE = re.compile(r'group-title="([^"]*)"')
LOGO_CACHE_DIR = PROJECT_ROOT / "data" / "logos"


def _get_channels() -> List[ChannelRecord]:
    global _channels_cache, _cache_time
    now = time.monotonic()
    if _channels_cache is not None and (now - _cache_time) < CACHE_TTL:
        return _channels_cache
    _channels_cache = load_state()
    _cache_time = now
    return _channels_cache


async def _get_stats() -> Stats:
    import re as _re
    from .store import _get_db

    cfg = get_generator_config()
    channels = _get_channels()
    stats = Stats()

    # ── alive count + group distribution → from m3u8 file (the actual
    # ── deduped output that users receive, so dashboard = subscription URL)
    m3u8_path = PROJECT_ROOT / cfg.get("output_dir", "data") / cfg.get("main_file", "live.m3u8")
    if m3u8_path.exists():
        content = m3u8_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("#EXTINF:"):
                stats.alive += 1
                m = _re.search(r'group-title="([^"]*)"', line)
                if m:
                    gname = m.group(1)
                    stats.groups[gname] = stats.groups.get(gname, 0) + 1

    # ── dead / timeout / error / audio → from DB (not in m3u8)
    for ch in channels:
        if ch.status == StreamStatus.DEAD:
            stats.dead += 1
        elif ch.status == StreamStatus.TIMEOUT:
            stats.timeout += 1
        elif ch.status == StreamStatus.ERROR:
            stats.error += 1
        elif ch.status == StreamStatus.AUDIO:
            stats.audio += 1

    # total = DB 全量（非求和），确保趋势比较口径一致
    stats.total = len(channels)

    timestamps = [ch.last_check for ch in channels if ch.last_check]
    if timestamps:
        stats.last_check = max(timestamps)

    # Load previous stats from SQLite for trend comparison
    try:
        db = await _get_db()
        from .store import _ensure_tables
        await _ensure_tables(db, "stats")
        try:
            cursor = await db.execute("SELECT * FROM stats_history ORDER BY id DESC LIMIT 1")
            row = await cursor.fetchone()
            if row:
                stats.check_duration_sec = dict(row).get("duration_sec", 0)
        finally:
            await db.close()
    except Exception:
        pass

    # Source health
    src_store = get_sources_store()
    all_srcs = src_store.all()
    stats.sources_count = len(all_srcs)
    return stats


def _get_cached_logo_url(ch: ChannelRecord) -> str:
    """Return the local cached logo URL for an alive channel, or empty string."""
    if not ch.tvg_logo or ch.status != StreamStatus.ALIVE:
        return ""
    url = ch.tvg_logo
    # Compute filename the same way as generator.cache_logos
    path = url.rsplit("?", 1)[0].rsplit("#", 1)[0]
    _, dot_ext = path.rsplit(".", 1) if "." in path.rsplit("/", 1)[-1] else ("", "")
    ext = f".{dot_ext.lower()}" if dot_ext.lower() in ("png","jpg","jpeg","gif","svg","webp") else ".png"
    h = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:12]
    fname = f"{h}{ext}"
    if (LOGO_CACHE_DIR / fname).exists():
        return f"/api/logo/{get_logo_token()}/{fname}"
    return ""


def _filter_m3u8_https_cors(
    content: str,
    https_only: bool = False,
    cors_only: bool = False,
    cors_urls: Optional[set] = None,
) -> str:
    """Remove EXTINF+URL pairs that don't match https/cors constraints.

    Args:
        content: Raw m3u8 text.
        https_only: Keep only HTTPS URLs.
        cors_only: Keep only CORS-compatible URLs.
        cors_urls: Pre-computed set of CORS-compatible URLs. If None and cors_only
                   is True, loads from DB (slower, prefer pre-computing).

    Returns:
        Filtered m3u8 text (EXTINF+URL pairs removed atomically).
    """
    if not https_only and not cors_only:
        return content

    if cors_only and cors_urls is None:
        cors_urls = {ch.url for ch in load_state() if ch.has_cors}

    lines = content.splitlines(keepends=True)
    filtered = []
    pending_extinf: Optional[str] = None
    for line in lines:
        is_extinf = line.startswith("#EXTINF:")
        is_url = not is_extinf and (line.startswith("http://") or line.startswith("https://"))

        if is_extinf:
            pending_extinf = line
            continue
        elif pending_extinf is not None and is_url:
            drop = False
            if https_only and line.startswith("http://"):
                drop = True
            if cors_only and line.strip() not in (cors_urls or set()):
                drop = True
            if not drop:
                filtered.append(pending_extinf)
                filtered.append(line)
            pending_extinf = None
        else:
            if pending_extinf is not None:
                filtered.append(pending_extinf)
                pending_extinf = None
            filtered.append(line)
    if pending_extinf is not None:
        filtered.append(pending_extinf)
    return "".join(filtered)


def _serve_favorites_m3u8(
    user: dict,
    https_only: bool = False,
    cors_only: bool = False,
    base_url: str = "",
) -> PlainTextResponse:
    """Dynamically build an m3u8 containing only the user's favorited channels."""
    fav_ids = set(user.get("favorites", []))
    if not fav_ids:
        return PlainTextResponse(
            content="#EXTM3U\n# No favorited channels\n\n",
            media_type="text/plain; charset=utf-8",
        )

    channels = load_state()
    fav_channels = [ch for ch in channels if ch.id in fav_ids and ch.status == StreamStatus.ALIVE]

    if not fav_channels:
        return PlainTextResponse(
            content="#EXTM3U\n# No alive favorited channels\n\n",
            media_type="text/plain; charset=utf-8",
        )

    # Group and render
    from .classifier import get_group_order
    from collections import OrderedDict
    groups: dict = {}
    for ch in fav_channels:
        groups.setdefault(ch.group, []).append(ch)

    # Apply ordering
    group_order = get_group_order()
    ordered = OrderedDict()
    for g in group_order:
        if g in groups:
            ordered[g] = groups.pop(g)
    for g in sorted(groups.keys()):
        ordered[g] = groups[g]

    # Build logo cache mapping for favorited channels (absolute URLs for external players)
    logo_cache = {}
    for ch in fav_channels:
        local = _get_cached_logo_url(ch)
        if local:
            orig = ch.tvg_logo or ""
            if orig:
                logo_cache[orig] = f"{base_url}{local}" if base_url else local
    content = _render_m3u8(ordered, fav_channels, logo_cache=logo_cache or None)

    # Apply user group filter
    content = _filter_m3u8_by_groups(content, user)

    # Apply https / cors filters
    cors_urls = {ch.url for ch in channels if ch.has_cors} if cors_only else None
    content = _filter_m3u8_https_cors(content, https_only, cors_only, cors_urls=cors_urls)

    # Rewrite EPG url-tvg to token-protected endpoint
    if user and user.get("subscription_token"):
        content = _rewrite_epg_url(content, user["subscription_token"], base_url)

    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
    )


# ===========================================================================
# AUTH ENDPOINTS
# ===========================================================================


@app.post("/api/auth/login")
async def api_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember_me: bool = Form(False),
):
    """Login: returns JWT + sets HttpOnly cookie. Supports 2FA verification and forced setup."""
    store = get_users_store()
    user = store.find(lambda u: u.get("username") == username)
    if not user or not verify_password(password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.get("is_active", False):
        raise HTTPException(status_code=403, detail="Account is disabled")

    # If force_2fa is on but not yet configured, force setup during login
    from jose import jwt as _jwt
    if user.get("force_2fa", False) and not user.get("totp_enabled", False):
        temp_token = create_temp_token(user["id"])
        return JSONResponse({
            "2fa_setup_required": True,
            "temp_token": temp_token,
            "remember_me": remember_me,
        })

    # If 2FA is enabled, return a temporary pre-auth token instead of the real JWT
    if user.get("totp_enabled", False):
        # Embed remember_me in the temp_token payload for the 2FA step
        _temp_payload = {
            "sub": user["id"],
            "type": "pre_auth",
            "remember_me": remember_me,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            "iat": datetime.now(timezone.utc),
        }
        temp_token = _jwt.encode(_temp_payload, get_jwt_secret(), algorithm="HS256")
        return JSONResponse({
            "2fa_required": True,
            "temp_token": temp_token,
        })

    return _issue_login_token(user, request, remember_me=remember_me)


def _issue_login_token(user: dict, request: Request, remember_me: bool = False) -> JSONResponse:
    """Issue a real JWT and set session cookie."""
    from datetime import timedelta

    if remember_me:
        expire = datetime.now(timezone.utc) + timedelta(days=30)
        max_age = 2592000  # 30 days
    else:
        expire = datetime.now(timezone.utc) + timedelta(hours=get_token_expire_hours())
        max_age = 86400  # 24 hours

    from jose import jwt
    payload = {
        "sub": user["id"],
        "role": user.get("role", "user"),
        "type": "access",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm="HS256")

    is_html = request.headers.get("accept", "").startswith("text/html")

    if is_html:
        resp = RedirectResponse(url="/", status_code=302)
    else:
        resp = JSONResponse({"access_token": token, "token_type": "bearer"})

    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=max_age,
        samesite="lax",
    )
    return resp


# ---------------------------------------------------------------------------
# 2FA (TOTP) second-factor verification
# ---------------------------------------------------------------------------


@app.post("/api/auth/login/2fa")
async def api_login_2fa(
    request: Request,
    temp_token: str = Form(...),
    totp_code: str = Form(...),
):
    """Second step of login when 2FA is enabled. Verifies TOTP code and issues real JWT."""
    payload = verify_temp_token(temp_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired temporary token. Please login again.")

    user_id = payload.get("sub")
    store = get_users_store()
    user = store.get(user_id)
    if not user or not user.get("is_active", False):
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Verify TOTP code
    secret = user.get("totp_secret", "")
    if not secret:
        raise HTTPException(status_code=400, detail="2FA not configured. Please login again.")

    import pyotp
    remember_me = payload.get("remember_me", False)
    totp = pyotp.TOTP(secret)
    if totp.verify(totp_code, valid_window=1):
        return _issue_login_token(user, request, remember_me=remember_me)

    # Try backup codes
    backup_codes = user.get("totp_backup_codes", [])
    used_idx = None
    for idx, code_hash in enumerate(backup_codes):
        if verify_backup_code(totp_code, code_hash):
            used_idx = idx
            break

    if used_idx is None:
        raise HTTPException(status_code=401, detail="Invalid 2FA code")

    # Remove used backup code
    new_codes = list(backup_codes)
    new_codes.pop(used_idx)
    store.update(user_id, {"totp_backup_codes": new_codes})
    return _issue_login_token(user, request)


@app.post("/api/auth/login/2fa/setup")
async def api_login_2fa_setup(request: Request):
    """Generate TOTP secret + QR code for forced 2FA setup during login (uses temp_token)."""
    body = await request.json()
    temp_token = body.get("temp_token", "")
    payload = verify_temp_token(temp_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired temporary token")

    store = get_users_store()
    user = store.get(payload["sub"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    import pyotp

    secret = pyotp.random_base32()
    issuer = "LivePool"
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name=issuer)

    return {"secret": secret, "uri": uri, "qr_svg": _generate_qr_svg(uri)}


@app.post("/api/auth/login/2fa/complete")
async def api_login_2fa_complete(request: Request):
    """Complete forced 2FA setup: verify code, save to user, issue JWT, return backup codes."""
    body = await request.json()
    temp_token = body.get("temp_token", "")
    secret = body.get("secret", "")
    code = body.get("code", "")

    payload = verify_temp_token(temp_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired temporary token")

    import pyotp
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid verification code")

    # Generate 8 backup codes
    backup_plain = []
    backup_hashes = []
    for _ in range(8):
        c = secrets.token_hex(4).upper()
        backup_plain.append(c)
        backup_hashes.append(hash_password(c))

    # Enable 2FA
    store = get_users_store()
    store.update(payload["sub"], {
        "totp_secret": secret,
        "totp_enabled": True,
        "totp_backup_codes": backup_hashes,
    })

    # Re-read user for login token
    user = store.get(payload["sub"])
    token = create_access_token(user["id"], user["role"])
    resp = JSONResponse({
        "access_token": token,
        "token_type": "bearer",
        "backup_codes": backup_plain,
    })
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=86400,
        samesite="lax",
    )
    return resp


@app.get("/api/auth/2fa/status")
async def api_2fa_status(user: dict = Depends(get_current_user)):
    """Get 2FA status for the current user."""
    store = get_users_store()
    full_user = store.get(user["id"])
    return {
        "enabled": bool(full_user.get("totp_enabled", False)),
    }


@app.post("/api/auth/2fa/setup")
async def api_2fa_setup(user: dict = Depends(get_current_user)):
    """Generate a new TOTP secret and return provisioning info. Not saved until /verify."""
    import pyotp

    secret = pyotp.random_base32()
    issuer = "LivePool"
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user["username"], issuer_name=issuer
    )

    return {"secret": secret, "uri": uri, "qr_svg": _generate_qr_svg(uri)}


@app.post("/api/auth/2fa/verify")
async def api_2fa_verify(request: Request, user: dict = Depends(get_current_user)):
    """Verify a TOTP code to confirm 2FA setup, then enable it."""
    import pyotp

    body = await request.json()
    secret = body.get("secret", "")
    code = body.get("code", "")

    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid verification code")

    # Generate 8 backup codes
    backup_plain = []
    backup_hashes = []
    for _ in range(8):
        c = secrets.token_hex(4).upper()
        backup_plain.append(c)
        backup_hashes.append(hash_password(c))

    # Enable 2FA
    store = get_users_store()
    store.update(user["id"], {
        "totp_secret": secret,
        "totp_enabled": True,
        "totp_backup_codes": backup_hashes,
    })

    return {
        "ok": True,
        "backup_codes": backup_plain,
    }


@app.post("/api/auth/2fa/disable")
async def api_2fa_disable(request: Request, user: dict = Depends(get_current_user)):
    """Disable 2FA. Requires current password."""
    body = await request.json()
    password = body.get("password", "")

    store = get_users_store()
    full_user = store.get(user["id"])
    if not verify_password(password, full_user.get("password_hash", "")):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    store.update(user["id"], {
        "totp_secret": "",
        "totp_enabled": False,
        "totp_backup_codes": [],
    })
    return {"ok": True}


@app.get("/api/auth/me")
async def api_me(user: dict = Depends(get_current_user)):
    """Return current user info."""
    return user


@app.put("/api/auth/me")
async def api_update_me(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Update own password or username. Password change requires current password."""
    body = await request.json()
    store = get_users_store()
    patch = {}
    if "username" in body and body["username"]:
        u = body["username"]
        if len(u) < 2 or len(u) > 32:
            raise HTTPException(status_code=400, detail="用户名长度需在 2-32 个字符之间")
        import re as _re
        if not _re.match(r'^[\w一-鿿㐀-䶿\-_@.]+$', u):
            raise HTTPException(status_code=400, detail="用户名包含非法字符")
        patch["username"] = u
    if "password" in body and body["password"]:
        current = body.get("current_password", "")
        # Re-read user to get password_hash (get_current_user strips it)
        full_user = store.get(user["id"])
        if not current or not verify_password(current, full_user.get("password_hash", "")):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        patch["password_hash"] = hash_password(body["password"])
    if patch:
        store.update(user["id"], patch)
    return {"ok": True}


@app.post("/api/auth/me/subscription-token")
async def api_regenerate_own_token(user: dict = Depends(get_current_user)):
    """Regenerate own subscription token (invalidates old one)."""
    token = secrets.token_urlsafe(24)
    store = get_users_store()
    store.update(user["id"], {
        "subscription_token": token,
        "subscription_enabled": True,
    })
    logger.info(f"User '{user['username']}' regenerated subscription token")
    return {"subscription_token": token}


@app.get("/api/auth/logout")
async def api_logout():
    """Clear session cookie."""
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ===========================================================================
# REGISTRATION (public)
# ===========================================================================


@app.post("/api/auth/register")
async def api_register(request: Request, username: str = Form(...), password: str = Form(...), invite_code: str = Form(...)):
    """Register a new user with an invitation code."""
    user = register_user(username, password, invite_code)
    token = create_access_token(user["id"], user["role"])
    is_html = request.headers.get("accept", "").startswith("text/html")
    if is_html:
        resp = RedirectResponse(url="/", status_code=302)
    else:
        resp = JSONResponse({"access_token": token, "token_type": "bearer", "user": user})
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=86400,
        samesite="lax",
    )
    return resp


@app.get("/api/auth/register/check-code")
async def api_check_invite_code(code: str = Query(...)):
    """Check if an invitation code is valid (for frontend real-time validation)."""
    try:
        invite = validate_invite_code(code)
        remaining = invite["max_uses"] - invite["used_count"]
        return {"valid": True, "remaining": remaining}
    except HTTPException as e:
        return {"valid": False, "detail": e.detail}
    except Exception:
        return {"valid": False, "detail": "邀请码无效"}


# ===========================================================================
# INVITE CODES (admin only)
# ===========================================================================


@app.get("/api/invite-codes")
async def api_invite_codes(admin: dict = Depends(require_admin)):
    """List all invitation codes."""
    from .store import get_invite_codes_store
    store = get_invite_codes_store()
    codes = store.all()
    return {"codes": codes}


@app.post("/api/invite-codes")
async def api_create_invite_codes(request: Request, admin: dict = Depends(require_admin)):
    """Generate one or more invitation codes (each usable once)."""
    body = await request.json()
    count = body.get("count", 1)
    expires_at = body.get("expires_at", "")

    from .store import get_invite_codes_store
    store = get_invite_codes_store()
    created = []
    for _ in range(count):
        code = secrets.token_urlsafe(12)
        item = {
            "code": code,
            "created_by": admin["username"],
            "max_uses": 1,
            "used_count": 0,
            "used_by": [],
            "is_active": True,
            "expires_at": expires_at,
        }
        item_id = store.add(item)
        created.append({**item, "id": item_id})
    logger.info(f"Admin '{admin['username']}' created {count} invite codes")
    return {"codes": created}


@app.put("/api/invite-codes/{code_id}")
async def api_update_invite_code(code_id: str, request: Request, admin: dict = Depends(require_admin)):
    """Update an invitation code (enable/disable, change max_uses)."""
    from .store import get_invite_codes_store
    store = get_invite_codes_store()
    existing = store.get(code_id)
    if not existing:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    body = await request.json()
    patch = {}
    for f in ("is_active", "max_uses", "expires_at"):
        if f in body:
            patch[f] = body[f]
    if patch:
        store.update(code_id, patch)
    return {"ok": True}


@app.delete("/api/invite-codes/{code_id}")
async def api_delete_invite_code(code_id: str, admin: dict = Depends(require_admin)):
    """Delete an invitation code."""
    from .store import get_invite_codes_store
    store = get_invite_codes_store()
    if not store.get(code_id):
        raise HTTPException(status_code=404, detail="邀请码不存在")
    store.delete(code_id)
    logger.info(f"Admin '{admin['username']}' deleted invite code {code_id}")
    return {"ok": True}


# ===========================================================================
# USER MANAGEMENT (admin only)
# ===========================================================================


@app.get("/api/users")
async def api_users(user: dict = Depends(get_current_user)):
    """List users. Admin sees all; regular users see only themselves."""
    store = get_users_store()
    users = store.all()
    for u in users:
        u.pop("password_hash", None)
        u.pop("totp_secret", None)
        u.pop("totp_backup_codes", None)
    if user.get("role") != "admin":
        users = [u for u in users if u.get("id") == user["id"]]
    return {"users": users}


@app.post("/api/users")
async def api_create_user(request: Request, admin: dict = Depends(require_admin)):
    """Create a new user."""
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    role = body.get("role", "user")
    subscribed_groups = body.get("subscribed_groups", "*")

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    if len(username) < 2 or len(username) > 32:
        raise HTTPException(status_code=400, detail="用户名长度需在 2-32 个字符之间")
    import re as _re
    if not _re.match(r'^[\w一-鿿㐀-䶿\-_@.]+$', username):
        raise HTTPException(status_code=400, detail="用户名包含非法字符")
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role must be admin or user")

    store = get_users_store()
    if store.find(lambda u: u.get("username") == username):
        raise HTTPException(status_code=409, detail="Username already exists")

    user = {
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "is_active": True,
        "subscription_token": "",
        "subscription_enabled": False,
        "subscribed_groups": subscribed_groups,
        "force_2fa": body.get("force_2fa", False),
    }
    user_id = store.add(user)
    logger.info(f"User '{username}' created by admin '{admin['username']}'")
    return {"id": user_id, "username": username, "role": role}


@app.put("/api/users/{user_id}")
async def api_update_user(user_id: str, request: Request, admin: dict = Depends(require_admin)):
    """Update user fields (username, password, role, is_active, subscription_enabled)."""
    store = get_users_store()
    existing = store.get(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    body = await request.json()

    patch = {}
    for field in ("username", "role", "is_active", "subscription_enabled", "subscribed_groups", "force_2fa"):
        if field in body:
            patch[field] = body[field]
    if "password" in body and body["password"]:
        patch["password_hash"] = hash_password(body["password"])

    # Auto-manage subscription token:
    #   enable  → auto-generate token if missing
    #   disable → clear token so old URL stops working
    if "subscription_enabled" in body:
        existing_token = existing.get("subscription_token", "")
        if body["subscription_enabled"] and not existing_token:
            patch["subscription_token"] = secrets.token_urlsafe(24)
            logger.info(f"Subscription token auto-generated for '{existing['username']}'")
        elif not body["subscription_enabled"]:
            patch["subscription_token"] = ""
            logger.info(f"Subscription token cleared for '{existing['username']}'")

    if patch:
        store.update(user_id, patch)
        logger.info(f"User '{existing['username']}' updated by admin '{admin['username']}'")
    return {"ok": True}


@app.delete("/api/users/{user_id}")
async def api_delete_user(user_id: str, admin: dict = Depends(require_admin)):
    """Delete a user."""
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    store = get_users_store()
    user = store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    store.delete(user_id)
    logger.info(f"User '{user['username']}' deleted by admin '{admin['username']}'")
    return {"ok": True}


@app.post("/api/users/{user_id}/subscription-token")
async def api_generate_subscription_token(user_id: str, admin: dict = Depends(require_admin)):
    """Generate or reset a user's subscription token."""
    store = get_users_store()
    user = store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    token = secrets.token_urlsafe(24)
    store.update(user_id, {
        "subscription_token": token,
        "subscription_enabled": True,
    })
    logger.info(f"Subscription token generated for '{user['username']}' by admin")
    return {"subscription_token": token}


@app.post("/api/users/{user_id}/reset-2fa")
async def api_reset_user_2fa(user_id: str, admin: dict = Depends(require_admin)):
    """Admin: reset another user's 2FA settings (e.g. lost authenticator)."""
    store = get_users_store()
    user = store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    store.update(user_id, {
        "totp_secret": "",
        "totp_enabled": False,
        "totp_backup_codes": [],
    })
    logger.info(f"2FA reset for '{user['username']}' by admin '{admin['username']}'")
    return {"ok": True}


# ===========================================================================
# SUBSCRIPTION ENDPOINTS (public, token-based)
# ===========================================================================


def _validate_subscription_token(token: str) -> dict:
    """Validate subscription token, return user dict or raise 403."""
    store = get_users_store()
    user = store.find(
        lambda u: u.get("subscription_token") == token
        and u.get("is_active", False)
        and u.get("subscription_enabled", False)
    )
    if not user:
        raise HTTPException(status_code=403, detail="Invalid or disabled subscription token")
    # Track subscription pull
    try:
        store.update(user["id"], {
            "pull_count": user.get("pull_count", 0) + 1,
            "last_pull_at": datetime.now().isoformat(),
        })
    except Exception:
        pass
    return user


def _filter_m3u8_by_groups(content: str, user: dict) -> str:
    """Filter m3u8 content to only include channels from user's subscribed groups."""
    groups_str = user.get("subscribed_groups")
    if groups_str is None or groups_str.strip() == "*":
        return content
    allowed = set(g.strip() for g in groups_str.split(",") if g.strip())

    lines = content.splitlines(keepends=True)
    filtered = []
    skip_active = False  # True = current block (EXTINF + following lines) is filtered out
    for line in lines:
        if line.startswith("#EXTINF:"):
            m = _RE_GROUP_TITLE.search(line)
            group_name = m.group(1) if m else ""
            skip_active = group_name not in allowed
            if skip_active:
                continue  # skip this EXTINF line

        if skip_active:
            # Skip all lines (URLs, blanks, comments) belonging to a filtered-out channel
            # until the next EXTINF resets skip_active above.
            continue

        filtered.append(line)
    return "".join(filtered)


def _rewrite_epg_url(content: str, token: str, base_url: str = "") -> str:
    """Replace raw EPG source URL in m3u8 header with token-protected LivePool URL."""
    from .epg import get_epg_source_url
    old_url = get_epg_source_url()
    if not old_url:
        return content
    new_url = f"{base_url}/tv/{token}/epg.xml"
    return content.replace(f'url-tvg="{old_url}"', f'url-tvg="{new_url}"')


def _serve_m3u8_file(path: Path, https_only: bool = False, cors_only: bool = False, user: dict = None, base_url: str = "") -> PlainTextResponse:
    """Read m3u8 file, optionally filter to HTTPS-only, CORS-safe, or user group streams."""
    if not path.exists():
        raise HTTPException(status_code=404, detail="No m3u8 generated yet")
    content = path.read_text(encoding="utf-8")

    # Apply user group filter
    if user:
        content = _filter_m3u8_by_groups(content, user)

    # Apply https / cors filters
    cors_urls = {ch.url for ch in load_state() if ch.has_cors} if cors_only else None
    content = _filter_m3u8_https_cors(content, https_only, cors_only, cors_urls=cors_urls)

    # Rewrite EPG url-tvg to token-protected endpoint
    if user and user.get("subscription_token"):
        content = _rewrite_epg_url(content, user["subscription_token"], base_url)

    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
    )


@app.get("/api/subscribe/{token}", response_class=PlainTextResponse)
async def api_subscribe(request: Request, token: str, https: int = 0, cors: int = 0):
    """Subscription endpoint. Add ?cors=1 to filter to CORS-compatible streams only."""
    clean_token = token.removesuffix(".m3u8").removesuffix(".m3u")
    user = _validate_subscription_token(clean_token)
    cfg = get_generator_config()
    path = PROJECT_ROOT / cfg.get("output_dir", "data") / cfg.get("main_file", "live.m3u8")
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    return _serve_m3u8_file(path, https_only=bool(https), cors_only=bool(cors), user=user, base_url=base_url)


@app.get("/api/subscribe/{token}/favorites", response_class=PlainTextResponse)
@app.get("/api/subscribe/{token}/favorites.m3u8", response_class=PlainTextResponse)
async def api_subscribe_favorites(request: Request, token: str, https: int = 0, cors: int = 0):
    """Subscription endpoint for user's favorited channels. Add ?cors=1 for CORS-only."""
    clean_token = token.removesuffix(".m3u8").removesuffix(".m3u")
    user = _validate_subscription_token(clean_token)
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    return _serve_favorites_m3u8(user, base_url=base_url, https_only=bool(https), cors_only=bool(cors))



# ---------------------------------------------------------------------------
# Short subscription URLs for IPTV players that reject API-looking paths
# ---------------------------------------------------------------------------


@app.get("/tv/{token}", response_class=PlainTextResponse)
async def tv_subscribe(request: Request, token: str, https: int = 0, cors: int = 0):
    """Short subscription URL. Add ?cors=1 for CORS-compatible streams only."""
    clean_token = token.removesuffix(".m3u8").removesuffix(".m3u")
    user = _validate_subscription_token(clean_token)
    cfg = get_generator_config()
    path = PROJECT_ROOT / cfg.get("output_dir", "data") / cfg.get("main_file", "live.m3u8")
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    return _serve_m3u8_file(path, https_only=bool(https), cors_only=bool(cors), user=user, base_url=base_url)


@app.get("/tv/{token}/favorites", response_class=PlainTextResponse)
@app.get("/tv/{token}/favorites.m3u8", response_class=PlainTextResponse)
async def tv_subscribe_favorites(request: Request, token: str, https: int = 0, cors: int = 0):
    """Short subscription URL for favorited channels. Add ?cors=1 for CORS-only."""
    clean_token = token.removesuffix(".m3u8").removesuffix(".m3u")
    user = _validate_subscription_token(clean_token)
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    return _serve_favorites_m3u8(user, base_url=base_url, https_only=bool(https), cors_only=bool(cors))




# ---------------------------------------------------------------------------
# Logo image serving (token-protected static cache)
# ---------------------------------------------------------------------------


@app.get("/api/logo/{token}/{filename}")
async def api_logo_serve(token: str, filename: str):
    """Serve a cached logo image.

    Token must match the deployment's logo token (derived from JWT secret
    or set via ``LOGO_TOKEN`` env var).  This prevents hotlinking of cached
    logos by external sites.
    """
    if token != get_logo_token():
        raise HTTPException(status_code=403, detail="Invalid logo token")

    # Path-traversal guard
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = LOGO_CACHE_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Logo not found")

    return FileResponse(file_path)


# ===========================================================================
# M3U8 DOWNLOAD (authenticated users)
# ===========================================================================


@app.get("/api/m3u8", response_class=PlainTextResponse)
async def api_download_m3u8(user: dict = Depends(get_current_user)):
    """Download the generated m3u8 file, filtered by subscribed groups (requires login)."""
    cfg = get_generator_config()
    path = PROJECT_ROOT / cfg.get("output_dir", "data") / cfg.get("main_file", "live.m3u8")
    if not path.exists():
        raise HTTPException(status_code=404, detail="M3U8 file not generated yet")
    content = path.read_text(encoding="utf-8")
    content = _filter_m3u8_by_groups(content, user)
    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=live.m3u8"},
    )




# ===========================================================================
# CHANNELS
# ===========================================================================


@app.get("/api/channels")
async def api_channels(
    group: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: Optional[str] = Query(None, description="Sort field: name, latency, resolution, last_check"),
    order: Optional[str] = Query("asc", description="asc or desc"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    user: dict = Depends(get_current_user),
):
    channels = _get_channels()
    if group:
        channels = [c for c in channels if c.group == group]
    if status:
        channels = [c for c in channels if c.status.value == status]
    if search:
        sl = search.lower()
        channels = [c for c in channels if sl in c.name.lower() or sl in c.url.lower()]

    # Sort
    sort_map = {
        "name": lambda c: c.name,
        "latency": lambda c: c.latency_ms or 99999,
        "resolution": lambda c: c.resolution or "",
        "last_check": lambda c: c.last_check or "",
    }
    if sort and sort in sort_map:
        reverse = order == "desc"
        if sort == "resolution":
            channels.sort(key=lambda c: resolution_score(c.resolution or ""), reverse=reverse)
        else:
            channels.sort(key=sort_map[sort], reverse=reverse)

    total = len(channels)
    start = (page - 1) * size
    page_data = channels[start:start + size]
    return {
        "total": total, "page": page, "size": size,
        "pages": max(1, (total + size - 1) // size) if total > 0 else 1,
        "data": [ch.to_dict() for ch in page_data],
    }


@app.get("/api/channels/{channel_id}")
async def api_channel_detail(channel_id: str, user: dict = Depends(get_current_user)):
    channels = _get_channels()
    for ch in channels:
        if ch.id == channel_id:
            return ch.to_dict()
    raise HTTPException(status_code=404, detail="Channel not found")


# ===========================================================================
# FAVORITES
# ===========================================================================


@app.get("/api/channels/{channel_id}/favorite")
async def api_favorite_status(channel_id: str, user: dict = Depends(get_current_user)):
    """Check if a channel is favorited by the current user."""
    favs = user.get("favorites", [])
    return {"favorited": channel_id in favs}


@app.post("/api/channels/{channel_id}/favorite")
async def api_favorite_toggle(channel_id: str, user: dict = Depends(get_current_user)):
    """Toggle favorite status for a channel."""
    store = get_users_store()
    favs = list(user.get("favorites", []))
    if channel_id in favs:
        favs.remove(channel_id)
        favorited = False
    else:
        favs.append(channel_id)
        favorited = True
    store.update(user["id"], {"favorites": favs})
    return {"favorited": favorited}


# ===========================================================================
# STATS & GROUPS
# ===========================================================================


@app.get("/api/stats")
async def api_stats(user: dict = Depends(get_current_user)):
    from .store import _get_db
    stats = await _get_stats()

    # Trend from previous snapshot
    trend = {}
    try:
        db = await _get_db()
        from .store import _ensure_tables
        await _ensure_tables(db, "stats")
        try:
            cursor = await db.execute("SELECT * FROM stats_history ORDER BY id DESC LIMIT 1 OFFSET 1")
            row = await cursor.fetchone()
            if row:
                prev = dict(row)
                trend = {
                    "prev_alive": prev.get("alive", stats.alive),
                    "prev_dead": prev.get("dead", stats.dead),
                    "prev_total": prev.get("total", stats.total),
                }
        finally:
            await db.close()
    except Exception:
        pass

    # Source health
    src_store = get_sources_store()
    sources = src_store.all()
    src_ok = sum(1 for s in sources if s.get("fetch_error", "") == "")
    src_err = len(sources) - src_ok

    return {
        "total": stats.total, "alive": stats.alive, "dead": stats.dead,
        "timeout": stats.timeout, "error": stats.error, "audio": stats.audio,
        "groups": stats.groups,
        "last_check": stats.last_check, "check_duration_sec": stats.check_duration_sec,
        "sources_count": stats.sources_count,
        "trend": trend,
        "source_health": {"ok": src_ok, "error": src_err},
    }





# ===========================================================================
# SOURCES CRUD (admin only)
# ===========================================================================


def _get_sources_with_stats() -> list:
    """Merge stored sources with channel counts. Falls back to config.yaml if store is empty."""
    store = get_sources_store()
    sources = store.all()

    # Fallback: if store is empty, pull from config.yaml
    if not sources:
        from .config import get_enabled_crawlers
        for cfg in get_enabled_crawlers():
            sources.append({
                "id": "",
                "name": cfg["name"],
                "type": cfg.get("type", "raw_m3u"),
                "urls": cfg.get("urls", []),
                "enabled": cfg.get("enabled", True),
            })

    channels = _get_channels()
    url_counts: Dict[str, int] = {}
    url_alive: Dict[str, int] = {}
    for ch in channels:
        src = ch.source or ""
        url_counts[src] = url_counts.get(src, 0) + 1
        if ch.status == StreamStatus.ALIVE:
            url_alive[src] = url_alive.get(src, 0) + 1
    for s in sources:
        total = 0
        alive = 0
        for url in s.get("urls", []):
            total += url_counts.get(url, 0)
            alive += url_alive.get(url, 0)
        s["channels_total"] = total
        s["channels_alive"] = alive
    return sources


@app.get("/api/sources")
async def api_sources(admin: dict = Depends(require_admin)):
    return {"sources": _get_sources_with_stats()}


@app.get("/api/local-seeds")
async def api_local_seeds(admin: dict = Depends(require_admin)):
    """List local seed files with channel stats."""
    import json
    import os as _os

    seeds_dir = PROJECT_ROOT / "data" / "sources"
    if not seeds_dir.exists():
        return {"seeds": []}

    # Read enable/disable state from SQLite
    from .store import _get_db, _ensure_tables
    db = await _get_db()
    try:
        await _ensure_tables(db, "local_seeds")
        cursor = await db.execute("SELECT filename, enabled FROM local_seeds")
        state_rows = await cursor.fetchall()
        state = {row["filename"]: bool(row["enabled"]) for row in state_rows}
    finally:
        await db.close()

    # Load channels to count per-source
    channels = _get_channels()
    url_counts: Dict[str, int] = {}
    url_alive: Dict[str, int] = {}
    for ch in channels:
        src = ch.source or ""
        url_counts[src] = url_counts.get(src, 0) + 1
        if ch.status == StreamStatus.ALIVE:
            url_alive[src] = url_alive.get(src, 0) + 1

    # Get last pipeline run time from stats_history
    _pipeline_time = ""
    try:
        db = await _get_db()
        try:
            cursor = await db.execute("SELECT timestamp FROM stats_history ORDER BY id DESC LIMIT 1")
            row = await cursor.fetchone()
            if row:
                _pipeline_time = row["timestamp"][:16].replace("T", " ") if row["timestamp"] else ""
        finally:
            await db.close()
    except Exception:
        pass

    seeds = []
    for f in sorted(seeds_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in (".m3u", ".m3u8", ".txt"):
            continue
        fname = f.name
        enabled = state.get(fname, True)

        # Count channels that came from this file
        file_path_str = str(f)
        total = url_counts.get(file_path_str, 0)
        alive = url_alive.get(file_path_str, 0)

        seeds.append({
            "name": fname,
            "size": f.stat().st_size,
            "enabled": enabled,
            "last_fetch_at": _pipeline_time,
            "channels_total": total,
            "channels_alive": alive,
        })

    return {"seeds": seeds}


@app.put("/api/local-seeds/{filename}")
async def api_toggle_local_seed(filename: str, request: Request, admin: dict = Depends(require_admin)):
    """Enable or disable a local seed file."""
    import json
    from urllib.parse import unquote

    filename = unquote(filename)
    seeds_dir = PROJECT_ROOT / "data" / "sources"
    file_path = seeds_dir / filename

    # Security: prevent path traversal
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    body = await request.json()
    enabled = body.get("enabled", True)

    from .store import _get_db, _ensure_tables
    db = await _get_db()
    try:
        await _ensure_tables(db, "local_seeds")
        await db.execute(
            "INSERT OR REPLACE INTO local_seeds (filename, enabled) VALUES (?, ?)",
            (filename, 1 if enabled else 0),
        )
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


@app.get("/api/sources/{source_id}")
async def api_source_detail(source_id: str, admin: dict = Depends(require_admin)):
    store = get_sources_store()
    s = store.get(source_id)
    if not s:
        raise HTTPException(status_code=404, detail="Source not found")
    return s


@app.post("/api/sources")
async def api_create_source(request: Request, admin: dict = Depends(require_admin)):
    body = await request.json()
    name = body.get("name", "").strip()
    stype = body.get("type", "raw_m3u").strip()
    urls = body.get("urls", [])
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if stype not in ("raw_m3u", "github_m3u"):
        raise HTTPException(status_code=400, detail="type must be raw_m3u or github_m3u")
    store = get_sources_store()
    sid = store.add({
        "name": name, "type": stype, "urls": urls, "enabled": True,
    })
    logger.info(f"Source '{name}' created by '{admin['username']}'")
    return {"id": sid, "ok": True}


@app.put("/api/sources/{source_id}")
async def api_update_source(source_id: str, request: Request, admin: dict = Depends(require_admin)):
    store = get_sources_store()
    if not store.get(source_id):
        raise HTTPException(status_code=404, detail="Source not found")
    body = await request.json()
    patch = {}
    for f in ("name", "type", "urls", "enabled"):
        if f in body:
            patch[f] = body[f]
    if patch:
        store.update(source_id, patch)
    return {"ok": True}


@app.delete("/api/sources/{source_id}")
async def api_delete_source(source_id: str, admin: dict = Depends(require_admin)):
    store = get_sources_store()
    if not store.get(source_id):
        raise HTTPException(status_code=404, detail="Source not found")
    store.delete(source_id)
    return {"ok": True}


@app.post("/api/sources/{source_id}/fetch")
async def api_fetch_source(source_id: str, admin: dict = Depends(require_admin)):
    """Trigger fetch from a single source."""
    task_id = str(uuid.uuid4())[:8]
    _tasks[task_id] = {"id": task_id, "status": "running", "progress": "Fetching single source...", "result": None}
    asyncio.create_task(_run_check_task(task_id))
    return {"task_id": task_id}


@app.post("/api/sources/test")
async def api_test_source(request: Request, admin: dict = Depends(require_admin)):
    """Test a source URL before saving."""
    import aiohttp
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    try:
        timeout = aiohttp.ClientTimeout(total=8, connect=3)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            start = time.monotonic()
            async with s.head(url, allow_redirects=True) as resp:
                elapsed = round((time.monotonic() - start) * 1000, 1)
                return {"ok": True, "http_code": resp.status, "latency_ms": elapsed, "content_type": resp.content_type or ""}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ===========================================================================
# SCHEDULE (admin only)
# ===========================================================================


@app.get("/api/schedule")
async def api_get_schedule(admin: dict = Depends(require_admin)):
    """Return current schedule config."""
    from .config import load_config
    cfg = load_config()
    sc = cfg.get("scheduler", {})
    return {
        "cron": sc.get("cron", "0 */6 * * *"),
        "timezone": sc.get("timezone", "Asia/Shanghai"),
    }


@app.put("/api/schedule")
async def api_update_schedule(request: Request, admin: dict = Depends(require_admin)):
    """Update schedule cron expression and persist."""
    body = await request.json()
    import yaml
    from .config import CONFIG_PATH, load_config

    cfg = load_config()
    if "cron" in body:
        cfg.setdefault("scheduler", {})["cron"] = body["cron"]
    if "timezone" in body:
        cfg.setdefault("scheduler", {})["timezone"] = body["timezone"]

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False)

    from .config import reload_config
    reload_config()

    # 同步更新正在运行的调度器
    global _scheduler
    if _scheduler and _scheduler.running:
        from apscheduler.triggers.cron import CronTrigger
        new_cron = cfg.get("scheduler", {}).get("cron", "0 */6 * * *")
        new_tz = cfg.get("scheduler", {}).get("timezone", "Asia/Shanghai")
        _scheduler.reschedule_job(
            "pipeline_run",
            trigger=CronTrigger.from_crontab(new_cron, timezone=new_tz),
        )

    return {"ok": True}


@app.get("/api/collector/proxy")
async def api_get_collector_proxy(admin: dict = Depends(require_admin)):
    """Get collector proxy config."""
    from .config import load_config
    cfg = load_config()
    return {"proxy": cfg.get("collector", {}).get("proxy", "")}


@app.put("/api/collector/proxy")
async def api_update_collector_proxy(request: Request, admin: dict = Depends(require_admin)):
    """Update collector proxy config."""
    body = await request.json()
    import yaml
    from .config import CONFIG_PATH, load_config, reload_config
    cfg = load_config()
    cfg.setdefault("collector", {})["proxy"] = body.get("proxy") or None
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False)
    reload_config()
    return {"ok": True}


@app.get("/api/epg/config")
async def api_get_epg_config(admin: dict = Depends(require_admin)):
    """Get current EPG config."""
    from .config import load_config
    cfg = load_config()
    return {"source": cfg.get("epg", {}).get("source", "")}


@app.put("/api/epg/config")
async def api_update_epg_config(request: Request, admin: dict = Depends(require_admin)):
    """Update EPG source URL and persist."""
    body = await request.json()
    import yaml
    from .config import CONFIG_PATH, load_config, reload_config

    cfg = load_config()
    source = body.get("source", "")
    cfg.setdefault("epg", {})["source"] = source

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False)

    reload_config()

    # Clear EPG cache so next request fetches fresh
    from .epg import _epg_cache, _epg_cache_time
    _epg_cache = None
    _epg_cache_time = 0

    logger.info(f"EPG source updated: {'set' if source else 'cleared'}")
    return {"ok": True}


# ===========================================================================
# SCHEDULER CONTROL (admin only)
# ===========================================================================


@app.get("/api/scheduler/status")
async def api_scheduler_status(user: dict = Depends(get_current_user)):
    """Return scheduler running state and next run time."""
    global _scheduler
    if _scheduler is None:
        return {"running": False, "next_run": None}
    job = _scheduler.get_job("pipeline_run")
    return {
        "running": _scheduler.running,
        "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


@app.post("/api/scheduler/start")
async def api_scheduler_start(admin: dict = Depends(require_admin)):
    global _scheduler
    if _scheduler and not _scheduler.running:
        _scheduler.start()
        return {"ok": True, "running": True}
    return {"ok": True, "running": _scheduler.running if _scheduler else False}


@app.post("/api/scheduler/stop")
async def api_scheduler_stop(admin: dict = Depends(require_admin)):
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.pause()
        return {"ok": True, "running": False}
    return {"ok": True, "running": False}


# ===========================================================================
# MANUAL CHECK
# ===========================================================================


@app.post("/api/check")
async def api_trigger_check(admin: dict = Depends(require_admin)):
    task_id = str(uuid.uuid4())[:8]
    _tasks[task_id] = {
        "id": task_id, "status": "running",
        "started_at": datetime.now().isoformat(),
        "progress": "Starting...", "result": None,
    }
    asyncio.create_task(_run_check_task(task_id))
    return {"task_id": task_id, "status": "running"}


async def _run_check_task(task_id: str):
    """Execute pipeline with stage-by-stage progress reporting."""
    def on_progress(step: str, detail: str):
        _tasks[task_id]["step"] = step
        _tasks[task_id]["progress"] = detail
        steps = _tasks[task_id].setdefault("steps_done", [])
        if step not in steps:
            steps.append(step)

    try:
        started = time.monotonic()
        _tasks[task_id]["progress"] = "开始检测..."
        stats = await run_pipeline(progress_callback=on_progress)
        elapsed = time.monotonic() - started
        _tasks[task_id].update({
            "status": "completed", "step": "done",
            "progress": f"检测完成: {stats.alive}/{stats.total} 存活, 耗时 {elapsed:.1f}s",
            "result": {"total": stats.total, "alive": stats.alive, "dead": stats.dead, "elapsed_sec": round(elapsed, 1)},
            "completed_at": datetime.now().isoformat(),
        })
    except Exception as e:
        logger.exception(f"Check task {task_id} failed")
        _tasks[task_id].update({
            "status": "failed", "step": "error", "progress": str(e),
            "completed_at": datetime.now().isoformat(),
        })


@app.get("/api/tasks")
async def api_task_list(user: dict = Depends(get_current_user)):
    """Return any currently running task (for auto-reconnect after page refresh)."""
    for tid, t in _tasks.items():
        if t.get("status") == "running":
            return t
    return {"status": "idle"}


@app.get("/api/tasks/{task_id}")
async def api_task_status(task_id: str, user: dict = Depends(get_current_user)):
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return _tasks[task_id]


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    return _render("login.html", {"request": request, "title": "登录"})


@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request, user: dict = Depends(get_current_user)):
    stats = await _get_stats()
    fav_ids = set(user.get("favorites", []))
    favorite_count = sum(1 for ch in _get_channels() if ch.id in fav_ids and ch.status == StreamStatus.ALIVE)
    return _render("dashboard.html", {
        "request": request, "stats": stats, "title": "仪表盘", "user": user,
        "favorite_count": favorite_count,
    })


@app.get("/channels", response_class=HTMLResponse)
async def page_channels(
    request: Request,
    group: Optional[str] = None, status: Optional[str] = None,
    search: Optional[str] = None, sort: Optional[str] = None, order: Optional[str] = "asc",
    favorite: Optional[str] = None,
    page: int = 1,
    user: dict = Depends(get_current_user),
):
    channels = _get_channels()
    stats = await _get_stats()
    if group: channels = [c for c in channels if c.group == group]
    if status: channels = [c for c in channels if c.status.value == status]
    if search:
        sl = search.lower()
        channels = [c for c in channels if sl in c.name.lower() or sl in c.url.lower()]

    # Filter by favorite status
    fav_ids = set(user.get("favorites", []))
    if favorite == "yes":
        channels = [c for c in channels if c.id in fav_ids]
    elif favorite == "no":
        channels = [c for c in channels if c.id not in fav_ids]

    # Sort
    sort_map = {
        "name": lambda c: c.name,
        "latency": lambda c: c.latency_ms or 99999,
        "resolution": lambda c: c.resolution or "",
        "last_check": lambda c: c.last_check or "",
    }
    if sort and sort in sort_map:
        if sort == "resolution":
            channels.sort(key=lambda c: resolution_score(c.resolution or ""), reverse=order == "desc")
        else:
            channels.sort(key=sort_map[sort], reverse=order == "desc")

    total = len(channels)
    size = 50
    page_data = channels[(page - 1) * size: page * size]
    # Attach display attributes
    for ch in page_data:
        ch._logo_url = _get_cached_logo_url(ch)
        ch._is_favorited = ch.id in fav_ids
    pages = max(1, (total + size - 1) // size) if total > 0 else 1
    return _render("channels.html", {
        "request": request, "channels": page_data, "total": total,
        "page": page, "pages": pages, "group": group or "",
        "status": status or "", "search": search or "",
        "favorite": favorite or "",
        "sort": sort or "", "order": order or "asc",
        "groups": list(stats.groups.keys()), "title": "频道列表", "user": user,
    })


@app.get("/sources", response_class=HTMLResponse)
async def page_sources(request: Request, admin: dict = Depends(require_admin)):
    return _render("sources.html", {
        "request": request, "title": "采集源管理", "user": admin,
    })


@app.get("/users", response_class=HTMLResponse)
async def page_users(request: Request, user: dict = Depends(get_current_user)):
    return _render("users.html", {
        "request": request, "title": "用户管理", "user": user,
    })
