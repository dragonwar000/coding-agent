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
| `src/coding_agent/cli.py` | Lệnh cho agent: `recall`, `stats`, `report`, `forget`, `brief`, `status`, `orca-create`, `orca-status`, `claim`, `orca-check`, `delegate`, `plan-*`, `inbox`, `worker-kick`, `worker-settle` |
| `src/coding_agent/project_install.py` | Cài, verify và gỡ coding-agent trong một dự án, như install.sh của setup (FR-001, FR-004) |
| `install.sh` | Lệnh một dòng cho `project_install` |
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

Cài, nâng cấp, hoặc gỡ bằng một dòng, chạy trong thư mục dự án:

```sh
# cài mới hoặc cài lại
curl -fsSL https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.sh | bash
# nâng cấp dự án đã cài, thêm các hook mới vào manifest cũ
curl -fsSL https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.sh | bash -s -- --add-new-hooks
# gỡ (thêm --purge --remove-manifest để gỡ sạch)
curl -fsSL https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.sh | bash -s -- --uninstall
```

URL này không đổi giữa các bản. `bootstrap.sh` tự ghim vào một commit code đã kiểm (`PINNED_REF`), nên lệnh luôn cài bản
mới nhất đã phát hành. Muốn cố định một bản: thay tên nhánh trong URL bằng SHA của commit chứa `bootstrap.sh`, hoặc đặt
`CODING_AGENT_REF=<sha>`. Xem nguồn trước khi cài: thêm `-s -- --print-source`.

Nếu đã clone repo, dùng `install.sh` trực tiếp:

```sh
bash install.sh <dự án>                           # cài: dò vendor, merge hook, ghi CI, verify
bash install.sh <dự án> --vendor claude           # ép vendor (claude, codex, hoặc claude,codex)
bash install.sh <dự án> --uninstall [--keep-core] # gỡ: hook của coding-agent, CI, bản copy package
bash install.sh <dự án> --clean                   # gỡ rồi cài lại
```

Gỡ khỏi dự án:

```sh
bash uninstall.sh <dự án>                              # gỡ hook, CI, bản copy package; giữ integration.yaml
bash uninstall.sh <dự án> --purge --remove-manifest    # gỡ sạch: thêm .bak, .coding-agent/, integration.yaml
```

Hook và cài đặt riêng của bạn trong `.claude/settings.json` được giữ nguyên. Bộ nhớ Zero-Mem ở `~/.coding-agent/zeromem`
nằm ngoài dự án và không bị xoá.

Installer thực hiện, theo thứ tự:

1. Kiểm PyYAML cho `python3` đang chạy installer. Thiếu thì tự `pip install pyyaml`.
2. Dò vendor: `.claude/` → Claude Code, `.codex/` hoặc `AGENTS.md` → Codex, không có gì thì Claude Code.
3. Chép package vào `harness/coding-agent/src/coding_agent`. Bản đã cài chỉ được thay khi còn dấu harness; bản cũ vào `.coding-agent/backups/`. Thư mục không có dấu bị từ chối.
4. Ghi `integration.yaml` nếu dự án chưa có. Manifest có sẵn không bao giờ bị ghi đè.
5. Merge hook vào `.claude/settings.json` và `.codex/hooks.json`: giữ hook và cài đặt của bạn, thay hook cũ của coding-agent, và giữ file `.bak`.
6. Ghi CI gate `.github/workflows/coding-agent.yml` nếu chưa có file này. Thêm `--no-ci` để bỏ qua.
7. Verify: chạy gate, và chạy đúng lệnh hook trong `settings.json` với payload mẫu (orca-guard phải chặn status sai, prompt-reset phải thoát 0). Thêm `--no-verify` để bỏ qua.

Cài lại là an toàn: lệnh giống nhau cho cùng kết quả. Gỡ giữ nguyên `integration.yaml` và mọi hook của bạn.

**Nâng cấp dự án đã cài:** chạy lại lệnh cài của bản mới trong dự án. Package được thay (bản cũ vào
`.coding-agent/backups/`), còn `integration.yaml` được giữ nguyên. Nếu bản mới có hook mà manifest của bạn chưa có,
installer in tên các hook đó. Thêm `--add-new-hooks` để bổ sung chúng vào cuối manifest (giữ `integration.yaml.bak`).
Các giá trị bạn đã sửa trong manifest không bị đổi.

