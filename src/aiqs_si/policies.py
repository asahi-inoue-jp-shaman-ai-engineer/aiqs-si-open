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
The production entry point additionally runs
policy_posture_problems() at startup so those defaults can never
take effect silently in production: the money- and exposure-bearing
policies must be set explicitly (an explicit null limit is an
explicit choice), or the operator must acknowledge the defaults
with AIQS_SI_ALLOW_DEFAULT_POLICIES=1.
"""
import json
import math
import os

PRICING_POLICY_ENV = "AIQS_SI_PRICING_POLICY_JSON"
GRANT_POLICY_ENV = "AIQS_SI_GRANT_POLICY_JSON"
LIMIT_POLICY_ENV = "AIQS_SI_LIMIT_POLICY_JSON"
ESTIMATE_POLICY_ENV = "AIQS_SI_ESTIMATE_POLICY_JSON"
EXECUTION_PROFILES_ENV = "AIQS_SI_CORE_EXECUTION_PROFILES_JSON"
ALLOW_DEFAULT_POLICIES_ENV = "AIQS_SI_ALLOW_DEFAULT_POLICIES"


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


def _number(env_name, obj, key, *, minimum=None, maximum=None,
            positive=False, default=None):
    """A finite JSON number (int or float, never a bool) for the
    seconds/factor values of the estimate policy. JSON's NaN and
    Infinity literals parse to non-finite floats and are rejected
    here. Errors name the key only, like every loader in this
    module."""
    if key not in obj:
        return default
    value = obj[key]
    # Integers are finite by definition and are bounded by direct
    # comparison below; math.isfinite would convert to float and
    # raise OverflowError on a huge JSON integer before the bounds
    # check can reject it with the controlled error.
    if type(value) not in (int, float) or (
            type(value) is float and not math.isfinite(value)):
        raise PolicyError(f"{env_name}: key {key!r} must be a finite number")
    if positive and value <= 0:
        raise PolicyError(f"{env_name}: key {key!r} must be positive")
    if minimum is not None and value < minimum:
        raise PolicyError(f"{env_name}: key {key!r} is below its minimum")
    if maximum is not None and value > maximum:
        raise PolicyError(f"{env_name}: key {key!r} exceeds its maximum")
    return value


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
    the accounting exists before enforcement is switched on.

    Units: scan_daily_quota_per_account is a COUNT of scans per
    account per day; scan_exposure_budget_total is in exposure units
    (scan_units_per_job per scan). The field names are the deployed
    env keys and stay stable; only the daily quota's counting basis
    was corrected (it previously summed units, so a quota below the
    per-scan unit cost rejected every scan)."""

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


class EstimatePolicy:
    """Effective device-lane durations behind the app API's duration
    estimates (foundation item 3, 2026-11-23).

    The seconds values are OPERATOR-MEASURED effective durations,
    deliberately NOT the device settings stamped into job payloads:
    the effective end-to-end duration of one irradiation shot on
    the lane exceeds its configured device seconds once device
    handling and server round trips are counted, and a scan has
    its own measured constant.
    Deriving the estimate from the 9-second setting would present a
    number the real device does not meet — the estimate must mirror
    the real machine, so the measured values live here as operator
    configuration, maintainable without a code change.

    Either seconds value is None until the operator sets it, and
    None means "no estimate exists": the API answers null instead
    of inventing a figure. The range factors widen the nominal
    total into the [min, max] band the UI presents; the band is a
    range on purpose, never a point promise."""

    def __init__(self, *, scan_seconds, irradiation_seconds_per_shot,
                 range_low_factor, range_high_factor):
        self.scan_seconds = scan_seconds
        self.irradiation_seconds_per_shot = irradiation_seconds_per_shot
        self.range_low_factor = range_low_factor
        self.range_high_factor = range_high_factor


