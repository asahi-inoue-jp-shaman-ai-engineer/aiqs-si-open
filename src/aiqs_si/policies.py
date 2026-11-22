"""Operator policies loaded from env JSON (OSS plan v4, Phase A).

Resonance-adjacent operating parameters — pricing, grant amounts and
their invariants, scan/claim limits, and named core execution
profiles — live outside the source tree so the private values are set
in the deployment's config/env and never in code, logs, or this
module's error messages. Every loader is strict: the JSON must be an
object, unknown keys are rejected, types and ranges are checked, and
errors name the offending KEY only, never the supplied value. With
the env var unset, each loader returns the built-in defaults, which
in this open-source tree are synthetic example values; production
deployments supply their real values through the environment.
"""
import json
import os

PRICING_POLICY_ENV = "AIQS_SI_PRICING_POLICY_JSON"
GRANT_POLICY_ENV = "AIQS_SI_GRANT_POLICY_JSON"
LIMIT_POLICY_ENV = "AIQS_SI_LIMIT_POLICY_JSON"
EXECUTION_PROFILES_ENV = "AIQS_SI_CORE_EXECUTION_PROFILES_JSON"


class PolicyError(ValueError):
    """A policy env value failed schema validation (value never echoed)."""


def _load_object(environ, env_name):
    raw = environ.get(env_name)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise PolicyError(f"{env_name}: must be a JSON object string")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise PolicyError(f"{env_name}: not valid JSON") from None
    if not isinstance(parsed, dict):
        raise PolicyError(f"{env_name}: top level must be a JSON object")
    return parsed


def _reject_unknown(env_name, obj, allowed):
    for key in obj:
        if key not in allowed:
            raise PolicyError(f"{env_name}: unknown key {key!r}")


def _int(env_name, obj, key, *, minimum=None, maximum=None, default=None):
    if key not in obj:
        return default
    value = obj[key]
    if type(value) is not int:
        raise PolicyError(f"{env_name}: key {key!r} must be an integer")
    if minimum is not None and value < minimum:
        raise PolicyError(f"{env_name}: key {key!r} is below its minimum")
    if maximum is not None and value > maximum:
        raise PolicyError(f"{env_name}: key {key!r} exceeds its maximum")
    return value


def _optional_int(env_name, obj, key, *, minimum=None, default=None):
    if key not in obj or obj[key] is None:
        return default
    return _int(env_name, obj, key, minimum=minimum, default=default)


def _str(env_name, obj, key, *, default=None, max_length=200):
    if key not in obj:
        return default
    value = obj[key]
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise PolicyError(f"{env_name}: key {key!r} must be a bounded string")
    return value


class PricingPolicy:
    def __init__(self, *, purchase_packs, credits_per_shot_tenths, price_version,
                 campaign_active, campaign_credits_per_shot_tenths, campaign_name,
                 max_shots, reading_credits=36):
        self.purchase_packs = purchase_packs
        # The standard rate is held in tenths of a credit so a
        # fractional rate stays exact in integer arithmetic
        # everywhere; session charges round half-up from tenths.
        self.credits_per_shot_tenths = credits_per_shot_tenths
        self.price_version = price_version
        self.campaign_active = campaign_active
        # Campaign machinery, retained dormant: no campaign runs by
        # default, so quotes carry no campaign-vs-standard distinction.
        self.campaign_credits_per_shot_tenths = campaign_credits_per_shot_tenths
        self.campaign_name = campaign_name
        self.max_shots = max_shots
        # Native-app AI reading price: a policy key so a price
        # change is a config edit, never a code change.
        self.reading_credits = reading_credits


DEFAULT_PURCHASE_PACKS = ((100, 100), (500, 400), (1000, 900))


