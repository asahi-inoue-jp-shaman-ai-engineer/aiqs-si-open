import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from aiqs_si.catalog import load_snapshot


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.json"
        self.payload = {"schema": "aiqs-si.catalog.v1",
                        "provenance": {"source": "synthetic-test", "version": "1", "category_id": "test"},
                        "fields": [{"name": "b", "entries": [{"id": "2", "text": "second"},
                                                                 {"id": "1", "text": "first"}]},
                                   {"name": "a", "entries": []}]}

    def save(self):
        self.path.write_text(json.dumps(self.payload), encoding="utf-8")
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def test_order_and_digest(self):
        digest = self.save()
        catalog, proof = load_snapshot(self.path, digest)
        self.assertEqual(list(catalog), ["b", "a"])
        self.assertEqual([e["id"] for e in catalog["b"]], ["2", "1"])
        self.assertEqual(proof["entry_count"], 2)
        with self.assertRaises(ValueError):
            load_snapshot(self.path, "0" * 64)

    def test_duplicate_id_rejected(self):
        self.payload["fields"][1]["entries"] = [{"id": "2", "text": "collision"}]
        with self.assertRaises(ValueError):
            load_snapshot(self.path, self.save())

    def test_missing_provenance_rejected(self):
        del self.payload["provenance"]["source"]
        with self.assertRaises(ValueError):
            load_snapshot(self.path, self.save())
