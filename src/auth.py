"""Authentication: JWT tokens, password hashing, FastAPI dependency injection."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from .config import load_config
from .store import get_users_store

logger = logging.getLogger(__name__)

# Known weak defaults that should trigger warnings
_DEFAULT_JWT_SECRETS = {"change-me", "livepool-secret-change-me-in-production", "tv-m3u8-secret-change-me-in-production"}
_DEFAULT_ADMIN_PASSWORD = "admin"

# ---------------------------------------------------------------------------
# Crypto setup
# ---------------------------------------------------------------------------

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
COOKIE_NAME = "livepool_session"


def _get_auth_config() -> dict:
    return load_config().get("auth", {})


def _get_jwt_secret() -> str:
    env_secret = os.environ.get("JWT_SECRET", "")
    if env_secret:
        _warn_if_default("JWT_SECRET env var", env_secret)
        return env_secret
    cfg_secret = _get_auth_config().get("jwt_secret", "change-me")
    _warn_if_default("auth.jwt_secret in config.yaml", cfg_secret)
    return cfg_secret


def _warn_if_default(source: str, value: str) -> None:
    if value in _DEFAULT_JWT_SECRETS:
        logger.warning(
            f"⚠️  {source} is a known default value!"
            f" Set $JWT_SECRET to a random string in production."
            f" Current: '{value}'"
        )


def _get_token_expire_hours() -> int:
    return _get_auth_config().get("token_expire_hours", 24)


def get_logo_token() -> str:
    """Return a global token used to protect cached logo images from hotlinking.

    Derived from the JWT secret so it's deterministic per deployment.
    Can be overridden via the ``LOGO_TOKEN`` environment variable.
    """
    env_token = os.environ.get("LOGO_TOKEN", "")
    if env_token:
        return env_token
    import hashlib
    secret = _get_jwt_secret()
    return hashlib.md5(secret.encode(), usedforsecurity=False).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Password utilities
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# Backup codes (2FA recovery codes)
# ---------------------------------------------------------------------------


def hash_backup_code(code: str) -> str:
    """Hash a backup code with bcrypt for storage."""
    return bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode()


def verify_backup_code(code: str, hashed: str) -> bool:
    """Verify a backup code against its stored bcrypt hash."""
    return bcrypt.checkpw(code.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# JWT utilities
# ---------------------------------------------------------------------------


def create_access_token(user_id: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=_get_token_expire_hours())
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm="HS256")


def create_temp_token(user_id: str) -> str:
    """Create a short-lived (5 min) token for the 2FA verification step."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=5)
    payload = {
        "sub": user_id,
        "type": "pre_auth",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm="HS256")


def verify_temp_token(token: str) -> Optional[dict]:
    """Verify a pre-auth temporary token. Returns payload or None."""
    payload = verify_token(token)
    if payload and payload.get("type") == "pre_auth":
        return payload
    return None