def load_pricing_policy(environ=None):
    environ = os.environ if environ is None else environ
    obj = _load_object(environ, PRICING_POLICY_ENV)
    if obj is None:
        return PricingPolicy(
            purchase_packs=DEFAULT_PURCHASE_PACKS, credits_per_shot_tenths=10,
            price_version="0.0.0-example", campaign_active=False,
            campaign_credits_per_shot_tenths=10, campaign_name="example-campaign",
            max_shots=99, reading_credits=10)
    _reject_unknown(PRICING_POLICY_ENV, obj,
                    {"purchase_packs", "credits_per_shot", "credits_per_shot_tenths",
                     "price_version", "campaign", "max_shots", "reading_credits"})
    packs = DEFAULT_PURCHASE_PACKS
    if "purchase_packs" in obj:
        raw = obj["purchase_packs"]
        if not isinstance(raw, list) or not raw:
            raise PolicyError(f"{PRICING_POLICY_ENV}: key 'purchase_packs' must be a nonempty list")
        checked = []
        for pair in raw:
            if (not isinstance(pair, list) or len(pair) != 2
                    or type(pair[0]) is not int or type(pair[1]) is not int
                    or pair[0] < 1 or pair[1] < 1):
                raise PolicyError(
                    f"{PRICING_POLICY_ENV}: key 'purchase_packs' must contain [yen, credits] positive integer pairs")
            checked.append((pair[0], pair[1]))
        packs = tuple(checked)
    if "credits_per_shot" in obj and "credits_per_shot_tenths" in obj:
        raise PolicyError(
            f"{PRICING_POLICY_ENV}: set only one of 'credits_per_shot' and "
            "'credits_per_shot_tenths'")
    if "credits_per_shot_tenths" in obj:
        standard_tenths = _int(PRICING_POLICY_ENV, obj,
                               "credits_per_shot_tenths",
                               minimum=1, default=10)
    elif "credits_per_shot" in obj:
        # Legacy whole-credit form, scaled exactly into tenths.
        standard_tenths = _int(PRICING_POLICY_ENV, obj, "credits_per_shot",
                               minimum=1, default=5) * 10
    else:
        standard_tenths = 10
    campaign_active, campaign_tenths, campaign_name = False, 10, "example-campaign"
    if "campaign" in obj:
        campaign = obj["campaign"]
        if not isinstance(campaign, dict):
            raise PolicyError(f"{PRICING_POLICY_ENV}: key 'campaign' must be an object")
        _reject_unknown(PRICING_POLICY_ENV, campaign,
                        {"active", "credits_per_shot", "credits_per_shot_tenths",
                         "name"})
        if "active" in campaign:
            if type(campaign["active"]) is not bool:
                raise PolicyError(f"{PRICING_POLICY_ENV}: key 'campaign.active' must be a boolean")
            campaign_active = campaign["active"]
        if "credits_per_shot" in campaign and "credits_per_shot_tenths" in campaign:
            raise PolicyError(
                f"{PRICING_POLICY_ENV}: key 'campaign' must set only one of "
                "'credits_per_shot' and 'credits_per_shot_tenths'")
        if "credits_per_shot_tenths" in campaign:
            campaign_tenths = _int(PRICING_POLICY_ENV, campaign,
                                   "credits_per_shot_tenths",
                                   minimum=1, default=10)
        elif "credits_per_shot" in campaign:
            # Legacy whole-credit form, scaled exactly into tenths.
            campaign_tenths = _int(PRICING_POLICY_ENV, campaign,
                                   "credits_per_shot",
                                   minimum=1, default=10) * 10
        campaign_name = _str(PRICING_POLICY_ENV, campaign, "name",
                             default="example-campaign", max_length=100)
    return PricingPolicy(
        purchase_packs=packs,
        credits_per_shot_tenths=standard_tenths,
        price_version=_str(PRICING_POLICY_ENV, obj, "price_version",
                           default="0.0.0-example", max_length=64),
        campaign_active=campaign_active,
        campaign_credits_per_shot_tenths=campaign_tenths,
        campaign_name=campaign_name,
        max_shots=_int(PRICING_POLICY_ENV, obj, "max_shots",
                       minimum=1, maximum=999, default=99),
        reading_credits=_int(PRICING_POLICY_ENV, obj, "reading_credits",
                             minimum=1, default=10))


class GrantPolicy:
    """Credit grant amounts plus the Phase A invariants (plan v4 §7):
    a per-inviter referral cap and a total grant budget. Either cap is
    None (dormant) until the operator sets a number; while dormant the
    historical behavior is unchanged."""

    def __init__(self, *, signup_campaign_credits, migration_credits,
                 referral_credits, legacy_loyalty_gift_credits,
                 referral_cap_per_inviter, total_budget,
                 referral_window_seconds=86400, referral_grace_until=None,
                 referral_grace_start=None):
        self.signup_campaign_credits = signup_campaign_credits
        self.migration_credits = migration_credits
        self.referral_credits = referral_credits
        self.legacy_loyalty_gift_credits = legacy_loyalty_gift_credits
        self.referral_cap_per_inviter = referral_cap_per_inviter
        self.total_budget = total_budget
        # Referral redemption window (referral-system-design-20261122
        # D2): a code may be redeemed within this many seconds of
        # account creation; referral_grace_until is an epoch-seconds
        # launch grace deadline (None = no grace) during which any
        # existing account may still redeem.
        self.referral_window_seconds = referral_window_seconds
        self.referral_grace_until = referral_grace_until
        # Launch-grace cohort boundary (fix round F-SI-1): when set,
        # the grace applies only to accounts created BEFORE this
        # epoch-seconds instant (legacy NULL-created_at accounts
        # count as pre-launch). None keeps the historical
        # deadline-only behavior for backward compatibility.
        self.referral_grace_start = referral_grace_start


