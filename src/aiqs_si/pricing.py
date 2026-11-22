"""Approved irradiation unit policy; purchasing/payment integration is separate.

Values come from the pricing policy (policies.load_pricing_policy):
the operator may override them through AIQS_SI_PRICING_POLICY_JSON in
the deployment's config/env. The defaults in this open-source
tree are synthetic example values.
"""
from dataclasses import dataclass

from .policies import load_pricing_policy

_POLICY = load_pricing_policy()

PURCHASE_PACKS = _POLICY.purchase_packs


def rate_display(rate_tenths):
    """Display/JSON form of a rate held in tenths: whole credits stay
    an int (50), a fractional rate becomes the exact decimal (3.9)."""
    if rate_tenths % 10 == 0:
        return rate_tenths // 10
    return rate_tenths / 10


# Per-shot rates are held internally in tenths of a credit so a
# fractional rate stays exact in integer arithmetic on every path
# (quote, reservation, settlement, refund, D-Planet routing).
#
# A job always settles on the quote accepted at submission, so a
# later rate change never reprices an accepted job. The campaign
# machinery stays in the policy, dormant, for a possible future
# promotion.
CREDITS_PER_SHOT_TENTHS = _POLICY.credits_per_shot_tenths
CREDITS_PER_SHOT = rate_display(CREDITS_PER_SHOT_TENTHS)
CAMPAIGN_NAME = _POLICY.campaign_name
CAMPAIGN_CREDITS_PER_SHOT_TENTHS = _POLICY.campaign_credits_per_shot_tenths
CAMPAIGN_ACTIVE = _POLICY.campaign_active
MAX_SHOTS = _POLICY.max_shots
PRICE_VERSION = _POLICY.price_version

# Display form of the (dormant) campaign rate.
CAMPAIGN_CREDITS_PER_SHOT = rate_display(CAMPAIGN_CREDITS_PER_SHOT_TENTHS)


def session_charge_credits(count, rate_tenths):
    """THE session charge — the single computation shared by quote,
    reservation, settlement and refund: round half-up of
    count × rate, in integer tenths, so every path agrees exactly
    and balances stay integers. At a rate of 10 tenths
    (1 credit/shot): 1 shot → 1, 9 → 9, 33 → 33, 99 → 99."""
    if type(count) is not int or count < 0:
        raise ValueError("Shot count must be a non-negative integer")
    if type(rate_tenths) is not int or rate_tenths < 1:
        raise ValueError("A positive integer rate in tenths is required")
    return (count * rate_tenths + 5) // 10


def active_rate_tenths():
    """Per-shot rate in tenths applied to new quotes under the policy."""
    return CAMPAIGN_CREDITS_PER_SHOT_TENTHS if CAMPAIGN_ACTIVE \
        else CREDITS_PER_SHOT_TENTHS

# Native-app AI reading (native-reading design §5): owner-decided
# 36 credits per reading, policy-overridable. A reading settles on
# the quote parked at prepare time, exactly like irradiation.
READING_CREDITS = _POLICY.reading_credits


def reading_quote():
    """Price terms for one native-app AI reading."""
    return {"credits": READING_CREDITS, "price_version": PRICE_VERSION}


def active_credits_per_shot():
    """Per-shot rate applied to new quotes under the current policy,
    in display form (the standard rate, 3.9)."""
    return rate_display(active_rate_tenths())


def purchase_quote(yen):
    """Approved manual-purchase packs. This does not initiate a payment."""
    if type(yen) is not int or yen not in dict(PURCHASE_PACKS):
        raise ValueError("Select an approved purchase pack")
    return {"yen": yen, "credits": dict(PURCHASE_PACKS)[yen], "currency": "JPY"}


@dataclass(frozen=True)
class CreditPrice:
    version: str = "2026-11-22"
    yen_per_credit: int = 1
    credits_per_shot_tenths: int = CREDITS_PER_SHOT_TENTHS
    campaign: str | None = None

    def __post_init__(self):
        if not self.version or type(self.yen_per_credit) is not int or self.yen_per_credit < 1:
            raise ValueError("A version and positive integer yen price are required")
        if type(self.credits_per_shot_tenths) is not int or self.credits_per_shot_tenths < 1:
            raise ValueError("A positive integer credits-per-shot rate in tenths is required")
        if self.campaign is not None and (type(self.campaign) is not str or not self.campaign):
            raise ValueError("A campaign name must be a non-empty string when present")

    @property
    def credits_per_shot(self):
        """The per-shot rate in display form (int when whole, else the
        exact decimal such as 3.9). Charges never use this form; they
        go through session_charge_credits on the tenths value."""
        return rate_display(self.credits_per_shot_tenths)


def current_price():
    """Price terms for a new quote: the standard rate, or the
    campaign rate while a campaign is active (none by default)."""
    if CAMPAIGN_ACTIVE:
        return CreditPrice(version=PRICE_VERSION,
                           credits_per_shot_tenths=CAMPAIGN_CREDITS_PER_SHOT_TENTHS,
                           campaign=CAMPAIGN_NAME)
    return CreditPrice(version=PRICE_VERSION)


def irradiation_quote(count, price=None):
    if price is None:
        price = current_price()
    if type(count) is not int or not 1 <= count <= MAX_SHOTS:
        raise ValueError(
            f"Irradiation count must be an integer from 1 to {MAX_SHOTS}")
    credits = session_charge_credits(count, price.credits_per_shot_tenths)
    return {"count": count, "strength": "weak" if count <= 33 else "medium" if count <= 66 else "strong",
            "credits": credits, "credits_per_shot": price.credits_per_shot,
            "credits_per_shot_tenths": price.credits_per_shot_tenths,
            "standard_credits_per_shot": CREDITS_PER_SHOT,
            "standard_credits_per_shot_tenths": CREDITS_PER_SHOT_TENTHS,
            "campaign": price.campaign,
            "yen_equivalent": credits * price.yen_per_credit,
            "yen_per_credit": price.yen_per_credit, "price_version": price.version}
