"""Scoped read-only collectors. Discovery does not grant additional privileges."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

SCOPES: dict[str, dict[str, str]] = {
    "order_specialist": {
        "get_order": "order",
        "get_order_items": "item",
        "get_sellers": "seller",
    },
    "payment_specialist": {
        "get_order_payments": "payment",
        "get_payment_timeline": "payment",
        "get_refund_timeline": "refund",
    },
    "shipment_specialist": {
        "get_shipment_summary": "shipment",
    },
    "policy_specialist": {"get_policy": "policy"},
}


@dataclass
class SpecialistResult:
    agent: str
    status: Literal["completed", "partial", "failed"] = "failed"
    case_id: str = ""
    findings: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    envelopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def rows(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Unwrap explicitly named collections without changing source records."""
    if isinstance(data, list):
        if not all(isinstance(row, dict) for row in data):
            raise ValueError("Evidence collection contains a non-object record")
        return data
    if isinstance(data, dict):
        for key in keys:
            if key in data:
                return rows(data[key])
        return [data] if data else []
    if data is None:
        return []
    raise ValueError("Unsupported evidence data shape")


def _identifiers(data: Any) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if (
                key.endswith("_id")
                and isinstance(value, (str, int))
                and not isinstance(value, bool)
            ):
                found.setdefault(key, set()).add(str(value))
            if isinstance(value, (dict, list)):
                for name, values in _identifiers(value).items():
                    found.setdefault(name, set()).update(values)
    elif isinstance(data, list):
        for row in data:
            for name, values in _identifiers(row).items():
                found.setdefault(name, set()).update(values)
    return found


async def dispatch_specialist(
    agent_name: str,
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    context: list[SpecialistResult] | None = None,
    policy_version: str | None = None,
) -> SpecialistResult:
    actor = agent_name.replace("_", "-")
    result = SpecialistResult(agent=actor, case_id=case_id)
    scope = SCOPES[agent_name]
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        decision_code="collect_authoritative_evidence",
    )
    available = set(await gateway.list_tools())
    identifiers: dict[str, set[str]] = {"order_id": {order_id}, "case_id": {case_id}}
    if policy_version:
        identifiers["policy_version"] = {policy_version}
    for prior in context or []:
        if prior.case_id != case_id:
            raise ValueError("Cross-case specialist handoff")
        for envelope in prior.envelopes.values():
            for key, values in _identifiers(envelope["data"]).items():
                if key not in {"case_id", "order_id"}:
                    identifiers.setdefault(key, set()).update(values)

    for tool_name, domain in scope.items():
        if tool_name not in available:
            result.errors.append(f"{tool_name}: not discovered")
            continue
        schema = gateway.tool_schemas[tool_name]
        required = set(schema.get("required", [])) - {"case_id"}
        properties = schema.get("properties", {})
        if not required and "order_id" in properties:
            required.add("order_id")
        if unknown := required - identifiers.keys():
            result.errors.append(f"{tool_name}: missing identifiers {sorted(unknown)}")
            continue
        many = [key for key in required if len(identifiers[key]) > 1]
        if len(many) > 1:
            result.errors.append(f"{tool_name}: ambiguous entity association")
            continue
        batches = sorted(identifiers[many[0]]) if many else [None]
        for value in batches:
            params = {
                key: value if many and key == many[0] else next(iter(identifiers[key]))
                for key in sorted(required)
            }
            try:
                envelope = await gateway.call(tool_name, case_id=case_id, **params)
                if envelope["domain"] != domain:
                    raise ValueError("Unexpected evidence domain")
                ref = envelope["evidence_ref"]
                result.envelopes[ref] = envelope
                if ref not in result.evidence_refs:
                    result.evidence_refs.append(ref)
                result.findings.setdefault(tool_name, []).append(envelope["data"])
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    evidence_refs=[ref],
                )
                for key, values in _identifiers(envelope["data"]).items():
                    if key not in {"case_id", "order_id"}:
                        identifiers.setdefault(key, set()).update(values)
            except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
                result.errors.append(f"{tool_name}: {type(exc).__name__}")
    result.status = "partial" if result.errors else "completed"
    if not result.envelopes:
        result.status = "failed"
    return result
