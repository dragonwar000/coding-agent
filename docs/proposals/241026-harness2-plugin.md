---
type: draft
title: Harness² — plugin tuỳ chọn tinh chỉnh harness của dự án (CLAUDE.local.md, .claude/skills)
status: proposed
tags: [propose, harness2, plugin, optional, improve]
timestamp: 2026-10-24
task: T-33c6357a
---

# 241026-harness2-plugin

**Status:** proposed
**Proposed:** 2026-10-24
**Sequence diagram:** [241026-harness2-plugin-seq.html](241026-harness2-plugin-seq.html)
**Tiền lệ:** đề xuất DSH đã duyệt `deepseek-harness/rudd/.agents/notes/proposed/feature/2026-10-24-harness2-plugin.md` (approach C, quyết định D1–D8) và plan của nó `…-plan.md` (engine protocol `dsh-harness-improve/1` ở T2).

## Request (restate)
Giống kế hoạch DSH đã duyệt: thêm Harness² vào coding-agent dưới dạng **plugin tuỳ chọn**. Khi cần, user bật nó và chạy một lệnh để Harness² tinh chỉnh những file harness mà coding-agent kiểm soát được; khi không cần, nó **tắt mặc định và hoàn toàn nằm im** — không import, không hook, không tốn gì, không lỗi, kể cả khi máy không cài harness2.

## Goal
Một user coding-agent gõ `python3 -m coding_agent.cli harness2 on`, rồi `python3 -m coding_agent.cli improve --task <file>`. coding-agent chạy harness2 như một chương trình ngoài trên các task của chính user, nhận lại hoặc "giữ harness hiện tại" kèm lý do, hoặc một diff giới hạn trong `CLAUDE.local.md` và `.claude/skills/**`. Chỉ khi user tự duyệt diff đó ở terminal thì file trong dự án mới đổi. User nào không bật thì coding-agent chạy y như hôm nay, từng byte cấu hình sinh ra giữ nguyên.

## Context
- **Tính năng tuỳ chọn có sẵn — khuôn để chép.** `auto_decide` (`manifest.py:AutoDecide`, `auto_decide_from`) là khối manifest `enabled: false` + lệnh `cli auto-decide on|off|status` ghi `.coding-agent/state/auto-decide.json`, state thắng manifest (`autodecide.effective`). `ratchet` và `evaluator` là khối `mode: "off"` mặc định; evaluator chạy một tiến trình con `claude -p` với `CODING_AGENT_EVALUATOR_NESTED=1` để phiên con không chạy lại hook (`evaluator.py:27`, `handlers.py:233`).
- **coding-agent kiểm soát gì trong một dự án.** `project_install.py` ghi `.claude/settings.json` (hook sinh từ `integration.yaml`), `.codex/hooks.json`, `harness/`, thêm `.coding-agent/` vào `.gitignore`. Hợp đồng coordinator (`coordinator.contract()`) và lời giao việc worker (`orca_cli.py`) là chuỗi Python trong package. coding-agent **chưa** quản lý `CLAUDE.md` hay `.claude/skills`. Worker chạy trong worktree riêng; `worktree_links.LINKED` liên kết phần cài chưa commit (`harness`, `integration.yaml`, `.claude/settings.json`, …) vào worktree đó.
- **Mọi module được import ở đầu file.** `cli.py:84` và `hooks/handlers.py:17` import thẳng `autodecide`, `evaluator`, `ratchet`… Một module plugin mới nếu theo lối này sẽ bị nạp ở mỗi lần hook chạy — trái yêu cầu "không import khi tắt".
- **Engine.** `/Users/thoaidd/orca/workspaces/harness2`, nhánh `feat/dsh-substrate` (Apache-2.0, Copyright 2026 Google LLC), remote `fork` = `github.com/dragonwar000/harness2` (private); không bao giờ push `origin`. `substrate/base.py` định nghĩa giao diện `Substrate` (`surface`, `materialise`, `execute`, `read_trajectory`, `preflight`). Có `codex.py` (codex-cli: `AGENTS.md` + `.agents/skills`, cần benchmark tự cấp launcher và endpoint Responses `HARNESS2_CODEX_*`), `dsh.py` (DSH qua Python SDK: always-on c1–c4 gộp vào `AGENTS.md`, skill ở `.dsh/skills`), `opencode.py`. **Không có substrate Claude Code**: `claude` CLI chỉ là improver (`core/proposer.py:_claude_cli`, đọc `total_cost_usd`, lọc env qua `_DROP_ENV`).
- **Trạng thái T2 của DSH (đã kiểm 2026-10-24).** HEAD của `feat/dsh-substrate` là `5115533`; chưa có `harness2/cli/dsh_improve.py`, chưa có protocol nào trong code. Nghĩa là đổi tên/tổng quát hoá protocol **lúc này** chưa phá code đã viết.
- **Worktree live** `/Users/thoaidd/Documents/Development/coding-agent-live` là bản các phiên đang dùng — đề xuất này không chạm nó.

## Unknowns đã soi (bước 0b)
- (a) Ý định: "thêm harness2 vào coding-agent" = chức năng "cải thiện harness của dự án đang dùng coding-agent", không phải cải thiện mã nguồn coding-agent. "Khi cần thì có thể không dùng" = tắt mặc định, nằm im khi tắt hoặc khi thiếu harness2.
- (b) Phạm vi file sửa được: xem **Editable set** — `CLAUDE.local.md` + `.claude/skills/**`; hợp đồng coordinator/worker loại khỏi bản này (lý do ở dưới).
- (c) Hình dạng dữ liệu: request/result JSON của engine, xem **Engine protocol**. Task file của user: YAML `{id, prompt, check?}`.
- (d) Môi trường: macOS (Darwin 25.6), Python ≥ 3.11 cho coding-agent; harness2 cần Python 3.12 + venv riêng (`uv`). Windows để sau như DSH.
- (e) Ba điều về Claude Code chưa đo trên máy thật → **T0 spike**, ghi ở Assumptions dạng find-out-later.

