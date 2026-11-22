"""Load an explicitly pinned master snapshot without network/database access."""
import hashlib
import json
from pathlib import Path


def load_snapshot(path, expected_sha256):
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("Catalog snapshot hash mismatch")
    snapshot = json.loads(raw)
    if snapshot.get("schema") != "aiqs-si.catalog.v1":
        raise ValueError("Unsupported catalog snapshot schema")
    provenance = snapshot.get("provenance")
    if not isinstance(provenance, dict) or not all(
        isinstance(provenance.get(key), str) and provenance[key].strip()
        for key in ("source", "version", "category_id")
    ):
        raise ValueError("Catalog provenance is required")
    fields = snapshot.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("Catalog has no fields")
    catalog = {}
    ids = set()
    count = 0
    for field in fields:
        name, entries = field.get("name"), field.get("entries")
        if not isinstance(name, str) or not name or name in catalog:
            raise ValueError("Invalid or duplicate field name")
        if not isinstance(entries, list):
            raise ValueError("Entries must be an ordered list")
        for entry in entries:
            identity = entry.get("id")
            if not isinstance(identity, str) or not identity or identity in ids:
                raise ValueError("Missing or duplicate catalog entry ID")
            if not isinstance(entry.get("text"), str) or not entry["text"]:
                raise ValueError("Catalog entry text is required")
            ids.add(identity)
            count += 1
        # Preserve snapshot order exactly; do not translate, enrich or sort here.
        catalog[name] = entries
    if not count:
        raise ValueError("Catalog has no entries")
    return catalog, {"sha256": digest, "field_count": len(catalog),
                     "entry_count": count, "provenance": provenance}
