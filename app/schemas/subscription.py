from typing import Literal

from pydantic import BaseModel, Field


class SubscriptionPlanRead(BaseModel):
    id: str
    name: str
    description: str
    price_minor: int = Field(ge=0)
    currency: str
    billing_period: Literal["month"]
    features: list[str]
    purchase_available: bool = False
    checkout_mode: Literal["preview", "mock"] = "preview"


class SubscriptionStatusRead(BaseModel):
    status: str
    is_premium: bool
    entitlements: list[str]
    plan_id: str | None = None
    purchase_available: bool = False


class CheckoutPreviewRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)


class CheckoutPreviewRead(BaseModel):
    id: str
    status: Literal["preview"] = "preview"
    plan: SubscriptionPlanRead
    activation_performed: bool = False
    message: str


class MockCheckoutRead(BaseModel):
    id: str
    status: Literal["pending"] = "pending"
    plan: SubscriptionPlanRead
    checkout_url: str
    expires_in_seconds: int = Field(gt=0, le=3600)
