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
"""


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
                    if isinstance(entry, dict) and entry.get("id") is not None:
                        index[entry["id"]] = (field_name, entry.get("text"))
    public = []
    if isinstance(raw_entries, list):
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            entry_id = raw.get("id") or raw.get("entry_id")
            resolved = index.get(entry_id)
            if resolved is None:
                # Unresolvable against the pinned catalog: the entry
                # is dropped rather than serving worker-supplied text.
                continue
            field_name, text = resolved
            public.append({"entry_id": entry_id, "field_name": field_name,
                           "text": text, "hits": _int_or_none(raw.get("hits")),
                           "rank": _int_or_none(raw.get("rank"))})
    public.sort(key=lambda entry: (entry["rank"] is None, entry["rank"]))
    return public


def _public_result(store, payload, result):
    view = {}
    if isinstance(result.get("mode"), str):
        view["mode"] = result["mode"]
    if isinstance(result.get("physical_output_verified"), bool):
        view["physical_output_verified"] = result["physical_output_verified"]
    if isinstance(result.get("reason"), str):
        view["reason"] = result["reason"]
    operation = payload.get("operation")
    if operation == "scan":
        scan = result.get("scan")
        scan = scan if isinstance(scan, dict) else {}
        view["scan"] = {
            "final_entries": _public_entries(store, payload,
                                             scan.get("final_entries")),
            "observer_coordinate_applied": bool(
                scan.get("observer_coordinate_applied")),
        }
    elif operation == "irradiation":
        for key in ("requested_count", "completed_count", "simulated_count"):
            if type(result.get(key)) is int:
                view[key] = result[key]
        for key in ("stop_command_acknowledged", "executed_physically"):
            if isinstance(result.get(key), bool):
                view[key] = result[key]
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
        view["result"] = _public_result(store, payload, result)
    return view
