---
type: draft
title: Auto-decide — agent tự chọn khi user không trả lời sau N giây
status: proposed
tags: [propose, hooks, ask-user, auto-decide]
timestamp: 2026-10-12
task:
---

# 121026-auto-decide-timeout

**Status:** proposed
**Proposed:** 2026-10-12
**Sequence diagram:** [121026-auto-decide-timeout-seq.html](121026-auto-decide-timeout-seq.html)

## Request (restate)
Cho user bật chế độ **auto-decide**: mỗi lần agent định hỏi user, nó chờ tối đa N giây (mặc định 60, user cấu hình được); user trả lời trong khoảng đó thì dùng câu trả lời của user, hết giờ mà không có trả lời thì agent tự chọn phương án và chạy tiếp.

## Context
- Repo này không có `llmwiki/`; nguồn thiết kế gốc là `harness/setup` → `031026-harness-claude-orca-integration.md` (README). Draft đặt ở `docs/proposals/` cạnh `docs/proposal-fr-mechanisms.md`.
- `integration.yaml` là **manifest duy nhất**: mọi hook khai `id/host/event/matcher/mode/assumption`; `gen.py` sinh `.claude/settings.json` từ đó, `gate.py` báo drift (FR-003). Hook mới phải đi qua đường này.
- `hooks/handlers.py:REGISTRY` — tên hook → handler; mỗi handler ghi quyết định bằng `ctx.note(...)` vào `.coding-agent/events.jsonl`.
- Hiện có 3 chỗ "agent hỏi user":
  1. Session chính gọi tool **`AskUserQuestion`** (hộp chọn trong terminal Claude Code, **không có timeout**).
  2. Worker hỏi coordinator qua `orca orchestration ask` (`orca_cli.open_questions`, `coordinator.question_lines`); hợp đồng coordinator (`coordinator.py:257`) bảo coordinator "hỏi người dùng trong hội thoại trước rồi mới trả lời worker". Worker bị cấm `AskUserQuestion` (`orca_cli.py:985`).
  3. Prompt xin quyền `permissionDecision: "ask"` của `coordinator-guard` (xoá worktree chưa gộp, sửa cấu hình hook — `coordinator.needs_user_confirmation`).
- Đã có tiền lệ tôn trọng lựa chọn "không hỏi" của user: `coordinator_guard` bỏ prompt khi `permission_mode == bypassPermissions` và ghi `confirm-skipped-bypass`.

## Unknowns đã soi (bước 0b)
- (a) Ý định: "agent hỏi user" — hiểu là cả session chính lẫn câu hỏi worker leo thang lên user. Prompt xin quyền (3) là cổng an toàn, KHÔNG thuộc phạm vi → Non-goals.
- (c) Hình dạng dữ liệu: input của `AskUserQuestion` = `questions[]{question, header, options[]{label, description}, multiSelect}` + trường `answers` (map câu hỏi → nhãn). Đây là chỗ hook điền câu trả lời.
- (d) Môi trường: Windows 11 + Python có `tkinter` 8.6 (đã kiểm `python -c "import tkinter"`). macOS/Linux headless có thể không có display.
- (e) Hai điều phụ thuộc Claude Code chưa đo trên phiên thật → ghi ở Assumptions dạng find-out-later.

## Global constraints
- Manifest duy nhất: mọi hook và cấu hình mới khai trong `integration.yaml` + `src/coding_agent/template.yaml`; `.claude/settings.json` chỉ được sinh bằng `python -m coding_agent.gen`, không sửa tay; `python -m coding_agent.gate --manifest integration.yaml` phải thoát 0.
- Mọi hook không `off` bắt buộc có `assumption`. Hook mới bắt đầu ở `mode: shadow` (FR-010) và manifest giữ `verified: false`.
- Cấu hình sai bị từ chối ngay lúc nạp manifest (`manifest.py`), không âm thầm thay bằng mặc định.
- Đường dẫn trong cấu hình sinh ra là tương đối, không tuyệt đối (`gen.py`).
- Mỗi quyết định của hook ghi qua `ctx.note(guard=..., kind=..., applied=..., detail=...)`.
- Không thêm dependency ngoài `pyyaml>=6` (runtime) và `pytest>=8` (test); `tkinter` là stdlib, được phép nhưng phải có đường lùi khi thiếu.
- Verify: `python3 -m pytest -q` xanh trước khi commit.