def verify_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=["HS256"])
    except JWTError as e:
        logger.debug(f"JWT verification failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get(COOKIE_NAME)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


async def get_current_user(request: Request) -> dict:
    """Require valid JWT. Raises 401 if not authenticated."""
    token = _extract_token(request)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reject non-access tokens (e.g. pre_auth temp tokens from 2FA flow)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    store = get_users_store()
    user = store.get(user_id)
    if not user or not user.get("is_active", False):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    user.pop("password_hash", None)
    user.pop("totp_secret", None)
    user.pop("totp_backup_codes", None)
    return user


async def require_admin(request: Request) -> dict:
    """Require admin role."""
    user = await get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


async def optional_user(request: Request) -> Optional[dict]:
    """Extract user if token present, don't require it."""
    token = _extract_token(request)
    if not token:
        return None
    payload = verify_token(token)
    if not payload:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    store = get_users_store()
    user = store.get(user_id)
    if user:
        user.pop("password_hash", None)
        user.pop("totp_secret", None)
        user.pop("totp_backup_codes", None)
    return user


# ---------------------------------------------------------------------------
# Auth middleware: redirect 401 → /login for HTML page requests
# ---------------------------------------------------------------------------


class AuthRedirectMiddleware:
    """Middleware that redirects 401 responses to /login for page (HTML) requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Check if this is a page request (browser) vs API request
        headers = dict(scope.get("headers", []))
        accept = headers.get(b"accept", b"").decode()
        is_page = "text/html" in accept

        # Skip login page itself to avoid redirect loop
        path = scope.get("path", "/")

        if is_page and path != "/login" and path != "/api/auth/login":
            # Wrap send to intercept 401
            async def wrapped_send(message):
                if message["type"] == "http.response.start" and message["status"] == 401:
                    # Redirect to login instead
                    message["status"] = 302
                    headers_list = [
                        (b"location", b"/login"),
                        (b"content-type", b"text/html"),
                    ]
                    message["headers"] = [
                        h for h in message.get("headers", []) if h[0] != b"www-authenticate"
                    ] + headers_list
                await send(message)

            await self.app(scope, receive, wrapped_send)
        else:
            await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def ensure_default_admin():
    """Create default admin account if no users exist."""
    store = get_users_store()
    if store.count() == 0:
        cfg = _get_auth_config()
        default_password = cfg.get("default_admin_password", "admin")
        if default_password == _DEFAULT_ADMIN_PASSWORD:
            logger.warning(
                f"⚠️  Default admin password is '{_DEFAULT_ADMIN_PASSWORD}'!"
                f" Change it immediately via Web UI or set auth.default_admin_password in config.yaml."
            )
        admin = {
            "id": "admin_001",
            "username": "admin",
            "password_hash": hash_password(default_password),
            "role": "admin",
            "is_active": True,
            "subscription_token": secrets.token_urlsafe(24),
            "subscription_enabled": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store.add(admin)
        logger.info(f"Default admin created: admin / {default_password}")
        logger.info(f"Admin subscription token: {admin['subscription_token']}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def validate_invite_code(code: str) -> dict:
    """Validate an invitation code. Returns the invite code record or raises HTTPException."""
    from .store import get_invite_codes_store

    store = get_invite_codes_store()
    invite = store.find(lambda c: c.get("code") == code)
    if not invite:
        raise HTTPException(status_code=400, detail="邀请码无效")
    if not invite.get("is_active", True):
        raise HTTPException(status_code=400, detail="邀请码已失效")
    if invite.get("expires_at"):
        try:
            exp = datetime.fromisoformat(invite["expires_at"])
            if datetime.now() > exp:
                raise HTTPException(status_code=400, detail="邀请码已过期")
        except (ValueError, TypeError):
            pass
    if invite.get("used_count", 0) >= invite.get("max_uses", 1):
        raise HTTPException(status_code=400, detail="邀请码已用完")

    return invite


def register_user(username: str, password: str, invite_code: str) -> dict:
    """Register a new user with an invitation code.

    Args:
        username: Desired username.
        password: Plaintext password.
        invite_code: Valid invitation code string.

    Returns:
        The created user dict (with sensitive fields stripped).

    Raises:
        HTTPException on validation failure.
    """
    from .store import get_invite_codes_store, get_users_store

    # Validate username (alphanumeric + CJK + common chars only)
    import re as _re
    if not username or len(username) < 2 or len(username) > 32:
        raise HTTPException(status_code=400, detail="用户名长度需在 2-32 个字符之间")
    if not _re.match(r'^[\w一-鿿㐀-䶿\-_@.]+$', username):
        raise HTTPException(status_code=400, detail="用户名包含非法字符")

    # Validate invite code
    invite = validate_invite_code(invite_code)

    # Check username uniqueness
    user_store = get_users_store()
    existing = user_store.find(lambda u: u.get("username") == username)
    if existing:
        raise HTTPException(status_code=409, detail="用户名已存在")

    # Create user
    user_id = secrets.token_hex(12)
    new_user = {
        "id": user_id,
        "username": username,
        "password_hash": hash_password(password),
        "role": "user",
        "is_active": True,
        "subscription_token": secrets.token_urlsafe(24),
        "subscription_enabled": True,
        "subscribed_groups": "*",
        "favorites": [],
        "created_at": datetime.now().isoformat(),
        "pull_count": 0,
        "last_pull_at": "",
    }
    user_store.add(new_user)

    # Update invite code usage — record the username so admin can see who used it
    used_by = list(invite.get("used_by", []))
    used_by.append(username)
    invite_store = get_invite_codes_store()
    invite_store.update(invite["id"], {
        "used_count": invite.get("used_count", 0) + 1,
        "used_by": used_by,
    })

    logger.info(f"User '{username}' registered via invite code")

    # Strip sensitive fields for return
    return {
        "id": new_user["id"],
        "username": new_user["username"],
        "role": new_user["role"],
        "subscription_token": new_user["subscription_token"],
    }


# Public accessors for internal functions (used by api.py)
def get_jwt_secret() -> str:
    return _get_jwt_secret()


def get_token_expire_hours() -> int:
    return _get_token_expire_hours()
