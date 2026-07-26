import logging
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config.settings import settings

logger = logging.getLogger(__name__)


engine = create_async_engine(settings.database_url, echo=settings.debug, pool_size=10, max_overflow=20)
logger.info("DB: engine created url=%s echo=%s", settings.database_url, settings.debug)

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
            await conn.run_sync(Base.metadata.create_all)
        logger.info("DB init_db: completed OK")
    except Exception as e:
        logger.warning("DB init_db: failed: %s", e)
        raise
