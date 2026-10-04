# coding-agent

Harness gắn chặt vào Claude Code CLI và Orca, được triển khai theo đề xuất
`llmwiki/wiki/sources/draft/031026-harness-claude-orca-integration.md` của repo `harness/setup`
(task `T-261003-01`). Bộ nhớ của agent dùng Zero-Mem (`zm`) với quy tắc episode của memory-distill.

Trạng thái: **chưa verified.** Hai hook ở `enforce`: `orca-guard` (FR-009, chặn status Orca ngoài enum)
và `stop-gate` (SC-002, lượt có verify đỏ bị tiếp tục tối đa `max_continuations` lần). Các hook còn lại
ở `shadow`: ghi quyết định, không chặn.

Repo này là package độc lập, không nằm trong `harness/setup`.

## Cấu trúc

| Đường dẫn | Vai trò |
|---|---|
| `integration.yaml` | Manifest duy nhất: hook, mode, assumption, verify, bộ nhớ, ngưỡng loop, cờ `verified` |
| `src/coding_agent/manifest.py` | Đọc và kiểm manifest; cấu hình sai bị từ chối ngay lúc nạp |
| `src/coding_agent/gen.py` | Sinh `.claude/settings.json` và `.codex/hooks.json` với đường dẫn tương đối; `--check` báo drift |
| `src/coding_agent/hooks/` | Sáu hook: `orca-guard`, `loop-guard`, `prompt-reset`, `stop-gate`, `pre-compact`, `session-restore` |
| `src/coding_agent/transcript.py` | Đọc một lượt từ transcript, có số dòng làm bằng chứng và số lần lỗi/bị từ chối |
| `src/coding_agent/orca.py` | Quy tắc Orca thuần: completion cần bằng chứng, task thuộc một repo, bằng chứng `CLAIMED-DONE BUT ABSENT` |
| `src/coding_agent/orca_cli.py` | Adapter tới `orca orchestration`: tạo task, claim, đổi trạng thái, liệt kê |
| `src/coding_agent/brief.py` | Brief cho một task của PLAN: Global constraints, Files, Interfaces (SC-005) |
| `src/coding_agent/memory/` | Zero-Mem: spool, truy hồi qua `zm mcp`, episode, ghi lượt |
| `src/coding_agent/install.py` | Cài và gỡ an toàn: không xoá thư mục không có dấu của harness |
| `src/coding_agent/cli.py` | Lệnh cho agent: `recall`, `stats`, `report`, `forget`, `brief`, `status`, `orca-create`, `orca-status`, `claim`, `orca-check` |
| `src/coding_agent/project_install.py` | Cài package, manifest và hook vào một dự án (FR-001, FR-004) |
| `src/coding_agent/gate.py` | Cổng của package (FR-003): thất bại khi file đang dùng lệch manifest |
| `tests/` | Test gồm `fixtures/fake_orca.py` làm Orca giả lập cho FR-013 |

## Cổng (FR-003)

```sh
PYTHONPATH=src python3 -m coding_agent.gate --manifest integration.yaml
```

Thoát 0 khi file đang dùng khớp manifest, 1 khi lệch hoặc thiếu, 2 khi manifest sai. Package này
dùng cổng riêng và không đăng ký vào `fdk-gate` của `harness/setup`: coding-agent không còn dùng
harness đó nữa.

## Chạy thử

```sh
uv venv .venv && uv pip install --python .venv/bin/python "pyyaml>=6" "pytest>=8"
.venv/bin/python -m pytest -q
```

Test `hooks` và `zeromem` cần binary `zm` trên `PATH` (hoặc `CODING_AGENT_ZM`); không có thì
các test đó được bỏ qua và báo rõ là bỏ qua. Test Orca dùng `fake_orca.py` qua `CODING_AGENT_ORCA`,
không cần Orca thật.

## Cài vào một dự án

