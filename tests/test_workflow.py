"""Synthetic fixtures are unit-test data only; never submitted as MCP evidence."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.policy import evaluate_policy, money
from student_agent.specialists import SpecialistResult
from student_agent.verifier import verify_output

ROOT = Path(__file__).resolve().parents[1]


def case() -> dict[str, Any]:
    return {
        "case_id": "TEST_CASE_001",
        "policy_version": "TEST_POLICY",
        "opened_at": "2020-01-10T00:00:00+00:00",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-1", "topic": "canceled_order_paid"},
            ],
        },
    }


def evidence(domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_TEST_FIXTURE_ONLY_" + domain.ljust(20, "x"),
        "result_hash": "sha256:" + "0" * 64,
        "domain": domain,
        "data": data,
    }


def results(
    status: str = "canceled",
    payments: list[dict[str, Any]] | None = None,
    refunds: list[dict[str, Any]] | None = None,
) -> list[SpecialistResult]:
    data = {
        "get_order": (
            "order",
            {
                "order_id": "order-1",
                "order_status": status,
                "order_estimated_delivery_date": "2020-01-08T00:00:00+00:00",
                "order_delivered_customer_date": "2020-01-07T00:00:00+00:00",
            },
        ),
        "get_order_items": (
            "item",
            [
                {
                    "order_id": "order-1",
                    "order_item_id": "item-1",
                    "seller_id": "seller-1",
                    "price": "90.00",
                    "freight_value": "10.00",
                }
            ],
        ),
        "get_order_payments": (
            "payment",
            payments
            if payments is not None
            else [
                {"payment_value": "100.00", "payment_reference": "payment-1"},
            ],
        ),
        "get_refund_timeline": ("refund", refunds or []),
        "get_policy": (
            "policy",
            {
                "policy_version": "TEST_POLICY",
                "rules": {
                    "canceled_order_paid": {
                        "case_status": "action_required",
                        "recommended_action": "issue_refund",
                        "responsible_parties": [{"party_type": "platform", "party_id": None}],
                    },
                    "unsupported_claim": {
                        "case_status": "no_action",
                        "recommended_action": "document_no_action",
                        "responsible_parties": [{"party_type": "customer", "party_id": None}],
                    },
                    "payment_mismatch": {
                        "case_status": "action_required",
                        "recommended_action": "reconcile_payment",
                        "responsible_parties": [
                            {"party_type": "payment_provider", "party_id": None}
                        ],
                    },
                    "refund_pending": {
                        "case_status": "needs_investigation",
                        "recommended_action": "monitor_refund",
                        "responsible_parties": [
                            {"party_type": "payment_provider", "party_id": None}
                        ],
                    },
                },
            },
        ),
    }
    collected = []
    for tool, (domain, payload) in data.items():
        envelope = evidence(domain, payload)
        collected.append(
            SpecialistResult(
                agent=domain,
                status="completed",
                case_id="TEST_CASE_001",
                findings={tool: [payload]},
                evidence_refs=[envelope["evidence_ref"]],
                envelopes={envelope["evidence_ref"]: envelope},
            )
        )
    return collected


def test_customer_claim_does_not_override_delivered_order() -> None:
    records = results("delivered")
    output, _ = evaluate_policy(case(), records)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    verify_output(output, case()["case_id"], records, Contracts(ROOT / "contracts/schemas"))


def test_cancellation_refunds_only_unreturned_payment() -> None:
    records = results(refunds=[{"status": "completed", "refund_amount": "30.10"}])
    output, bindings = evaluate_policy(case(), records)
    assert output["financial_resolution"]["recommended_refund_brl"] == 69.9
    assert bindings["issue_refund"]


def test_pending_refund_does_not_create_second_refund() -> None:
    output, _ = evaluate_policy(
        case(),
        results(
            refunds=[
                {"status": "pending", "refund_amount": "100.00"},
            ]
        ),
    )
    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_overcharge_refunds_excess_only() -> None:
    output, _ = evaluate_policy(
        case(),
        results(
            "delivered",
            payments=[
                {"payment_value": "135.00", "payment_reference": "payment-1"},
            ],
        ),
    )
    assert output["assessment"]["primary_issue"] == "payment_mismatch"
    assert output["financial_resolution"]["recommended_refund_brl"] == 35


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "-1", "0.001"])
def test_money_rejects_invalid_values(value: Any) -> None:
    with pytest.raises(ValueError):
        money(value)


def test_missing_payment_is_not_zero_payment() -> None:
    output, _ = evaluate_policy(case(), results(payments=[]))
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["confidence"] < 0.7


def test_verifier_rejects_cross_case_and_unknown_references() -> None:
    records = results()
    output, _ = evaluate_policy(case(), records)
    contracts = Contracts(ROOT / "contracts/schemas")
    tampered = deepcopy(output)
    tampered["evidence_refs"].append("ev_UNKNOWN_REFERENCE_000000000000")
    with pytest.raises(ValueError, match="not retrieved"):
        verify_output(tampered, case()["case_id"], records, contracts)
    records[0].case_id = "OTHER_CASE"
    with pytest.raises(ValueError, match="Cross-case"):
        verify_output(output, case()["case_id"], records, contracts)


class Session:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def list_tools(self, *, params: Any = None) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="get_order",
                    input_schema={
                        "type": "object",
                        "required": ["case_id", "order_id"],
                        "properties": {
                            "case_id": {"type": "string"},
                            "order_id": {"type": "string"},
                        },
                    },
                )
            ],
            next_cursor=None,
        )

    async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> Any:
        self.calls.append(arguments)
        return SimpleNamespace(is_error=False, structured_content=self.payload, content=[])


def test_gateway_rejects_cross_case_reference_reuse() -> None:
    async def run() -> None:
        session = Session(evidence("order", {"order_id": "order-1"}))
        gateway = EvidenceGateway(session, Contracts(ROOT / "contracts/schemas"))
        await gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1")
        assert session.calls[0]["case_id"] == "TEST_CASE_001"
        with pytest.raises(ValueError, match="another case"):
            await gateway.call("get_order", case_id="TEST_CASE_002", order_id="order-1")

    asyncio.run(run())


def test_gateway_rejects_wrong_order_in_nested_records() -> None:
    async def run() -> None:
        gateway = EvidenceGateway(
            Session(evidence("order", [{"order_id": "wrong-order"}])),
            Contracts(ROOT / "contracts/schemas"),
        )
        with pytest.raises(ValueError, match="mismatched order_id"):
            await gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1")

    asyncio.run(run())


def test_refund_tool_failure_withholds_positive_remedy() -> None:
    records = [record for record in results() if record.agent != "refund"]
    output, _ = evaluate_policy(case(), records)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["resolution_actions"] == ["verify_refund_history"]


def test_future_capture_is_not_added_to_current_case_refund() -> None:
    records = results()
    order = records[0].findings["get_order"][0]
    order["order_purchase_timestamp"] = "2020-01-01T00:00:00+00:00"
    payload = {
        "events": [
            {
                "event_at": "2020-01-02T00:00:00+00:00",
                "event_type": "captured",
                "status": "confirmed",
                "amount_brl": "100.00",
            },
            {
                "event_at": "2020-02-02T00:00:00+00:00",
                "event_type": "captured",
                "status": "confirmed",
                "amount_brl": "999.00",
            },
        ]
    }
    envelope = evidence("payment", payload)
    envelope["evidence_ref"] = "ev_TEST_FIXTURE_TIMELINE_000001"
    records.append(
        SpecialistResult(
            agent="payment",
            status="completed",
            case_id="TEST_CASE_001",
            findings={"get_payment_timeline": [payload]},
            evidence_refs=[envelope["evidence_ref"]],
            envelopes={envelope["evidence_ref"]: envelope},
        )
    )
    output, _ = evaluate_policy(case(), records)
    assert output["financial_resolution"]["recommended_refund_brl"] == 100
    assert output["assessment"]["confidence"] == 0.85
    assert output["data_conflicts"][0]["field"] == "payment_event_scope"
    assert output["data_conflicts"][0]["selected_source"] == "get_payment_timeline"


def test_verifier_rejects_unbalanced_refund_lines() -> None:
    records = results()
    output, _ = evaluate_policy(case(), records)
    output["financial_resolution"]["refund_lines"][0]["amount_brl"] = 99
    with pytest.raises(ValueError, match="does not equal"):
        verify_output(output, case()["case_id"], records, Contracts(ROOT / "contracts/schemas"))


def test_gateway_never_calls_undiscovered_tool() -> None:
    async def run() -> None:
        session = Session(evidence("order", {}))
        gateway = EvidenceGateway(session, Contracts(ROOT / "contracts/schemas"))
        with pytest.raises(ValueError, match="not discovered"):
            await gateway.call("invented_tool", case_id="TEST_CASE_001")
        assert session.calls == []

    asyncio.run(run())


def test_packaging_rejects_unconsumed_evidence(tmp_path: Path) -> None:
    import json

    from student_agent.cases import CaseSet
    from student_agent.submission import validate_artifacts
    from student_agent.trace import TraceWriter

    contracts = Contracts(ROOT / "contracts/schemas")
    output, _ = evaluate_policy(case(), results())
    cid = case()["case_id"]
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / f"{cid}.json").write_text(json.dumps(output), encoding="utf-8")
    trace = TraceWriter(tmp_path / "traces/trace.jsonl", contracts)
    for event in [
        "case_received",
        "task_assigned",
        "handoff",
        "verification_completed",
        "case_finalized",
    ]:
        trace.emit(case_id=cid, event_type=event, actor="test-agent")
    case_set = CaseSet("test", "l3a", (cid,), {cid: case()})
    with pytest.raises(ValueError, match="not consumed"):
        validate_artifacts(tmp_path, case_set, contracts)


def test_policy_entitlement_survives_missing_refund_history_with_prerequisite() -> None:
    records = [record for record in results() if record.agent != "refund"]
    policy = records[-1].findings["get_policy"][0]
    policy["rules"]["canceled_order_paid"]["refund_brl"] = 100
    output, bindings = evaluate_policy(case(), records)
    assert output["financial_resolution"]["recommended_refund_brl"] == 100
    assert output["assessment"]["case_status"] == "action_required"
    assert output["assessment"]["confidence"] < 0.7
    assert output["resolution_actions"] == ["verify_refund_history", "issue_refund"]
    assert bindings["verify_refund_history"]
    verify_output(output, case()["case_id"], records, Contracts(ROOT / "contracts/schemas"))


def test_seller_attribution_cites_seller_envelope() -> None:
    records = results()
    policy = records[-1].findings["get_policy"][0]
    policy["rules"]["canceled_order_paid"]["responsible_parties"] = [
        {"party_type": "seller", "party_id": "seller-from-another-order"}
    ]
    envelope = evidence("seller", [{"seller_id": "seller-1"}])
    records.append(
        SpecialistResult(
            agent="seller",
            status="completed",
            case_id="TEST_CASE_001",
            findings={"get_sellers": [envelope["data"]]},
            evidence_refs=[envelope["evidence_ref"]],
            envelopes={envelope["evidence_ref"]: envelope},
        )
    )
    output, _ = evaluate_policy(case(), records)
    assert envelope["evidence_ref"] in output["evidence_refs"]
    assert output["root_cause_analysis"]["responsible_parties"][0]["party_id"] == "seller-1"
    assert output["data_conflicts"][0]["selected_source"] == "get_order_items"
    verify_output(output, case()["case_id"], records, Contracts(ROOT / "contracts/schemas"))


def test_payment_sequential_is_preserved_as_payment_reference() -> None:
    output, _ = evaluate_policy(
        case(),
        results(
            payments=[
                {"payment_value": "100.00", "payment_sequential": "1"},
            ]
        ),
    )
    assert output["affected_entities"]["payment_references"] == ["1"]


def test_freight_refund_is_bounded_by_policy_and_confirmed_payment() -> None:
    records = results("delivered", payments=[{"payment_value": "16.00"}])
    order = records[0].findings["get_order"][0]
    order["order_delivered_customer_date"] = "2020-01-09T00:00:00+00:00"
    order["order_delivered_carrier_date"] = "2020-01-02T00:00:00+00:00"
    item = records[1].findings["get_order_items"][0][0]
    item["shipping_limit_date"] = "2020-01-03T00:00:00+00:00"
    item["freight_value"] = "18.00"
    records[-1].findings["get_policy"][0]["rules"]["late_delivery_logistics"] = {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": 16,
        "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
    }
    envelope = evidence(
        "shipment",
        {
            "events": [
                {
                    "event_type": "delivered_late",
                    "status": "confirmed",
                    "actor": "logistics_provider",
                }
            ]
        },
    )
    records.append(
        SpecialistResult(
            agent="shipment",
            status="completed",
            case_id="TEST_CASE_001",
            findings={"get_shipment_summary": [envelope["data"]]},
            evidence_refs=[envelope["evidence_ref"]],
            envelopes={envelope["evidence_ref"]: envelope},
        )
    )
    output, _ = evaluate_policy(case(), records)
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["financial_resolution"]["recommended_refund_brl"] == 16
    assert output["financial_resolution"]["refund_lines"][0]["entity_id"] == "item-1"


def test_failed_run_preserves_previous_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from student_agent import cli
    from student_agent.cases import CaseSet

    (tmp_path / "outputs").mkdir()
    (tmp_path / "traces").mkdir()
    previous = {
        tmp_path / "outputs/TEST_CASE_001.json": b"previous-output",
        tmp_path / "traces/trace.jsonl": b"previous-trace",
        tmp_path / "traces/evidence.json": b"previous-evidence",
    }
    for path, content in previous.items():
        path.write_bytes(content)
    monkeypatch.setattr(cli.Settings, "load", lambda root: object())
    monkeypatch.setattr(
        cli,
        "load_case_set",
        lambda root: CaseSet(
            "test",
            "l3a",
            ("TEST_CASE_001",),
            {"TEST_CASE_001": case()},
        ),
    )
    monkeypatch.setattr(cli, "Contracts", lambda root: Contracts(ROOT / "contracts/schemas"))

    async def fail_after_partial_write(root: Path, *args: Any) -> None:
        assert root != tmp_path
        (root / "outputs").mkdir()
        (root / "outputs/TEST_CASE_001.json").write_text("partial-new-output")
        raise RuntimeError("simulated gateway outage")

    monkeypatch.setattr(cli, "_collect", fail_after_partial_write)
    with pytest.raises(RuntimeError, match="simulated gateway outage"):
        asyncio.run(cli._run(tmp_path))
    for path, content in previous.items():
        assert path.read_bytes() == content
    assert not list((tmp_path / "traces").glob(".run-*"))