## Non-goals
- Không tự quyết **prompt xin quyền** (`permissionDecision: "ask"` của `coordinator-guard`: xoá worktree/nhánh chưa gộp, sửa cấu hình hook). Đó là cổng an toàn; auto-decide không bao giờ trả lời thay.
- Không hỗ trợ Codex ở bản này (Codex không có `AskUserQuestion`).
- Không đổi cách worker hỏi coordinator (`orca orchestration ask`) và không cho coordinator tự trả lời worker theo timer riêng.
- Không làm UI cấu hình; bật/tắt bằng CLI + manifest.
- Không cho Fable tự bịa phương án mới ngoài danh sách lựa chọn — Fable chỉ được chọn trong các phương án agent đã đưa ra.

## Approaches
1. **Hook PreToolUse trên `AskUserQuestion` + cửa sổ đếm ngược cục bộ (CHỌN).** Khi auto-decide bật, hook chặn lời gọi, mở một cửa sổ nhỏ (tkinter, chạy subprocess) hiện câu hỏi, các lựa chọn và đồng hồ đếm ngược. User bấm → dùng câu trả lời đó; hết giờ → hook chọn phương án khuyến nghị. Hook trả `permissionDecision: allow` + `updatedInput.answers` nên tool chạy như user đã trả lời; kèm `additionalContext` báo "tự chọn sau 60s" để agent nói lại với user.
   - ➕ Một cơ chế phủ cả câu hỏi của session chính lẫn câu hỏi worker mà coordinator leo thang (coordinator cũng hỏi bằng `AskUserQuestion`). Code nằm gọn trong hook + module thuần, test được.
   - ➖ Trong lúc chờ, user trả lời ở **cửa sổ popup**, không phải trong terminal (terminal đang bị hook giữ). Không có display / thiếu tkinter → lùi về hỏi bình thường (không timeout).
2. **Hook từ chối `AskUserQuestion`, bắt agent gọi lệnh `coding_agent.cli ask --timeout 60` (Bash chặn N giây), user trả lời bằng `cli answer` ở terminal khác hoặc file.** ➕ Không cần GUI. ➖ User phải mở terminal thứ hai và biết id câu hỏi — khó dùng hơn hẳn; output Bash không hiện liên tục cho user nên dễ không thấy câu hỏi.
3. **Chỉ sửa prompt/hợp đồng ("hỏi rồi chờ 60s").** ➖ Không khả thi: model không đo được thời gian chờ, và hộp `AskUserQuestion` của Claude Code không có timeout — nó chờ vô hạn. Loại.

Chọn **1** vì là cách duy nhất vừa giữ được quyền trả lời của user trong 60s vừa thật sự tự chạy tiếp khi hết giờ, mà không bắt user đổi thói quen làm việc ngoài một popup.

## Requirements (FR)
- **FR-001**: Hệ thống PHẢI có cấu hình `auto_decide` trong manifest gồm `enabled` (mặc định `false`) và `timeout_s` (mặc định `60`, nguyên trong khoảng 10–3600); giá trị ngoài khoảng bị từ chối lúc nạp manifest.
- **FR-002**: User PHẢI bật/tắt và đổi thời gian chờ được bằng lệnh `python -m coding_agent.cli auto-decide on|off|status [--timeout N]`, có hiệu lực từ câu hỏi kế tiếp mà không cần sinh lại `.claude/settings.json`; giá trị CLI ghi đè giá trị manifest.
- **FR-003**: Khi auto-decide tắt, `AskUserQuestion` PHẢI chạy y như hiện nay (hook không can thiệp).
- **FR-004**: Khi auto-decide bật, hệ thống PHẢI hiện câu hỏi, các lựa chọn và thời gian còn lại cho user, và dùng câu trả lời của user nếu có trước khi hết giờ.
- **FR-005**: Hết giờ mà user chưa trả lời, hệ thống PHẢI gọi model **Fable** (`claude-fable-5-1`) với câu hỏi, các lựa chọn và ngữ cảnh gần nhất của phiên để chọn một phương án trong danh sách (kèm một câu lý do). Fable lỗi, quá hạn (`fable_timeout_s`) hoặc trả nhãn ngoài danh sách thì dùng phương án được agent gợi ý: nhãn chứa `(Recommended)`/`(Khuyến nghị)`, không có thì phương án đầu tiên; câu `multiSelect` lấy mọi phương án gợi ý, không có thì phương án đầu.
- **FR-010**: User PHẢI tắt được việc gọi Fable (`auto_decide.fable: false` hoặc `cli auto-decide --no-fable`); khi tắt, hết giờ dùng thẳng phương án được gợi ý.
- **FR-006**: Sau mỗi lần tự chọn, agent PHẢI nhận được ghi chú "đã tự chọn X sau N giây vì không có trả lời (người chọn: Fable | gợi ý của agent; lý do: …)" để nói lại với user, và hệ thống PHẢI ghi event `auto-decided` (câu hỏi, lựa chọn, thời gian chờ).
- **FR-007**: Không hiện được cửa sổ chờ (không display, thiếu tkinter, lỗi subprocess), hệ thống PHẢI lùi về hỏi bình thường và ghi event `auto-decide-fallback`; không bao giờ tự chọn mà user không được thấy câu hỏi.
- **FR-008**: Câu hỏi về thao tác phá huỷ hoặc ra bên ngoài (xoá, ghi đè, commit/push, gửi ra ngoài) cũng PHẢI được tự chọn khi hết giờ như mọi câu khác, theo đúng FR-005 — **không** ưu tiên phương án "an toàn", chọn phương án được gợi ý (user quyết 2026-10-12). Ghi chú FR-006 và event `auto-decided` của các câu này PHẢI gắn cờ `sensitive: true` để dễ rà lại.
- **FR-009**: Khi auto-decide bật, hợp đồng coordinator PHẢI yêu cầu hỏi user bằng `AskUserQuestion` (không hỏi bằng văn bản rồi kết thúc lượt) và đặt phương án an toàn/khuyến nghị lên đầu, để câu hỏi worker leo thang cũng được hưởng timeout.

