import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request

from app.api.webhook import router as webhook_router
from app.api.conversation import router as conversation_router
from app.api.debug import router as debug_router
from app.api.demo import router as demo_router
from app.api.auth import router as auth_router
from app.api.analytics import router as analytics_router
from app.api.branding import router as branding_router
from app.config.settings import settings
from app.database.session import init_db
from app.models.schemas import HealthOut
from app.services.logging_config import configure_logging

configure_logging(environment=settings.environment, debug=settings.debug)
logger = logging.getLogger(__name__)


def check_api_key_configured() -> None:
    """Raise RuntimeError if API_KEY is unset — never silently skip auth.

    An empty API_KEY means every webhook request is unauthenticated in
    single-tenant mode (``authenticate_request`` short-circuits). That is
    unacceptable once real client lead data flows through the app.
    """
    if not settings.api_key:
        raise RuntimeError(
            "API_KEY must be set. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )


def check_allowed_origins_production() -> None:
    """Raise RuntimeError if ENV=production and ALLOWED_ORIGINS is wildcard."""
    if settings.environment == "production" and "*" in (settings.allowed_origins or []):
        raise RuntimeError(
            "ALLOWED_ORIGINS must not contain '*' when ENV=production. "
            "List the explicit dashboard origins instead."
        )


check_api_key_configured()
check_allowed_origins_production()

_redis_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis_client
    logger.info("Starting Lead Qualification Agent (vertical=%s, business_name=%s, environment=%s)...",
                settings.vertical, settings.business_name, settings.environment)
    if settings.database_url:
        try:
            await init_db()
            logger.info("Database initialized")
        except Exception as e:
            logger.warning("Database unavailable, running without persistence: %s", e)

    # Initialize Redis on startup
    if settings.redis_url:
        try:
            from app.services.redis import get_redis
            _redis_client = await get_redis()
            if _redis_client:
                logger.info("Redis initialized at startup")
            else:
                logger.info("Redis unavailable (starting without it)")
        except Exception as e:
            logger.warning("Redis initialization failed: %s", e)

    # Initialize Sentry in production
    if settings.environment == "production" and settings.sentry_dsn:
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                environment=settings.environment,
                traces_sample_rate=0.1,
                profiles_sample_rate=0.0,
            )
            logger.info("Sentry initialized for environment=%s", settings.environment)
        except Exception as e:
            logger.warning("Sentry initialization failed: %s", e)

    yield

    logger.info("Shutting down Lead Qualification Agent...")
    if _redis_client:
        try:
            from app.services.redis import close_redis
            await close_redis()
            logger.info("Redis connection closed")
        except Exception:
            pass


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins or [],
    allow_credentials=settings.allowed_origins not in ([], ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    import uuid
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def resolve_tenant_from_domain(request: Request, call_next):
    """Auto-detect tenant from Host header if not already set by auth."""
    if getattr(request.state, "tenant_id", None) is None:
        host = request.headers.get("host")
        if host:
            from app.services.domain import resolve_tenant_from_host
            try:
                tid = await resolve_tenant_from_host(host)
                if tid:
                    request.state.tenant_id = tid
            except Exception:
                pass
    response = await call_next(request)
    return response


app.include_router(webhook_router)
app.include_router(conversation_router)
app.include_router(auth_router)
app.include_router(demo_router)
if settings.debug and settings.environment != "production":
    app.include_router(debug_router)
app.include_router(analytics_router)
app.include_router(branding_router)


@app.get("/health", response_model=HealthOut)
async def health_check() -> HealthOut:
    db_ok = False
    redis_ok = False

    if settings.database_url:
        from sqlalchemy import text
        from app.database.session import async_session_factory
        try:
            async with async_session_factory() as s:
                await s.execute(text("SELECT 1"))
            db_ok = True
        except Exception as e:
            logger.warning("Health check: DB unavailable: %s", e)

    if settings.redis_url:
        try:
            from app.services.redis import is_redis_available
            redis_ok = await is_redis_available()
        except Exception:
            redis_ok = False

    all_ok = (db_ok if settings.database_url else True) and (redis_ok if settings.redis_url else True)
    return HealthOut(
        status="ok" if all_ok else "degraded",
        version="1.0.0",
        database=db_ok,
        redis=redis_ok,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.debug)
