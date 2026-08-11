import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import bcrypt
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.database.models import User, Organization

logger = logging.getLogger(__name__)


def check_jwt_secret_configured() -> None:
    """Raise RuntimeError if AUTH_ENABLED=true but JWT_SECRET_KEY is unset."""
    if settings.auth_enabled and not settings.jwt_secret_key:
        raise RuntimeError(
            "JWT_SECRET_KEY must be set when AUTH_ENABLED=true. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )


check_jwt_secret_configured()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(user_id: UUID, role: str, organization_id: UUID | None = None) -> str:
    expires_delta = timedelta(minutes=settings.jwt_access_token_ttl_minutes)
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": expire,
        "type": "access",
    }
    if organization_id:
        payload["org_id"] = str(organization_id)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: UUID) -> str:
    expires_delta = timedelta(days=settings.jwt_refresh_token_ttl_days)
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": str(user_id),
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return payload
    except JWTError as e:
        logger.warning("Token decode failed: %s", e)
        return None


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    email: str,
    password: str,
    display_name: str,
    role: str = "agent",
    organization_id: UUID | None = None,
) -> User:
    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        role=role,
        organization_id=organization_id,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    local, _, domain = email.partition("@")
    masked_email = f"{local[:2]}***@{domain}" if domain else "***"
    logger.info("Created user: id=%s email=%s role=%s org=%s", user.id, masked_email, role, organization_id)
    return user