## Success criteria (SC)
- **SC-001**: Với auto-decide bật và user vắng mặt, một phiên có câu hỏi chạy tiếp sau đúng thời gian đã cấu hình (±5 s) mà không cần ai chạm vào. *Bằng chứng: test hook với timeout 1 s trả `answers` khuyến nghị; thử tay trên Windows với 60 s.*
- **SC-002**: User có mặt trả lời được trong thời gian chờ và câu trả lời của họ luôn thắng phương án tự chọn. *Bằng chứng: test giả lập cửa sổ trả về lựa chọn thứ 2.*
- **SC-003**: User đổi thời gian chờ bằng một lệnh và lần hỏi kế tiếp dùng giá trị mới. *Bằng chứng: test CLI + hook đọc state.*
- **SC-004**: Sau mỗi lần tự chọn, user đọc được trong câu trả lời của agent là câu nào đã bị tự chọn và chọn gì. *Bằng chứng: `additionalContext` + event `auto-decided` trong `events.jsonl`.*
- **SC-005**: Khi auto-decide tắt, trải nghiệm hỏi đáp không đổi gì so với hiện tại. *Bằng chứng: test hook trả output rỗng; `gate` thoát 0.*

## Assumptions
- Phạm vi "agent hỏi user" = `AskUserQuestion` của session chính, gồm cả câu coordinator hỏi lại user thay worker; không gồm prompt xin quyền. (default)
- Mặc định `enabled: false` — user phải chủ động bật. (default)
- Khoảng hợp lệ của `timeout_s`: 10–3600 giây. (default)
- Cấu hình người dùng bật bằng CLI lưu ở `.coding-agent/state/auto-decide.json` (theo repo, không theo phiên), ghi đè manifest; không sửa `integration.yaml` để tránh prompt xác nhận cấu hình hook của `coordinator-guard`. (default)
- Chỗ user trả lời trong lúc chờ là cửa sổ popup tkinter (luôn nằm trên cùng, có đồng hồ đếm ngược, nút cho từng lựa chọn + nút "Chọn khuyến nghị ngay"). (default)
- Mô-đun mới tên `src/coding_agent/autodecide.py`; hook id `auto-decide`, host `claude_code`, event `PreToolUse`, matcher `AskUserQuestion`, khởi đầu `mode: shadow` — ở shadow hook vẫn chờ và điền câu trả lời (chức năng do user bật, không phải guard chặn), mode chỉ quyết có ghi thêm cảnh báo hay không. (default)
- Claude Code chấp nhận `permissionDecision: "allow"` + `updatedInput` có `answers` cho `AskUserQuestion` trong PreToolUse hook và bỏ qua hộp hỏi. (default, find-out-later → kiểm trên phiên thật; sai thì lùi về `deny` + lý do chứa lựa chọn đã tự chọn)
- Hook phải được khai `timeout` ≥ `timeout_s` + 15 s trong settings sinh ra, nếu không Claude Code giết hook trước khi hết giờ chờ; vì giá trị CLI có thể lớn hơn manifest, `gen.py` khai timeout theo trần 3600 + 15. (default, find-out-later → đo timeout mặc định của hook trên bản Claude Code đang dùng)
- Câu hỏi phá huỷ/ra ngoài: **được tự chọn, theo phương án được gợi ý, không ưu tiên phương án an toàn** — user quyết 2026-10-12 (đã trả lời câu cần làm rõ). Nhận diện "nhạy cảm" chỉ để gắn cờ, không để chặn.
- Fable được gọi qua Claude Code headless: `claude -p --model claude-fable-5-1 --output-format json` với prompt chỉ cho phép trả một nhãn trong danh sách + một câu lý do — dùng phiên đăng nhập sẵn của user, không cần API key, không thêm dependency. (default)
- Ngữ cảnh gửi cho Fable: câu hỏi, các lựa chọn, prompt gần nhất của user và tối đa ~4000 ký tự cuối của lượt hiện tại lấy từ transcript (`transcript.py`). (default)
- `fable_timeout_s` mặc định 90 giây; `fable` mặc định `true` khi auto-decide bật. Timeout hook trong settings = trần `timeout_s` + `fable_timeout_s` + 15. (default)
- Lệnh `claude` có trên PATH khi hook chạy (hook chạy bên trong Claude Code). (default, find-out-later → kiểm trên Windows; thiếu thì coi như Fable lỗi và dùng phương án gợi ý)

