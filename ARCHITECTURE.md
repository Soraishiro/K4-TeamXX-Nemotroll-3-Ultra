# L3A Architecture Record

Tài liệu này ghi lại kiến trúc phối hợp đa tác tử (A2A), phân định trách nhiệm, vòng đời bằng chứng (Evidence Lifecycle), chính sách xử lý sự cố (Failure Policy) và các bất biến kiểm định (Verification Invariants) cho hệ thống điều tra khiếu nại thương mại điện tử Day09 L3A.

---

## 1. System Overview

Hệ thống được thiết kế theo mô hình Directed Acyclic Graph (DAG) phối hợp đa tác tử (Agent-to-Agent - A2A), kết nối với MCP Evidence Gateway để thu thập dữ liệu có thẩm quyền làm bằng chứng giải quyết khiếu nại.

### Sơ đồ kiến trúc luồng dữ liệu & phối hợp A2A:

```text
               +-----------------------+
               |  Coordinator / Router |
               +-----------------------+
                           |
            +--------------+--------------+
            | (Handoff)    | (Handoff)    | (Handoff)
            v              v              v
     +--------------+ +--------------+ +----------------+
     |  Order/Item  | |   Payment    | |    Shipment    |
     |    Agent     | |    Agent     | |     Agent      |
     +--------------+ +--------------+ +----------------+
            |              |              |
      [get_order]    [get_payments]  [get_shipment]
     [get_items]    [get_timeline]        |
            |        [get_refunds]        |
            |              |              |
            +--------------+--------------+
                           |
              (MCP Evidence Collection)
                           v
                 +-------------------+
                 |   Policy Agent    | <--- [get_policy]
                 +-------------------+
                           |
                      (Handoff)
                           v
                 +-------------------+
                 |  Verifier Agent   |
                 +-------------------+
                           |
                   (Validated Output)
                           v
                      [END OUTPUT]
```

### Luồng vận hành chi tiết:
1. **Input**: Hệ thống nhận case từ `inputs/<case_id>.json` chứa thông tin khiếu nại của khách hàng (`customer_request`) và phiên bản chính sách (`policy_version`).
2. **Coordinator/Router**: Phân rã khiếu nại, phát sự kiện `task_assigned` và handoff song song cho 3 Specialist Agents phụ trách 3 domain độc lập.
3. **Specialist Agents (Order, Payment, Shipment)**: Chỉ truy vấn các MCP tools được cấp quyền. Với mỗi bằng chứng nhận được, phát sự kiện `tool_result_consumed` và trích xuất thực thể liên quan.
4. **Handoff to Policy Agent**: Các specialist đóng gói kết quả điều tra và chuyển giao (`handoff`) tới Policy Agent.
5. **Policy Agent**: Truy vấn `get_policy`, tổng hợp bằng chứng từ các specialist, đối chiếu quy tắc nghiệp vụ để xác định `primary_issue`, đánh giá từng claim, tính toán `financial_resolution`, xác định `root_cause_analysis` và phát sự kiện `policy_decided`.
6. **Verifier Agent**: Nhận đề xuất giải quyết, kiểm tra toàn bộ 7 Verification Invariants và kiểm định tính hợp lệ theo JSON Schema `l3a-output-v2.schema.json`. Phát sự kiện `verification_completed`.
7. **Final Output & Trace**: Kết quả được ghi vào `outputs/<case_id>.json` và observable trace được append vào `traces/trace.jsonl`.

---

## 2. Agent Ownership & Tool Permissions

Nhằm tuân thủ nguyên tắc đặc quyền tối thiểu (Principle of Least Privilege), mỗi agent chỉ được cấp quyền truy cập vào các công cụ MCP thuộc phạm vi nghiệp vụ của mình.

