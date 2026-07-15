import os
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.database import Base
from app.database import get_db
from app.middleware.security import LightweightSecurityMiddleware
from app.models.user import User
from app.routers.subscriptions import (
    complete_mock_payment,
    create_checkout_preview,
    create_mock_checkout,
    get_my_subscription,
    list_subscription_plans,
    router as subscriptions_router,
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
    assert plans[0].purchase_available is True
    assert plans[0].checkout_mode == "mock"
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


def test_mock_payment_link_activates_only_its_signed_user() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = make_user()
        db.add(user)
        db.commit()
        db.refresh(user)

        checkout = create_mock_checkout(CheckoutPreviewRequest(plan_id="premium-monthly"), user)
        token = parse_qs(urlparse(checkout.checkout_url).query)["checkout_token"][0]
        response = complete_mock_payment(token, db)
        db.refresh(user)

        assert checkout.status == "pending"
        assert checkout.checkout_url.startswith("http://5.181.21.13:8000/api/subscriptions/mock-payment?")
        assert response.status_code == 200
        assert "Premium" in response.body.decode("utf-8")
        assert user.subscription_status == "premium"
    engine.dispose()


def test_invalid_mock_payment_token_does_not_activate_user() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = make_user()
        db.add(user)
        db.commit()
        db.refresh(user)

        with pytest.raises(HTTPException) as exc_info:
            complete_mock_payment("invalid-token", db)
        db.refresh(user)

        assert exc_info.value.status_code == 401
        assert user.subscription_status == "inactive"
    engine.dispose()


def test_signed_payment_page_is_reachable_without_app_or_auth_tokens() -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = make_user()
        db.add(user)
        db.commit()
        db.refresh(user)
        checkout = create_mock_checkout(CheckoutPreviewRequest(plan_id="premium-monthly"), user)
        checkout_path = urlparse(checkout.checkout_url).path + "?" + urlparse(checkout.checkout_url).query

        test_app = FastAPI()
        test_app.add_middleware(LightweightSecurityMiddleware)
        test_app.include_router(subscriptions_router)

        def override_db():
            yield db

        test_app.dependency_overrides[get_db] = override_db
        response = TestClient(test_app).get(checkout_path)
        db.refresh(user)

        assert response.status_code == 200
        assert response.headers["cache-control"].startswith("no-store")
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        assert user.subscription_status == "premium"
    engine.dispose()
