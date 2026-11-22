"""Catalog persistence family of the Store (OSS plan v4, Phase A).

Split out of store.py along the plan's lines: the pinned resonance
catalog snapshots live here, the credit-grant family lives in
grants.py, and Store itself keeps accounts, jobs, claims, and
settlement. The mixin expects the host class to provide
``transaction()``; the public API and signatures are unchanged.
"""
import hashlib
import json


class CatalogStore:
    def save_catalog(self, catalog):
        # A list preserves field order even though object keys are canonicalized.
        fields = [{"name": name, "entries": entries} for name, entries in catalog.items()]
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO catalogs VALUES (?,?)", (digest, encoded))
        return digest

    def get_catalog(self, digest):
        with self.transaction() as db:
            row = db.execute("SELECT content FROM catalogs WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ValueError("Pinned catalog unavailable")
        if hashlib.sha256(row["content"].encode()).hexdigest() != digest:
            raise ValueError("Pinned catalog integrity failure")
        return {field["name"]: field["entries"] for field in json.loads(row["content"])}
