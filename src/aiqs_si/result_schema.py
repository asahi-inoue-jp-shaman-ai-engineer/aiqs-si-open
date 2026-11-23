"""Strict public schema for worker job results (Astra final round #2).

One schema shared by every surface that can publish a job result.
Store.finish validates a worker result BEFORE persistence
(fail-closed: a malformed result is rejected, never stored), and the
public serializer plus execution_proof sanitize through the same
helpers on the way out, so a row written before this schema existed
can never publish a nested object or a free-form string either.

Only the public-boundary fields are constrained here. A worker
result also carries private payload (scan bodies, shot records,
device observations); that content never leaves through these
helpers — the serializer resolves public entry text from the pinned
catalog, and the proof publishes only the scalar fields below.
"""

# The only execution modes a result may claim. The synthetic device
# and the diode device are the two implementations that exist.
MODE_VALUES = frozenset({"hardware", "synthetic"})

# The only reason codes a job result may carry. Worker failure codes
# come from worker.py (plus device_unavailable_before_execution, the
# worker-reported "failed before any execution" code the replay
# tests exercise); the rest are written by the store itself
# (liveness expiry, scan-receipt reconciliation, operator review).
REASON_VALUES = frozenset({
    "required_prayer_inputs_missing",
    "device_unavailable_before_execution",
    "device_mode_mismatch",
    "execution_settings_mismatch",
    "target_context_unavailable",
    "catalog_unavailable_or_invalid",
    "execution_outcome_unknown",
    "worker_contact_lost",
    "scan_result_delivery_unconfirmed",
    "operator_reconciled",
})

COUNT_KEYS = ("requested_count", "completed_count", "simulated_count")
BOOL_KEYS = ("stop_command_acknowledged", "executed_physically",
             "physical_output_verified", "stopped")
MAX_COUNT = 999999
# Scan results: the engine publishes at most 10 final entries
# (PUBLIC_SCAN_ENTRIES — the serializer never publishes more than
# the engine contract). The finish-time bound leaves an order of
# magnitude of headroom for producers while keeping the entry
# list, and every scalar in it, finitely sized.
# hits is a resonance count (bounded like every other count — an
# unbounded integer would be a covert channel: a whole catalog
# digest fits in one), rank is a 1-based position that cannot
# exceed the number of entries in its own list, and the serializer
# publishes each entry id at most once, so repetition cannot
# multiply the channel either.
MAX_SCAN_ENTRIES = 100
PUBLIC_SCAN_ENTRIES = 10
MAX_ENTRY_ID_LENGTH = 256


def _valid_count(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def _valid_rank(value, cap=None):
    return (type(value) is int and 1 <= value <= MAX_COUNT
            and (cap is None or value <= cap))


def _valid_entry_id(value):
    return ((isinstance(value, str) and 1 <= len(value) <= MAX_ENTRY_ID_LENGTH)
            or type(value) is int)


def public_mode(value):
    """The value if it is a known mode, else None."""
    return value if isinstance(value, str) and value in MODE_VALUES else None


def public_reason(value):
    """The value if it is a known reason code, else None."""
    return value if isinstance(value, str) and value in REASON_VALUES else None


def public_count(value):
    """The value if it is a bounded integer count, else None."""
    return value if _valid_count(value) else None


def public_bool(value):
    """The value if it is a boolean, else None."""
    return value if type(value) is bool else None


def public_rank(value, cap=None):
    """The value if it is a bounded 1-based rank, else None. When
    the size of the entry list it belongs to is known, a rank
    beyond that size is meaningless and also publishes as None."""
    return value if _valid_rank(value, cap) else None


def public_entry_id(value):
    """The value if it is a usable entry identifier, else None."""
    return value if _valid_entry_id(value) else None


def validate_worker_result(result):
    """Raise ValueError unless every public-schema field present in
    a worker-submitted result is well-typed. Called by Store._finish
    before anything is persisted."""
    if not isinstance(result, dict):
        raise ValueError("Job result must be an object")
    if "mode" in result and public_mode(result["mode"]) is None:
        raise ValueError("Job result mode is not a known mode")
    if "reason" in result and public_reason(result["reason"]) is None:
        raise ValueError("Job result reason is not a known reason code")
    for key in COUNT_KEYS:
        if key in result and not _valid_count(result[key]):
            raise ValueError(f"Job result {key} must be a bounded integer count")
    for key in BOOL_KEYS:
        if key in result and type(result[key]) is not bool:
            raise ValueError(f"Job result {key} must be a boolean")
    if "scan" in result:
        scan = result["scan"]
        if not isinstance(scan, dict):
            raise ValueError("Job result scan must be an object")
        if ("observer_coordinate_applied" in scan
                and type(scan["observer_coordinate_applied"]) is not bool):
            raise ValueError(
                "Job result scan observer_coordinate_applied must be a boolean")
        entries = scan.get("final_entries")
        if entries is not None:
            if not isinstance(entries, list) or len(entries) > MAX_SCAN_ENTRIES:
                raise ValueError("Job result scan entries must be a bounded list")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("Job result scan entry must be an object")
                entry_id = entry.get("id", entry.get("entry_id"))
                if entry_id is not None and not _valid_entry_id(entry_id):
                    raise ValueError("Job result scan entry id is not a valid identifier")
                if "hits" in entry and not _valid_count(entry["hits"]):
                    raise ValueError("Job result scan entry hits must be a bounded count")
                if "rank" in entry and not _valid_rank(entry["rank"], cap=len(entries)):
                    raise ValueError("Job result scan entry rank must be a bounded rank")
    if "catalog_digest" in result and not (
            isinstance(result["catalog_digest"], str)
            and 1 <= len(result["catalog_digest"]) <= 128):
        raise ValueError("Job result catalog_digest must be a bounded string")
    if "intention_usage" in result and result["intention_usage"] != "recorded_only":
        raise ValueError("Job result intention_usage is not a known value")
    if "entry_ids" in result and not isinstance(result["entry_ids"], list):
        raise ValueError("Job result entry_ids must be a list")
