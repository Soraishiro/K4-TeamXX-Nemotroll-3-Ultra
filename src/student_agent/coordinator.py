"""Complaints select investigations, never verdicts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CoordinatorPlan:
    case_id: str
    agents: tuple[str, ...]


def plan_case(case: dict[str, Any]) -> CoordinatorPlan:
    topics = {claim.get("topic") for claim in case["customer_request"].get("claims", [])}
    agents = ["order_specialist", "payment_specialist"]
    if topics & {"late_delivery_seller", "late_delivery_logistics", "unsupported_claim"}:
        agents.append("shipment_specialist")
    agents.append("policy_specialist")
    return CoordinatorPlan(case_id=case["case_id"], agents=tuple(agents))