Cần có: Python 3.11 trở lên, git, và lệnh verify trong manifest chạy được trong PATH (mặc định `python3 -m pytest -q`).
Hook chạy bằng `python3` trên PATH, nên `python3` đó cũng phải có PyYAML.

Kiểm tra trạng thái sau khi cài:

```sh
PYTHONPATH=harness/coding-agent/src python3 -m coding_agent.gate --manifest integration.yaml   # 0: khớp manifest
```

## Windows

Trạng thái: script cài và gỡ bằng PowerShell đã chạy được với PowerShell 7 trên macOS. **Chưa chạy trên máy Windows thật**,
nên coi các bước dưới là hướng dẫn cần kiểm lại lần đầu dùng.

### Cần có

- **Git for Windows.** Bắt buộc, không chỉ để có `git`: trên Windows, Claude Code chạy hook bằng Git Bash, và lệnh hook của
  coding-agent viết theo cú pháp shell POSIX. Không có Git Bash thì Claude Code chạy hook bằng PowerShell, lệnh hook lỗi, và
  các guard ngừng chặn mà không báo. Nếu Claude Code không tìm thấy Git Bash, đặt trong `settings.json`:
  `"env": { "CLAUDE_CODE_GIT_BASH_PATH": "C:\\Program Files\\Git\\bin\\bash.exe" }`.
- **Python 3.11 trở lên** từ python.org, có trong PATH. Trên Windows lệnh thường là `python`, không phải `python3`.
  Installer tự chọn và ghi vào `integration.yaml` (`python: python`).
- **PyYAML** cho đúng Python đó. Installer tự cài nếu thiếu.

### Cài

PowerShell, trong thư mục dự án:

```powershell
irm https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.ps1 | iex
```

Hoặc Git Bash, trong thư mục dự án (cùng lệnh với macOS và Linux):

```sh
curl -fsSL https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.sh | bash
```

Nếu PowerShell chặn script: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` rồi chạy lại.

### Nâng cấp

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.ps1))) --add-new-hooks
```

### Gỡ

```powershell
# gỡ hook, CI, bản copy package; giữ integration.yaml và hook riêng của bạn
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.ps1))) --uninstall
# gỡ sạch
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.ps1))) --uninstall --purge --remove-manifest
```

Git Bash: `curl -fsSL https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.sh | bash -s -- --uninstall`.

### Kiểm sau khi cài

```powershell
$env:PYTHONPATH = "harness\coding-agent\src"; python -m coding_agent.gate --manifest integration.yaml
```

Thoát 0 là file hook khớp manifest. Bước verify của installer chạy thử lệnh hook bằng `sh` hoặc `bash`; nếu không có cả hai
trong PATH (chạy từ PowerShell mà Git Bash không nằm trong PATH), installer báo bỏ qua bước đó và nhắc cài Git for Windows.

### Khác biệt và giới hạn

- Đổi lệnh Python sau khi cài: sửa `python:` trong `integration.yaml` rồi chạy lại lệnh cài. Giá trị chỉ là tên lệnh
  (`python`, `python3`, `py`), không nhận đường dẫn có dấu cách hay tham số.
- Lệnh verify mặc định là `<python> -m pytest -q`. Đổi trong `integration.yaml` cho hợp dự án.
- `verify.when: changed` (mặc định) bỏ qua verify khi lượt không sửa file: không có `Write`/`Edit` thành công trong transcript
  và cây làm việc của gốc không đổi so với đầu lượt (commit của `HEAD`, cùng trạng thái, kích thước và giờ sửa của từng file bẩn). Ngoài git, chỉ transcript làm bằng chứng. Không chắc thì vẫn verify.
  Event là `no-change`. Đặt `when: always` để verify mọi lượt như trước.
- `verify.scope: changed-repos` cho workspace nhiều repo (gốc không phải một dự án): lệnh verify chạy trong từng git repo con
  có file bị sửa thay vì ở gốc. Mặc định `root`.
- Zero-Mem cần binary `zm` trong PATH. Chưa kiểm bản `zm` cho Windows.
- Orca: chưa kiểm coding-agent với Orca trên Windows.
- WSL: dùng hướng dẫn Linux bên trong WSL. Khi đó Claude Code cũng phải chạy trong WSL.

