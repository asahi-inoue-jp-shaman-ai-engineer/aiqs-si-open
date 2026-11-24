"""Regression tests for the publication order of scan entries
(ranking backfill + A8 fix, owner spec 2026-11-24).

The published ranking IS the ranking: QRS descending, ties
broken by hits descending, then by the entry id alone
(type-discriminated, so the integer 1 and the string "1" are
both valid ids and must never tie). The order is total and,
over distinct entry ids, independent of the stored order
within the first-100 projection (the bounded prefix this
serializer has always applied). The QRS value itself never
publishes — the published fields and values are unchanged
(rank stays the per-field provenance) — but relative QRS
standing is visible through the order, by the owner's ruling:
the ranking's meaning is QRS order. (An earlier same-day A8
draft ordered by hits alone, on the premise that order must
not disclose relations between unpublished QRS values —
Astra backfill + A8 review round 1, item 4. The owner
superseded that premise: sort by QRS descending, take the
top ten, show them in that order; within one QRS, prefer
more hits.)

A row whose QRS does not read as an integer (legacy rows
predate the field; malformed values are not schema-validated)
sorts below every row with a known QRS, and inside that band
the order is hits descending — so a QRS-less legacy result
publishes in exactly the earlier A8 hits order. Within any
band, an entry whose hits do not publish (None) sorts below
every entry with a known count.

Deduplication (first occurrence wins — the long-standing
anti-covert-channel rule) and the bounded projection (first
100 stored rows, cap of 10) are unchanged. For stored results
with more than ten distinct resolvable entries — the engine
emits at most ten, but finish validation accepts up to 100
rows, so this covers any such stored result, including a
currently valid worker submission — the published and
executed set is the top ten BY the publication order
(decision recorded in the A8 round-2 review materials:
membership preservation and a ranking-based cap cannot both
hold there, and the owner's reading of the ranking governs).
canonical_scan_entries inherits the order, so irradiation
executes the selection in the order it was shown
(display = selection = execution).
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiqs_si.store import Store
from aiqs_si.result_schema import validate_worker_result
from aiqs_si.serializers import canonical_scan_entries, public_job_view


class PublicationOrderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "queue.sqlite")
        self.owner = self.store.create_test_account(5000)

    def legacy_scan(self, key, catalog, final_entries):
        """Persist a completed scan row directly (the shape of a
        stored worker result) and return its public view entries."""
        digest = self.store.save_catalog(catalog)
        job = self.store.submit(self.owner, key, {"operation": "scan"}, cost=0)
        payload = {"operation": "scan", "catalog_digest": digest}
        result = {"mode": "synthetic",
                  "scan": {"observer_coordinate_applied": False,
                           "final_entries": final_entries}}
        db = sqlite3.connect(self.store.path)
        db.execute("UPDATE jobs SET state='completed', payload=?, result=? "
                   "WHERE id=?",
                   (json.dumps(payload), json.dumps(result), job["id"]))
        db.commit()
        db.close()
        view = public_job_view(self.store, self.store.get(self.owner, job["id"]))
        return view["result"]["scan"]["final_entries"]

    def test_backfill_mix_publishes_qrs_descending_then_hits(self):
        # Stored in engine order: two adopted rows (qrs 150) and
        # two backfilled rows (qrs 100, 33). QRS governs: inside
        # the 150 band the hits tie-break puts b1 (130) over a1
        # (40), and c1 (125 hits but qrs 100) stays below BOTH
        # 150s — the published hits column is deliberately not
        # monotone. Under the old rank sort this published as
        # a1, c1, b2, b1; under the hits-only A8 draft as
        # b1, c1, b2, a1.
        catalog = {"fa": [{"id": "a1", "text": "A1"}],
                   "fb": [{"id": "b1", "text": "B1"}, {"id": "b2", "text": "B2"}],
                   "fc": [{"id": "c1", "text": "C1"}]}
        stored = [
            {"id": "a1", "hits": 40, "rank": 1, "qrs": 150},
            {"id": "b1", "hits": 130, "rank": 2, "qrs": 150},
            {"id": "c1", "hits": 125, "rank": 1, "qrs": 100},
            {"id": "b2", "hits": 110, "rank": 1, "qrs": 33},
        ]
        entries = self.legacy_scan("mix", catalog, stored)
        self.assertEqual([e["entry_id"] for e in entries],
                         ["b1", "a1", "c1", "b2"])
        self.assertEqual([e["hits"] for e in entries], [130, 40, 125, 110])
        # Values are the worker's own (rank stays per-field
        # provenance) and no resonance internal leaks into the
        # published shape — QRS orders the list but never appears.
        self.assertEqual([e["rank"] for e in entries], [2, 1, 1, 1])
        for entry in entries:
            self.assertEqual(set(entry),
                             {"entry_id", "field_name", "text", "hits", "rank"})

    def test_qrs_ties_break_by_hits_descending(self):
        # Owner spec: within one QRS, the entry with more hits
        # ranks higher — however the rows were stored.
        catalog = {"fa": [{"id": "x1", "text": "X1"}, {"id": "x2", "text": "X2"},
                          {"id": "x3", "text": "X3"}]}
        rows = [
            {"id": "x1", "hits": 10, "rank": 1, "qrs": 100},
            {"id": "x2", "hits": 30, "rank": 1, "qrs": 100},
            {"id": "x3", "hits": 20, "rank": 1, "qrs": 100},
        ]
        expected = ["x2", "x3", "x1"]
        first = self.legacy_scan("qtie-a", catalog, rows)
        second = self.legacy_scan("qtie-b", catalog, list(reversed(rows)))
        self.assertEqual([e["entry_id"] for e in first], expected)
        self.assertEqual([e["entry_id"] for e in second], expected)

    def test_full_ties_fall_to_entry_id_and_are_stored_order_independent(self):
        catalog = {"fa": [{"id": "m0", "text": "M0"}, {"id": "m1", "text": "M1"},
                          {"id": "m2", "text": "M2"}, {"id": "m3", "text": "M3"}]}
        rows = [
            {"id": "m2", "hits": 100, "rank": 1, "qrs": 100},
            {"id": "m0", "hits": 100, "rank": 1, "qrs": 100},
            {"id": "m3", "hits": 100, "rank": 1, "qrs": 100},
            {"id": "m1", "hits": 100, "rank": 1, "qrs": 100},
        ]
        # Equal QRS and equal hits: entry_id ascending — the
        # same published order however the rows were stored.
        expected = ["m0", "m1", "m2", "m3"]
        first = self.legacy_scan("ties-a", catalog, rows)
        second = self.legacy_scan("ties-b", catalog, list(reversed(rows)))
        self.assertEqual([e["entry_id"] for e in first], expected)
        self.assertEqual([e["entry_id"] for e in second], expected)

    def test_qrs_swap_changes_the_published_order(self):
        # The mirror of the superseded A8 invariance test, kept
        # deliberately: with ids, hits and ranks fixed, swapping
        # only the QRS values swaps the published order. Under
        # the owner's spec this disclosure-through-order is the
        # ranking working as intended, not a leak.
        catalog = {"fa": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]}
        left = [{"id": "a", "hits": 100, "rank": 2, "qrs": 25},
                {"id": "b", "hits": 100, "rank": 2, "qrs": 50}]
        right = [{"id": "a", "hits": 100, "rank": 2, "qrs": 50},
                 {"id": "b", "hits": 100, "rank": 2, "qrs": 25}]
        first = self.legacy_scan("qrs-left", catalog, left)
        second = self.legacy_scan("qrs-right", catalog, right)
        self.assertEqual([e["entry_id"] for e in first], ["b", "a"])
        self.assertEqual([e["entry_id"] for e in second], ["a", "b"])

    def test_mixed_type_ids_have_a_total_order(self):
        # Astra round 1, counterexample C: the integer 1 and the
        # string "1" are distinct, both-valid ids whose string
        # forms collide; without a type discriminator their order
        # would follow the stored order.
        catalog = {"fa": [{"id": 1, "text": "int one"},
                          {"id": "1", "text": "str one"}]}
        rows = [{"id": 1, "hits": 100, "rank": 1, "qrs": 150},
                {"id": "1", "hits": 100, "rank": 1, "qrs": 150}]
        first = self.legacy_scan("mixed-a", catalog, rows)
        second = self.legacy_scan("mixed-b", catalog, list(reversed(rows)))
        self.assertEqual([e["entry_id"] for e in first], [1, "1"])
        self.assertEqual([e["entry_id"] for e in second], [1, "1"])

    def test_excess_rows_publish_the_top_ten_by_the_ranking_order(self):
        # For a stored result with more than ten distinct
        # resolvable entries, the published set is the top ten
        # by the publication order — and the canonical execution
        # set is the same set in the same order. The lone
        # qrs-150 row takes the first slot despite zero hits;
        # the weakest row of the qrs-100 band (e01) is the one
        # cut, not the zero-hit row.
        catalog = {"field": [{"id": "top", "text": "Top"}] +
                            [{"id": f"e{n:02}", "text": f"E{n}"}
                             for n in range(1, 11)]}
        stored = [{"id": "top", "hits": 0, "rank": 1, "qrs": 150}] + \
                 [{"id": f"e{n:02}", "hits": n, "rank": 1, "qrs": 100}
                  for n in range(1, 11)]
        entries = self.legacy_scan("excess", catalog, stored)
        expected = ["top"] + [f"e{n:02}" for n in range(10, 1, -1)]
        self.assertEqual([e["entry_id"] for e in entries], expected)
        digest = self.store.save_catalog(catalog)
        canonical = canonical_scan_entries(
            self.store, {"operation": "scan", "catalog_digest": digest}, stored)
        self.assertEqual([e["id"] for e in canonical], expected)

    def test_conflicting_duplicates_publish_the_first_occurrence(self):
        # The duplicate policy predates A8 and is retained by
        # decision: a repeated id publishes once, with its FIRST
        # occurrence's values (a later value never leaves — the
        # anti-covert-channel rule). A maximum-value policy would
        # be permutation-invariant, but it would publish a later
        # occurrence's value and so violate that mandate; the
        # mandate is the reason for the decision (Astra round 2
        # correction). Permutation invariance is therefore
        # claimed over distinct-id lists, not over conflicting
        # duplicates.
        catalog = {"fa": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]}
        stored = [{"id": "a", "hits": 1, "rank": 1, "qrs": 150},
                  {"id": "b", "hits": 50, "rank": 1, "qrs": 150},
                  {"id": "a", "hits": 100, "rank": 1, "qrs": 150}]
        entries = self.legacy_scan("dupe", catalog, stored)
        self.assertEqual([(e["entry_id"], e["hits"]) for e in entries],
                         [("b", 50), ("a", 1)])

    def test_adopted_only_membership_and_values_unchanged(self):
        # Adopted-only result (every row at the threshold): the
        # fix changes only the order where the stored order
        # disagreed with the ranking — membership and values are
        # exactly the stored ones.
        catalog = {"fa": [{"id": "q1", "text": "Q1"}],
                   "fb": [{"id": "q2", "text": "Q2"}]}
        stored = [
            {"id": "q1", "hits": 60, "rank": 1, "qrs": 150},
            {"id": "q2", "hits": 140, "rank": 2, "qrs": 150},
        ]
        entries = self.legacy_scan("adopted", catalog, stored)
        self.assertEqual([e["entry_id"] for e in entries], ["q2", "q1"])
        self.assertEqual({e["entry_id"]: (e["hits"], e["rank"]) for e in entries},
                         {"q1": (60, 1), "q2": (140, 2)})

    def test_adopted_only_already_ranking_ordered_is_unchanged(self):
        catalog = {"fa": [{"id": "p1", "text": "P1"}, {"id": "p3", "text": "P3"}],
                   "fb": [{"id": "p2", "text": "P2"}]}
        stored = [
            {"id": "p1", "hits": 128, "rank": 1, "qrs": 150},
            {"id": "p2", "hits": 121, "rank": 1, "qrs": 150},
            {"id": "p3", "hits": 105, "rank": 2, "qrs": 150},
        ]
        entries = self.legacy_scan("adopted-ordered", catalog, stored)
        self.assertEqual([e["entry_id"] for e in entries], ["p1", "p2", "p3"])
        self.assertEqual([e["hits"] for e in entries], [128, 121, 105])

    def test_unpublishable_hits_sort_below_known_counts_within_a_band(self):
        catalog = {"field": [{"id": "one", "text": "First"},
                             {"id": "two", "text": "Second"}]}
        # Same QRS band: the entry whose hits do not publish
        # sorts below the entry with a known count.
        stored = [
            {"id": "one", "hits": int("ab" * 32, 16), "rank": 1, "qrs": 150},
            {"id": "two", "hits": 5, "rank": 2, "qrs": 150},
        ]
        entries = self.legacy_scan("poisoned-band", catalog, stored)
        self.assertEqual([e["entry_id"] for e in entries], ["two", "one"])
        self.assertIsNone(entries[1]["hits"])
        # Across bands, QRS governs first: the qrs-150 entry
        # leads even though its hits do not publish and the
        # qrs-50 entry has a known count.
        stored = [
            {"id": "one", "hits": int("ab" * 32, 16), "rank": 1, "qrs": 150},
            {"id": "two", "hits": 5, "rank": 2, "qrs": 50},
        ]
        entries = self.legacy_scan("poisoned-cross", catalog, stored)
        self.assertEqual([e["entry_id"] for e in entries], ["one", "two"])
        self.assertIsNone(entries[0]["hits"])
        self.assertEqual(entries[1]["hits"], 5)

    def test_malformed_qrs_sorts_below_known_qrs(self):
        # QRS is not schema-validated on stored rows: a string, a
        # NaN or a missing value must never break ordering — such
        # rows form the unknown band below every known QRS, in
        # hits order among themselves. The one row with a real
        # QRS (10, the lowest value here) still leads the list.
        catalog = {"fa": [{"id": "s1", "text": "S1"}, {"id": "s2", "text": "S2"},
                          {"id": "s3", "text": "S3"}, {"id": "s4", "text": "S4"}]}
        stored = [
            {"id": "s1", "hits": 100, "rank": 1, "qrs": "150"},
            {"id": "s2", "hits": 100, "rank": 1},
            {"id": "s3", "hits": 100, "rank": 1, "qrs": float("nan")},
            {"id": "s4", "hits": 100, "rank": 1, "qrs": 10},
        ]
        entries = self.legacy_scan("bad-qrs", catalog, stored)
        self.assertEqual([e["entry_id"] for e in entries],
                         ["s4", "s1", "s2", "s3"])

    def test_projection_boundary_at_one_hundred_rows(self):
        # Decision D, pinned (Astra round 2, item 6): the bounded
        # projection examines only the first 100 stored rows — a
        # prior anti-poisoning mandate. A resolvable row AT the
        # boundary publishes; the same row one place past it does
        # not, and the canonical execution projection agrees. The
        # reversed list publishing the winner is the expected
        # consequence of the prefix rule, not an objection to it.
        # Finish validation itself accepts 100 rows and rejects
        # 101, so rows past the prefix exist only in legacy data.
        catalog = {"field": [{"id": "winner", "text": "Winner"}]}
        junk = [{"id": f"missing-{i}", "hits": 0, "rank": 1, "qrs": 0}
                for i in range(100)]
        winner = {"id": "winner", "hits": 999, "rank": 1, "qrs": 150}
        at_boundary = junk[:99] + [winner]
        beyond_boundary = junk + [winner]
        validate_worker_result({"scan": {"final_entries": at_boundary}})
        with self.assertRaises(ValueError):
            validate_worker_result({"scan": {"final_entries": beyond_boundary}})
        entries = self.legacy_scan("boundary-in", catalog, at_boundary)
        self.assertEqual([e["entry_id"] for e in entries], ["winner"])
        entries = self.legacy_scan("boundary-out", catalog, beyond_boundary)
        self.assertEqual(entries, [])
        digest = self.store.save_catalog(catalog)
        self.assertEqual(canonical_scan_entries(
            self.store, {"operation": "scan", "catalog_digest": digest},
            beyond_boundary), [])
        entries = self.legacy_scan("boundary-rev", catalog,
                                   list(reversed(beyond_boundary)))
        self.assertEqual([e["entry_id"] for e in entries], ["winner"])

    def test_canonical_entries_follow_the_published_order(self):
        # Irradiation binds its execution input from the published
        # projection: the order it executes is the order shown —
        # QRS first, so the qrs-150 entry leads although the
        # qrs-100 entry carries more hits.
        catalog = {"fa": [{"id": "a1", "text": "A1"}],
                   "fb": [{"id": "b1", "text": "B1"}]}
        digest = self.store.save_catalog(catalog)
        stored = [
            {"id": "a1", "hits": 130, "rank": 2, "qrs": 100},
            {"id": "b1", "hits": 40, "rank": 1, "qrs": 150},
        ]
        canonical = canonical_scan_entries(
            self.store, {"operation": "scan", "catalog_digest": digest}, stored)
        self.assertEqual([e["id"] for e in canonical], ["b1", "a1"])


if __name__ == "__main__":
    unittest.main()