Cần: Python 3.11 trở lên, git, và PyYAML trong `python3` mà hook chạy bằng. Nếu thiếu PyYAML, hook báo lỗi
trong stderr và ghi event `manifest-error`; cổng `gate` thoát 2.

```sh
# 1. Cài package, manifest, và hook vào dự án (không ghi đè manifest có sẵn)
PYTHONPATH=<đường dẫn coding-agent>/src python3 -m coding_agent.project_install --project <dự án> [--python-src harness/src] [--verify "python3 -m pytest -q"]
# 2. Cổng: thoát 0 khi file hook đang dùng khớp manifest
PYTHONPATH=<dự án>/harness/src python3 -m coding_agent.gate --manifest <dự án>/integration.yaml
```

Installer chép `src/coding_agent` vào `<dự án>/<python_src>/coding_agent`. Bản đã cài chỉ được thay khi
còn dấu harness; bản cũ được sao lưu vào `.coding-agent/backups/`. Thư mục không có dấu bị từ chối và
giữ nguyên. Lệnh hook dùng `$CLAUDE_PROJECT_DIR` (Claude Code) hoặc `$(git rev-parse --show-toplevel)`
(Codex), nên file sinh ra không chứa đường dẫn tuyệt đối của máy nào.

Lệnh `verify` trong manifest chạy trong PATH của hook. Dự án cần có công cụ đó (ví dụ pytest) trong PATH.

## Mode và assumption

- `mode` có ba giá trị: `off` (không chạy), `shadow` (ghi quyết định, không chặn), `enforce` (được chặn).
- Mặc định là `shadow` khi manifest không khai `mode` (FR-010).
- Chỉ `orca-guard` ở `enforce` (FR-009). Đổi một hook sang `enforce` là quyết định riêng, phải ghi vào manifest.
- Mọi hook không `off` bắt buộc có `assumption`. Thiếu thì manifest bị từ chối lúc nạp.
- Lưu ý YAML: `mode: off` không quote sẽ thành `false`. Loader báo lỗi và yêu cầu quote.
- Cờ `verified: false` là bắt buộc. Khi `false`, các ngưỡng và assumption là mặc định ước đoán.

## Bộ nhớ của agent

- Store là thư mục `<CODING_AGENT_ZEROMEM_HOME>/workspaces/<sha256(root)[:16]>` (mặc định
  `~/.coding-agent/zeromem`). Mỗi repo một store, nên hai repo không đọc lẫn của nhau.
- Mỗi lượt kết thúc, `stop-gate` ghi hội thoại và, khi lượt đổi file và verify đạt, một episode.
  Quy tắc lọc câu tạm thời và cắt độ dài theo memory-distill.
- UUID suy ra từ số dòng transcript, nên ghi lại cùng lượt không tạo bản sao: zm bỏ qua UUID đã có.
- `python3 -m coding_agent.cli recall "<câu hỏi>"` trả về các bản ghi liên quan nhất.
- Embedder: manifest đặt `hash` vì máy này không có model bge-small. Hash chỉ so khớp từ vựng.

## Orca

- Lệnh task của Orca chạy trong một Run. Đặt `CODING_AGENT_ORCA_RUN=<run_id>` hoặc truyền `--run`. Không có
  Run thì CLI trả `run_required`. `orca orchestration run-list` liệt kê các Run.
- `status [--run ID]` đọc task của Run qua CLI thật và chỉ hiện task do repo này tạo, theo sổ liên kết.

- `orca-create` tạo task với `project` (tên repo) và `T-id` trong spec, rồi ghi liên kết vào
  `.coding-agent/links.jsonl` (FR-006).
- `claim` chỉ cho phép khi task thuộc repo hiện tại. Từ chối thì ghi event `claim-refused` (FR-008).
- `orca-status --status completed` cần `--basis predicate|verifier|human`. Thiếu basis hoặc
  `agentReported` thì không gọi Orca, ghi `unverified` (FR-007).
- `orca-check --tasks FILE --strict` thoát 1 khi có task `completed` mà artifact khai báo bị thiếu.