| Actor | Input | Trách nhiệm | Output / Handoff | Quyền hạn MCP Tools |
| :--- | :--- | :--- | :--- | :--- |
| **Coordinator** | `case` JSON từ `case-set` | Tiếp nhận case, phân tích claims & topics, điều phối luồng thực thi, giám sát tiến độ | Handoff task tới Order, Payment, Shipment Agents | **Không** (Zero MCP Tools) |
| **Order/Item Agent** | `case_id`, `claimed_order_id` | Xác minh trạng thái đơn hàng, danh sách mặt hàng, định danh người bán và ngữ cảnh sản phẩm | Findings: `order_ids`, `item_ids`, `seller_ids`, `order_data`, `evidence_refs` $\rightarrow$ Policy Agent | `get_order`, `get_order_items`, `get_sellers`, `get_product_context` |
| **Payment Agent** | `case_id`, `claimed_order_id` | Kiểm tra giao dịch thanh toán, phương thức thanh toán, timeline thanh toán và trạng thái hoàn tiền | Findings: `payment_references`, `payments_data`, `refund_data`, `evidence_refs` $\rightarrow$ Policy Agent | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| **Shipment Agent** | `case_id`, `claimed_order_id` | Kiểm tra timeline giao vận, hạn chót người bán giao hàng cho đơn vị vận chuyển và thời điểm giao thực tế | Findings: `shipment_ids`, `shipment_data`, `evidence_refs` $\rightarrow$ Policy Agent | `get_shipment_summary` |
| **Policy Agent** | `case`, findings từ Order, Payment, Shipment | Truy xuất chính sách, đánh giá từng claim (supported/unsupported), xác định nguyên nhân gốc rễ và mức hoàn tiền | Proposed Resolution: `assessment`, `root_cause_analysis`, `financial_resolution`, `resolution_actions` $\rightarrow$ Verifier Agent | `get_policy` |
| **Verifier Agent** | Proposed Resolution từ Policy Agent | Kiểm tra schema validation, tính nhất quán số tiền, tính toàn vẹn của bằng chứng và logic nghiệp vụ | Validated Case Output JSON theo public contract | **Không** (Zero MCP Tools) |

---

## 3. A2A Protocol

Giao thức giao tiếp giữa các tác tử tuân thủ cấu trúc định dạng chuẩn và có thể quan sát (observable) qua Trace log:

1. **Message Envelope & Correlation**:
   - Mọi thông điệp và sự kiện đều được gắn kèm `case_id` làm khóa tương quan duy nhất (correlation key).
   - Mỗi sự kiện trace mang định danh duy nhất `event_id` (dạng `evt_[A-Za-z0-9_-]{12,96}`).
2. **Handoff Conditions**:
   - Coordinator chỉ handoff cho Specialists sau khi case được tiếp nhận (`case_received`).
   - Specialists chỉ handoff cho Policy Agent sau khi đã hoàn thành truy vấn MCP hoặc đã thực hiện đủ số lần retry thất bại.
   - Policy Agent chỉ handoff cho Verifier Agent sau khi đã phát sự kiện `policy_decided`.
3. **Phòng chống lặp và Deadlock (Loop Avoidance)**:
   - Luồng phối hợp là đồ thị có hướng không chu trình (DAG) nghiêm ngặt 1 chiều: `Coordinator -> Specialists -> Policy Agent -> Verifier Agent`.
   - Không hỗ trợ quay lui (backtracking) vòng lặp giữa các agent nhằm đảm bảo chặn đứng nguy cơ lặp vô hạn và cạn kiệt tài nguyên.
4. **Observable Trace Only**:
   - Chỉ trace các sự kiện và decision code quan sát được theo schema `trace-event-v1.schema.json`:
     `case_received` $\rightarrow$ `task_assigned` $\rightarrow$ `tool_result_consumed` $\rightarrow$ `handoff` $\rightarrow$ `policy_decided` $\rightarrow$ `verification_completed` $\rightarrow$ `case_finalized`.
   - **Tuyệt đối không** đưa prompt, chain-of-thought, dữ liệu nhạy cảm hay API keys vào trace log.

---

## 4. Evidence Lifecycle

Bằng chứng (Evidence) là nền tảng tối cao để đưa ra quyết định giải quyết tranh chấp:

1. **Validation**: Mọi phản hồi từ MCP Gateway được bọc trong envelope chuẩn và phải vượt qua kiểm định schema `day09-mcp-evidence-v1` (`evidence_ref`, `result_hash`, `domain`, `data`).
2. **Extraction & Tracking**:
   - Khi nhận evidence hợp lệ, Agent trích xuất mã định danh `evidence_ref` (định dạng `^ev_[A-Za-z0-9_-]{20,96}$`).
   - Ngay lập tức phát sự kiện `tool_result_consumed` qua `TraceWriter`, ghi nhận `actor`, `tool_name` và `evidence_refs`.
3. **Mapping vào Output**:
   - Mọi `evidence_ref` dùng làm căn cứ trong `claim_assessments` phải nằm trong mảng `evidence_refs` tổng thể của output case đó.
   - Chỉ trích dẫn các bằng chứng thực sự hỗ trợ cho kết luận tương ứng.
4. **Cô lập tuyệt đối (Zero Cross-Case Reuse)**:
   - Tuyệt đối không tái sử dụng `evidence_ref` giữa các case khác nhau.
   - Mỗi `evidence_ref` phải được tạo ra trong phiên làm việc với đúng `case_id` của case đang xử lý.

---

## 5. Failure Policy

Chính sách ứng phó sự cố được thiết kế theo cơ chế phòng thủ nhiều lớp (Defense-in-depth):

