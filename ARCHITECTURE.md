# L3A Architecture Record

## 1. System overview

`day09 run` validates the 100-case manifest, authenticates to MCP, discovers tool
input schemas, and executes up to four independent cases concurrently. Each case
uses a sequential, bounded workflow:

```text
Input -> Coordinator -> Order -> Payment -> [Shipment] -> Policy -> Verifier
                            |                |              |
                            +--- MCP evidence +---- trace --+-> atomic output
```

Customer topics route investigations only. They never determine the verdict.
The public `contracts/scoring/scoring-policy-v2.json` defines scoring weights and
hard gates, not financial entitlements. Business rules come from the case-scoped
`get_policy` envelope. The authoritative output shape is the repository's
`l3a-output-v2.schema.json`; the earlier illustrative prompt has incompatible keys.
Public contracts are not modified.

## 2. Agent ownership

| Actor | Input | Allowed tools | Output |
| --- | --- | --- | --- |
| Coordinator | Exact manifest case and claim topics | None | Bounded ordered plan |
| Order | case_id, claimed_order_id | get_order, get_order_items, get_sellers | Original envelopes and entity identifiers |
| Payment | Same case/order | get_order_payments, get_payment_timeline, get_refund_timeline | Charges and refund event records |
| Shipment | Delivery investigation | get_shipment_summary | Handoff, deadlines, delivery events |
| Policy retrieval | case_id, policy_version | get_policy | Authoritative business rules |
| Policy evaluation | Validated specialist results | None | Proposed issue, remedies, trace bindings |
| Verifier | Proposal and case-owned envelopes | None | Schema and consistency validation |

Tool names are the actual Gateway discovery results, explicitly authorized by the
user in place of the initial prompt's illustrative whitelist. Discovery cannot
expand an actor's domain privileges. Customer/product history tools are not used.

## 3. A2A protocol

`SpecialistResult` carries actor, case_id, completion status, original envelopes,
references, findings grouped by tool, and sanitized error classifications. All
handoffs retain the exact case_id. The coordinator rejects cross-case handoffs.
Each specialist receives earlier results and only derives identifiers from their
envelopes. Multiple unrelated identifier sets are not joined by Cartesian product.
There are no recursive handoffs or agent-generated executable instructions.

Lifecycle: case_received -> task_assigned -> tool_result_consumed -> handoff ->
policy_decided -> verification_completed -> case_finalized. One policy trace
record per action/refund reason binds it to actual case evidence. Trace attributes
contain observable results, not hidden reasoning, credentials, or provider errors.

## 4. Evidence lifecycle

Discovery input schemas validate every call. The original evidence envelope is
validated against the MCP JSON Schema before consumption. Nested case/order IDs
are checked against call scope. A gateway-session ownership registry rejects a
reference reused across cases and any mutation to an already seen envelope.
Raw data and evidence_ref are never rewritten. `traces/evidence.json` retains raw
responses locally for inspection; it is excluded from the submission archive.
Local checks establish consistency with retrieved envelopes; only the server's
MCP audit can independently attest team/run provenance. The gateway's result_hash
is retained verbatim; no undocumented hash canonicalization is assumed.

Policy evaluation uses Decimal cents and timezone-aware timestamps. Payment and
refund events are bounded by purchase time and case opened_at. Duplicate item
records are compared against the order's purchase/estimated-delivery window;
remaining ambiguity causes manual review. Excluded timeline records and conflicts
are disclosed in data_conflicts, with selected_source and the resolution rule when
the temporal scope is resolved. Resolved scope differences cap confidence at 0.85;
unresolved contradictions remain below 0.70. Seller IDs in generic
policy rules are reconciled to the seller actually supported by item evidence.
The raw policy envelope is preserved, and mismatches are reported. Seller attribution
cites the seller envelope as well as item and policy evidence. Payment sequential
identifiers are retained verbatim as payment references.

## 5. Failure policy

| Failure | Retry | Behavior | Observable result |
| --- | --- | --- | --- |
| MCP timeout | None automatically | Partial result; no invented response | retrieval_errors in handoff |
| Tool not discovered or required identifier unavailable | None | Tool not called | Partial/failed specialist |
| Tool returns an error | None | Preserve missing evidence, continue independent calls | Partial result and reduced confidence |
| Refund history unavailable | None | Preserve only policy-backed, evidence-bounded recommendation; require history check before refund | verify_refund_history before refund action; confidence below 0.70 |
| Ambiguous or contradictory records | None | Record conflict; abstain when material | insufficient_evidence / manual review |
| Invalid envelope or cross-case ID/ref | None | Reject response before consumption | Failed collection or run validation |
| Invalid output or failed verifier | None | Do not finalize that case | Run fails; no valid package |

Each MCP call has a 60-second timeout. No automatic retries are issued because an
uncertain request may already have an audit record. A failed authentication or
initial discovery does not delete previous outputs. New runs collect into a temporary
directory under the workspace and validate the complete inventory before replacing
the current outputs/trace. Collection or transport failures discard the staging
directory and preserve the previous run. Missing authoritative order evidence aborts
the run rather than finalizing an empty evidence result.

## 6. Verification invariants

- Exact case_id and strict public output/trace schemas.
- All cited refs belong to consumed envelopes from this case.
- Claim refs are a subset of the output's cited refs.
- Refund lines sum exactly to the recommendation and reference in-scope entities.
- No-action and insufficient-evidence outcomes have zero refunds.
- Seller responsibility identifies a seller observed in case evidence.
- Unproven simultaneous seller/logistics fault is rejected.
- Late-delivery compensation is itemized freight, bounded by policy, unreturned
  confirmed payments and order value; allocations sum exactly to that budget.
- Confidence is below 0.70 for partial/unresolved evidence; resolved temporal scope
  differences cap it at 0.85. There is no automatic 1.0.
- Packaging validates complete lifecycle ordering and output-to-trace linkage.

Confirmed captures alone do not prove that two equal charges are duplicates.
Duplicate-charge classification requires explicit duplicate evidence. Reconciliation
mismatch events support only their recorded correction amounts; ordinary overcharges
support only the excess. Refund status events and policy govern pending/failed refunds.
Missing records are not interpreted as a zero balance. A recommended amount is not
an authorization to transfer funds: without refund history, the output requires
verification before the refund action. If no explicit policy monetary rule backs
the recommendation, the workflow abstains and reports needs_investigation.

## 7. Reproducibility

The workflow is deterministic Python; no LLM, prompt, random classification, or
external model is used. Event IDs and wall-clock trace timestamps are intentionally
unique per run. Python >=3.11 and the repository dependency ranges apply; the
installed environment can be recorded with `python -m pip freeze`. The implementation
uses the MCP 2.x snake_case client API. Concurrency defaults to 4 and is configurable
from 1 to 8. JSON output writes use temporary files followed by atomic replacement.

```text
python -m pytest -q
ruff check .
day09 validate-inputs
day09 mcp-tools --describe
day09 run --concurrency 4
day09 validate
day09 package --output dist/submission.zip
```

`day09 inspect-case <case_id> <discovered-tools...>` is a read-only diagnostic command.
Its responses are for inspection, not substituted into a later run. The submission
contains only manifest.json, trace.jsonl and the 100 output JSON files. Local input,
raw evidence, .env and API keys are not included. Keep runtime artifacts Git-ignored.