def load_grant_policy(environ=None):
    environ = os.environ if environ is None else environ
    obj = _load_object(environ, GRANT_POLICY_ENV)
    if obj is None:
        return GrantPolicy(signup_campaign_credits=0, migration_credits=100,
                           referral_credits=100, legacy_loyalty_gift_credits=100,
                           referral_cap_per_inviter=None, total_budget=None,
                           referral_window_seconds=86400, referral_grace_until=None,
                           referral_grace_start=None)
    _reject_unknown(GRANT_POLICY_ENV, obj,
                    {"signup_campaign_credits", "migration_credits",
                     "referral_credits", "legacy_loyalty_gift_credits",
                     "referral_cap_per_inviter", "total_budget",
                     "referral_window_seconds", "referral_grace_until",
                     "referral_grace_start"})
    return GrantPolicy(
        signup_campaign_credits=_int(GRANT_POLICY_ENV, obj, "signup_campaign_credits",
                                     minimum=0, default=0),
        migration_credits=_int(GRANT_POLICY_ENV, obj, "migration_credits",
                               minimum=0, default=100),
        referral_credits=_int(GRANT_POLICY_ENV, obj, "referral_credits",
                              minimum=0, default=100),
        legacy_loyalty_gift_credits=_int(GRANT_POLICY_ENV, obj,
                                         "legacy_loyalty_gift_credits",
                                         minimum=0, default=100),
        referral_cap_per_inviter=_optional_int(GRANT_POLICY_ENV, obj,
                                               "referral_cap_per_inviter", minimum=0),
        total_budget=_optional_int(GRANT_POLICY_ENV, obj, "total_budget", minimum=0),
        referral_window_seconds=_int(GRANT_POLICY_ENV, obj, "referral_window_seconds",
                                     minimum=0, default=86400),
        referral_grace_until=_optional_int(GRANT_POLICY_ENV, obj,
                                           "referral_grace_until", minimum=0),
        referral_grace_start=_optional_int(GRANT_POLICY_ENV, obj,
                                           "referral_grace_start", minimum=0))


class LimitPolicy:
    """Scan exposure limits (plan v4 §9 R3-2) and the worker claim TTL
    (plan v4 §8). Quota/budget are None (dormant) until the operator
    sets approved numbers; the reservation ledger runs either way so
    the accounting exists before enforcement is switched on."""

    def __init__(self, *, scan_daily_quota_per_account, scan_exposure_budget_total,
                 scan_units_per_job, claim_ttl_seconds,
                 reading_daily_cap_per_account=None):
        self.scan_daily_quota_per_account = scan_daily_quota_per_account
        self.scan_exposure_budget_total = scan_exposure_budget_total
        self.scan_units_per_job = scan_units_per_job
        self.claim_ttl_seconds = claim_ttl_seconds
        # Native-app AI reading daily cap (native-reading design
        # §4.4): None (dormant) until the operator sets the approved
        # number; the count runs in the reserve transaction either
        # way once readings exist.
        self.reading_daily_cap_per_account = reading_daily_cap_per_account


def load_limit_policy(environ=None):
    environ = os.environ if environ is None else environ
    obj = _load_object(environ, LIMIT_POLICY_ENV)
    if obj is None:
        return LimitPolicy(scan_daily_quota_per_account=None,
                           scan_exposure_budget_total=None,
                           scan_units_per_job=1, claim_ttl_seconds=3600,
                           reading_daily_cap_per_account=None)
    _reject_unknown(LIMIT_POLICY_ENV, obj,
                    {"scan_daily_quota_per_account", "scan_exposure_budget_total",
                     "scan_units_per_job", "claim_ttl_seconds",
                     "reading_daily_cap_per_account"})
    return LimitPolicy(
        scan_daily_quota_per_account=_optional_int(
            LIMIT_POLICY_ENV, obj, "scan_daily_quota_per_account", minimum=0),
        scan_exposure_budget_total=_optional_int(
            LIMIT_POLICY_ENV, obj, "scan_exposure_budget_total", minimum=0),
        scan_units_per_job=_int(LIMIT_POLICY_ENV, obj, "scan_units_per_job",
                                minimum=1, maximum=1000, default=1),
        claim_ttl_seconds=_int(LIMIT_POLICY_ENV, obj, "claim_ttl_seconds",
                               minimum=60, maximum=86400, default=3600),
        reading_daily_cap_per_account=_optional_int(
            LIMIT_POLICY_ENV, obj, "reading_daily_cap_per_account", minimum=0))


_PROFILE_KEYS = {"cycles", "top_mfs", "top_entries", "qrs_threshold",
                 "duration_sec", "count"}


def load_execution_profiles(environ=None):
    """Named core execution profiles: {name: settings-dict}.

    Returns plain dicts; callers build ExecutionSettings from them so
    its range validation stays the single authority. Profile names
    and values are operator configuration, never logged here.
    """
    environ = os.environ if environ is None else environ
    obj = _load_object(environ, EXECUTION_PROFILES_ENV)
    if obj is None:
        return {}
    profiles = {}
    for name, settings in obj.items():
        if not isinstance(name, str) or not name or len(name) > 64:
            raise PolicyError(f"{EXECUTION_PROFILES_ENV}: profile names must be bounded strings")
        if not isinstance(settings, dict):
            raise PolicyError(f"{EXECUTION_PROFILES_ENV}: each profile must be an object")
        _reject_unknown(EXECUTION_PROFILES_ENV, settings, _PROFILE_KEYS)
        missing = _PROFILE_KEYS - set(settings)
        if missing:
            raise PolicyError(
                f"{EXECUTION_PROFILES_ENV}: profile is missing required settings keys")
        profiles[name] = dict(settings)
    return profiles
