import logging
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config.settings import settings
from app.services.logging_config import redact

logger = logging.getLogger(__name__)


engine = create_async_engine(settings.database_url, echo=settings.debug, pool_size=10, max_overflow=20)
logger.info("DB: engine created url=%s echo=%s", redact(settings.database_url), settings.debug)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
logger.info("DB: async_session_factory created")


class Base(DeclarativeBase):
    pass


async def get_session():
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    logger.info("DB init_db: starting...")
    try:
        async with engine.begin() as conn:
            def _check_and_create(sync_conn):
                from sqlalchemy import inspect
                inspector = inspect(sync_conn)
                if "alembic_version" in inspector.get_table_names():
                    logger.info("DB: alembic_version table found — schema managed by Alembic, skipping create_all")
                    return
                logger.warning("DB: no alembic_version table — running create_all for dev convenience. "
                               "Run 'alembic stamp head' once to switch to Alembic management.")
                Base.metadata.create_all(sync_conn)
            await conn.run_sync(_check_and_create)
        logger.info("DB init_db: completed OK")
    except Exception as e:
        logger.warning("DB init_db: failed: %s", e)
        raise
