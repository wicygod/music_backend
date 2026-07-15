from typing import Final

from app.models.user import User


PREMIUM_ENTITLEMENT: Final = "audio.equalizer"
PREMIUM_STATUSES: Final[frozenset[str]] = frozenset({"premium", "trial", "support"})


def has_premium_entitlement(user: User) -> bool:
    return (user.subscription_status or "inactive").strip().lower() in PREMIUM_STATUSES


def subscription_entitlements(user: User) -> list[str]:
    return [PREMIUM_ENTITLEMENT] if has_premium_entitlement(user) else []
