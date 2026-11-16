"""Read-time authorization for exported memory snapshots; no state mutations."""

import json


def decision_evidence(raw_evidence, rationale):
    """Include legacy atomic-clause proof, previously stored only in rationale."""
    proof = json.loads(raw_evidence)
    if not isinstance(proof, list) or not proof:
        raise ValueError("Decision evidence required")
    try:
        details = json.loads(rationale)
    except ValueError, TypeError:
        details = None  # Plain-text human review rationale has no structured clauses.
    if isinstance(details, dict) and "clauses" in details:
        clauses = details["clauses"]
        if not isinstance(clauses, list) or not clauses:
            raise ValueError("Invalid stored clauses")
        for clause in clauses:
            if not isinstance(clause, dict) or clause.get("supported") is not True:
                raise ValueError("Accepted decision has unsupported clause")
            refs = clause.get("evidence")
            if not isinstance(refs, list) or not refs:
                raise ValueError("Missing clause evidence")
            proof.extend(refs)
    if any(
        not isinstance(ref, dict)
        or not isinstance(ref.get("quote"), str)
        or not ref["quote"].strip()
        or not isinstance(ref.get("msg_id"), str)
        or not ref["msg_id"].isascii()
        or not ref["msg_id"].isdigit()
        or len(ref["msg_id"]) > 19
        or not 0 < int(ref["msg_id"]) <= 2**63 - 1
        for ref in proof
    ):
        raise ValueError("Invalid stored decision evidence")
    return list({(ref["msg_id"], ref["quote"]): ref for ref in proof}.values())


def accepted_review_evidence(db, fact):
    """Return current decision proof only if the snapshot still matches its ledger."""
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('memory_candidates','memory_decisions')"
        )
    }
    if len(tables) != 2:
        return None
    current = db.execute(
        "SELECT subject,category,claim,evidence,status FROM memory_candidates WHERE fact_id=?",
        (fact.get("id"),),
    ).fetchone()
    if current is None or current[4] != "accepted":
        return None
    if any(fact.get(key) != current[i] for i, key in enumerate(("subject", "category", "claim"))):
        return None
    try:
        if json.loads(current[3]) != fact.get("evidence"):
            return None
        decision = db.execute(
            "SELECT decision,evidence,rationale FROM memory_decisions WHERE fact_id=? "
            "ORDER BY decision_id DESC LIMIT 1",
            (fact["id"],),
        ).fetchone()
        if decision is None or decision[0] != "accepted":
            return None
        return decision_evidence(decision[1], decision[2])
    except ValueError, TypeError:
        return None
