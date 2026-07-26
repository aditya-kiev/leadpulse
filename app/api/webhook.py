import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.agent.graph import run_agent
from app.agent.gemini import GeminiRateLimitError
from app.api.deps import verify_api_key, authenticate_request
from app.models.schemas import (
    MessageIn,
    MessageOut,
    StartConversationIn,
    StartConversationOut,
)
from app.services.memory import memory_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


def _resolve_tenant_id(request: Request) -> uuid.UUID | None:
    return getattr(request.state, "tenant_id", None)


@router.post("/start", response_model=StartConversationOut)
async def start_conversation(
    request: Request,
    payload: StartConversationIn,
    _auth: tuple = Depends(authenticate_request),
) -> StartConversationOut:
    session_id = payload.session_id or str(uuid.uuid4())
    tenant_id = _resolve_tenant_id(request)
    logger.debug("start_conversation session=%s tenant_id=%s", session_id, tenant_id)
    try:
        result = await run_agent(
            session_id, "Hi, I'm interested in your services.", payload.channel, tenant_id=tenant_id
        )

        state_persisted = True
        try:
            await memory_service.save_state(session_id, result, tenant_id=tenant_id)
        except Exception as db_err:
            state_persisted = False
            logger.exception(
                "save_state failed for session %s: %s", session_id, db_err
            )

        last_message = ""
        if result.get("conversation_history"):
            for msg in reversed(result["conversation_history"]):
                if msg.get("role") == "assistant":
                    last_message = msg["content"]
                    break

        return StartConversationOut(
            session_id=session_id,
            message=last_message or "Hello! How can I help you today?",
            lead_status=result.get("lead_status"),
            state_persisted=state_persisted,
        )
    except GeminiRateLimitError as e:
        logger.exception("start_conversation Gemini rate limited")
        raise HTTPException(
            status_code=429,
            detail="The AI service is temporarily rate limited. Please try again in a minute.",
        ) from e
    except Exception as e:
        logger.exception("start_conversation failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "ai_unavailable", "message": "The AI service is temporarily unavailable."},
        ) from e


@router.post("/message", response_model=MessageOut)
async def handle_message(
    request: Request,
    payload: MessageIn,
    _auth: tuple = Depends(authenticate_request),
) -> MessageOut:
    tenant_id = _resolve_tenant_id(request)
    logger.debug("handle_message session=%s tenant_id=%s", payload.session_id, tenant_id)
    try:
        result = await run_agent(
            payload.session_id, payload.message, payload.channel,
            tenant_id=tenant_id,
        )

        state_persisted = True
        try:
            await memory_service.save_state(payload.session_id, result, tenant_id=tenant_id)
        except Exception as db_err:
            state_persisted = False
            logger.exception(
                "save_state failed for session %s: %s",
                payload.session_id, db_err,
            )

        last_message = ""
        if result.get("conversation_history"):
            for msg in reversed(result["conversation_history"]):
                if msg.get("role") == "assistant":
                    last_message = msg["content"]
                    break

        return MessageOut(
            session_id=payload.session_id,
            reply=last_message or "I understand. Let me help you with that.",
            lead_status=result.get("lead_status"),
            booking_confirmed=result.get("booking_confirmed", False),
            meeting_time=result.get("meeting_time"),
            human_escalated=result.get("human_escalated", False),
            next_action=result.get("next_action"),
            state_persisted=state_persisted,
        )
    except GeminiRateLimitError as e:
        logger.exception("handle_message Gemini rate limited")
        raise HTTPException(
            status_code=429,
            detail="The AI service is temporarily rate limited. Please try again in a minute.",
        ) from e
    except Exception as e:
        logger.exception("handle_message failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "ai_unavailable", "message": "The AI service is temporarily unavailable."},
        ) from e