## Coordinator: bắt buộc chia việc theo graph

Session chính là **coordinator**. Nó lập plan, giao từng node cho worker Orca, gộp kết quả, và trả lời người dùng.
Nó không tự sửa file.

- **`coordinator-guard` (enforce)** chặn trong session coordinator: `Write`, `Edit`, `MultiEdit`, `NotebookEdit`; lệnh shell
  sửa nội dung hay viết lại trạng thái (`sed -i`, `>`, `rm`, `git reset`, `git checkout`, `pip install`, ...); và tool
  `Agent`/`Task` (subagent nội bộ không qua graph). Guard bỏ phần nằm trong dấu nháy trước khi so khớp, nên `echo "a -> b"`
  hay `git commit -m "rm cũ"` không bị chặn; `$(...)` và backtick trong nháy kép vẫn được xét vì shell chạy chúng.
- **Sửa cấu hình hook** (`.claude/settings.json`, `.claude/settings.local.json`, `integration.yaml`) bằng `Write`/`Edit`
  không bị chặn mà host hỏi người dùng xác nhận, để guard luôn tắt được từ trong phiên.
- **Coordinator vẫn được:** đọc file, lệnh đọc, ghi `.coding-agent/plan.yaml`, chạy một lệnh `coding_agent.cli` đơn lẻ,
  và `git add` / `git commit` / `git merge` để gộp nhánh của worker.
- **Tự giao việc:** chỉ worker được ghi. Khi việc cần ghi hoặc khi guard chặn một lệnh, coordinator giao ngay trong cùng
  lượt (`delegate` cho một việc nhỏ, graph cho nhiều việc), không xin phép người dùng và không viết lại lệnh để lách guard.
  Chưa có Orca Run thì nó tự chạy `run-init`. Lời từ chối của guard kèm sẵn lệnh `delegate`. Nó chỉ hỏi người dùng về
  quyết định thật sự của họ (phạm vi, thao tác phá huỷ hoặc ra bên ngoài).
- **Ai là worker:** chỉ session chạy trong worktree mà `worker-start` của coding-agent tạo ra. Danh sách nằm ở
  `<git-common-dir>/coding-agent-workers.jsonl`, dùng chung cho mọi worktree. Session ở worktree khác (kể cả workspace
  Orca) là coordinator. Root không phải repo git (folder project) giữ danh sách ở `<root>/.coding-agent/workers.jsonl`. `CODING_AGENT_ROLE=coordinator|worker|maintainer` ghi đè.
- **Maintainer:** khởi động phiên với `CODING_AGENT_ROLE=maintainer` cho việc không phải code (pull repo, sinh tài liệu).
  Guard không chặn, nhưng mỗi lệnh nó lẽ ra chặn được ghi event `maintainer-allowed`. Vai trò này chỉ đến từ biến môi
  trường do người khởi động phiên đặt, không phải file agent tự ghi được.
- **Phát hiện phần guard bỏ sót:** guard shell là heuristic. Cuối lượt, `stop-gate` so `git status` với đầu lượt và ghi
  event `direct-change` nếu cây của coordinator có file mới đổi. Đây là phát hiện, không hoàn tác. Sửa tay của người dùng
  trong lúc đó cũng bị tính.
- **Hợp đồng và bảng việc** được nạp đầu phiên, sau nén ngữ cảnh, và mỗi prompt.
- **Nới lỏng:** đổi `coordinator-guard` sang `mode: shadow` trong `integration.yaml` rồi cài lại. Khi đó hook chỉ nhắc.

Lưu ý khi dùng:
- Worker tách nhánh từ commit hiện tại của coordinator. Việc chưa commit ở cây chính thì worker không thấy.
- Worktree mới chưa có phụ thuộc đã cài (ví dụ `node_modules`), nên lệnh verify ở `stop-gate` của worker sẽ fail cho tới khi
  worker cài. Cấu hình setup của repo trong Orca để tránh.
- Orca đánh task `completed` khi worker tự báo xong. Bảng việc xếp nó vào "báo xong nhưng chưa có bằng chứng"; node phụ
  thuộc vẫn được giao tiếp. Kiểm kết quả trước khi báo người dùng.
- Sửa một dòng cũng phải qua graph. Không có ngoại lệ cho việc nhỏ.