Adapter giả định `task-create` và `task-update` trả `result.task.id`, và `task-list` trả
`result.tasks` hoặc một danh sách. Giả định này chưa đo trên Orca thật (ADAPT-CHECKLIST, mục 3).

## Các task của đề xuất

| Task | Trạng thái | Ghi chú |
|---|---|---|
| 1. Manifest và trình sinh | Xong | `gen` chỉ đề xuất, `--apply` mới ghi. Vị trí manifest vẫn là repo này, không phải `harness/integration.yaml` của setup |
| 2. Vòng đời Claude Code ↔ Orca | Xong ở shadow | `orca-guard`, `prompt-reset`, `stop-gate`, `pre-compact`, `session-restore` đã nối |
| 3. Hoàn thành có bằng chứng | Xong | `completion()` ở `orca.py`, `set_status()` ở `orca_cli.py` |
| 4. Cách ly theo repo | Xong | Claim từ repo khác bị từ chối và ghi event |
| 5. Guard vòng lặp và ngân sách | Xong ở shadow | `loop-guard` có trần lượt và denial budget; `stop-gate` ghi `denial-budget` |
| 6. An toàn cài đặt | Một phần | `install.py` và `project_install.py` đã chạy được trên dự án thử. Ghim commit khi cài (FR-005) chưa làm: xem dưới |
| 7. Fixture end-to-end | Một phần | Fake Orca CLI (có Run, `run_required`) và test CLI chạy thật. Chưa có dispatch nên chưa chạy đủ propose → gate → dispatch → verify. Đăng ký vào `fdk-gate`, `mechanisms.yaml`, `harness-doctor` của setup: bỏ, vì coding-agent tự đứng riêng |
| FR-014 / SC-001. Lệnh `status` | Xong | `status [--run ID]`, đọc task của Run qua CLI thật (đã kiểm với Orca cài trên máy) |

Các câu hỏi mở của đề xuất đã có câu trả lời (ghi trong đề xuất, mục Assumptions):

1. Trình sinh chỉ ghi `*.proposed.json`; `--apply` là thao tác có chủ ý.
2. Prompt của người dùng được giữ trong `.coding-agent/state/` theo phiên, tối đa 20 cái, hết hạn sau
   bảy ngày, chỉ trên máy của người dùng.

## Chưa làm, và vì sao

- **Dispatch:** chưa có lệnh giao task cho worker. Vì vậy chuỗi propose → gate → dispatch → verify chưa chạy được từ đầu đến cuối (FR-013).
- **Định dạng Orca chưa đo đủ:** `task-list` đã kiểm trên Orca thật (`result.tasks`, task có `task_title`, `result`).
  `task-create` và `task-update` chưa kiểm vì gọi sẽ tạo hoặc đổi trạng thái task thật. Giả định nằm ở `tasks_of` và `task_id_of`.
- **Enum status:** khớp CLI đang cài: `pending, ready, dispatched, completed, failed, blocked`.
  `in_progress` đã bỏ.
- **Ngoài package:** `harness/setup` không còn là nơi chạy coding-agent, nên các mục của setup
  (`fdk-gate`, `mechanisms.yaml`, `harness-doctor`, bootstrap) không áp dụng cho package này.
  - FR-005 (cài từ commit đã ghim): chưa làm. Repo `dragonwar000/coding-agent` đã có, PR #1 đang mở. Phải merge rồi ghim SHA.
  - `install.sh` của setup vẫn có `rm -rf` trên `fdk/tools` và `harness/scripts` (dòng 242). Không sửa theo
    quyết định của người duyệt. Repo này không dùng `install.sh` đó.
- **Độ phủ:** 82% dòng lệnh. Chưa đặt cổng 100% theo từng file, vì đề xuất không yêu cầu cho repo này.
- **Repo:** đã tách khỏi `harness/` thành repo riêng tại `~/Documents/Development/coding-agent`. Remote: `dragonwar000/coding-agent` (private).
