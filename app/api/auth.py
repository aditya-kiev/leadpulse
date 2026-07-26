import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.database.session import get_session
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    create_user,
    decode_token,
    get_user_by_email,
    get_user_by_id,
    hash_password,
    verify_password,
)
from app.api.deps import get_current_user, require_role

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class LoginOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    role: str
    organization_id: str | None = None


class RegisterIn(BaseModel):
    email: str
    password: str
    display_name: str
    organization_name: str | None = None


class RegisterOut(BaseModel):
    user_id: str
    email: str
    role: str
    organization_id: str | None = None


class RefreshIn(BaseModel):
    refresh_token: str


class RefreshOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    organization_id: str | None = None
    is_active: bool


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginIn, session: AsyncSession = Depends(get_session)):
    user = await get_user_by_email(session, payload.email)
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    access_token = create_access_token(
        str(user.id), user.role,
        str(user.organization_id) if user.organization_id else None,
    )
    refresh_token = create_refresh_token(str(user.id))
    return LoginOut(
        access_token=access_token,
        refresh_token=refresh_token,
        user_id=str(user.id),
        role=user.role,
        organization_id=str(user.organization_id) if user.organization_id else None,
    )


@router.post("/register", response_model=RegisterOut)
async def register(payload: RegisterIn, session: AsyncSession = Depends(get_session)):
    existing = await get_user_by_email(session, payload.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = await create_user(
        session,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        role="org_admin",
        organization_id=None,
    )
    return RegisterOut(
        user_id=str(user.id),
        email=user.email,
        role=user.role,
        organization_id=str(user.organization_id) if user.organization_id else None,
    )


@router.post("/refresh", response_model=RefreshOut)
async def refresh(payload: RefreshIn, session: AsyncSession = Depends(get_session)):
    token_data = decode_token(payload.refresh_token)
    if not token_data or token_data.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = await get_user_by_id(session, UUID(token_data["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or disabled")
    access_token = create_access_token(
        str(user.id), user.role,
        str(user.organization_id) if user.organization_id else None,
    )
    return RefreshOut(access_token=access_token)


@router.get("/me", response_model=UserOut)
async def get_me(
    current_user: tuple = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    user_id, role, org_id = current_user
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = await get_user_by_id(session, UUID(str(user_id)))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        organization_id=str(user.organization_id) if user.organization_id else None,
        is_active=user.is_active,
    )
