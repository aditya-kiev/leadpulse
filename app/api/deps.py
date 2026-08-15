import logging
import time
from collections import defaultdict, deque
from uuid import UUID

from fastapi import Header, HTTPException, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.gemini import demo_rpm_limit
from app.config.settings import settings
from app.database.crud import get_organization_by_widget_key
from app.database.session import get_session
from app.services.auth import decode_token, get_user_by_id
from app.services.demo_tokens import verify_demo_token
from app.services.redis import RedisSlidingWindowRateLimiter

logger = logging.getLogger(__name__)

# Per-IP in-memory fallback used when Redis is unavailable (per-process only).
_webhook_inmem: dict[str, deque[float]] = defaultdict(deque)


def reset_webhook_rate_limits() -> None:
    """Clear in-memory rate-limit state (test helper)."""
    _webhook_inmem.clear()


def _inmem_webhook_allowed(ip: str, limit: int, window: float = 60.0) -> bool:
    now = time.monotonic()
    dq = _webhook_inmem[ip]
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


async def rate_limit_webhook(request: Request) -> None:
    """Basic per-IP rate limiting for public webhook endpoints.

    Uses the shared Redis sliding-window limiter keyed by client IP. Falls
    back to a per-process in-memory sliding window when Redis is down so the
    webhook is never completely unguarded. Returns 429 with Retry-After when
    the limit (webhook_rpm_limit) is exceeded.
    """
    rpm = settings.webhook_rpm_limit
    if rpm <= 0:
        return
    ip = request.client.host if request.client else "unknown"

    # Rate limiter is deliberately keyed per client IP. This is fine for the
    # current embedded widget/browser use case (one IP per end-user), but it
    # should be revisited (e.g. keyed by widget_key/tenant_id) before selling
    # server-to-server API access that sits behind shared infrastructure,
    # where many tenants can share a single egress IP.
    limiter = RedisSlidingWindowRateLimiter(key_prefix=f"ratelimit:webhook:{ip}")
    wait = await limiter.acquire(rpm, window=60)
    if wait == -1:
        if not _inmem_webhook_allowed(ip, rpm):
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down.",
                headers={"Retry-After": "60"},
            )
        return
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please slow down.",
            headers={"Retry-After": str(int(wait) + 1)},
        )


async def _extract_session_id(request: Request) -> str | None:
    """Extract session_id from request JSON body or path params."""
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body.get("session_id")
    except Exception:
        pass
    return request.path_params.get("session_id")


async def _verify_demo_token(request: Request, x_demo_token: str) -> bool:
    """Verify a demo token, extracting session_id from body or path."""
    sid = await _extract_session_id(request)
    if sid and verify_demo_token(x_demo_token, sid):
        demo_rpm_limit.set(settings.demo_token_rpm_limit)
        return True
    return False


