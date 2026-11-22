"""Synthetic device — PUBLIC STUB.

A deterministic stand-in for the production device, used by the
open-source shell's embedded test worker and local runner. Its
scan performs NO resonance matching: it returns the first catalog
entries in order with zero hits, explicitly marked
``score_kind="stub"``, so stub output can never be mistaken for a
real ranking (independence requirement: the stub's ordering and
score shape deliberately do not mirror the production engine).
Irradiation is simulated locally and never touches hardware.
"""


class SyntheticDevice:
    mode = "synthetic"
    supports_execution_control = True

    def __init__(self):
        self.events = []

    def scan(self, catalog, *, observer_coordinate=None):
        self.events.append("scan_started")
        entries = []
        for _field_name, field_entries in (catalog or {}).items():
            for entry in field_entries:
                if isinstance(entry, dict) and entry.get("id") is not None:
                    entries.append({"id": entry["id"], "hits": 0,
                                    "rank": len(entries) + 1})
                if len(entries) >= 3:
                    break
            if len(entries) >= 3:
                break
        self.events.append("scan_finished")
        return {"score_kind": "stub",
                "observer_coordinate_applied": bool(observer_coordinate),
                "final_entries": entries}

    def irradiate(self, entries, *, count, control=None):
        completed = 0
        for number in range(1, count + 1):
            if control and control()["stop_requested"]:
                break
            completed = number
            if control:
                control(completed)
        self.events.extend(["synthetic_on", "synthetic_off"])
        return {"mode": self.mode, "executed_physically": False,
                "entry_ids": [entry["id"] for entry in entries],
                "stopped": True,
                "requested_count": count, "simulated_count": completed,
                "completed_count": completed,
                "stop_command_acknowledged": True}
