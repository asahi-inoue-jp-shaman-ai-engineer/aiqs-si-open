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
# hits is a draw count (bounded like every other count — an
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


def validate_result_semantics(result, *, operation, state=None,
                              reserved_count=None,
                              progress_completed=None):
    """Semantic binding (batch 2d, MEDIUM 5): the type schema
    above cannot see the job, so a well-typed result could still
    LIE — a one-shot irradiation claiming requested_count=999999,
    or physical_output_verified=true, a claim the server has no
    mechanism to verify (the genuine workers always report False;
    the proof bundle hardcodes False for the same reason). Called
    by Store._finish with the job's authoritative records: the
    reserved count from the job payload and the server-tracked
    execution progress.

    Rules:
    - physical_output_verified=true is always rejected: no
      server-side verification exists, so the claim is unprovable
      by construction.
    - Irradiation counts bind to the reservation: requested must
      equal it, completed/simulated must not exceed it, and a
      reported completed count must equal the server-tracked
      progress whenever progress was recorded.
    - Mode coherence: simulated shots exist only in synthetic
      mode; physical execution exists only in hardware mode.
    - Scan hits carry NO cycle-count bound. An earlier rule
      assumed an entry's hits are "the number of scan cycles in
      which it resonated"; the engine does not work that way.
      Entry-stage hits are draw counts: scan_pro makes N*cycles
      draws over a field's N entries and every draw lands on
      exactly one entry, so an entry's expected hits EQUAL the
      cycle count and the top entries — the ones a scan
      publishes — sit above it as a matter of course. The
      field-selection stage ratio-normalizes its scores upward
      on top of that, and core.calculate_percentage is a
      relative index that legitimately exceeds 100. Every one of
      the 27 historical real device scans (cycles=88) exceeded
      88 (peak hits 116-128), the glass post-release smoke scan
      returned hits of 101-118, and the post-rollback recovery
      scan returned 101-129, so the bound rejected every genuine
      scan finish (glass release post-release check, 2026-11-24)
      and was removed. Unlike the irradiation
      counts, hits have no server-side authoritative record to
      bind to; the finite bound that remains is the type
      schema's — hits must be a bounded integer (see
      validate_worker_result / MAX_COUNT), which is the
      anti-covert-channel measure.
    """
    if result.get("physical_output_verified") is True:
        raise ValueError(
            "Job result physical_output_verified cannot be claimed: "
            "the server has no physical-output verification mechanism")
    mode = result.get("mode")
    if operation == "irradiation" and reserved_count is not None:
        if ("requested_count" in result
                and result["requested_count"] != reserved_count):
            raise ValueError(
                "Job result requested_count does not match the reserved count")
        for key in ("completed_count", "simulated_count"):
            if key in result and result[key] > reserved_count:
                raise ValueError(
                    f"Job result {key} exceeds the reserved count")
        if (progress_completed is not None
                and "completed_count" in result
                and result["completed_count"] != progress_completed):
            raise ValueError(
                "Job result completed_count does not match recorded progress")
    if result.get("simulated_count"):
        if mode != "synthetic":
            raise ValueError(
                "Job result simulated_count requires synthetic mode")
    if result.get("executed_physically") is True and mode != "hardware":
        raise ValueError(
            "Job result executed_physically requires hardware mode")
    if operation == "irradiation" and progress_completed is not None:
        # Simulated execution is bound to the AUTHORITATIVE
        # progress, not merely to the reservation (round 3): a
        # cancelled finish claimed simulated shots the progress
        # record says never ran, while the settlement refunded
        # the whole reservation and the public view published
        # the phantom count. On a synthetic device every
        # completed shot is a simulated one, so the counts are
        # equal there, not just bounded.
        _simulated = result.get("simulated_count") or 0
        if _simulated > progress_completed:
            raise ValueError(
                "Job result simulated_count exceeds the recorded execution progress")
        if mode == "synthetic" and state in ("completed", "cancelled") \
                and _simulated != progress_completed:
            raise ValueError(
                "Job result simulated_count does not match the recorded execution progress")
    if operation == "irradiation" and state == "cancelled" \
            and progress_completed is None:
        # A cancelled finish with no progress record has no
        # authoritative execution to claim against.
        if result.get("completed_count") or result.get("simulated_count"):
            raise ValueError(
                "Job result for a cancelled irradiation without a "
                "progress record cannot claim executed shots")
    if operation == "irradiation" and state == "unknown" \
            and progress_completed is None:
        # An unknown finish is an UNCONFIRMED execution: with no
        # progress record there is no authoritative execution to
        # claim against (same reasoning as cancelled above), and
        # publishing the worker's figures would present the
        # reservation as executed shots the owner cannot verify.
        if result.get("completed_count") or result.get("simulated_count"):
            raise ValueError(
                "Job result for an unknown irradiation without a "
                "progress record cannot claim executed shots")
        if result.get("executed_physically") is True:
            raise ValueError(
                "Job result for an unknown irradiation without a "
                "progress record cannot claim physical execution")
    if operation == "irradiation" and progress_completed == 0 \
            and result.get("executed_physically") is True:
        # Zero recorded progress means nothing executed, whatever
        # the final state: a physical-execution claim would
        # publish an execution the settlement treats as never
        # having happened (the reservation is released in full).
        raise ValueError(
            "Job result with zero recorded execution progress "
            "cannot claim physical execution")
    if operation == "irradiation" and state == "failed":
        # Final-state coherence (round 2): a FAILED irradiation
        # is one the worker refused or could not start — the
        # genuine failure results carry only a reason. A failed
        # result that also claims executed shots would publish a
        # physical execution the settlement treats as never
        # having happened (the reservation is released in full).
        # Executions whose outcome is genuinely uncertain travel
        # as 'unknown', not 'failed'.
        if result.get("completed_count") or result.get("simulated_count"):
            raise ValueError(
                "Job result for a failed irradiation cannot claim executed shots")
        if result.get("executed_physically") is True:
            raise ValueError(
                "Job result for a failed irradiation cannot claim physical execution")
    if operation == "irradiation" and state == "completed" \
            and reserved_count is not None:
        # A completed irradiation certifies the reserved total:
        # with progress, the count must equal it (checked above);
        # without any progress record, a reported count below the
        # reservation — above all zero — must not take the
        # completed settlement path, and an absent count has
        # nothing to certify completion with.
        if progress_completed is None:
            if "completed_count" not in result:
                raise ValueError(
                    "Job result for a completed irradiation must report "
                    "its completed count when no progress record exists")
            if result["completed_count"] != reserved_count:
                raise ValueError(
                    "Job result completed_count does not match the reserved count")

