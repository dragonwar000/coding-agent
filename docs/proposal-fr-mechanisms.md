# Đề xuất lại cơ chế FR cho coding-agent

**Trạng thái:** proposed
**Phạm vi:** package `coding-agent` là repo riêng tại `~/Documents/Development/coding-agent`, không nằm trong `harness/`.
**Thay thế:** phần cơ chế của đề xuất `llmwiki/wiki/sources/draft/031026-harness-claude-orca-integration.md` (repo `harness/setup`, task `T-261003-01`). Các FR và SC giữ nguyên ý định, nhưng cơ chế đổi để không phụ thuộc `fdk-gate`, `mechanisms.yaml`, `harness-doctor` hay bất kỳ file nào của setup.

## Bối cảnh

Đề xuất gốc đặt manifest ở `harness/integration.yaml` và dựa vào `fdk-gate`, `mechanisms.yaml`, `harness-doctor`, validator mirror và policy parity của setup. coding-agent không còn chạy trong harness đó. Cơ chế dưới đây chỉ dùng những thứ nằm trong repo này: manifest, gen, gate, package Python, và một Orca CLI thật.

## Ràng buộc toàn cục

- Mọi guard mới ở `shadow` và phải khai `assumption`. Chỉ guard được khai `enforce` mới chặn (FR-010, FR-011).
- Hook không gọi model. Mọi quyết định là hàm tất định trên payload và trạng thái.
- File sinh ra không chứa đường dẫn tuyệt đối của máy nào (FR-002).
- Hook fail-open: lỗi nội bộ thoát 0 và ghi event `hook-error`.
- Orca chỉ được gọi qua `orca orchestration` (CLI thật). Test dùng `fake_orca.py` qua `CODING_AGENT_ORCA`.
- `verified: false` là mặc định cho đến khi người duyệt đổi. Các ngưỡng là ước đoán có ADAPT-CHECKLIST.

## Yêu cầu chức năng

Cột "Bằng chứng" là tên test hiện có. "Chưa" nghĩa là chưa có cơ chế hoặc test.

| ID | Yêu cầu | Cơ chế | Bằng chứng | Trạng thái |
|---|---|---|---|---|
| FR-001 | Mọi hook của Claude Code và Codex khai trong một manifest, và file host sinh từ manifest | `integration.yaml` → `manifest.load` → `gen.render`. Đổi so với đề xuất gốc: manifest chỉ khai hook. Lệnh Orca không nằm trong manifest, vì chúng đi qua `orca_cli` | `test_manifest_gen`, `test_shipped_manifest` | Xong |
| FR-002 | Đường dẫn tương đối, không có đường dẫn máy cụ thể | `$CLAUDE_PROJECT_DIR` và `$(git rev-parse --show-toplevel)` trong `gen.py` | `test_renders_relative_commands_for_both_hosts` | Xong |
| FR-003 | Cổng thất bại khi file đang dùng lệch manifest | `gate.py` thay cho `fdk-gate`. Thoát 0, 1 (lệch hoặc thiếu), 2 (manifest sai). Chưa có pre-push hook hay CI gọi nó | `test_gate.py` (4 test, gồm sửa tay file sinh ra) | Xong ở mức lệnh. Chưa có nơi nào tự chạy |
| FR-004 | Cài không xoá thư mục không có dấu harness và không có bản sao lưu | `install.py`: dấu `.coding-agent-owned` kèm digest cây thư mục, sao lưu trước khi thay, từ chối nếu dấu không khớp | `test_install.py` (gồm `test_an_unmarked_scripts_directory_is_never_removed`) | Xong |
| FR-005 | Cài từ một commit đã ghim, không từ nhánh di động | Chưa có. Vì repo đã tách, package phải được cài vào repo đích từ một commit ghim, không còn nằm cạnh repo đích. Đề xuất: `pip install git+<url>@<sha>` hoặc `install.py --source <url> --ref <sha>` (từ chối ref không phải SHA 40 ký tự), và khi đó lệnh hook bỏ `PYTHONPATH` | Chưa | Chưa làm. Cần repo đã publish và SHA ghim |
| FR-006 | Task Orca tạo qua harness mang `T-id` và `project` từ gốc repo, liên kết ghi vào sổ | `orca_cli.create_task`: spec có dòng `project:` và `t_id:`. Liên kết ghi vào `.coding-agent/links.jsonl` | `test_create_records_the_t_id_link_and_the_project_in_the_spec` | Xong (adapter; chưa đo trên Orca thật) |
| FR-007 | Task chỉ `completed` khi basis là chứng cứ. `agentReported` thành `unverified` | `orca.completion` và `orca_cli.set_status`. Completion không có chứng cứ không gọi Orca | `test_completion_*`, `test_completion_without_proof_never_reaches_orca` | Xong |
| FR-008 | Từ chối claim task thuộc repo khác, và ghi event | `orca_cli.claim` dựa trên sổ liên kết. Refusal ghi event `orca-project/claim-refused` | `test_a_claim_from_another_repository_is_refused_and_logged` | Xong |
| FR-009 | Chặn `task-update --status` ngoài enum | `orca-guard` ở `enforce` (người duyệt đồng ý). Enum khớp CLI đang cài, đã bỏ `in_progress`. Có hint về `result.task.id` khi create | `test_orca_guard_blocks_a_status_outside_the_enum`, `test_the_guard_status_enum_matches_the_gate` | Xong |
| FR-010 | Guard mới mặc định `shadow`, có `assumption`, event có `applied` | Manifest mặc định `shadow`; hook không `off` mà thiếu assumption thì không nạp được | `test_rejects_bad_config_at_load`, `test_without_an_env_mode_the_manifest_mode_applies` | Xong |
| FR-011 | Hook fail-open. Chỉ `enforce` mới chặn, và lý do chặn ghi có cấu trúc | `hooks.run` bắt mọi lỗi. Lý do chặn đi qua stderr và được ghi vào `events.detail` | `test_run_is_fail_open_when_a_handler_raises`, `test_a_malformed_payload_never_breaks_the_session` | Xong. "Có cấu trúc" hiện là event, không phải stderr JSON |
| FR-012 | Báo cáo tổng hợp số lần nhắc và chặn theo guard | `coding_agent.cli report` đọc `events.jsonl` | `test_report_summarises_applied_and_total_decisions` | Xong |
| FR-013 | Fixture end-to-end: tạo, cổng, dispatch, verify, với Orca giả. `CLAIMED-DONE BUT ABSENT` luôn thất bại | Các mảnh đã có: `fake_orca.py`, `orca-check --strict`. Thiếu: `dispatch` trong adapter và một kịch bản test chạy đủ chuỗi | `test_claimed_done_but_absent_fails_the_strict_check`, `test_the_cli_create_status_and_claim_commands_run_end_to_end` (từng bước, chưa đủ chuỗi) | Một phần |

