"""Final contract and cross-field checks; never infer a decision here."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .contracts import Contracts
from .specialists import SpecialistResult


def verify_output(
    output: dict[str, Any],
    case_id: str,
    results: list[SpecialistResult],
    contracts: Contracts,
) -> None:
    contracts.validate_output(output, f"outputs/{case_id}.json")
    if output["case_id"] != case_id or any(r.case_id != case_id for r in results):
        raise ValueError("Cross-case result")
    owned = {ref for result in results for ref in result.envelopes}
    cited = set(output["evidence_refs"])
    if not cited <= owned:
        raise ValueError("Output cites evidence not retrieved for this case")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= cited:
            raise ValueError("Claim cites evidence absent from the output")
    money = output["financial_resolution"]
    total = Decimal(str(money["recommended_refund_brl"]))
    lines = sum((Decimal(str(line["amount_brl"])) for line in money["refund_lines"]), Decimal(0))
    if total != lines:
        raise ValueError("Refund total does not equal refund lines")
    if total > 0 and not cited:
        raise ValueError("Refund lacks evidence")
    entities = output["affected_entities"]
    scoped = set().union(*map(set, entities.values()))
    for line in money["refund_lines"]:
        if line["entity_id"] is not None and line["entity_id"] not in scoped:
            raise ValueError("Refund line references an unrelated entity")
    parties = output["root_cause_analysis"]["responsible_parties"]
    for party in parties:
        if party["party_type"] == "seller" and party["party_id"] not in entities["seller_ids"]:
            raise ValueError("Seller responsibility is outside evidence scope")
    if {p["party_type"] for p in parties} >= {"seller", "logistics_provider"}:
        raise ValueError("Multi-party fault requires an explicit carrier-log verification")
    assessment = output["assessment"]
    if assessment["case_status"] == "no_action" and total:
        raise ValueError("No-action cases cannot recommend a refund")
    unresolved = any(c["selected_source"] is None for c in output["data_conflicts"])
    if unresolved and assessment["confidence"] >= 0.70:
        raise ValueError("Unresolved source conflicts require confidence below 0.70")
    for conflict in output["data_conflicts"]:
        if (
            conflict["selected_source"] is not None
            and conflict["selected_source"] not in (conflict["sources"])
        ):
            raise ValueError("Selected conflict source is not among the reported sources")
    if assessment["primary_issue"] == "insufficient_evidence" and (
        total or assessment["case_status"] != "needs_investigation"
    ):
        raise ValueError("Insufficient evidence cannot authorize a financial remedy")