## Global constraints
- **Lõi: nằm im khi tắt.** Khi `harness2.enabled` là `false` (mặc định), khối vắng mặt, hoặc harness2 không cài: không module plugin nào được import lúc hook chạy hay lúc `cli` khởi động; `.claude/settings.json` và `.codex/hooks.json` sinh ra giống từng byte bản hiện tại; không tiến trình con, không file mới dưới `.coding-agent/harness2/`, không cảnh báo, không lỗi.
- **Không có hook mới.** Plugin chỉ sống trong lệnh CLI. Không đụng `REGISTRY`, không thêm entry vào `integration.yaml.hooks`.
- **Import lười.** `cli.py` chỉ import `coding_agent.harness_improve` bên trong nhánh của lệnh `harness2`/`improve`; `hooks/handlers.py` không bao giờ import nó. coding-agent **không bao giờ import package Python `harness2`** — chỉ chạy nó như tiến trình con qua console script (ranh giới license + không phụ thuộc).
- Manifest duy nhất: khối mới khai trong `integration.yaml` + `src/coding_agent/template.yaml`; cấu hình sai bị từ chối lúc nạp (`manifest.py`), không âm thầm thay bằng mặc định. Khối `harness2` tự kiểm được, không cần harness2 đã cài.
- Không thêm dependency ngoài `pyyaml>=6` (runtime) và `pytest>=8` (test).
- **Không chép, dịch, hay diễn giải lại mã, prompt, test của harness2 vào repo này.** Engine giả trong test viết từ protocol ở tài liệu này.
- Mọi sửa file của user phải qua `improve apply` với duyệt của người ở TTY; không cờ nào tắt được việc duyệt.
- Fork harness2: chỉ push remote `fork`, không bao giờ `origin`; file mới trong fork mang header Apache-2.0 như file xung quanh.
- Verify: `python3 -m pytest -q` xanh trước khi commit; `python -m coding_agent.gate --manifest integration.yaml` thoát 0.

## Approaches
1. **Bọc mỏng: `improve` gọi thẳng CLI harness2, harness2 tự ghi vào dự án.** ➕ Ít code nhất. ➖ harness2 quyết file nào sửa và ghi ở đâu — mất duyệt (D1), allowlist (D2), kiểm file đổi giữa chừng; coding-agent không có chỗ thay engine. Loại, giống approach A bên DSH.
2. **Port Propose–Probe–Compose sang Python trong coding-agent.** ➕ Cùng ngôn ngữ, không cần venv riêng. ➖ Là tác phẩm phái sinh của mã Apache-2.0: phải mang header, thông báo sửa đổi, license riêng trong một repo không có LICENSE; tách khỏi mã nghiên cứu; chất lượng phải đo lại từ đầu. Loại ở bản đầu, giống approach B bên DSH.
3. **coding-agent sở hữu quy trình, harness2 là engine ngoài qua một protocol chung (CHỌN).** coding-agent giữ mọi thứ user thấy và mọi lần ghi file: cấu hình, task, staging, ngân sách, lưu trữ run, diff, duyệt, apply. harness2 chạy như tiến trình con, đọc một request JSON, ghi một result JSON. Đây là approach C của DSH chuyển sang coding-agent.
   - ➕ Duyệt/allowlist/retention/cost cap nằm trong code coding-agent có test; một chỗ duy nhất để thay engine; không mã Apache nào vào repo.
   - ➖ Cần việc phía fork (substrate Claude Code + entry point), và user tự cài harness2.

Chọn **3**.

### Executor (ai chạy các rollout) — chọn trong approach 3
| | **3a. `ClaudeCodeSubstrate` mới trong fork (CHỌN)** | 3b. Dùng lại `CodexSubstrate` | 3c. Dùng lại `DshSubstrate` |
|---|---|---|---|
| Rollout giống phiên thật? | Có — cùng `claude`, cùng cách nạp `CLAUDE.local.md` và `.claude/skills` | Không — Codex đọc `AGENTS.md` + `.agents/skills`; phải ánh xạ file, bằng chứng tương phản đo hành vi Codex chứ không phải Claude Code | Không — tương tự, đo DeepSeek |
| Việc phía fork | ~400 dòng: surface, materialise, `claude -p --output-format stream-json`, renderer trajectory, preflight | Ít ở substrate, nhưng codex substrate không có launcher sẵn (benchmark phải cấp) + cần endpoint `HARNESS2_CODEX_*` | Gần như 0, nhưng cần DSH + Node + key DeepSeek trên máy coding-agent |
| Chi phí rollout | Cao hơn (Sonnet/Haiku qua đăng nhập Claude) → cost cap phải tính cả rollout | Tuỳ endpoint | Rẻ nhất (< 0.01 USD/rollout ở đợt đo DSH) |
| Phụ thuộc thêm | Không (claude đã có) | codex dist + endpoint | DSH runtime |

Chọn **3a**: harness được tinh chỉnh để Claude Code đọc; probe trên engine khác không chứng minh gì cho phiên Claude Code, và lý do tồn tại của Propose–Probe–Compose là đo tương phản hành vi trên đúng executor. 3c giữ làm phương án rẻ để thử nhanh nếu cost cap là trở ngại, nhưng không phải mặc định.

