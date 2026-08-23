import logging
import uuid

from fastapi import APIRouter, Depends, Request

from app.api.deps import rate_limit_demo_token
from app.services.demo_tokens import issue_demo_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/demo", tags=["demo"])


@router.post("/token", dependencies=[Depends(rate_limit_demo_token)])
async def get_demo_token(request: Request):
    session_id = f"demo-{uuid.uuid4().hex[:8]}"
    token, expires_at = issue_demo_token(session_id)
    return {"session_id": session_id, "token": token, "expires_at": expires_at}