| Sự cố (Failure) | Retry? | Fallback Strategy | Trace Event / Code |
| :--- | :--- | :--- | :--- |
| **MCP Timeout / Network drop** | Có (Tối đa 2 lần, Exponential backoff 0.5s, 1.0s) | Đánh dấu domain evidence là `unavailable`. Không chặn đứng toàn bộ hệ thống | `task_assigned` ghi nhận retry; nếu hỏng hẳn thì ghi nhận thiếu evidence |
| **Not Found / Tool Error** | Không retry (Lỗi 4xx/dữ liệu không tồn tại) | Coi như không tìm thấy dữ liệu. **Tuyệt đối không** bịa đặt dữ liệu hoặc tự tạo `evidence_ref` giả | Phản ánh vào kết luận với verdict `insufficient_evidence` |
| **Source Conflict (Mâu thuẫn dữ liệu)** | Không | Đưa vào danh sách `data_conflicts`, ưu tiên dữ liệu từ MCP Gateway có chữ ký/hash thay vì message của khách hàng | Ghi nhận trong output `data_conflicts` với `selected_source` và `resolution_code` |
| **Invalid Specialist Result** | Không | Verifier Agent phát hiện vi phạm bất biến $\rightarrow$ đưa case về trạng thái an toàn `needs_investigation` | `verification_completed` với decision code `VERIFIED_WITH_SAFE_FALLBACK` |

### Quy tắc bất di bất dịch:
> **Không bao giờ chuyển dữ liệu bị thiếu hoặc lỗi mạng thành dữ liệu phỏng đoán.** Khi thiếu bằng chứng, hệ thống luôn chọn phương án an toàn nhất: `primary_issue = "insufficient_evidence"`, `case_status = "needs_investigation"`, và khuyến nghị xác minh thêm.

---

## 6. Verification Invariants

Trước khi xuất file `outputs/<case_id>.json`, Verifier Agent kiểm tra nghiêm ngặt 7 bất biến cốt lõi:

1. **Schema Strictness**: Output phải khớp 100% với JSON Schema `l3a-output-v2.schema.json`. Không chứa bất kỳ field thừa nào ngoài schema (`additionalProperties: false`).
2. **Case Identity Scope**: Trường `case_id` trong output phải khớp tuyệt đối với `case_id` của input đang xử lý.
3. **Evidence Authenticity**: Mọi `evidence_ref` trong output phải có định dạng hợp lệ (`^ev_[A-Za-z0-9_-]{20,96}$`) và được sinh ra từ chính MCP session của case này.
4. **Claim Coverage**: 100% các `claim_id` xuất hiện trong `customer_request.claims` phải có đánh giá tương ứng trong mảng `claim_assessments`.
5. **Financial Conservation**: Số tiền đề xuất hoàn `recommended_refund_brl` phải bằng chính xác tổng `amount_brl` của tất cả các dòng trong `refund_lines` (sai số = 0.00). Tiền tệ luôn cố định là `"BRL"`.
6. **Action Consistency**:
   - Nếu `case_status == "action_required"`: mảng `resolution_actions` không được rỗng.
   - Nếu `case_status == "no_action"`: `recommended_refund_brl` phải bằng `0.0`.
7. **Confidence Bounds**: Giá trị `confidence` trong `assessment` và từng `claim_assessments` luôn nằm trong đoạn đóng $[0.0, 1.0]$.

---

## 7. Reproducibility

- **Runtime Environment**: Python 3.11+ trên môi trường ảo `.venv`.
- **Dependencies**: Khóa cứng phiên bản trong `pyproject.toml` (jsonschema, mcp, httpx2, pydantic, anyio).
- **Concurrency & Resource Limits**:
  - Hỗ trợ xử lý bất đồng bộ (asyncio) với giới hạn timeout cho MCP Gateway là 300 giây.
  - Tối đa 20 thực thể (entities) và 30 evidence refs trên mỗi output case theo giới hạn schema.
- **Tính tiền định (Determinism)**: Thuật toán đưa ra kết luận dựa trên luật nghiệp vụ và bằng chứng xác định từ MCP Gateway, không phụ thuộc vào yếu tố ngẫu nhiên (Zero temperature / deterministic policy resolution).
- **Quy trình kiểm thử & thực thi chuẩn**:
  ```bash
  # 1. Kiểm tra tính toàn vẹn input
  day09 validate-inputs

  # 2. Chạy quy trình đa tác tử cho 100 case
  day09 run

  # 3. Thẩm định output và trace log
  day09 validate

  # 4. Đóng gói nộp bài
  day09 package --output dist/submission.zip
  ```
