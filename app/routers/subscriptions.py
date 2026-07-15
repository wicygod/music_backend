from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import PUBLIC_BASE_URL
from app.database import get_db
from app.models.user import User
from app.routers.auth import current_user
from app.schemas.subscription import (
    CheckoutPreviewRead,
    CheckoutPreviewRequest,
    MockCheckoutRead,
    SubscriptionPlanRead,
    SubscriptionStatusRead,
)
from app.services.admin_monitor import record_event
from app.services.auth_service import (
    MOCK_CHECKOUT_EXPIRES_SECONDS,
    create_mock_checkout_ticket,
    decode_mock_checkout_ticket,
    require_active_user,
)
from app.services.subscription_service import has_premium_entitlement, subscription_entitlements


router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])

PREMIUM_MONTHLY_PLAN = SubscriptionPlanRead(
    id="premium-monthly",
    name="Premium",
    description="Расширенная настройка звука в Million Music.",
    price_minor=19_900,
    currency="RUB",
    billing_period="month",
    features=[
        "10-полосный эквалайзер",
        "Усиление баса, чистоты и защита от перегрузки",
        "Готовые звуковые профили и собственные настройки",
    ],
    purchase_available=True,
    checkout_mode="mock",
)


@router.get("/plans", response_model=list[SubscriptionPlanRead])
def list_subscription_plans(_: User = Depends(current_user)) -> list[SubscriptionPlanRead]:
    return [PREMIUM_MONTHLY_PLAN]


@router.get("/me", response_model=SubscriptionStatusRead)
def get_my_subscription(user: User = Depends(current_user)) -> SubscriptionStatusRead:
    is_premium = has_premium_entitlement(user)
    return SubscriptionStatusRead(
        status=user.subscription_status or "inactive",
        is_premium=is_premium,
        entitlements=subscription_entitlements(user),
        plan_id=PREMIUM_MONTHLY_PLAN.id if is_premium else None,
        purchase_available=PREMIUM_MONTHLY_PLAN.purchase_available,
    )


@router.post("/checkout-preview", response_model=CheckoutPreviewRead)
def create_checkout_preview(
    payload: CheckoutPreviewRequest,
    _: User = Depends(current_user),
) -> CheckoutPreviewRead:
    if payload.plan_id != PREMIUM_MONTHLY_PLAN.id:
        raise HTTPException(status_code=404, detail="Subscription plan not found")
    return CheckoutPreviewRead(
        id=f"preview-{uuid4().hex}",
        plan=PREMIUM_MONTHLY_PLAN,
        activation_performed=False,
        message="Payment is not connected yet. No charge or subscription activation was performed.",
    )


@router.post("/checkout", response_model=MockCheckoutRead)
def create_mock_checkout(
    payload: CheckoutPreviewRequest,
    user: User = Depends(current_user),
) -> MockCheckoutRead:
    if payload.plan_id != PREMIUM_MONTHLY_PLAN.id:
        raise HTTPException(status_code=404, detail="Subscription plan not found")
    checkout_id = uuid4().hex
    ticket = create_mock_checkout_ticket(user.id, PREMIUM_MONTHLY_PLAN.id)
    checkout_url = f"{PUBLIC_BASE_URL}/api/subscriptions/mock-payment?{urlencode({'checkout_token': ticket})}"
    return MockCheckoutRead(
        id=checkout_id,
        plan=PREMIUM_MONTHLY_PLAN,
        checkout_url=checkout_url,
        expires_in_seconds=MOCK_CHECKOUT_EXPIRES_SECONDS,
    )


@router.get("/mock-payment", response_class=HTMLResponse, include_in_schema=False)
def complete_mock_payment(
    checkout_token: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        payload = decode_mock_checkout_ticket(checkout_token, PREMIUM_MONTHLY_PLAN.id)
        user = require_active_user(db, int(payload["sub"]))
    except (HTTPException, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired checkout token") from exc

    if not has_premium_entitlement(user):
        user.subscription_status = "premium"
        db.add(user)
        db.commit()
    record_event("subscription", "Mock Premium checkout completed", path="/api/subscriptions/mock-payment")
    html = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Оплата завершена</title>
  <style>
    *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#080b14;color:#f8fafc;font-family:Inter,Arial,sans-serif}
    main{display:grid;max-width:420px;justify-items:center;gap:14px;padding:32px;text-align:center}svg{width:46px;height:46px;padding:11px;border:1px solid #2f855a;border-radius:50%;color:#86efac;background:#123524;stroke-width:2}
    h1{margin:0;font-size:22px}p{margin:0;color:#94a3b8;font-size:14px;line-height:1.55}
  </style>
</head>
<body><main><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg><h1>Оплата прошла</h1><p>Premium уже активирован. Можно вернуться в Million Music.</p></main></body>
</html>"""
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        },
    )
