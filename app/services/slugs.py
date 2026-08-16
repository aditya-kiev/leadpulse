"""Shared slug helpers used by onboarding, registration, and any code that
provisions tenant rows (organizations.slug is unique)."""
import re

from sqlalchemy.ext.asyncio import AsyncSession


def slugify(name: str) -> str:
    """Lowercase, alphanumeric + hyphen slug for tenant subdomains."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "client"


async def unique_slug(session: AsyncSession, base: str) -> str:
    """Return ``base`` or a ``-2/-3/...`` suffix when the slug is taken.

    Runs a uniqueness check against the real DB using the provided session.
    """
    from app.database.crud import get_organization_by_slug

    candidate = base
    counter = 2
    while await get_organization_by_slug(session, candidate) is not None:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate
