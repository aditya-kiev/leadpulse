import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.webhook import router as webhook_router
from app.api.conversation import router as conversation_router
from app.api.debug import router as debug_router
from app.api.demo import router as demo_router
from app.api.auth import router as auth_router
from app.config.settings import settings
from app.database.session import init_db
from app.models.schemas import HealthOut

logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Lead Qualification Agent (vertical=%s, business_name=%s)...",
                settings.vertical, settings.business_name)
    if settings.database_url:
        try:
            await init_db()
            logger.info("Database initialized")
        except Exception as e:
            logger.warning("Database unavailable, running without persistence: %s", e)
    yield
    logger.info("Shutting down Lead Qualification Agent...")


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

app.include_router(webhook_router)
app.include_router(conversation_router)
app.include_router(auth_router)
app.include_router(demo_router)
if settings.debug:
    app.include_router(debug_router)


@app.get("/health", response_model=HealthOut)
async def health_check() -> HealthOut:
    return HealthOut(status="ok", version="1.0.0")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.debug)