def load_estimate_policy(environ=None):
    environ = os.environ if environ is None else environ
    obj = _load_object(environ, ESTIMATE_POLICY_ENV)
    if obj is None:
        return EstimatePolicy(scan_seconds=None,
                              irradiation_seconds_per_shot=None,
                              range_low_factor=0.8, range_high_factor=1.6)
    _reject_unknown(ESTIMATE_POLICY_ENV, obj,
                    {"scan_seconds", "irradiation_seconds_per_shot",
                     "range_low_factor", "range_high_factor"})

    def seconds(key, maximum):
        # An explicit null is the operator's recorded "no measured
        # value" (the _optional_int convention of the limit policy):
        # that operation simply has no estimate.
        if key in obj and obj[key] is None:
            return None
        return _number(ESTIMATE_POLICY_ENV, obj, key,
                       positive=True, maximum=maximum)

    low = _number(ESTIMATE_POLICY_ENV, obj, "range_low_factor",
                  positive=True, maximum=1, default=0.8)
    high = _number(ESTIMATE_POLICY_ENV, obj, "range_high_factor",
                   minimum=1, maximum=5, default=1.6)
    if not low < high:
        # Equal or inverted factors would present the range as a
        # point promise; the estimate must keep its width.
        raise PolicyError(
            f"{ESTIMATE_POLICY_ENV}: range_low_factor must be below "
            "range_high_factor")
    return EstimatePolicy(
        scan_seconds=seconds("scan_seconds", 86400),
        irradiation_seconds_per_shot=seconds(
            "irradiation_seconds_per_shot", 3600),
        range_low_factor=low, range_high_factor=high)


def policy_posture_problems(environ=None):
    """Startup posture check (Astra final round #5): the money- and
    exposure-bearing policies must be EXPLICIT in the deployment
    env, so a missing or partial variable can never silently
    substitute the built-in defaults in production.

    Explicit includes an explicit null: a limit key set to null is
    the operator's recorded choice of unlimited operation (with the
    reservation ledger and monitoring running), not an omission.
    The development/demo path stays available through the explicit
    AIQS_SI_ALLOW_DEFAULT_POLICIES=1 acknowledgement, which accepts
    the built-in defaults as a deliberate choice.

    Returns a list of human-readable problems naming env vars and
    keys only — never values. An empty list means the posture is
    explicit. The loaders themselves are unchanged: their defaults
    remain the historical values for tests and demo use.
    """
    environ = os.environ if environ is None else environ
    if environ.get(ALLOW_DEFAULT_POLICIES_ENV) == "1":
        return []
    problems = []

    def parsed(env_name):
        try:
            return _load_object(environ, env_name)
        except PolicyError:
            problems.append(f"{env_name}: set but not a valid JSON object")
            return "invalid"

    pricing = parsed(PRICING_POLICY_ENV)
    if pricing is None:
        problems.append(
            f"{PRICING_POLICY_ENV}: not set; built-in default prices "
            "would apply silently")
    elif pricing != "invalid":
        if not ("credits_per_shot_tenths" in pricing
                or "credits_per_shot" in pricing):
            problems.append(
                f"{PRICING_POLICY_ENV}: does not set a shot rate "
                "('credits_per_shot_tenths'); the default rate would apply silently")
        # Partial specification is not explicit (batch 2d,
        # MEDIUM 4): the purchase packs, the per-session shot
        # ceiling and the reading price are money-bearing too,
        # and each silently inherited its default while only the
        # rate was pinned.
        for key in ("purchase_packs", "max_shots", "reading_credits"):
            if key not in pricing:
                problems.append(
                    f"{PRICING_POLICY_ENV}: does not set {key!r}; "
                    "the default would apply silently")
        # An ACTIVE campaign is money-bearing on its own (batch
        # 2e, R4): a campaign block that omits its rate silently
        # inherits the base rate, charging a price nobody pinned
        # for the campaign.
        campaign = pricing.get("campaign")
        if isinstance(campaign, dict) and campaign.get("active") is True:
            if not ("credits_per_shot_tenths" in campaign
                    or "credits_per_shot" in campaign):
                problems.append(
                    f"{PRICING_POLICY_ENV}: the active campaign does not "
                    "set its own shot rate; it would silently inherit "
                    "the base rate")

    grant = parsed(GRANT_POLICY_ENV)
    if grant is None:
        problems.append(
            f"{GRANT_POLICY_ENV}: not set; built-in default grant "
            "amounts would apply silently")
    elif grant != "invalid":
        if "referral_credits" not in grant:
            problems.append(
                f"{GRANT_POLICY_ENV}: does not set 'referral_credits'; "
                "the default grant amount would apply silently")
        # The Phase A grant invariants are exposure-bearing: a
        # cap left unmentioned is dormant by default, which must
        # be a recorded choice (a number, or an explicit null
        # for monitored-unlimited), not an omission.
        for key in ("referral_cap_per_inviter", "total_budget"):
            if key not in grant:
                problems.append(
                    f"{GRANT_POLICY_ENV}: does not set {key!r}; set a "
                    "number or an explicit null (dormant, monitored)")
        # The remaining grant amounts are money-bearing too
        # (batch 2e, R4): signup/migration/loyalty amounts left
        # unmentioned silently inherited their built-in defaults
        # while only the referral amount was pinned.
        for key in ("signup_campaign_credits", "migration_credits",
                    "legacy_loyalty_gift_credits"):
            if key not in grant:
                problems.append(
                    f"{GRANT_POLICY_ENV}: does not set {key!r}; "
                    "the default grant amount would apply silently")

    limit = parsed(LIMIT_POLICY_ENV)
    if limit is None:
        problems.append(
            f"{LIMIT_POLICY_ENV}: not set; built-in default limits "
            "(unlimited) would apply silently")
    elif limit != "invalid":
        for key in ("scan_daily_quota_per_account",
                    "scan_exposure_budget_total",
                    "reading_daily_cap_per_account"):
            if key not in limit:
                problems.append(
                    f"{LIMIT_POLICY_ENV}: does not set {key!r}; set a "
                    "number or an explicit null (unlimited, monitored)")
        # The per-job scan width is an operator decision like
        # its sibling keys (batch 2e, R4), not a default to
        # inherit silently. (claim_ttl_seconds stays a loader
        # default: it is a liveness bound, not an exposure or
        # money value — recorded in the batch 2e notes.)
        if "scan_units_per_job" not in limit:
            problems.append(
                f"{LIMIT_POLICY_ENV}: does not set 'scan_units_per_job'; "
                "the default would apply silently")

    # Presence is not validity (batch 2e, R4): a key present
    # with a wrong-typed or out-of-range value passed the
    # presence checks above while the loader would reject it
    # later — or, on a path that never loads it, never. Run the
    # real loaders for every policy that parsed, and surface a
    # loader rejection as a posture problem at the same gate.
    for env_name, parsed_obj, loader in (
            (PRICING_POLICY_ENV, pricing, load_pricing_policy),
            (GRANT_POLICY_ENV, grant, load_grant_policy),
            (LIMIT_POLICY_ENV, limit, load_limit_policy)):
        if isinstance(parsed_obj, dict):
            try:
                loader(environ)
            except PolicyError as error:
                problems.append(f"{env_name}: {error}")
    return problems


