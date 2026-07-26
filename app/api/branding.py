import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import get_current_user, require_role
from app.database.crud import get_organization_by_id, update_organization
from app.database.session import async_session_factory
from app.services.branding import get_branding, BrandingConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org", tags=["branding"])


class BrandingUpdate(BaseModel):
    brand_name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None


class BrandingOut(BaseModel):
    brand_name: str
    logo_url: str
    primary_color: str
    primary_light: str
    primary_dark: str
    primary_soft: str
    text_on_primary: str
    custom_domain: str
    has_branding: bool


@router.get("/branding", response_model=BrandingOut)
async def get_branding_endpoint(
    request: Request,
    _auth: tuple = Depends(require_role("org_admin", "super_admin")),
):
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="Tenant context required")

    async with async_session_factory() as session:
        org = await get_organization_by_id(session, tenant_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")

        b = get_branding(
            brand_name=org.brand_name,
            logo_url=org.logo_url,
            primary_color=org.primary_color,
            custom_domain=org.custom_domain,
        )
        return BrandingOut(
            brand_name=b.brand_name,
            logo_url=b.logo_url,
            primary_color=b.primary_color,
            primary_light=b.primary_light,
            primary_dark=b.primary_dark,
            primary_soft=b.primary_soft,
            text_on_primary=b.text_on_primary,
            custom_domain=b.custom_domain,
            has_branding=b.has_branding,
        )


@router.put("/branding", response_model=BrandingOut)
async def update_branding_endpoint(
    body: BrandingUpdate,
    request: Request,
    _auth: tuple = Depends(require_role("org_admin", "super_admin")),
):
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="Tenant context required")

    updates = {}
    if body.brand_name is not None:
        updates["brand_name"] = body.brand_name
    if body.logo_url is not None:
        updates["logo_url"] = body.logo_url
    if body.primary_color is not None:
        if not body.primary_color.startswith("#") or len(body.primary_color) != 7:
            raise HTTPException(status_code=422, detail="primary_color must be a hex color like #4F46E5")
        updates["primary_color"] = body.primary_color

    async with async_session_factory() as session:
        org = await get_organization_by_id(session, tenant_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")

        await update_organization(session, tenant_id, **updates)
        await session.commit()

        org = await get_organization_by_id(session, tenant_id)
        b = get_branding(
            brand_name=org.brand_name,
            logo_url=org.logo_url,
            primary_color=org.primary_color,
            custom_domain=org.custom_domain,
        )
        return BrandingOut(
            brand_name=b.brand_name,
            logo_url=b.logo_url,
            primary_color=b.primary_color,
            primary_light=b.primary_light,
            primary_dark=b.primary_dark,
            primary_soft=b.primary_soft,
            text_on_primary=b.text_on_primary,
            custom_domain=b.custom_domain,
            has_branding=b.has_branding,
        )
