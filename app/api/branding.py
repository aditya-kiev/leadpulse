import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import get_current_user, require_role
from app.database.crud import get_organization_by_id, update_organization
from app.database.session import async_session_factory
from app.services.branding import get_branding, BrandingConfig
from app.services.domain_verify import (
    generate_verification_token,
    verify_domain_txt,
    build_verification_instructions,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org", tags=["branding"])


class BrandingUpdate(BaseModel):
    brand_name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    custom_domain: str | None = None


class BrandingOut(BaseModel):
    brand_name: str
    logo_url: str
    primary_color: str
    primary_light: str
    primary_dark: str
    primary_soft: str
    text_on_primary: str
    custom_domain: str
    custom_domain_status: str = "unverified"
    tls_status: str = "none"
    domain_verification_token: str | None = None
    has_branding: bool
    verification_instructions: str | None = None


def _build_branding_out(org, b: BrandingConfig, verification_instructions: str | None = None) -> BrandingOut:
    return BrandingOut(
        brand_name=b.brand_name,
        logo_url=b.logo_url,
        primary_color=b.primary_color,
        primary_light=b.primary_light,
        primary_dark=b.primary_dark,
        primary_soft=b.primary_soft,
        text_on_primary=b.text_on_primary,
        custom_domain=b.custom_domain,
        custom_domain_status=org.custom_domain_status,
        tls_status=org.tls_status,
        domain_verification_token=org.domain_verification_token if org.custom_domain_status == "pending" else None,
        has_branding=b.has_branding,
        verification_instructions=verification_instructions,
    )


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
        instructions = None
        if org.custom_domain and org.custom_domain_status == "pending" and org.domain_verification_token:
            instructions = build_verification_instructions(org.custom_domain, org.domain_verification_token)
        return _build_branding_out(org, b, instructions)


@router.put("/branding", response_model=BrandingOut)
async def update_branding_endpoint(
    body: BrandingUpdate,
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

        updates = {}
        if body.brand_name is not None:
            updates["brand_name"] = body.brand_name
        if body.logo_url is not None:
            updates["logo_url"] = body.logo_url
        if body.primary_color is not None:
            if not body.primary_color.startswith("#") or len(body.primary_color) != 7:
                raise HTTPException(status_code=422, detail="primary_color must be a hex color like #4F46E5")
            updates["primary_color"] = body.primary_color
        if body.custom_domain is not None:
            updates["custom_domain"] = body.custom_domain.lower().strip()
            if body.custom_domain:
                token = generate_verification_token()
                updates["domain_verification_token"] = token
                updates["custom_domain_status"] = "pending"
                updates["tls_status"] = "none"
            else:
                updates["domain_verification_token"] = None
                updates["custom_domain_status"] = "unverified"
                updates["tls_status"] = "none"

        await update_organization(session, tenant_id, **updates)
        await session.commit()

        org = await get_organization_by_id(session, tenant_id)
        b = get_branding(
            brand_name=org.brand_name,
            logo_url=org.logo_url,
            primary_color=org.primary_color,
            custom_domain=org.custom_domain,
        )
        instructions = None
        if org.custom_domain and org.custom_domain_status == "pending" and org.domain_verification_token:
            instructions = build_verification_instructions(org.custom_domain, org.domain_verification_token)
        return _build_branding_out(org, b, instructions)


@router.post("/branding/verify-domain", response_model=BrandingOut)
async def verify_domain_endpoint(
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
        if not org.custom_domain:
            raise HTTPException(status_code=400, detail="No custom domain configured")
        if org.custom_domain_status == "verified":
            raise HTTPException(status_code=400, detail="Domain already verified")
        if not org.domain_verification_token:
            raise HTTPException(status_code=400, detail="No verification token. Re-set the custom domain to generate one.")

        matched = await verify_domain_txt(org.custom_domain, org.domain_verification_token)
        if not matched:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"DNS verification failed. Ensure a TXT record exists at "
                    f"_leadpulse-verify.{org.custom_domain} with value "
                    f"{org.domain_verification_token}"
                ),
            )

        await update_organization(
            session, tenant_id,
            custom_domain_status="verified",
            tls_status="none",
        )
        await session.commit()

        org = await get_organization_by_id(session, tenant_id)
        b = get_branding(
            brand_name=org.brand_name,
            logo_url=org.logo_url,
            primary_color=org.primary_color,
            custom_domain=org.custom_domain,
        )
        return _build_branding_out(org, b)
