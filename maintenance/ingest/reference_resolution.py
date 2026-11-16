"""Exact reference-key lookup; never fuzzy text or nearest-time matching."""

from collections import defaultdict


def build_index(rows):
    """Rows belong to one already-filtered conversation: id, uid, seq, timestamp."""
    index = defaultdict(set)
    for identifier, sender_uid, sequence, timestamp in rows:
        if sender_uid and sequence and timestamp:
            index[(str(sender_uid), str(sequence), int(timestamp))].add(str(identifier))
    return index


def resolve_reference(reference, index):
    uid, sequence, timestamp = (reference.get(k) for k in ("sender_uid", "sequence", "timestamp"))
    if not uid or not sequence or not timestamp:
        return {"status": "incomplete_key"}
    candidates = index.get((str(uid), str(sequence), int(timestamp)), set())
    if not candidates:
        return {"status": "not_found"}
    if len(candidates) != 1:
        return {"status": "ambiguous"}
    identifier = next(iter(candidates))
    if reference.get("msg_id") is not None and reference["msg_id"] != identifier:
        return {"status": "conflicting_explicit_id"}
    return {"status": "unique", "msg_id": identifier}


def enrich_reply(reply, index):
    """Keep explicit IDs immutable; add independently traceable resolution."""
    references = []
    for reference in reply["references"]:
        resolution = resolve_reference(reference, index)
        references.append(
            {
                **reference,
                "resolution_status": resolution["status"],
                "resolved_msg_id": resolution.get("msg_id"),
                "resolution_method": "conversation_uid_sequence_exact_timestamp_v1",
            }
        )
    return {**reply, "references": references}