async def authenticate_request(
    request: Request,
    x_api_key: str | None = Header(None),
    x_demo_token: str | None = Header(None),
    x_widget_key: str | None = Header(None),
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> tuple[UUID | None, str, UUID | None]:
    """
    Authenticate the request via JWT, master API key, widget key, or demo token.
    Returns (user_id, role, organization_id).

    In single-tenant mode (settings.auth_enabled=False), no auth is required
    and a synthetic super_admin identity is returned for backward compat.
    In multi-tenant mode (settings.auth_enabled=True), at least one auth
    method must succeed or a 401 is raised.
    """
    # 0 — Widget key (tenant-bound client widgets). Long-lived per-organization
    # key issued at onboarding. It must scope the request to that org ONLY —
    # never super_admin — and never fall back to unauthenticated/tenant-less.
    if x_widget_key:
        org = await get_organization_by_widget_key(session, x_widget_key)
        if org is None:
            raise HTTPException(status_code=401, detail="Invalid or missing widget key")
        request.state.tenant_id = org.id
        request.state.user_id = None
        request.state.role = "agent"
        return (None, "agent", org.id)

    if not settings.auth_enabled:
        # Legacy single-tenant mode — require API key if configured, allow local dev
        if settings.api_key:
            if x_api_key == settings.api_key:
                request.state.tenant_id = None
                request.state.user_id = None
                request.state.role = "super_admin"
                return (None, "super_admin", None)
            if x_demo_token and await _verify_demo_token(request, x_demo_token):
                request.state.tenant_id = None
                request.state.user_id = None
                request.state.role = "super_admin"
                return (None, "super_admin", None)
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        request.state.tenant_id = None
        request.state.user_id = None
        request.state.role = "super_admin"
        return (None, "super_admin", None)

    # 1 — JWT (dashboard users)
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
        payload = decode_token(token)
        if payload is not None and payload.get("type") == "access":
            user_id_str = payload.get("sub")
            role = payload.get("role", "agent")
            org_id_str = payload.get("org_id")
            if user_id_str:
                user = await get_user_by_id(session, UUID(user_id_str))
                if user and user.is_active:
                    request.state.tenant_id = UUID(org_id_str) if org_id_str else None
                    request.state.user_id = UUID(user_id_str)
                    request.state.role = role
                    return (UUID(user_id_str), role, UUID(org_id_str) if org_id_str else None)

        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2 — Master API key (server-to-server integrations)
    if settings.api_key and x_api_key == settings.api_key:
        request.state.tenant_id = None
        request.state.user_id = None
        request.state.role = "super_admin"
        return (None, "super_admin", None)

    # 3 — Demo token (static HTML demo widgets, limited RPM)
    if x_demo_token and await _verify_demo_token(request, x_demo_token):
        request.state.tenant_id = None
        request.state.user_id = None
        request.state.role = "super_admin"
        return (None, "super_admin", None)

    raise HTTPException(
        status_code=401,
        detail="Authentication required. Provide a JWT (Authorization: Bearer "
               "<token>), API key (X-Api-Key), or demo token (X-Demo-Token).",
    )


async def verify_api_key(
    request: Request,
    x_api_key: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> None:
    """Legacy auth dependency — kept for backward compat during migration.
    Delegates to authenticate_request internally."""
    # When auth_enabled, JWT auth is handled by authenticate_request
    if settings.auth_enabled:
        return
    # Legacy single-tenant mode
    if settings.api_key and x_api_key == settings.api_key:
        return
    if x_demo_token and await _verify_demo_token(request, x_demo_token):
        return
    if not settings.api_key:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def get_current_user(
    request: Request,
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> tuple[UUID | None, str, UUID | None]:
    """
    Resolve the current user from JWT. Returns (user_id, role, organization_id).

    When auth_enabled is False, returns a synthetic super_admin user for
    backward compatibility with single-tenant mode.
    """
    if not settings.auth_enabled:
        request.state.tenant_id = None
        request.state.user_id = None
        request.state.role = "super_admin"
        return (None, "super_admin", None)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[len("Bearer "):]
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Token is not an access token")

    user_id = payload.get("sub")
    role = payload.get("role", "agent")
    org_id = payload.get("org_id")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = await get_user_by_id(session, UUID(user_id))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or disabled")

    request.state.tenant_id = UUID(org_id) if org_id else None
    request.state.user_id = UUID(user_id)
    request.state.role = role

    return (UUID(user_id), role, UUID(org_id) if org_id else None)


async def get_optional_tenant_id(
    request: Request,
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> UUID | None:
    """
    Resolve tenant_id from the current auth context.
    In single-tenant mode, returns None.
    In multi-tenant mode, returns org_id from JWT, or None if API key / demo
    token was used (those grant super_admin scope with no tenant restriction).
    """
    if not settings.auth_enabled:
        return None
    if authorization and authorization.startswith("Bearer "):
        _, _, org_id = await get_current_user(request, authorization, session)
        return org_id
    # API key or demo token path — super_admin scope, no tenant restriction
    return None


def require_role(*roles: str):
    """Dependency factory: requires the current user to have one of the given roles."""
    async def role_checker(current_user: tuple = Depends(get_current_user)) -> tuple:
        _, role, _ = current_user
        if role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires one of these roles: {', '.join(roles)}",
            )
        return current_user
    return role_checker


def get_tenant_id(request: Request) -> UUID | None:
    """Extract tenant_id from request state (set by authenticate_request or get_current_user)."""
    return getattr(request.state, "tenant_id", None)
