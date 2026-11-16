import unittest

from maintenance.core.references import effective_target
from maintenance.ingest.reference_resolution import build_index, enrich_reply, resolve_reference


class ReferenceResolutionTests(unittest.TestCase):
    def test_enrichment_preserves_source_id_and_shared_gate_rejects_conflicts(self):
        index = build_index([(1, "a", 10, 100)])
        base = {"sender_uid": "a", "sequence": "10", "timestamp": 100, "msg_id": None}
        reply = {"text": "body", "references": [base, base | {"msg_id": "999"}]}
        enriched = enrich_reply(reply, index)
        first, conflict = enriched["references"]
        self.assertIsNone(first["msg_id"])
        self.assertEqual(effective_target(first), "1")
        self.assertEqual(conflict["msg_id"], "999")
        self.assertIsNone(effective_target(conflict))
        self.assertIsNone(effective_target(first | {"resolution_method": "unverified_guess"}))
        self.assertNotIn("resolved_msg_id", reply["references"][0])

    def test_key_needs_sender_sequence_and_exact_timestamp(self):
        index = build_index([(1, "a", 10, 100), (2, "b", 10, 100), (3, "a", 10, 101)])
        ref = {"sender_uid": "a", "sequence": "10", "timestamp": 100}
        self.assertEqual(resolve_reference(ref, index), {"status": "unique", "msg_id": "1"})
        self.assertEqual(resolve_reference(ref | {"timestamp": 102}, index)["status"], "not_found")
        self.assertEqual(resolve_reference(ref | {"sender_uid": None}, index)["status"], "incomplete_key")
        self.assertEqual(resolve_reference(ref | {"msg_id": "2"}, index)["status"], "conflicting_explicit_id")

    def test_collisions_are_not_resolved_and_ids_remain_exact(self):
        ref = {"sender_uid": "a", "sequence": "10", "timestamp": 100}
        index = build_index([(7512497993392950692, "a", 10, 100)])
        self.assertEqual(resolve_reference(ref, index)["msg_id"], "7512497993392950692")
        index = build_index([(1, "a", 10, 100), (2, "a", 10, 100)])
        self.assertEqual(resolve_reference(ref, index), {"status": "ambiguous"})
