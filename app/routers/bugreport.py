import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import BUGREPORT_SERVICE_URL
from app.database import get_db
from app.models.user import User
from app.schemas.bugreport import BugReportCreate, BugReportResponse
from app.services.admin_monitor import record_event


router = APIRouter(prefix="/api/bugreport", tags=["bugreport"])


def _word_count(text: str) -> int:
    return len([word for word in text.strip().split() if word])


@router.post("", response_model=BugReportResponse)
async def create_bugreport(
    payload: BugReportCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> BugReportResponse:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Bug report text is required")
    if _word_count(text) > 130:
        raise HTTPException(status_code=422, detail="Bug report must be 130 words or fewer")

    user = db.get(User, int(user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                BUGREPORT_SERVICE_URL,
                json={"user_login": user.login, "text": text},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Bug report service is unavailable") from exc

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Bug report service rejected the request")

    record_event(
        "bugreport",
        f"Пользователь {user.login} отправил баг-репорт",
        ip=request.client.host if request.client else None,
        path="/api/bugreport",
    )
    return BugReportResponse(ok=True, message="Bug report sent")
