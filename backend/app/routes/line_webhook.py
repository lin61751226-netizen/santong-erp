from fastapi import APIRouter, Header, HTTPException, Request
from sqlmodel import Session

from app.core.db import engine
from app.services.line import line_service, process_webhook_event


router = APIRouter(prefix="/api/line", tags=["line"])


@router.post("/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str | None = Header(default=None, alias="x-line-signature"),
):
    body = await request.body()
    if not line_service.verify_signature(body, x_line_signature):
        raise HTTPException(status_code=400, detail="LINE webhook signature 驗證失敗")

    payload = await request.json()
    events = payload.get("events", [])
    with Session(engine) as session:
        for event in events:
            await process_webhook_event(session, event)
    return {"ok": True}