Đã kiểm với Orca cài trên máy (2026-10-04): `run-init`, `plan-apply`, `plan-next`, `board` chạy trọn một graph 2 node,
node sau chỉ được giao khi node trước xong. Chưa kiểm: `task-update` qua `orca-status`.

## Chia việc theo graph

Việc có nhiều phần phụ thuộc nhau được khai trong một plan YAML. Mỗi node là một task Orca; phụ thuộc được chuyển
cho Orca qua `--deps`. Worker chỉ được khởi động cho node đã xong hết phụ thuộc.

```yaml
tasks:
  - id: protocol
    title: Wire protocol
    spec: Định nghĩa message và validate
  - id: server
    title: Server
    spec: Room actor, transport
    deps: [protocol]
    agent: codex        # tuỳ chọn
```

```sh
python3 -m coding_agent.cli run-init --objective "..."   # một lần cho mỗi terminal coordinator; lưu vào .coding-agent/orca-run
python3 -m coding_agent.cli plan-apply plan.yaml         # tạo task theo thứ tự phụ thuộc; chạy lại không tạo trùng
python3 -m coding_agent.cli plan-next plan.yaml --max 2  # khởi động worker cho các node sẵn sàng
python3 -m coding_agent.cli plan-status plan.yaml        # graph: trạng thái từng node và phụ thuộc còn mở
```

Worktree và nhánh của worker được đặt tên từ tiêu đề task: chữ thường không dấu, nối bằng `-`, tối đa 40 ký tự
(ví dụ "3D: model chó, chủ nhà" → `3d-model-cho-chu-nha`). Orca hiện tiêu đề đầy đủ trên dòng của worker, và tự thêm
`-2`, `-3` khi tên trùng. Vì vậy `title` của node nên là một tóm tắt ngắn của việc cần làm.

Plan bị từ chối khi có id trùng, phụ thuộc không tồn tại, tự phụ thuộc, hoặc vòng lặp. Node có phụ thuộc `failed`
hay `blocked` không bao giờ được giao. Ánh xạ plan id → Orca task id nằm ở `.coding-agent/plan.json`.
**Khi worker xong:** Orca gửi `worker_done` về hộp thư của Run. Mỗi prompt, bảng việc của coordinator liệt kê các báo cáo
chưa xử lý, kèm node tương ứng và việc cần làm. Đọc bảng không đánh dấu đã đọc; `inbox --ack` mới đánh dấu.

```sh
python3 -m coding_agent.cli inbox          # báo cáo worker chưa xử lý
python3 -m coding_agent.cli inbox --ack    # đánh dấu đã xử lý
```

### Folder project: root chứa nhiều repo git

Khi root là một thư mục thường (không phải repo git) chứa nhiều repo độc lập, Orca từ chối tạo worktree con
(`Folder projects cannot create orchestration worktrees`). Khi đó phải nói worker làm trong repo nào:

```sh
python3 -m coding_agent.cli delegate --title "..." --spec "..." --repo vgps/app [--base-branch develop]
python3 -m coding_agent.cli worker-start --task-id <id> --repo vgps/app
```

```yaml
tasks:
  - id: app
    title: Màn hình đăng nhập
    spec: ...
    repo: vgps/app      # repo git mà worktree của worker được tạo trong đó; tương đối so với root, hoặc tuyệt đối
    base: develop       # tuỳ chọn; mặc định là HEAD hiện tại của repo đó
```

- Worktree luôn được tạo bằng `orca worktree create --repo path:<repo> --no-parent` (không dùng `new-child`), rồi giao việc bằng
  `worker-start` vào terminal của agent trong worktree đó (xem "Khởi động worker"). Orca chưa biết repo (`repo_not_found`) thì
  coding-agent chạy `orca repo add` và thử lại một lần.
- Root là repo git và không có `--repo`: repo đích là chính repo của root.
- Root không phải repo git mà thiếu `--repo`: lệnh dừng **trước khi tạo task** và liệt kê các repo git tìm thấy dưới root
  (sâu tối đa 3 cấp, bỏ thư mục ẩn, `node_modules`, `venv`, `build`, `dist`). `plan-apply` kiểm mọi node trước khi tạo task nào.
- Worker được ghi ở hai nơi: danh sách của repo đích (để session trong worktree là worker) và danh sách của root (để bảng việc,
  `worktree-list`, `worktree-clean` của coordinator thấy nó). "Commit chưa gộp" được so với HEAD của repo đích.