## Impact — file bị ảnh hưởng
| File | Thay đổi |
|---|---|
| `integration.yaml`, `src/coding_agent/template.yaml` | thêm khối `auto_decide` và hook `auto-decide` |
| `src/coding_agent/manifest.py` | đọc/kiểm `auto_decide`; hook có thể khai `timeout_s` |
| `src/coding_agent/gen.py` | ghi `timeout` cho entry hook khi có |
| `src/coding_agent/autodecide.py` (mới) | đọc cấu hình hiệu lực, chọn phương án, dựng output hook, cửa sổ đếm ngược |
| `src/coding_agent/hooks/handlers.py` | handler `auto_decide` + đăng ký `REGISTRY` |
| `src/coding_agent/cli.py` | lệnh `auto-decide on|off|status [--timeout N]` |
| `src/coding_agent/coordinator.py` | thêm một dòng hợp đồng khi auto-decide bật |
| `src/coding_agent/project_install.py` | không đổi logic; `--add-new-hooks` sẽ mang hook mới vào dự án đã cài |
| `README.md`, `tests/` | tài liệu + test mới |

## Hành vi hiện có có thể vỡ
- `gate`/`gen --check`: thêm trường `timeout` vào entry hook làm settings đã cài của dự án khác bị báo drift cho tới khi chạy lại `gen` / `--add-new-hooks`.
- `test_shipped_manifest.py`, `test_manifest_gen.py`: snapshot manifest/settings đổi → phải cập nhật.
- `coordinator.contract`: thêm dòng có điều kiện; test hợp đồng hiện có không được đổi khi auto-decide tắt.
- Hook chạy subprocess GUI: nếu cửa sổ treo, phiên bị giữ tới timeout của hook — do đó subprocess tự đóng ở `timeout_s` và hook kill nó ở `timeout_s + 5`.

## Plan
- [ ] **T1 — Cấu hình:** khối `auto_decide {enabled, timeout_s, fable, fable_timeout_s}` trong manifest + template, kiểm khoảng; `Hook.timeout_s` tuỳ chọn; `gen.py` ghi `timeout`; khai hook `auto-decide`.
- [ ] **T2 — CLI + state:** `cli auto-decide on|off|status [--timeout N]` ghi/đọc `.coding-agent/state/auto-decide.json`; `autodecide.effective(root, manifest)` trả cấu hình hiệu lực (CLI > manifest).
- [ ] **T3 — Logic chọn thuần:** `autodecide.suggested(questions)` (phương án gợi ý, nhánh lùi của FR-005); `autodecide.is_sensitive(question)` chỉ để gắn cờ (FR-008); `autodecide.valid_answer(questions, answers)` loại nhãn ngoài danh sách; `autodecide.hook_output(questions, answers, auto)` dựng JSON PreToolUse.
- [ ] **T4 — Cửa sổ đếm ngược:** `python -m coding_agent.autodecide prompt` đọc câu hỏi từ stdin, hiện popup tkinter có đếm ngược, in JSON câu trả lời hoặc `{"timeout": true}`; tự đóng ở `timeout_s`.
- [ ] **T8 — Fable chọn khi hết giờ:** `autodecide.ask_fable(questions, context, timeout_s)` chạy `claude -p --model claude-fable-5-1`, parse nhãn + lý do, kiểm bằng `valid_answer`; lỗi/quá hạn → `None`.
- [ ] **T5 — Hook `auto-decide`:** handler gọi T2→T4→(hết giờ: T8, lùi T3)→output; ghi event `auto-decided` / `answered-by-user` / `auto-decide-fallback`; đăng ký `REGISTRY`.
- [ ] **T6 — Hợp đồng coordinator:** khi auto-decide bật, thêm dòng yêu cầu hỏi bằng `AskUserQuestion` và đặt phương án khuyến nghị lên đầu.
- [ ] **T7 — Test + README:** test T1–T6 (cửa sổ được giả lập qua biến môi trường), cập nhật snapshot, mục "Auto-decide" trong README.