### Yêu cầu bổ sung cho coding-agent

| ID | Yêu cầu | Cơ chế | Bằng chứng | Trạng thái |
|---|---|---|---|---|
| FR-014 | Người điều phối đọc trạng thái trên một màn hình, nhóm theo repo | `orca.reconcile` đã nhóm (running, never_dispatched, stuck, unverified). Thiếu: lệnh `coding_agent.cli status` in ra các nhóm đó | `test_reconcile_groups_without_changing_anything` (chỉ tầng hàm) | Một phần |
| FR-015 | Chỉ ghi bộ nhớ khi lượt có bằng chứng; hội thoại ghi riêng | `memory/record.py` và `stop_gate`: lượt verify `ok` ghi episode, lượt khác chỉ ghi hội thoại | `test_records_for_a_verified_turn_have_stable_keys`, `test_unverified_turns_store_conversation_only` | Xong |
| FR-016 | Sau khi nén ngữ cảnh, khôi phục các prompt gần nhất (FR-022 của đề xuất gốc) | `pre-compact` ghi event. `session-restore` (SessionStart, matcher `compact`) đưa 3 prompt gần nhất vào context | `test_session_restore_puts_the_recent_prompts_back_after_a_compaction`, `test_pre_compact_records_the_compaction_without_changing_the_session` | Xong |
| FR-017 | Trần số lần gọi tool bị từ chối hoặc lỗi trong một lượt | `loop.max_denials_per_turn`. `loop-guard` đọc `denials` từ transcript. `stop-gate` ghi `denial-budget` | `test_the_denial_budget_blocks_retries_in_enforce_and_only_records_in_shadow` | Xong (ở shadow; enforce của loop-guard đang shadow) |
| FR-018 | Manifest khai rõ đã verify hay chưa | `verified: true|false` bắt buộc, kèm ADAPT-CHECKLIST | `test_rejects_bad_config_at_load` (trường hợp `verified missing`, `verified not a boolean`) | Xong (chỉ là khai báo, không chặn gì) |

## Mục tiêu thành công

