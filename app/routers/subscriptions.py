from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.models.user import User
from app.routers.auth import current_user
from app.schemas.subscription import (
    CheckoutPreviewRead,
    CheckoutPreviewRequest,
    SubscriptionPlanRead,
    SubscriptionStatusRead,
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
    purchase_available=False,
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