## Agent Task Assignment
| Task | Agent (CLI) | Lý do chọn | Status |
|---|---|---|---|
| T1 | codex | sửa cấu hình + validator theo khuôn có sẵn, rẻ | pending |
| T2 | codex | lệnh CLI theo mẫu các lệnh khác trong `cli.py` | pending |
| T3 | codex | hàm thuần, test dễ | pending |
| T4 | claude | GUI + xử lý tiến trình/timeout đa nền tảng, dễ sai tinh vi | pending |
| T8 | claude | prompt + parse đầu ra model, xử lý lỗi/timeout tiến trình con | pending |
| T5 | claude | ghép hook với output Claude Code chưa kiểm chứng (`updatedInput`), cần phán đoán | pending |
| T6 | codex | thêm một dòng hợp đồng có điều kiện | pending |
| T7 | codex | test + tài liệu theo spec đã rõ | pending |

## Render brief
- **T1:** user/manifest (legacy) → `manifest.load` kiểm `auto_decide` (add) → giá trị sai bị từ chối (block) → `gen.py` ghi entry hook kèm `timeout` (add) → `gate` thấy khớp (legacy).
- **T2:** user chạy `cli auto-decide on --timeout 90` (add) → ghi `state/auto-decide.json` (add) → `status` in giá trị hiệu lực (add) → `--timeout 5` bị từ chối (block).
- **T3:** hook đưa `questions` (legacy) → `suggested` tìm `(Recommended)` (add) → không có thì phương án đầu (add) → `is_sensitive` chỉ gắn cờ (add) → `valid_answer` loại nhãn lạ (block).
- **T8:** hết giờ (add) → hook gọi `claude -p --model claude-fable-5-1` (add) → Fable trả nhãn + lý do (add) → nhãn lạ / lỗi / quá 90 s → dùng phương án gợi ý (block).
- **T4:** hook mở subprocess popup (add) → user bấm trong hạn → trả lựa chọn (add) → hết giờ → `{"timeout": true}` (add) → không display → lỗi, hook lùi (block).
- **T5:** Claude Code gọi `AskUserQuestion` (legacy) → hook `auto-decide` đọc cấu hình (add) → tắt: không làm gì (legacy) → bật: popup → hết giờ: Fable rồi lùi gợi ý → `allow` + `answers` + `additionalContext` (add) → fallback hỏi thường (block).
- **T6:** SessionStart (legacy) → `contract()` (legacy) → thêm dòng auto-decide nếu bật (add) → coordinator hỏi bằng `AskUserQuestion`, timeout áp dụng (add).
- **T7:** pytest với popup giả lập (add) → snapshot cập nhật (add) → gate 0 (legacy).
Prose chi tiết từng task: xem file HTML đi kèm.

## Self-review
1. **Phủ yêu cầu:** bật tính năng → FR-001/002 (T1, T2); chờ 60 s rồi tự chọn → FR-004/005 (T3, T4, T5); cấu hình thời gian chờ → FR-001/002 (T1, T2). Thêm FR-006/007/008/009 cho minh bạch, đường lùi, an toàn và đường câu hỏi worker.
2. **Placeholder:** không còn TBD/TODO. Không còn câu cần làm rõ.
2b. **Cập nhật 2026-10-12:** câu cần làm rõ đã được user trả lời (phương án 2, chọn phương án được gợi ý, không ưu tiên an toàn) + thêm Fable (FR-005, FR-010, T8).
3. **Nhất quán tên:** `auto_decide` (khoá manifest), `auto-decide` (hook id + lệnh CLI), `autodecide` (module), `timeout_s` dùng thống nhất; event `auto-decided` / `answered-by-user` / `auto-decide-fallback`.
- Task ID: repo này không có `harness/scripts/code-logger.py` → để trống `task:`.