- **Giới hạn đã biết:** worktree tạo từ repo con không mang hook harness và `integration.yaml` của thư mục cha. Worker ở đó
  chạy không có `stop-gate`, không có verify và không có guard của harness, trừ khi repo con tự cài coding-agent. Coordinator
  phải tự kiểm kết quả (đọc diff, chạy verify trong worktree) trước khi gộp.

Chưa có: tự chạy `plan-next` mà không cần coordinator (bảng việc chỉ hiện ở prompt kế tiếp của người dùng), và ratchet giữ
bản tốt nhất như DSH.

## Khi worker không nhận đề bài / không báo

Đo trên Orca CLI 1.4.206 (Windows 11, 2026-10-07): `worker-start` có thể trả `ok: true` nhưng thoát 1, với
`result.stage = turn_start_unobserved` (worktree và terminal đã tạo, Claude Code mở ở prompt trống, đề bài không tới).
Preamble mà worker đọc bằng `dispatch-show` không có token `--dispatch-capability`, nên `worker_done` của nó bị Orca từ chối và
về hộp thư với subject `Rejected worker_done: ...`; task vẫn `blocked`, và `orca-status --status completed` bị từ chối khi
dispatch còn active. coding-agent xử lý như sau:

- **Tự động trong `plan-next` / `delegate`:** câu trả lời `ok: true` được nhận dù exit khác 0 (exit ghi ở `_exit`). Worktree luôn
  được ghi vào sổ worker (tìm bằng `git worktree list` theo tên nhánh khi Orca không trả `effects`), nên guard nhận đúng vai worker.
  Khi turn start chưa được quan sát, coordinator đợi TUI lên (tối đa 30 giây), gửi kick-off một dòng qua `orca terminal send --enter
  --wait-submit 20` tới `assignee_handle` của dispatch, đọc lại màn hình sau 5 giây và gửi lại tối đa 2 lần. Event `kicked`
  (`applied` là đã thấy kick-off trên màn hình hay chưa). Kick thất bại không làm `plan-next` lỗi: lệnh để gửi tay in ra stderr.
- **`worker-kick --task-id <id>`:** phục hồi tay cho worker vẫn im: ghi worktree của task vào sổ worker (như `worker-adopt`) rồi gửi
  kick-off như trên. Thoát 1 khi không thấy kick-off trên màn hình.
- **`worker-settle --task-id <id> --basis verifier|predicate|human [--artifact FILE ...]`:** chốt task mà worker đã báo nhưng báo cáo
  bị từ chối. Coordinator kiểm kết quả trước; lệnh `worker-abandon` dispatch còn active rồi gửi `completed` theo quy tắc bằng chứng
  (FR-007). Dispatch đã settled thì không abandon. Bảng việc và `inbox` in gợi ý lệnh này ngay dưới báo cáo `Rejected worker_done`.

## Khởi động worker

- **Chờ agent sẵn sàng:** `worker-start --agent` gõ đề bài ngay khi agent còn đang khởi động, và đề bài bị mất. Vì vậy
  coding-agent tạo worktree kèm agent trước, đọc màn hình terminal của agent tới khi prompt trống (tối đa 90 giây), rồi mới
  giao việc vào đúng terminal đó (`worker-start --terminal`). Thấy hộp thoại folder trust hoặc hộp xác nhận Bypass Permissions,
  hay hết giờ, thì không giao gì và báo lỗi; harness không bao giờ tự trả lời các hộp thoại đó trên màn hình.
- **Chung folder trust với coordinator:** worker Claude không dừng ở hộp `Quick safety check`. Worktree được tạo không kèm
  agent, thư mục repo chính và worktree được đánh dấu tin cậy trong config của Claude Code (`~/.claude.json`, hoặc theo
  `CLAUDE_CONFIG_DIR`), rồi agent mới được mở trong terminal riêng. Chỉ ghi khi thư mục của coordinator đã được tin cậy; config
  không đọc được thì không ghi gì. Event `folder-trust` (`shared` / `skipped`). Tắt bằng `CODING_AGENT_SHARE_TRUST=off`.