| ID | Tiêu chí | Bằng chứng tầng máy | Trạng thái |
|---|---|---|---|
| SC-001 | Đọc được trên một màn hình: task đang chạy, thuộc repo nào, đã verify chưa | Lệnh `status` (FR-014) | Chưa, vì thiếu lệnh |
| SC-002 | Không task nào được coi là xong khi thiếu chứng cứ | `stop-gate` ở `enforce`: lượt có verify đỏ bị tiếp tục. `orca-check --strict` thất bại với `CLAIMED-DONE BUT ABSENT` | `test_stop_gate_blocks_a_failing_verification_in_enforce`, `test_stop_gate_gives_up_after_the_continuation_budget`, `test_claimed_done_but_absent_fails_the_strict_check` | Xong ở mức hook |
| SC-003 | Cài không mất file của dự án | `test_an_unmarked_scripts_directory_is_never_removed` | Xong |
| SC-004 | Sửa một hook trong manifest đổi đúng một chỗ, và sửa tay file sinh ra làm cổng đỏ | `test_editing_a_live_file_by_hand_fails_the_gate` | Xong |
| SC-005 | Agent nhận đủ Global constraints, Files và Interfaces của task | `brief.brief` (test so khớp). Chưa có đường giao brief qua dispatch | Một phần |
| SC-006 | Cài bản mới không làm phiên nào bị chặn thêm, trừ guard đã khai enforce | `test_the_shipped_manifest_is_unverified_and_only_orca_guard_enforces`, và `orca-guard` chỉ chặn status ngoài enum | Xong. Thay thế SC cũ, vì `ge-backcompat-test.sh` là của setup |

## Không làm

- Không đăng ký vào `fdk-gate`, `mechanisms.yaml`, `harness-doctor`, validator mirror, policy parity, hay R20 / ADR-018 của setup. coding-agent không chạy trong đó.
- Không có hook nào gọi model (fresh evaluator, `evaluator.count`).
- Không có tool hay plugin MCP chặn thay cho hook (đề xuất gốc đã loại phương án C).
- Không hỗ trợ Windows trong giai đoạn này.

## Kế hoạch

- [x] T1 Manifest và gen: FR-001, FR-002, FR-010, FR-018.
- [x] T2 Vòng đời hook: `orca-guard`, `loop-guard`, `prompt-reset`, `stop-gate`, `pre-compact`, `session-restore`.
- [x] T3 Hoàn thành có chứng cứ: FR-007.
- [x] T4 Cách ly theo repo: FR-006, FR-008.
- [x] T5 Guard vòng lặp và denial budget: FR-017.
- [x] T6 An toàn cài đặt: FR-004 (install.py). Cổng FR-003 ở mức lệnh.
- [ ] T6b Cài từ commit đã ghim: FR-005. Cần commit đã publish.
- [ ] T7a Lệnh `status`: FR-014, SC-001.
- [ ] T7b Dispatch trong adapter và kịch bản e2e đủ chuỗi: FR-013.
- [ ] T7c Cổng chạy tự động: thêm pre-push hook hoặc CI gọi `coding_agent.gate` và `pytest`.

## Ma trận yêu cầu

| Task | FR |
|---|---|
| T1 | FR-001, FR-002, FR-010, FR-018 |
| T2 | FR-009, FR-011, FR-016 |
| T3 | FR-007 |
| T4 | FR-006, FR-008 |
| T5 | FR-017 |
| T6 | FR-003, FR-004 |
| T6b | FR-005 |
| T7a | FR-012, FR-014 |
| T7b | FR-013 |
| T7c | FR-003 |

## Giả định

- (default) Orca trả `result.task.id` ở `task-create` và `task-update`, và trả `result.tasks` hoặc một danh sách ở `task-list`. Chưa đo trên Orca thật. Cần chạy `orca orchestration task-list --json` và sửa `tasks_of` / `task_id_of` nếu khác.
- (default) Một tool call bị từ chối hoặc lỗi là một `tool_result` có `is_error: true` trong transcript.

## Quyết định đã chốt

1. `stop-gate` ở `enforce` (SC-002). Lượt có verify đỏ bị tiếp tục tối đa `max_continuations` lần.
2. `in_progress` đã bỏ khỏi enum, khớp CLI đang cài.
3. coding-agent là repo riêng, tách khỏi `harness/`.

## Câu hỏi còn mở

1. [CẦN LÀM RÕ] FR-005: publish repo coding-agent lên đâu (GitHub nào, tên gì), và ghim SHA nào. Hiện repo chưa có remote, nên chưa có SHA để ghim.

## Rủi ro

- Giả định JSON của Orca sai: adapter lỗi ở production nhưng test vẫn xanh vì fake theo giả định. Giảm bằng ADAPT-CHECKLIST mục 3.
- `orca-guard` và `stop-gate` ở enforce có thể chặn sai nếu giả định về enum hoặc verify lệch thực tế. Giảm bằng cách chạy shadow trước khi đặt `verified: true`.
- Denial đếm theo `is_error`: một lệnh shell lỗi bình thường cũng được tính. Giảm bằng ngưỡng 5 và chế độ shadow.
- Cổng FR-003 không tự chạy nếu chưa có pre-push hook hoặc CI (T7c).

## Origin

- Nguồn: đề xuất `031026-harness-claude-orca-integration` (`harness/setup`, `T-261003-01`).
- Chưa commit.
