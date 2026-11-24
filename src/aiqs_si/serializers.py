"""Public-boundary serializers (OSS plan v4 section 6).

Every job-shaped value that leaves through the public MCP boundary
passes through public_job_view, an ALLOWLIST projection: fields not
listed here do not leave, however the stored job evolves. In
particular the worker's raw scan result never leaves as-is — entry
text and field names are resolved from the trusted server-side
catalog pinned by the job's catalog_digest (never from the
worker-returned payload), and resonance internals (qrs, group,
total_mfs, selected_mfs, threshold, limits, catalog_digest, storage
paths, media URLs, descriptions) have no path out.

The scalar fields that DO leave (mode, reason, counts, flags) pass
through the strict public result schema (result_schema): mode and
reason are fixed vocabularies, counts are bounded integers, flags
are booleans. A worker result carrying a nested object or a
free-form diagnostic string in one of those fields publishes
nothing for that field.
"""
from .result_schema import (MAX_SCAN_ENTRIES, PUBLIC_SCAN_ENTRIES,
                            public_bool, public_count, public_entry_id,
                            public_mode, public_rank, public_reason)


def _int_or_none(value):
    return value if type(value) is int else None


def _public_entries(store, payload, raw_entries):
    digest = payload.get("catalog_digest")
    index = {}
    if isinstance(digest, str):
        try:
            catalog = store.get_catalog(digest)
        except (ValueError, KeyError):
            catalog = None
        if catalog:
            for field_name, entries in catalog.items():
                for entry in entries:
                    if isinstance(entry, dict) and public_entry_id(entry.get("id")) is not None:
                        index[entry["id"]] = (field_name, entry.get("text"))
    public = []
    if isinstance(raw_entries, list):
        seen = set()
        for raw in raw_entries[:MAX_SCAN_ENTRIES]:
            if not isinstance(raw, dict):
                continue
            # A malformed identifier (list, dict, oversized string)
            # never reaches the index lookup: it would be unhashable
            # or unresolvable, and a legacy row must not poison the
            # read of an otherwise valid scan.
            entry_id = public_entry_id(raw.get("id") or raw.get("entry_id"))
            if entry_id is None:
                continue
            # Each entry id publishes at most once (the catalog index
            # resolves an id to exactly one field/text pair, so a
            # repetition could only multiply the hits/rank channel).
            if entry_id in seen:
                continue
            seen.add(entry_id)
            resolved = index.get(entry_id)
            if resolved is None:
                # Unresolvable against the pinned catalog: the entry
                # is dropped rather than serving worker-supplied text.
                continue
            field_name, text = resolved
            public.append({"entry_id": entry_id, "field_name": field_name,
                           "text": text, "hits": public_count(raw.get("hits")),
                           "rank": public_rank(raw.get("rank"))})
    public.sort(key=lambda entry: (entry["rank"] is None, entry["rank"]))
    # Publication is bounded by the engine contract, and a rank is
    # only meaningful within the list actually published: a rank
    # beyond the published length (reachable when duplicate or
    # excess entries padded the stored list) publishes as None.
    public = public[:PUBLIC_SCAN_ENTRIES]
    for entry in public:
        if entry["rank"] is not None and entry["rank"] > len(public):
            entry["rank"] = None
    return public


def _public_result(store, job, payload, result):
    view = {}
    mode = public_mode(result.get("mode"))
    if mode is not None:
        view["mode"] = mode
    verified = public_bool(result.get("physical_output_verified"))
    if verified is not None:
        # The server never verifies physical output (batch 2d,
        # MEDIUM 5): a stored True — legacy row or worker claim —
        # is not a claim the server can stand behind, and the
        # execution proof has always published False for exactly
        # this field. Publishing the raw True here made result
        # and proof contradict each other.
        view["physical_output_verified"] = False
    reason = public_reason(result.get("reason"))
    if reason is not None:
        view["reason"] = reason
    operation = payload.get("operation")
    if operation == "scan":
        scan = result.get("scan")
        scan = scan if isinstance(scan, dict) else {}
        view["scan"] = {
            "final_entries": _public_entries(store, payload,
                                             scan.get("final_entries")),
            "observer_coordinate_applied":
                public_bool(scan.get("observer_coordinate_applied")) is True,
        }
    elif operation == "irradiation":
        # Counts are derived from the authoritative records where
        # they exist (batch 2d, MEDIUM 5): the requested count is
        # the reservation in the payload, the completed count is
        # the server-tracked execution progress. The worker's own
        # figures remain only as a fallback for rows that predate
        # those records, sanitized by the schema as before.
        execution = job.get("execution")
        execution = execution if isinstance(execution, dict) else {}
        authoritative = {
            "requested_count": public_count(payload.get("irradiation_count")),
            "completed_count": public_count(execution.get("completed_count")),
        }
        for key in ("requested_count", "completed_count", "simulated_count"):
            count = authoritative.get(key)
            if count is None:
                count = public_count(result.get(key))
            if count is not None:
                view[key] = count
        for key in ("stop_command_acknowledged", "executed_physically"):
            flag = public_bool(result.get(key))
            if flag is not None:
                view[key] = flag
    return view


def public_job_view(store, job):
    """The public DTO for one job (MCP job_result / cancel_job /
    scan / irradiate responses)."""
    payload = job.get("payload") or {}
    view = {"id": job["id"], "state": job["state"], "cost": job["cost"],
            "operation": payload.get("operation")}
    if isinstance(payload.get("input_mode"), str):
        view["input_mode"] = payload["input_mode"]
    if payload.get("operation") == "irradiation":
        view["irradiation_count"] = _int_or_none(payload.get("irradiation_count"))
    execution = job.get("execution")
    if isinstance(execution, dict):
        view["execution"] = {
            "completed_count": _int_or_none(execution.get("completed_count")),
            "stop_requested": bool(execution.get("stop_requested")),
        }
    result = job.get("result")
    if isinstance(result, dict):
        view["result"] = _public_result(store, job, payload, result)
    return view