- **Cùng permission mode với coordinator:** hook ghi `permission_mode` của phiên coordinator vào
  `.coding-agent/state/permission-mode`. Worker Claude chạy `claude --permission-mode <mode>` theo thứ tự: `--permission-mode`
  của `worker-start` / `delegate` / `plan-next`, rồi `CODING_AGENT_WORKER_PERMISSION_MODE`, rồi mode đã ghi (tìm ở `--root`,
  `$CLAUDE_PROJECT_DIR`, thư mục làm việc, rồi các thư mục cha của `--root`). Agent khác (codex) giữ cách khởi động của nó và
  việc bỏ qua được ghi event `permission-mode`.

## Dọn worktree của worker

Khi task của worker đã `completed` hoặc `failed` và worktree **sạch, đã gộp** (không file chưa commit, không commit nào mà HEAD
của coordinator chưa có), coding-agent **tự xoá** nó, không hỏi ai. Việc tự dọn chạy khi dựng bảng việc (mỗi prompt của
coordinator) và sau `plan-next`, `plan-status`, `inbox`, `delegate`, `worktree-list`; mỗi worktree xoá được in một dòng
`auto-clean: removed <tên> (<task>)` và ghi event `worktree-auto-removed`. Lỗi khi tự dọn chỉ là cảnh báo, không đổi mã thoát
của lệnh. Đặt `CODING_AGENT_AUTO_CLEAN=off` để tắt.

Worker đã báo xong mà nhánh chưa gộp thì worktree được giữ để coordinator còn kiểm; gộp nhánh vào HEAD xong thì lần chạy sau
tự xoá nó. Bảng việc chỉ còn liệt kê worktree có việc chưa gộp hoặc chưa commit.

```sh
python3 -m coding_agent.cli worktree-list                                   # tự dọn trước, rồi liệt kê worktree còn việc
python3 -m coding_agent.cli worktree-clean --task-id <id>                   # xoá một worktree sạch, đã gộp (khi tự dọn bị tắt)
python3 -m coding_agent.cli worktree-clean --all                            # xoá mọi worktree sạch, đã gộp; giữ phần còn lại
python3 -m coding_agent.cli worktree-clean --task-id <id> --discard --yes   # bỏ cả việc chưa gộp (mất code)
```

- Không cần `--yes` để xoá worktree sạch, đã gộp. Chỉ `--discard` mới cần `--yes`: thiếu thì lệnh từ chối. Hook
  `coordinator-guard` trả `ask` cho `worktree-clean --discard`, nên Claude Code hỏi người dùng xác nhận trước khi lệnh chạy, ở
  mọi mode trừ `off` và trừ khi phiên đang `bypassPermissions`. Coordinator vẫn phải hỏi người dùng trong hội thoại trước.
- Worktree còn file chưa commit, hoặc còn commit của worker mà nhánh của coordinator chưa có, được **giữ lại**. Với
  `--task-id` thì lệnh thoát 1; với `--all` thì đó là chủ ý và lệnh thoát 0. Nếu git không so sánh được, worktree cũng được giữ.
- File harness tự ghi vào worktree (`.coding-agent/events.jsonl`, `.coding-agent/state/`, `orca-run`, `links.jsonl`,
  `workers.jsonl`, `plan.json`, `installed.json`, `backups/`) không tính là việc; `.coding-agent/plan.yaml` thì có. Trên Windows,
  thay đổi chỉ ở bit thực thi (repo clone bằng git của WSL/Linux) cũng không tính.
- Tự dọn chỉ thấy task của Run đang dùng (`CODING_AGENT_ORCA_RUN`, hoặc Run đã lưu); worktree của task thuộc Run cũ phải xoá tay.
- Xoá gồm: nhả terminal của worker, `orca worktree rm` (xoá cả nhánh), và ghi `removed` vào sổ worker.

Worker bắt đầu từ commit hiện tại của coordinator (`--base-branch`). Không truyền thì Orca tạo worktree từ nhánh gốc mặc định
của repo, và worker sẽ thiếu việc mới nhất. Commit gốc của worktree được ghi vào sổ để chỉ đếm commit do worker thêm.
Đã kiểm với Orca cài trên máy (2026-10-05): tạo, chạy, liệt kê và xoá một worktree thật.

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
| 6. An toàn cài đặt | Một phần | `install.py` và `project_install.py`: cài, cài lại, gỡ, verify. Ghim commit khi cài (FR-005) chưa làm: xem dưới |
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