### Một engine protocol chung với DSH — khuyến nghị: CÓ
Tổng quát hoá `dsh-harness-improve/1` thành **`harness-improve/1`**, tham số hoá theo `substrate`, một console script `harness2-improve` phục vụ cả DSH lẫn coding-agent. Lý do:
- T2 của DSH chưa viết dòng nào (HEAD `5115533`), nên đổi bây giờ không mất gì; đợi sau sẽ thành hai entry point gần trùng nhau trong một fork phải tự bảo trì.
- Phần lớn T2 không phụ thuộc substrate: kiểm request, user-task adapter, cost cap trong `Proposer`, ghi result nguyên tử, xử lý tín hiệu, `--protocol`. Chỉ khác: tập `editable` hợp lệ, tên file overlay, khối launch của executor, phạm vi ngân sách.
- Khối `dsh` giữ nguyên hình dạng; phía DSH chỉ đổi hằng số protocol, thêm `"substrate": "dsh"`, và chuyển `improver.maxUsd` sang `budget`. Đây là sửa plan DSH đã duyệt → cần user đồng ý (câu hỏi Q1).

## Editable set — và vì sao
| Đường dẫn | Sửa được? | Lý do |
|---|---|---|
| `CLAUDE.local.md` | **Có** (overlay, ≤ `max_overlay_bytes`, mặc định 16 384) | Tương đương `AGENTS.local.md` của DSH: Claude Code nạp nó sau `CLAUDE.md`, là file riêng của user (thường gitignore), cộng thêm chứ không thay. Không đụng `CLAUDE.md` của team. |
| `.claude/skills/<name>/SKILL.md` và file `.md` khác trong thư mục skill | **Có** | Claude Code quét skill dự án ở đây; chỉ name + description quảng bá, thân đọc khi cần. |
| File không phải `.md` trong `.claude/skills/**` (script, binary) | Không | Cùng lý do D2 loại c7: mã chạy được, diff text không đủ để duyệt. |
| `CLAUDE.md`, `~/.claude/**`, `.claude/settings*.json`, `.claude/agents/**`, `integration.yaml`, `harness/` | Không | File chung của team, file toàn cục của user, hoặc cấu hình hook mà `coordinator-guard` đã bảo vệ; subagent (c5) và hook (c8) bị loại như D2. |
| Hợp đồng coordinator / lời giao việc worker | Không (bản này) | Là chuỗi Python trong package, dùng chung cho mọi dự án và có snapshot test; muốn probe phải dựng Orca + nhiều agent trong rollout — không executor nào đo được. Mở lại khi có cơ chế overlay hợp đồng theo dự án và bằng chứng SC-003. |

`editable` trong manifest chỉ nhận tập con của hai pattern `CLAUDE.local.md` và `.claude/skills/**`; mọi giá trị khác bị từ chối lúc nạp.

## Engine protocol `harness-improve/1` (thay `dsh-harness-improve/1`)
Request do coding-agent ghi (mode `0600`, không chứa bí mật, đường dẫn tuyệt đối):

```json
{
  "protocol": "harness-improve/1",
  "substrate": "claude_code",
  "runId": "hi-20261024T101500-1a2b3c4d",
  "runDir": "/repo/.coding-agent/harness2/runs/hi-20261024T101500-1a2b3c4d/engine",
  "resultPath": "/repo/.coding-agent/harness2/runs/hi-20261024T101500-1a2b3c4d/result.json",
  "tasks": [{ "id": "release-notes", "prompt": "Viết release notes cho người dùng từ changes/." }],
  "sourcesDir": "/repo/.coding-agent/harness2/runs/hi-…/workspace/sources",
  "baseDir": "/repo/.coding-agent/harness2/runs/hi-…/workspace/base",
  "editable": ["CLAUDE.local.md", ".claude/skills/**"],
  "instructionFile": "CLAUDE.local.md",
  "maxOverlayBytes": 16384,
  "k": 1,
  "mode": "parallel",
  "improver": { "command": "/opt/homebrew/bin/claude", "model": "sonnet", "sessionTimeoutSeconds": 900 },
  "budget": { "maxUsd": 3.0, "scope": "total" },
  "executor": { "model": "sonnet", "rolloutTimeoutSeconds": 600 },
  "claudeCode": { "command": "/opt/homebrew/bin/claude", "tools": ["Read", "Write", "Edit", "Glob", "Grep"], "maxTurns": 40 }
}
```

Bảng theo substrate (engine kiểm trước mọi lời gọi model, vi phạm → exit 2 + một dòng stderr `{"error": "<field>: <reason>"}`):

| `substrate` | `editable` ⊆ | `instructionFile` | khối launch | `budget.scope` mặc định |
|---|---|---|---|---|
| `dsh` | `.dsh/skills/**`, `AGENTS.local.md` | `AGENTS.local.md` | `dsh` (giữ nguyên plan DSH T2) | `improver` |
| `claude_code` | `.claude/skills/**`, `CLAUDE.local.md` | `CLAUDE.local.md` | `claudeCode` | `total` |