def enforce_policy_posture(environ=None):
    """Raise ValueError unless the policy posture is explicit.

    The CLI entry point has enforced this since batch 2c; the
    composition factories enforce it too (batch 2d, MEDIUM 4),
    so an ASGI/MCP composition assembled in code cannot bring
    the built-in defaults into a production deployment silently
    just because it never passed through the CLI."""
    problems = policy_posture_problems(environ)
    if problems:
        raise ValueError(
            "Operator policy posture incomplete: " + "; ".join(problems))


def enforce_policy_coherence(store, environ=None):
    """The policies IN EFFECT must be the policies the deployment
    environment NOW describes (batch 2e, R4; extended to every
    production constructor in batch 2g). Two snapshots can
    silently diverge from a fresh env load: the pricing module
    froze its policy at import time, and the store froze its
    grant/limit policies at construction. A host that imports or
    constructs before the service env is fully applied would
    then run production on stale money/exposure values while the
    env — and the posture check — say otherwise. Field-wise
    comparison against fresh loads; any divergence refuses the
    composition. Policy values themselves are never reported.

    Batch 2e ran this only in create_application; the direct
    constructors (auth.create_http_server, mcp_server's
    create_account_server) called posture alone, so the
    counterexample — import pricing under one env, then supply
    complete valid policies and construct the MCP server
    directly — kept the stale import-time rates (Astra batch2f
    diff review, R4). Every production gate now runs both."""
    from . import pricing
    held_pairs = (
        ("pricing", pricing._POLICY, load_pricing_policy(environ)),
        ("grant", store.grant_policy, load_grant_policy(environ)),
        ("limit", store.limit_policy, load_limit_policy(environ)),
    )
    for name, held, fresh in held_pairs:
        if vars(held) != vars(fresh):
            raise ValueError(
                f"the {name} policy in effect diverges from the "
                "deployment environment; refusing to start a "
                "production composition on stale policy values")


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
