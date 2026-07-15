import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.database import Base
from app.models.user import User
from app.routers.subscriptions import (
    create_checkout_preview,
    get_my_subscription,
    list_subscription_plans,
)
from app.schemas.subscription import CheckoutPreviewRequest
from app.services.subscription_service import has_premium_entitlement


def make_user(status: str = "inactive") -> User:
    return User(
        login=f"listener-{status}",
        nickname="Listener",
        password_hash="test",
        subscription_status=status,
    )


def test_premium_entitlement_accepts_paid_and_granted_access() -> None:
    assert not has_premium_entitlement(make_user("inactive"))
    assert has_premium_entitlement(make_user("premium"))
    assert has_premium_entitlement(make_user("trial"))
    assert has_premium_entitlement(make_user("support"))


def test_subscription_status_and_plan_are_consistent() -> None:
    user = make_user("premium")
    plans = list_subscription_plans(user)
    status = get_my_subscription(user)

    assert len(plans) == 1
    assert plans[0].id == "premium-monthly"
    assert plans[0].purchase_available is False
    assert status.is_premium is True
    assert status.plan_id == plans[0].id
    assert status.entitlements == ["audio.equalizer"]


def test_checkout_preview_never_activates_subscription() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = make_user()
        db.add(user)
        db.commit()
        db.refresh(user)

        preview = create_checkout_preview(CheckoutPreviewRequest(plan_id="premium-monthly"), user)
        db.refresh(user)

        assert preview.status == "preview"
        assert preview.activation_performed is False
        assert user.subscription_status == "inactive"
    engine.dispose()
