"""Server-side duration estimates for the app API (foundation item 3).

The estimate is a RANGE in seconds for one scan or one irradiation,
including the wait behind the jobs already occupying the single
device lane. It exists so the app's confirmation screens can say
"about this long, queue wait included" with a width, never a point
promise.

The computation rests on three bases, and returns None when any is
missing — an invented figure would break the same honesty rule as
a simulated progress display (the UI mirrors the real device,
never a model of it):

1. Operator-measured effective durations (the estimate policy,
   policies.load_estimate_policy). NOT the device setting stamped
   into job payloads: a shot's effective end-to-end
   duration on the lane exceeds its configured device seconds. The client
   never receives the setting, and no estimate is ever derived
   from it.
2. The durable lane state (Store.lane_estimate_basis): every
   queued, running or unresolved job ahead, in claim order, each
   costed at the same effective durations.
3. Worker presence (Store.worker_last_seen): an authorized worker
   completes a claim poll every couple of seconds while idle and
   sends execution heartbeats while a job runs; only SUCCESSFUL
   operations record contact, so a worker that can never claim
   (misconfigured, failing) proves nothing. Without a fresh
   contact the lane is not known to be moving at all — a worker
   that is offline makes any finite completion figure a fiction —
   so a missing or stale contact removes the basis entirely.

Why not statistics over past jobs: the store keeps no durable
claim-start timestamp (execution_liveness.seen_at is overwritten by
heartbeats, and worker_claims stores only an expiry), so a
started-to-finished duration cannot be computed honestly from
stored data. The measured policy constants — reviewed against the
worker host's local execution log — are the maintainable basis
instead.

Deliberate conservatism, all of it documented for the UI contract:
- A running job ahead contributes NOTHING to the range's lower
  bound and its FULL duration to the upper bound: its start time
  is not durably known (see above), so its remaining time lies
  anywhere in [0, full]. Queued jobs ahead have not started, so
  their full duration counts toward both bounds.
- A job in state 'unknown' anywhere in the store means the lane
  is blocked for reconciliation (claim_next refuses to claim ANY
  job while one exists — the block is global, not positional), so
  the wait is unbounded and no estimate exists.
- A lane job whose payload no longer decodes, or whose operation /
  shot count cannot be read, removes the basis: no estimate.
- An operation whose policy seconds are unset has no estimate,
  and one unset operation ahead also removes the basis for the
  operations behind it. An unmeasured request answers before any
  store read, so the dark (unconfigured) feature costs nothing.
- An estimate computed for an existing job is given only while
  that job is still queued: once it is running, its own remaining
  time cannot be bounded (same start-time problem), and once it is
  terminal there is nothing left to estimate.
- If rounding collapses the range to a point, no estimate is
  returned — a point value would read as a promise.
"""
import math
import time

from .policies import load_estimate_policy
from .pricing import MAX_SHOTS

_POLICY = load_estimate_policy()

_OPERATIONS = ("scan", "irradiation")

# The worker loop polls the claim queue every couple of seconds and
# the store throttles contact writes to one per 30 s; three minutes
# without contact means the worker is gone, not merely slow.
WORKER_PRESENCE_WINDOW_SEC = 180


def current_policy():
    """The process-wide estimate policy (env-loaded, like pricing)."""
    return _POLICY


def _own_seconds(policy, operation, count):
    """Effective seconds for one job, or None when unmeasured —
    including a lane entry whose operation or shot count could not
    be read (the store marks those None): an unreadable job ahead
    removes the basis rather than being silently costed as zero."""
    if operation == "scan":
        return policy.scan_seconds
    if operation != "irradiation" or type(count) is not int:
        return None
    per_shot = policy.irradiation_seconds_per_shot
    if per_shot is None:
        return None
    return per_shot * count


def estimate_from_lane(policy, operation, count, ahead):
    """The pure computation over lane entries.

    `ahead` entries are Store lane entries ({state, operation,
    irradiation_count}); the caller supplies either the whole lane
    (a job not yet submitted) or only the entries ahead of an
    existing job. Returns the estimate dict or None per the module
    docstring's rules."""
    own = _own_seconds(policy, operation, count)
    if own is None:
        return None
    low_nominal = own
    high_nominal = own
    for entry in ahead:
        if entry["state"] == "unknown":
            return None
        if entry["operation"] == "irradiation" and (
                type(entry["irradiation_count"]) is not int
                or not 1 <= entry["irradiation_count"] <= MAX_SHOTS):
            # A stored count the current submission path could
            # never have written: the payload is not a basis.
            return None
        entry_seconds = _own_seconds(policy, entry["operation"],
                                     entry["irradiation_count"])
        if entry_seconds is None:
            return None
        high_nominal += entry_seconds
        if entry["state"] != "running":
            # A running job may finish at any moment; only its
            # full duration is certain to bound the wait.
            low_nominal += entry_seconds
    min_seconds = math.floor(low_nominal * policy.range_low_factor)
    max_seconds = math.ceil(high_nominal * policy.range_high_factor)
    if max_seconds <= min_seconds:
        # A collapsed range would read as a point promise.
        return None
    return {"min_seconds": min_seconds,
            "max_seconds": max_seconds,
            "includes_queue_wait": True,
            "jobs_ahead": len(ahead)}


def duration_estimate(store, operation, count=None, *, policy=None,
                      before_job_id=None, owner=None):
    """The estimate for one prospective or existing job.

    With before_job_id/owner, the lane basis is the jobs ahead of
    that existing job (owner-validated by the store, KeyError like
    queue_status) and an estimate is returned only while that job
    is still queued; without them it is the whole current lane, for
    a job not yet submitted. Raises ValueError for an unknown
    operation or an out-of-range irradiation count — input errors,
    distinct from a missing basis, which answers None."""
    if policy is None:
        policy = _POLICY
    if operation not in _OPERATIONS:
        raise ValueError("Unknown operation for duration estimate")
    if operation == "irradiation" and (
            type(count) is not int or not 1 <= count <= MAX_SHOTS):
        raise ValueError(
            f"Irradiation count must be an integer from 1 to {MAX_SHOTS}")
    if _own_seconds(policy, operation, count) is None:
        # Unmeasured operation: no basis, and no store reads at
        # all — the unconfigured feature stays free.
        return None
    last_seen = store.worker_last_seen()
    if last_seen is None:
        # No worker has ever completed a claim or heartbeat; a
        # finite completion figure would be invented.
        return None
    age = time.time() - last_seen
    if age < 0 or age > WORKER_PRESENCE_WINDOW_SEC:
        # Stale contact means the worker is gone. A future contact
        # timestamp means the wall clock moved under the record, so
        # its age cannot be certified either.
        return None
    basis = store.lane_estimate_basis(owner=owner, job_id=before_job_id)
    if basis["blocked"] or basis["over_budget"]:
        # claim_next refuses every claim while an unresolved
        # execution exists anywhere; the wait is unbounded even
        # for a job positioned ahead of it.
        return None
    if before_job_id is not None and basis["target_state"] != "queued":
        return None
    return estimate_from_lane(policy, operation, count, basis["entries"])