- `budget.scope: total` cộng `total_cost_usd` của cả improver lẫn rollout `claude -p`; engine dừng trước phiên improver **hoặc** rollout kế tiếp khi tổng ≥ `maxUsd`, ghi `stopReason: cost_cap`.
- `CLAUDE_CONFIG_DIR` đọc từ env của engine, không bao giờ từ request (như plan DSH T2).
- Result, `--protocol`, mã thoát (0/1/2/143), sự kiện progress trên stdout, ghi result nguyên tử — giữ nguyên như plan DSH T2, chỉ đổi hằng số protocol; `ledger.rollouts[]` có thêm `usd` cho substrate `claude_code`. `proposalDir` chỉ chứa `CLAUDE.local.md` (khi khác rỗng) và `.claude/skills/<name>/**`.
- Rollout `claude_code`: workspace = `sourcesDir` + cây đã render, `claude -p --output-format stream-json --verbose --model <executor.model> --permission-mode acceptEdits --allowedTools <tools> --max-turns <maxTurns>` trong thư mục scratch, `CLAUDE_CONFIG_DIR` riêng (xem D3'), không có `.claude/settings*.json` → không hook nào của coding-agent hay của user chạy trong rollout.

## Decisions — D1–D8 của DSH làm mặc định, và chỗ khác
| | DSH (đã duyệt) | coding-agent | Khác? |
|---|---|---|---|
| D1 Duyệt | Mọi sửa cần duyệt | Giống. Cơ chế: `improve apply <run>` in diff đầy đủ rồi đọc xác nhận từ **TTY**; stdin không phải TTY (Bash tool của agent, `claude -p`, CI) → từ chối. Không cờ `--yes`. Kiểm file đích không đổi từ lúc run bắt đầu (sha256). | Cơ chế khác, nguyên tắc giữ |
| D2 Editable | `.dsh/skills/**` + `AGENTS.local.md` | `.claude/skills/**` (chỉ `.md`) + `CLAUDE.local.md` | Ánh xạ, cùng nguyên tắc |
| D3 Improver & tiền | `claude` + `sonnet`, `CLAUDE_CONFIG_DIR` = AI Account mặc định của DSH, `maxImproverUsd = 2.00` chỉ tính improver | `claude` + `sonnet`; `CLAUDE_CONFIG_DIR` = thư mục riêng `~/.coding-agent/harness2/claude-home` user đăng nhập một lần, không bao giờ `~/.claude` của terminal; **`max_usd = 3.00` tính tổng improver + rollout** vì rollout cũng là Claude | **Khác → Q2** |
| D4 Lưu run | `$DSH_HOME/harness-improve/runs/<runId>/`, 0700, 30 ngày | `.coding-agent/harness2/runs/<runId>/` của repo (đã gitignore), 0700, `retention_days = 30` (1–3650); dọn khi chạy bất kỳ lệnh `harness2`/`improve` nào, không daemon | Chỉ khác vị trí |
| D5 Phiên làm task | Opt-in `allowSessionTasks: false`, `excludeGlobs` `.env*`, `*.pem`, `**/credentials*` | Giống: `allow_session_tasks: false`; prompt đầu tiên của phiên lấy qua `transcript.py`, hiện nguyên văn trước khi xác nhận | Không |
| D6 Kích hoạt | Thủ công | Thủ công: chỉ lệnh `improve`; không hook, không lịch, không dòng hợp đồng coordinator | Không |
| D7 Fork | Public fork, pin SHA | Fork **private** `dragonwar000/harness2` (thực tế hiện tại), remote `fork`, pin SHA trong README | Ghi nhận thực tế, không hỏi lại |
| D8 Check | Mỗi task có `check` tuỳ chọn, chạy sau engine | Giống; coding-agent chạy `check` trên deliverables của từng rollout, kết quả chỉ vào báo cáo | Không |

Hai câu hỏi còn treo bên DSH (điều khoản subscription cho `claude -p` chạy không người trông; Google CLA) **áp dụng nguyên** cho đây và không hỏi lại. Lưu ý: với 3a, rollout cũng chạy `claude -p` trên subscription, nên câu hỏi điều khoản nặng hơn.

## Manifest
```yaml
# Harness² (tuỳ chọn, 241026-harness2-plugin): `improve --task <file>` chạy harness2 như chương trình ngoài để đề xuất
# sửa CLAUDE.local.md và .claude/skills; chỉ `improve apply` ở TTY mới ghi file. Tắt: không import, không hook, không tốn gì.
# Bật/tắt bằng `python -m coding_agent.cli harness2 on|off`; giá trị đó ghi đè khối này.
harness2:
  enabled: false
  command: harness2-improve      # tên lệnh trên PATH hoặc đường dẫn tuyệt đối tới console script trong venv harness2
  improver_model: sonnet
  executor_model: sonnet
  max_usd: 3.00                  # tổng improver + rollout cho một run (0.10–50)
  k: 1                           # 1–4
  mode: parallel                 # parallel | sequential
  editable: ["CLAUDE.local.md", ".claude/skills/**"]
  max_overlay_bytes: 16384       # 1024–60000
  timeout_s: 1800                # cả run, 60–14400
  retention_days: 30             # 1–3650
  allow_session_tasks: false
  exclude_globs: [".env*", "*.pem", "**/credentials*"]
```
Kiểm lúc nạp (`manifest.harness2_from`, cùng khuôn `auto_decide_from`): kiểu và khoảng; `editable` tập con không rỗng của hai pattern; `command` là tên trần (`PYTHON_NAME`) hoặc đường dẫn tuyệt đối; `mode` trong enum; model là tên hợp lệ như `evaluator.model`. Khối vắng = mặc định (tắt). Không kiểm harness2 có cài hay không lúc nạp — đó là việc của `preflight`.

## Lệnh CLI
- `harness2 on|off|status`: ghi/đọc `.coding-agent/state/harness2.json` (state thắng manifest, như `auto-decide`). `status` in cấu hình hiệu lực + `installed: true|false` (chạy `<command> --protocol`, chỉ khi được gọi) + các run gần nhất.
- `improve [--task FILE ...] [--session ID ...] [--k N] [--max-usd X]`: kiểm `enabled`, preflight, kiểm task, staging, ghi request, khởi engine **tách khỏi terminal** (process group riêng do coding-agent giữ pid), in `runId` rồi thoát ngay — vì Bash tool của agent có trần 10 phút.
- `improve list | show RUN | cancel RUN | delete RUN`: liệt kê; xem kết quả (outcome, lý do, bằng chứng, diff, chi tiêu, kết quả check); huỷ cả cây tiến trình; xoá một run.
- `improve apply RUN`: chỉ từ TTY (D1).

Task file (YAML):
```yaml
id: release-notes                      # ^[a-z0-9][a-z0-9-]{0,63}$
prompt: "Viết release notes cho người dùng từ changes/."
check: { command: "python3 scripts/check_notes.py", timeout_s: 120 }   # tuỳ chọn, D8
```

## Non-goals
- Không chạy tự động/định kỳ, không hook, không dòng hợp đồng coordinator; không sửa file nào khi chưa duyệt.
- Không sửa hợp đồng coordinator, lời giao việc worker, `CLAUDE.md`, `.claude/settings*.json`, `.claude/agents/**`, hook, script trong skill.
- Không dùng Codex làm executor (worker Codex của coding-agent đọc `AGENTS.md` — để bản sau nếu có nhu cầu, cùng protocol với substrate `codex`).
- Không đưa bài học của reflector vào prompt phiên; không thư viện kinh nghiệm phía coding-agent.
- Không đóng gói harness2/Python 3.12 cùng coding-agent; không Windows ở bản này.
- Không chấm task bằng model judge.

## Requirements (FR)
- **FR-001**: Manifest PHẢI có khối `harness2` như mục Manifest, `enabled` mặc định `false`; khối vắng = tắt; giá trị sai bị từ chối lúc nạp với tên trường.
- **FR-002** *(lõi)*: Khi tắt (mặc định, khối vắng, hoặc `harness2 off`), hệ thống PHẢI không import `coding_agent.harness_improve` trong mọi đường hook và lúc `cli` khởi động, không sinh entry hook nào, không đổi một byte nào của `.claude/settings.json`/`.codex/hooks.json` sinh ra, không tạo file dưới `.coding-agent/harness2/`, không chạy tiến trình con.
- **FR-003** *(lõi)*: Khi harness2 không cài, nạp manifest, mọi hook và mọi lệnh khác PHẢI chạy như cũ; chỉ `harness2 status` báo `installed: false` kèm cách cài, và `improve` thoát 2 với thông báo nêu thứ còn thiếu — không ngoại lệ, không cảnh báo ở chỗ khác.
- **FR-004**: User PHẢI bật/tắt bằng `cli harness2 on|off|status`, hiệu lực ngay ở lệnh kế tiếp, không cần sinh lại settings; state ghi đè manifest.
- **FR-005**: Một run CHỈ bắt đầu bằng lệnh `improve` khi `enabled` hiệu lực là `true`; coding-agent không bao giờ tự khởi run từ hook, hợp đồng, hay lịch.
- **FR-006**: Preflight PHẢI kiểm `<command> --protocol` (đúng `harness-improve/1`), `claude` trên PATH, đăng nhập trong `CLAUDE_CONFIG_DIR` riêng (`claude auth status`), và thất bại trước mọi lời gọi model với thông báo nêu thứ thiếu và cách sửa.
- **FR-007**: Mọi task PHẢI được kiểm (id hợp lệ, prompt có, kích thước trong giới hạn) trước khi tiêu tiền.
- **FR-008**: Rollout PHẢI chạy trên bản sao scratch; run không bao giờ ghi vào cây làm việc thật hay `~/.claude`; sources = file git theo dõi trừ `exclude_globs`, trừ mọi đường `editable` khớp, trừ phần cài coding-agent (`worktree_links.LINKED`).
- **FR-009**: Không output của check, rubric hay điểm nào tới improver; request không có trường check; coding-agent kiểm lại result không chứa dấu hiệu verdict.
- **FR-010**: Tổng chi tiêu improver + rollout PHẢI dừng trước phiên kế tiếp khi đạt `max_usd`; báo chi tiêu tới lúc dừng.
- **FR-011**: Kết quả là `keep_base` kèm lý do, hoặc diff chỉ trong allowlist kèm bằng chứng, chi tiêu, thời gian; file ngoài allowlist, file không phải `.md` trong skill, overlay quá `max_overlay_bytes`, hoặc đề xuất xoá file → chuyển thành `keep_base` với lý do.
- **FR-012**: `improve apply` PHẢI in diff chính xác và đọc xác nhận từ TTY ở mỗi lần gọi; stdin không phải TTY → từ chối; file đích đổi kể từ lúc run bắt đầu → từ chối; không cờ/cấu hình nào tắt được việc duyệt; không đường nào khác ghi file đề xuất.
- **FR-013**: `improve cancel` PHẢI kết thúc cả cây tiến trình (engine, improver, rollout `claude`).
- **FR-014**: Dữ liệu run nằm ở `.coding-agent/harness2/runs/<runId>/` quyền 0700, xoá sau `retention_days`; scratch rollout xoá khi run kết thúc; `improve delete` xoá một run ngay.
- **FR-015**: Phiên cũ chỉ thành task khi `allow_session_tasks` bật và user xác nhận đúng các phiên đó; chỉ prompt đầu tiên của người, hiện nguyên văn trước khi xác nhận.
- **FR-016**: Task có `check` thì coding-agent (không phải harness2) chạy nó trên deliverables của từng rollout sau khi engine xong, lưu output vào run để báo cáo.
- **FR-017**: Khi bật và `CLAUDE.local.md` tồn tại, worktree worker PHẢI nhận được file đó qua `worktree_links` để overlay áp dụng cho cả worker (Q3); khi tắt, `LINKED` giữ nguyên.

## Success criteria (SC)
- **SC-001** *(lõi)*: Với khối vắng hoặc `enabled: false`, test chứng minh `coding_agent.harness_improve` không có trong `sys.modules` sau khi chạy từng handler và sau `cli --help`; `gen` sinh settings giống từng byte bản trước; `gate` thoát 0. *Bằng chứng: `tests/test_harness2_dormant.py`.*
- **SC-002**: Với `command` trỏ tới đường dẫn không tồn tại, toàn bộ test suite hiện có xanh, và `improve` thoát 2 với thông báo nêu thiếu `harness2-improve`, không tốn tiền. *Bằng chứng: test.*
- **SC-003**: Trên task headroom (T3), ít nhất 2/3 run cho harness được chọn nâng điểm check trung bình trên 4 rollout mới ≥ 0.10 so với base, hoặc trả `keep_base`; không run nào hạ điểm. *Bằng chứng: T9, ghi vào tài liệu này.*
- **SC-004**: Chi tiêu không bao giờ vượt `max_usd` cộng chi phí của một phiên (improver hoặc rollout); chi tiêu báo khớp `total_cost_usd` của CLI trong 0.01 USD.
- **SC-005**: Qua test suite và mọi run thật: 0 file trong dự án đổi trước khi duyệt, 0 file ngoài allowlist đổi sau khi duyệt.
- **SC-006**: `improve cancel` không để lại tiến trình harness2/`claude` nào sau 10 giây.
- **SC-007**: Với 1 task cỡ toy và k=1, user nhận `keep_base` hoặc diff trong 15 phút.

## Assumptions
- Lệnh của user: `harness2 on|off|status` và `improve …` (theo gợi ý của coordinator 2026-10-24). Module mới tên `coding_agent.harness_improve` (tránh trùng tên package `harness2`). (default)
- Khi bật, run vẫn chỉ bắt đầu bằng lệnh tường minh; agent có thể chạy `improve` khi user bảo (coordinator-guard cho phép một lệnh `coding_agent.cli` đơn), `enabled` + `max_usd` giới hạn thiệt hại nếu agent tự chạy. (default)
- Executor rollout mặc định `sonnet`, tools `Read,Write,Edit,Glob,Grep` (không Bash, không web) và `--permission-mode acceptEdits` trong scratch; task có thể khai `bash: true` để thêm `Bash`. (default — Q4)
- Claude Code (bản `claude -p`) nạp `CLAUDE.local.md` ở gốc workspace sau `CLAUDE.md` và quét `.claude/skills/<name>/SKILL.md`. (find-out-later → T0; nếu `CLAUDE.local.md` không còn được nạp thì overlay đổi sang `.claude/skills/project-overlay/SKILL.md` hoặc một dòng `@import` — sẽ cần duyệt lại)
- `CLAUDE_CONFIG_DIR` trỏ tới thư mục riêng cô lập được `~/.claude/CLAUDE.md`, skill/plugin/hook/MCP toàn cục của user khỏi rollout, và giữ đăng nhập riêng trên macOS (keychain). (find-out-later → T0; lùi: `CLAUDE_CODE_OAUTH_TOKEN` từ `claude setup-token` truyền qua env, không ghi vào request)
- `claude -p --output-format stream-json` trả `total_cost_usd` ở dòng `result`, đủ để cộng ngân sách rollout. (find-out-later → T0)
- `improve apply` chạy được khi user gõ ở một terminal Orca/shell thường (có TTY); prefix `!` trong Claude Code không cấp TTY nên không dùng được — README nói rõ. (find-out-later → T0)
- Engine chạy từ venv harness2 riêng do user cài bằng `uv` từ fork đã pin SHA; coding-agent gọi `harness.command` với env đã lọc + `CLAUDE_CONFIG_DIR` + `TMPDIR` trỏ vào run dir. (default)
- Rollout không mang `.claude/settings*.json`, `integration.yaml`, `harness/` nên không hook coding-agent nào chạy trong đó; tiến trình engine vẫn được đặt `CODING_AGENT_HARNESS2_NESTED=1` để chẩn đoán, không handler nào cần đọc nó. (default)

## Impact — file bị ảnh hưởng
| File | Thay đổi |
|---|---|
| `integration.yaml`, `src/coding_agent/template.yaml` | thêm khối `harness2` (`enabled: false`), không thêm hook |
| `src/coding_agent/manifest.py` | dataclass `Harness2` + `harness2_from`; `Manifest.harness2` |
| `src/coding_agent/harness_improve.py` (mới) | state on/off, preflight, task, staging, request, spawn/cancel, lưu trữ + retention, diff, apply TTY, check runner |
| `src/coding_agent/cli.py` | lệnh `harness2`, `improve` — import lười trong nhánh lệnh |
| `src/coding_agent/worktree_links.py` | thêm `CLAUDE.local.md` vào tập link **chỉ khi** bật (Q3) |
| `src/coding_agent/hooks/handlers.py`, `gen.py`, `coordinator.py` | **không đổi** |
| `README.md`, `tests/` | mục "Harness² (tuỳ chọn)", test dormancy, engine giả theo protocol |
| fork harness2 (repo khác) | `harness2/cli/improve.py`, `harness2/substrate/claude_code.py`, `harness2/benchmarks/user_tasks.py`, `core/proposer.py` (cost cap), `pyproject.toml`, `docs/` |

## Hành vi hiện có có thể vỡ
- `test_shipped_manifest.py`, snapshot manifest: khối mới trong `integration.yaml`/`template.yaml` → cập nhật snapshot. Settings sinh ra **không** đổi (không hook) — có test khẳng định.
- `worktree_links`: link thêm `CLAUDE.local.md` khi bật; user đã có `CLAUDE.local.md` riêng sẽ thấy nó xuất hiện trong worktree worker — đúng ý nhưng là hành vi mới, chỉ khi bật.
- `cli.py` có nhánh import lười đầu tiên trong file; test dormancy chốt lại để không ai "dọn" nó lên đầu file.
- Plan DSH đã duyệt: T2 đổi protocol (`harness-improve/1`, `substrate`, `budget`) — DSH T3–T8 phải theo (Q1).

## Plan
Fork harness2 (repo `/Users/thoaidd/orca/workspaces/harness2`, nhánh `feat/dsh-substrate`, push `fork`):
- [ ] **T0 — Spike Claude Code (gate cho T2):** đo trên máy thật: `CLAUDE.local.md` có được `claude -p` nạp; `CLAUDE_CONFIG_DIR` riêng cô lập + giữ đăng nhập (keychain macOS); `total_cost_usd` trong stream-json; `!` có TTY không. Ghi kết quả vào Assumptions của tài liệu này.
- [ ] **T1 — Protocol chung `harness-improve/1` (CHUNG với DSH T2, thay thế nó):** `harness2/cli/improve.py` + console script `harness2-improve`; kiểm request theo bảng substrate; user-task adapter trung lập; `Proposer.max_usd` + `budget.scope`; ghi result nguyên tử; tín hiệu; `--protocol`. Binding `dsh` đúng như plan DSH T2. Test với `deepseek_harness` và `claude` giả.
- [ ] **T2 — `ClaudeCodeSubstrate` (riêng coding-agent):** `harness2/substrate/claude_code.py` — `CLAUDE_CODE_SURFACE` (c1–c4 gộp vào `CLAUDE.local.md`, c6 `.claude/skills` `must_match`, không c5/c7/c8), `materialise`, `execute` qua `claude -p --output-format stream-json` trong scratch với `CLAUDE_CONFIG_DIR` riêng, renderer trajectory stream-json, `preflight`, `usd` mỗi rollout vào ledger; đăng ký `SUBSTRATES["cc"]`; binding `claude_code` trong T1.
- [ ] **T3 — Task headroom (CHUNG với DSH T1):** task toy có base < 80% trên checker, trung lập substrate (prompt + sources + checker); đo base trên cả DSH và Claude Code.

coding-agent:
- [ ] **T4 — Manifest + công tắc:** `Harness2` + `harness2_from` + kiểm khoảng; khối trong `integration.yaml`/`template.yaml`; `cli harness2 on|off|status` + `.coding-agent/state/harness2.json`; import lười.
- [ ] **T5 — Run:** `harness_improve`: task file, preflight, staging sources/base theo allowlist + `exclude_globs`, ghi request, spawn tách terminal với process group riêng, `list/show/cancel/delete`, lưu trữ 0700 + retention.
- [ ] **T6 — Apply + allowlist:** đọc result, kiểm protocol/allowlist/`.md`/kích thước/marker verdict, dựng diff, `apply` TTY + kiểm sha256 file đích, link `CLAUDE.local.md` vào worktree worker khi bật.
- [ ] **T7 — Check runner + phiên làm task:** chạy `check` trên deliverables sau engine (D8); `--session` qua `transcript.py` sau opt-in + xem trước nguyên văn (D5).
- [ ] **T8 — Test + README:** `test_harness2_dormant.py` (SC-001/002), engine giả viết từ protocol, test apply/TTY/stale, cost cap báo cáo, mục README "Harness² (tuỳ chọn)" kèm license + SHA fork.
- [ ] **T9 — Đánh giá thật SC-003 (gate):** trên task T3, ghi kết quả vào tài liệu này trước khi chuyển `implemented`.

Phụ thuộc:
```text
T0 ──> T2 ──┐
T1 ──> T2   ├──> T9 (gate)
T3 ─────────┤
T4 ──> T5 ──> T6 ──┐
       T5 ──> T7 ──┴──> T8 ──┘
```
T5/T6 test bằng engine giả theo protocol ở tài liệu này, không chờ T1 merge. T8 có e2e engine thật tự bỏ qua khi chưa cài T1+T2.

## Agent Task Assignment
| Task | Agent (CLI) | Lý do chọn | Chung với DSH? | Status |
|---|---|---|---|---|
| T0 | claude | đo hành vi Claude Code thật, cần phán đoán | — | pending |
| T1 | claude | thiết kế lại entry point dùng chung, ảnh hưởng hai dự án | **Có** (thay DSH T2) | pending |
| T2 | claude | tiến trình con, stream-json, cô lập cấu hình — dễ sai tinh vi | — | pending |
| T3 | codex | task + checker theo spec rõ | **Có** (DSH T1) | pending |
| T4 | codex | theo khuôn `auto_decide` có sẵn | — | pending |
| T5 | claude | spawn tách terminal, process group, quyền file | — | pending |
| T6 | claude | cổng an toàn (TTY, stale, allowlist) | — | pending |
| T7 | codex | check runner + đọc transcript theo spec | — | pending |
| T8 | codex | test + tài liệu theo spec đã rõ | — | pending |
| T9 | claude | chạy thật, tốn tiền, đọc kết quả | **Có** (DSH T9 đo riêng) | pending |

## Render brief
- **T0:** người chạy spike → `claude -p` trong scratch có `CLAUDE.local.md` (add) → đọc trajectory xem file được nạp (add) → `CLAUDE_CONFIG_DIR` riêng: còn đăng nhập? (add) → không → lùi `CLAUDE_CODE_OAUTH_TOKEN` (block).
- **T1:** coding-agent/DSH ghi request (add) → `harness2-improve` kiểm theo bảng substrate (add) → sai → exit 2 (block) → chạy PPC qua substrate (legacy) → cost cap → `stopReason: cost_cap` (block) → ghi result nguyên tử (add).
- **T2:** engine (legacy) → `ClaudeCodeSubstrate.materialise` gộp c1–c4 → `CLAUDE.local.md` (add) → `claude -p stream-json` trong scratch (add) → trajectory + `usd` (add) → c5/c7/c8 → từ chối (block).
- **T3:** task headroom (add) → base trên DSH và Claude Code < 80% (add) → checker chỉ vào báo cáo (legacy).
- **T4:** user `harness2 on` (add) → `state/harness2.json` (add) → `manifest.harness2_from` kiểm (add) → sai → từ chối (block) → tắt → không import, settings y nguyên (legacy).
- **T5:** user `improve --task t.yaml` (add) → tắt → thoát (block) → preflight `--protocol` (add) → thiếu harness2 → exit 2 (block) → staging + request (add) → spawn engine tách terminal (add) → in runId (add).
- **T6:** engine xong → result (add) → kiểm allowlist/marker (add) → vi phạm → keep_base (block) → `improve apply` ở TTY → diff + xác nhận (add) → không TTY / file đổi → từ chối (block) → ghi file (add).
- **T7:** engine xong (legacy) → chạy `check` trên deliverables (add) → output chỉ vào báo cáo (add) → `--session` khi chưa opt-in → từ chối (block).
- **T8:** pytest dormancy (add) → engine giả (add) → snapshot (add) → gate 0 (legacy).
- **T9:** 3 run thật trên task T3 (add) → so điểm base/adopted (add) → không cải thiện → khuyến nghị bỏ tính năng (block).
Prose chi tiết từng task: xem file HTML đi kèm.

## Licensing
- coding-agent không có file LICENSE (repo riêng); harness2 là Apache-2.0, không có `NOTICE`. Approach 3 không đưa dòng mã harness2 nào vào coding-agent: coding-agent chạy một chương trình cài độc lập qua subprocess và không import nó.
- `ClaudeCodeSubstrate` và entry point chung sống trong fork, mang header Apache-2.0 "Copyright 2026 Google LLC" như file xung quanh, kèm ghi chú sửa đổi theo §4(b).
- README coding-agent nêu tên Harness², license Apache-2.0, fork và SHA đã pin. Theo §6 license không cấp quyền thương hiệu: tên lệnh `harness2` chỉ mô tả engine được gọi (dùng hợp lý để nêu nguồn gốc), không đặt tên package/sản phẩm theo Harness².
- Đóng góp ngược upstream chờ bằng chứng SC-003 và Google CLA — câu hỏi treo bên DSH.

## Risks
- **Chưa có lợi ích đo được.** Hai run DSH thật đều giữ base với ~1.2 USD/run. Nếu T9 không cho thấy cải thiện, bỏ tính năng thay vì ship.
- **Chi phí cao hơn DSH.** Rollout cũng là Claude; run k=1 một task ước ~2–3 USD (8 phiên improver ~1.2 USD + 8 rollout Sonnet). Cost cap tổng giới hạn, nhưng một run dừng giữa chừng vẫn tốn tiền.
- **Dữ liệu rời máy.** Improver và executor nhận prompt task, mã nguồn dự án (trừ `exclude_globs`), trajectory đầy đủ. Phiên làm task mở rộng tới hội thoại thật.
- **Điều khoản subscription.** `claude -p` không người trông cho cả improver lẫn rollout; nếu không phù hợp điều khoản, phải chuyển sang API key — cần fork bỏ lọc `ANTHROPIC_API_KEY`.
- **Rollout ghi file tuỳ ý trong scratch.** `acceptEdits` trong thư mục tạm; không Bash mặc định để giảm phạm vi; vẫn chạy bằng quyền của user.
- **Hành vi Claude Code chưa kiểm** (`CLAUDE.local.md`, cô lập `CLAUDE_CONFIG_DIR`, chi phí trong stream-json) — T0 chặn T2.
- **Overlay lớn dần.** `CLAUDE.local.md` cộng vào mọi phiên; `max_overlay_bytes` giới hạn.
- **Bảo trì fork + phụ thuộc chéo DSH.** Protocol chung nghĩa là đổi protocol phải đồng bộ hai dự án; được bù bằng chỉ một entry point phải bảo trì.
- **Đánh đổi:** không Windows, không Codex executor, không sửa hợp đồng coordinator, cần venv Python 3.12 riêng.

## Câu hỏi mở (chỉ những chỗ khác D1–D8)
- **Q1 — Protocol chung:** đồng ý sửa plan DSH đã duyệt, T2 thành `harness-improve/1` (thêm `substrate`, `budget`, console script `harness2-improve`) để một entry point phục vụ cả hai? *Khuyến nghị: có.*
- **Q2 — Tiền (khác D3):** ngân sách tính **tổng** improver + rollout, mặc định `max_usd = 3.00`; improver và executor đăng nhập bằng thư mục riêng `~/.coding-agent/harness2/claude-home` (user `/login` một lần) thay vì AI Account của DSH. *Khuyến nghị: có.*
- **Q3 — Worker (mới):** khi bật, link `CLAUDE.local.md` vào worktree worker để overlay áp dụng cho worker chứ không chỉ coordinator? *Khuyến nghị: có.*
- **Q4 — Công cụ rollout (mới):** rollout mặc định không có Bash (`Read,Write,Edit,Glob,Grep`), task khai `bash: true` mới thêm? *Khuyến nghị: có — DSH rollout có shell, nhưng ở đây rollout chạy bằng quyền user trên máy thật.*

## Self-review
1. **Phủ yêu cầu:** plugin tuỳ chọn → FR-001/004 (T4); tắt là nằm im, thiếu harness2 không lỗi → FR-002/003, SC-001/002 (T4, T8); refine thứ coding-agent kiểm soát → Editable set, FR-011/012 (T5, T6); giống kế hoạch DSH → approach 3 = C, bảng D1–D8; executor → 3a vs 3b/3c; protocol chung → khuyến nghị + Q1; licensing, FR/SC, non-goals, risks, task T0–T9 kèm cột "Chung với DSH".
2. **Placeholder:** không còn TBD/TODO; mọi chỗ chưa đo đều là find-out-later gắn T0.
3. **Nhất quán tên:** khối manifest `harness2`, lệnh `harness2 on|off|status` + `improve`, module `harness_improve`, state `.coding-agent/state/harness2.json`, run `.coding-agent/harness2/runs/<runId>/`, protocol `harness-improve/1`, console script `harness2-improve`, substrate `claude_code` (token `cc`).
4. **Không chạm:** không code sản phẩm, không worktree live, không push.
