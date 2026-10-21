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
| `src/coding_agent/autodecide.py` | Auto-decide: câu hỏi `AskUserQuestion` chờ N giây trong cửa sổ đếm ngược, hết giờ thì Fable hoặc phương án gợi ý chọn |
| `src/coding_agent/ratchet.py` | Ratchet: giữ hoặc quay lại từng lần thử đã verify theo một lệnh metric, snapshot bằng git qua index tạm |
| `src/coding_agent/evaluator.py` | Evaluator: một tiến trình `claude -p` mới chấm lượt theo rubric; grounding kiểm bằng chứng của từng tiêu chí |
| `src/coding_agent/brief.py` | Brief cho một task của PLAN: Global constraints, Files, Interfaces (SC-005) |
| `src/coding_agent/memory/` | Zero-Mem: spool, truy hồi qua `zm mcp`, episode, ghi lượt |
| `src/coding_agent/install.py` | Cài và gỡ an toàn: không xoá thư mục không có dấu của harness |
| `src/coding_agent/cli.py` | Lệnh cho agent: `recall`, `stats`, `report`, `forget`, `brief`, `status`, `orca-create`, `orca-status`, `claim`, `orca-check`, `delegate`, `plan-*`, `inbox`, `worker-kick`, `worker-resume`, `worker-settle`, `wiki-sync`, `wikieval` |
| `src/coding_agent/wiki/` | Công cụ wiki của dự án: neo đồng bộ code→wiki (`sync.py`) và bộ eval goldens (`evaluate.py`) |
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

## Auto-decide

Khi bật, mỗi lần agent hỏi bạn bằng `AskUserQuestion`, câu hỏi hiện trong một cửa sổ nhỏ có đồng hồ đếm ngược.
Bạn chọn trong thời gian chờ thì agent dùng câu trả lời của bạn. Hết giờ thì Fable (`claude-fable-5-1`) chọn một
phương án trong danh sách; Fable lỗi hoặc tắt thì dùng phương án agent gợi ý (nhãn có `(Recommended)`, không có thì
phương án đầu). Câu hỏi về xoá, ghi đè, commit/push cũng được tự chọn, và được gắn cờ `sensitive` trong
`.coding-agent/events.jsonl`. Đóng cửa sổ thì câu hỏi quay về terminal như thường. Hộp xin quyền của
`coordinator-guard` không bao giờ được tự trả lời.

```sh
python -m coding_agent.cli auto-decide on --timeout 60   # bật, chờ 60 giây (10 đến 3600)
python -m coding_agent.cli auto-decide --no-fable        # hết giờ thì dùng thẳng phương án gợi ý
python -m coding_agent.cli auto-decide status            # xem cấu hình đang có hiệu lực
python -m coding_agent.cli auto-decide off
```

Giá trị mặc định nằm ở khối `auto_decide:` của `integration.yaml`; lệnh trên ghi `.coding-agent/state/auto-decide.json`
và ghi đè khối đó từ câu hỏi kế tiếp. Cần `tkinter` và màn hình; thiếu thì câu hỏi hỏi như thường.

## Wiki: wiki-sync và wikieval

Hai công cụ wiki chạy từ coding-agent, không cần bản global của `setup` ở `~/.claude/harness`. Cả hai tất định,
không gọi model. Chạy trong thư mục dự án; dự án có `harness/coding-agent/src` vendored thì đặt
`PYTHONPATH=harness/coding-agent/src`.

```sh
python -m coding_agent.cli wiki-sync --check          # 0 = wiki current, 3 = code đổi (cờ code-drift vào stale.json), 2 = chưa có neo
python -m coding_agent.cli wiki-sync --mark-synced    # chốt neo sau khi rà wiki; chỉ ghi khi có gì đổi
python -m coding_agent.cli wiki-sync --flags-for "slug1,slug2"   # đường đọc của /query: luôn thoát 0
python -m coding_agent.cli wikieval --outputs harness/evals/outputs.json --check            # 2 khi có golden tụt
python -m coding_agent.cli wikieval --outputs harness/evals/outputs.json --write-baseline
python -m coding_agent.cli wikieval --self-test       # assert của mỗi golden chạy trên chính `expected` của nó
```

`wiki-sync` tìm `llmwiki/wiki` (hoặc `wiki/`) từ `--root` đi ngược lên. Dự án là gốc repo hoặc thư mục con được track
thì neo theo git (`.last-sync.json` giữ `gitHead`); dự án không có git, untracked hay bị repo cha ignore thì neo
theo dấu vân tay cây file (`mode: tree`, kèm `.last-sync-tree.json`). Chế độ tree bỏ qua thư mục build/vendor/cache,
file runtime (`.claude/audit/`, `*.jsonl` trong `.claude/`) và file bị `.gitignore` loại, kể cả `.gitignore` của
repo con. Chi phí mỗi lượt `--check` ghi vào `harness/metrics/sync-log.jsonl` khi thư mục đó có sẵn.

`wikieval` đọc goldens ở `fdk/wiki/sources/evals` hoặc `llmwiki/wiki/sources/evals` (`--evals-dir` để chọn thư mục
khác), baseline ở `<root>/harness/metrics/eval-baseline.json` và cấu hình adapter ở `<root>/harness/wikieval.config.yaml`
(thiếu thì tier 2 và tier 3 tắt). Mọi đường dẫn mặc định tính từ `--root` (mặc định thư mục hiện tại), không bao giờ
từ nơi cài coding-agent. `--write-baseline` từ chối ghi đè một baseline có dữ liệu bằng lượt chạy không chấm được
golden nào.

Định dạng `.last-sync.json`, `.last-sync-tree.json`, `stale.json` và `eval-baseline.json` (`wikieval-baseline/v1`)
giữ nguyên như bản của `setup`, nên neo và baseline có sẵn dùng tiếp được.

## Ratchet và evaluator

Hai cơ chế từ bài "Graph Engineering: The Karpathy Loop" (§II và §V). Cả hai chạy trong `stop-gate`, sau khi lệnh
verify đạt, và chỉ trong phiên tự ghi file: worker, maintainer, hoặc coordinator khi `coordinator-guard` không ở
`enforce`. Chỉ chặn (tức là bắt lượt chạy tiếp) khi khối đó và `stop-gate` cùng ở `enforce`; `shadow` chỉ ghi event.

### Ratchet

Mỗi lần Stop có verify đạt là một lần thử. Lần đạt đầu tiên của lượt là baseline. Lần sau chạy lệnh `metric` (số ở
dòng cuối stdout): tốt hơn điểm tốt nhất quá `min_delta` thì giữ, không thì cây file quay về bản đã giữ; metric lỗi
hoặc không in số cũng quay về. Sau mỗi lần thử, model được nhắc thử thêm một thay đổi có lý do. Ratchet dừng sau
`no_improve_stop` lần liên tiếp không cải thiện hoặc `max_trials_per_turn` lần thử, và để cây file ở bản tốt nhất.
Lượt hết `max_continuations` mà verify vẫn đỏ thì cây file quay về bản tốt nhất đã giữ.

```yaml
ratchet:
  mode: enforce
  assumption: "Số in ở dòng cuối của lệnh metric đo đúng chất lượng của việc."
  metric: "python3 scripts/score.py"
  direction: max          # min khi số nhỏ hơn là tốt hơn
  min_delta: 0
  no_improve_stop: 3
  max_trials_per_turn: 20
  metric_timeout_s: 600
```

- Baseline là lần đạt đầu tiên của lượt, không phải trạng thái trước lượt: lượt chỉ hỏi đáp không tốn một lần chạy
  metric, và trạng thái trước lượt có thể chưa qua verify.
- Snapshot là commit git qua index tạm (`GIT_INDEX_FILE`): không đụng HEAD, nhánh, hay index của bạn. File trong
  `.gitignore` và thư mục `.coding-agent/` nằm ngoài snapshot, nên không bị quay lại. Mỗi lần thử có ref dưới
  `refs/coding-agent/ratchet/<session>/` (`kept` là bản đang giữ), nên lần bị quay lại vẫn xem được bằng `git show`.
- Event `ratchet` trong `.coding-agent/events.jsonl`: `baseline`, `kept`, `reverted`, `crash`, `stopped` (lý do
  `no-improve`, `max-trials`, `no-change`), `unavailable` (không phải git, hoặc git lỗi).
- `metric` rỗng thì ratchet không chạy ở mọi mode.

### Evaluator

Sau verify (và ratchet đã dừng), một tiến trình `claude -p` mới chấm lượt. Nó không thấy hội thoại, chỉ nhận yêu cầu
gần nhất của người dùng, `rubric` (rỗng thì nó tự viết 2 đến 6 tiêu chí), và các lệnh verify đã đạt; nó chỉ có
`Read`, `Grep`, `Glob`, không MCP, không skill, không nạp cấu hình của dự án. Nó trả một báo cáo JSON:
`verdict` (`ok`, `not-ok`, `impossible`, `unverifiable`) và từng tiêu chí `met`.

```yaml
evaluator:
  mode: enforce
  assumption: "Một evaluator mới, chỉ đọc workspace, phát hiện được việc chưa xong mà lệnh verify bỏ sót."
  rubric:
    - "Có test cho trường hợp lỗi mạng"
  model: haiku
  timeout_s: 180
  grounding: enforce      # off | shadow | enforce
```

- `grounding` khác `off`: mỗi tiêu chí phải kèm `evidence` và `requiredEvidence`. Mỗi mục evidence của tiêu chí báo
  đạt phải là đường dẫn có thật bên trong workspace, hoặc đúng nguyên văn một lệnh verify đã đạt. `enforce`: tiêu chí
  báo đạt mà không có evidence, hoặc có mục không kiểm được, bị tính là chưa đạt.
- `enforce`: lượt `not-ok` chạy tiếp với mỗi tiêu chí chưa đạt một dòng JSON
  `{"decision":"revise","criterion":"c1","claim":"…","reason":"…","required_evidence":[…]}`, tối đa
  `verify.max_continuations` vòng mỗi lượt. `impossible` và `unverifiable` chỉ ghi event, không chặn.
- Event `evaluator`: `evaluator-verdict` (verdict, verdict có hiệu lực sau grounding, từng tiêu chí, chi phí),
  `evaluator-error` (thiếu `claude`, hết giờ, báo cáo không đọc được; lượt kết thúc như thường), `budget-exhausted`.
- Tiến trình con chạy với `CODING_AGENT_EVALUATOR_NESTED=1`, nên `stop-gate` trong phiên của nó không chạy lại.
  Test dùng `CODING_AGENT_EVALUATOR_CLAUDE` để trỏ tới một `claude` giả.

### Giới hạn

- Ratchet mặc định `shadow` nhưng không chạy cho tới khi bạn đặt `metric`. Evaluator mặc định `"off"`: khi bật (kể cả
  `shadow`) nó gọi một `claude -p` (Haiku) ở mỗi lượt có sửa file và verify đạt, tốn thời gian và quota.
- `stop-gate` khai `timeout_s: 900` vì Claude Code giết hook sau 60 giây theo mặc định; verify, metric và evaluator cùng
  chạy trong hook đó. Tăng `metric_timeout_s` hay `evaluator.timeout_s` thì tăng `timeout_s` của `stop-gate` theo.
- Restore của ratchet quay cả thay đổi bạn tự sửa tay trong lúc agent đang chạy lượt đó.
- Ref dưới `refs/coding-agent/ratchet/` không tự dọn: `git for-each-ref refs/coding-agent/ratchet` để xem,
  `git update-ref -d <ref>` để xoá.
- Lệnh git của ratchet chạy trực tiếp, không qua sandbox; lệnh metric chạy như lệnh verify.
- Grounding chỉ kiểm bằng chứng có tồn tại, không kiểm nó chứng minh được tiêu chí.
- Evaluator thêm lượt chạy tiếp sau khi ratchet đã dừng; các thay đổi đó không được ratchet đo lại trong lượt.

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
bash install.sh <dự án> --link <checkout>/src      # live link: package là symlink tới checkout, không copy
```

### Live link mode

Dự án có thể chạy coding-agent thẳng từ một checkout sống thay vì một bản copy: sửa coding-agent (hoặc merge vào
`main` rồi `git pull` trong checkout đó) là dự án dùng ngay, không cần cài lại.

```sh
bash install.sh <dự án> --link ~/Documents/Development/coding-agent-live/src
```

- `<src-dir>` là thư mục chứa `coding_agent/` (thường là `<checkout>/src`). Installer đổi sang đường dẫn tuyệt đối và
  dừng ngay (không đổi gì) nếu thiếu `<src-dir>/coding_agent/__init__.py`.
- Bước 3 không chép package: `harness/coding-agent/src/coding_agent` thành symlink tới `<src-dir>/coding_agent`
  (Windows không có quyền symlink: junction; không được nữa: copy). Báo cáo ghi rõ loại đã tạo. Nếu phải **copy** thì
  báo cáo ghi `COPIED ... live updates do NOT apply`: khi đó phải cài lại mới nhận thay đổi (bật Developer Mode để có symlink).
- Bản copy cũ chỉ bị thay khi còn dấu harness (giữ backup ở `.coding-agent/backups/`). Link cũ thì được trỏ lại; link
  không bao giờ bị đi theo khi xoá.
- Cài lại **không** có `--link` trên dự án đã link: link được **giữ nguyên** (không lặng lẽ đổi thành copy). Muốn quay
  về bản copy: `bash install.sh <dự án> --clean`.
- `--uninstall` xoá link như một link, checkout sống không bị đụng; `--uninstall --keep-core` giữ link.
- Verify (chạy hook với payload mẫu) chạy qua link. Worker worktree nhận `harness/` của coordinator như thường, và
  package trong đó vẫn trỏ về checkout sống.
- Checkout sống nên nằm ở nhánh ổn định (`main`): mọi phiên của dự án chạy đúng code đang có trong đó.

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
  sửa nội dung file (`sed -i`, `>`, `rm`, `pip install`, ...); và tool
  `Agent`/`Task` (subagent nội bộ không qua graph). Guard bỏ phần nằm trong dấu nháy trước khi so khớp, nên `echo "a -> b"`
  hay `git commit -m "rm cũ"` không bị chặn; `$(...)` và backtick trong nháy kép vẫn được xét vì shell chạy chúng.
- **Sửa cấu hình hook** (`.claude/settings.json`, `.claude/settings.local.json`, `integration.yaml`) bằng `Write`/`Edit`
  không bị chặn mà host hỏi người dùng xác nhận, để guard luôn tắt được từ trong phiên.
- **Coordinator vẫn được:** đọc file, lệnh đọc, ghi `.coding-agent/plan.yaml`, chạy một lệnh `coding_agent.cli` đơn lẻ,
  và chạy mọi lệnh git (add, commit, merge, push, fetch, pull, checkout, switch, branch, rebase, stash, apply, restore,
  rm, tag, cherry-pick, worktree, ...), kể cả `push --force`, `reset --hard`, `clean -f`. Khởi động phiên với
  `CODING_AGENT_COORDINATOR_GIT=safe` để chặn các dạng phá huỷ làm mất dữ liệu không lấy lại được: `git push --force` /
  `-f` / `--force-with-lease` / `+refspec`, `git reset --hard`, `git clean -f` (mọi tổ hợp cờ có `f`),
  `git checkout -- <path>` và `git checkout .`, `git restore` không có `--staged`, `git stash drop` / `clear`,
  `git branch -D`. Ở chế độ đó guard xét cả lệnh nối bằng `&&`/`;`/`|` và script trong `sh -c`.
- **Tự giao việc:** chỉ worker được ghi. Khi việc cần ghi hoặc khi guard chặn một lệnh, coordinator giao ngay trong cùng
  lượt (`delegate` cho một việc nhỏ, graph cho nhiều việc), không xin phép người dùng và không viết lại lệnh để lách guard.
  Chưa có Orca Run thì nó tự chạy `run-init`. Lời từ chối của guard kèm sẵn lệnh `delegate`. Nó chỉ hỏi người dùng về
  quyết định thật sự của họ (phạm vi, thao tác phá huỷ hoặc ra bên ngoài).
- **Ai là worker:** chỉ session chạy trong worktree mà `worker-start` của coding-agent tạo ra (hoặc `worker-adopt` ghi sổ). Danh sách nằm ở
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
python3 -m coding_agent.cli inbox                             # báo cáo worker chưa xử lý
python3 -m coding_agent.cli inbox --ack --task-id <id>        # đánh dấu đã xử lý chỉ báo cáo của task đó (lặp được)
python3 -m coding_agent.cli inbox --ack                       # đánh dấu đã xử lý cả hộp thư
```

- `inbox --ack` in từng báo cáo vừa đánh dấu (task, repo, subject). Gộp xong một task thì ack đúng task đó bằng `--task-id`:
  ack cả hộp thư làm báo cáo chưa xem mất khỏi bảng. Orca chỉ ack được cả một lô (delivery); báo cáo ack theo task được ghi vào
  `.coding-agent/acked.jsonl` và bị lọc khỏi bảng/`inbox`, còn lô Orca chỉ được ack khi mọi báo cáo trong lô đã được ack.
- **Run theo terminal:** bảng việc, `inbox` và các lệnh CLI làm việc trong Run (khi không có `--run`) lấy Run theo thứ tự:
  `--run`, `CODING_AGENT_ORCA_RUN` do người dùng đặt, Run mà Orca đang gắn với terminal coordinator (`orca orchestration
  run-current`, hoặc lỗi `consumer_fenced: ... bound to run_X`), rồi mới tới `.coding-agent/orca-run`. Nhiều terminal coordinator
  dùng chung file đó, nên Run của terminal chỉ dùng trong tiến trình; file chỉ được ghi khi chưa lưu Run nào (`run-init` vẫn ghi
  như cũ). Khi khác Run đã lưu, dòng đầu của bảng ghi `bảng theo Run X của terminal này (orca-run lưu Y)` và event `run-differs`
  được ghi tối đa một lần mỗi phiên (đánh dấu ở `.coding-agent/state/orca-run.<session>`).
- **Giao vào repo khác:** `delegate`/`plan-next` với `--root` khác thư mục coordinator (`$CLAUDE_PROJECT_DIR`, không có thì thư mục
  làm việc). Bảng việc, báo cáo, `inbox`, `worktree-list`, tự dọn và `roots-list` cùng đọc một danh sách (`orca_cli.all_roots`):
  thư mục coordinator; mọi thư mục trong cây của nó (tối đa 3 cấp, không vào thư mục ẩn, `node_modules`... hay bên trong repo con)
  có `.coding-agent/links.jsonl`, tìm lại mỗi lần đọc; và repo ngoài cây ghi trong `.coding-agent/roots.json` (lệnh `--root` ghi
  vào đó chỉ khi root nằm ngoài cây). Từ mỗi root đọc links, `plan.json` và sổ worker; dòng của task ở repo khác kèm tên repo.
  Task của Run mà chưa sổ nào biết (giao trước khi có `roots.json`) được tìm lại repo từ Orca (`orchestration worker-list` →
  worktree → repo chính, hoặc main worktree Orca liệt kê) và ghi vào `roots.json` nếu `links.jsonl` của repo đó có task; lỗi Orca
  ghi event `guard=roots`, thử lại sau 10 phút. `roots-list` liệt kê kèm nguồn (`own`, `tree`, `roots.json`); `roots-add <path>...`
  ghi tay repo ngoài cây.
- **Worker đang hỏi:** worker chờ trả lời `orca orchestration ask` (type `question`/`escalation`, hoặc `payload.question`) nằm ở
  nhóm đầu bảng `worker đang hỏi, chờ trả lời (N):` kèm lựa chọn, msg id và lệnh `orca orchestration reply --id <msg> --run <run>`,
  và không còn nằm trong `đang chạy`. Đã trả lời = có tin cùng thread, worker báo cáo task, hoặc câu hỏi mới hơn của cùng dispatch;
  heartbeat và `send` thường tới dispatch không tính. Task đang chạy mà worker im lặng quá 20 phút ghi `(không có tin X phút)`.
  `inbox` (không `--ack`) in cả câu hỏi mở.
- Nhóm `xong, worktree còn việc chưa gộp hoặc chưa commit` lấy từ git và sổ worker, không từ hộp thư: nó luôn hiện, kể cả khi
  hộp thư không đọc được hay báo cáo đã được ack, tới khi nhánh được gộp hoặc bỏ.

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
  `worktree-list`, `worktree-clean` của coordinator thấy nó). "Commit chưa gộp" được so với HEAD của repo đích (và nhánh base
  của task trong repo đó).
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

## Khi worker bị dừng vì mất mạng / lỗi API tạm thời

Đã xảy ra (2026-10-12): 5 worker Claude Code dừng 3 giờ vì mất mạng. Màn hình mỗi worker kết thúc bằng
`API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)` (có khi kèm `API Error: Connection lost
mid-response`), dòng `Worked for 3h 5m · done 9:24 PM`, rồi ô nhập trống. Orca vẫn báo dispatch `ready`, bảng việc vẫn ghi
"đang chạy", và khi mạng có lại không có gì đánh thức worker. coding-agent xử lý như sau (`src/coding_agent/resume.py`):

- **Phát hiện:** với mỗi task chưa `completed`/`failed` của coordinator này, đọc màn hình terminal của dispatch. Worker là
  **stalled** khi ô nhập trống, không có spinner, và trong 15 dòng không trống phía trên ô nhập, dòng `API Error` cuối cùng là
  lỗi tạm thời (`ENOTFOUND`, `ECONNRESET`, `ECONNREFUSED`, `ETIMEDOUT`, `EAI_AGAIN`, `Connection lost`, `Can't reach the API`,
  `fetch failed`, `overloaded`, `rate limit`, mã 429/500/502/503/504/529; bảng `TRANSIENT_MARKS`), và sau nó chưa có lượt làm
  việc mới (dòng `⏺` không phải lỗi). Lỗi mà thử lại không sửa được (401/403, invalid API key, credit, context length, hoặc
  `API Error` lạ) là **blocked**: chỉ báo, không tự resume.
- **Lượt bị cắt ngang giữa chừng (thêm sau sự cố 2026-10-15/16):** hai worker gần xong việc (một worker đã chạy đạt e2e, chỉ
  còn commit và gửi `worker_done`) bị cắt lượt với đúng hai dòng `API Error: The response stopped arriving. The response above
  may be incomplete.` và `API Error: Your computer went to sleep mid-response. The response above may be incomplete.` (đo trên
  terminal thật). Trước đây chúng bị xếp **blocked** và `worker-resume --watch` in "needs a person" suốt nhiều giờ. Giờ các lỗi
  ngắt lượt sau là tạm thời, được tự resume: `response stopped arriving`, `went to sleep mid-response`, `Request timed out`,
  `socket hang up`, `network error`, `API Error: terminated`, `stream ended` / `stream error`, `UND_ERR_*`, `Premature close`.
  Lỗi vĩnh viễn (401/403, credit, context length, `/login`) vẫn **blocked** dù dòng lỗi có kèm các chữ trên.
- **Kiểm mạng trước khi gửi:** mở kết nối TCP tới `api.anthropic.com:443` (timeout 3 giây). Không nối được thì không gửi gì,
  chỉ ghi nhận "đang chờ mạng". Đổi đích bằng `CODING_AGENT_API_HOST=host` hoặc `host:port` (ví dụ proxy bắt buộc).
- **Resume:** gửi một dòng không dấu vào terminal của worker qua `orca terminal send --enter --wait-submit 20`: mạng đã có
  lại, làm tiếp task từ chỗ đang dở, kiểm lại lệnh và shell nền đang chạy dở, xong thì commit và gửi `worker_done`.
- **Chống gửi lặp:** mỗi lần gửi ghi vào `.coding-agent/resumes.jsonl` kèm dấu vân tay của màn hình. Không resume lại cùng một
  màn hình, tối đa 5 lần cho mỗi dispatch, cách nhau ít nhất 60 giây. Quá giới hạn thì bảng việc ghi "bị chặn, cần người xử lý".
- **Tự động mỗi lượt của coordinator:** bước dựng bảng việc (hook `SessionStart` / `UserPromptSubmit`, lệnh `board`) quét và
  resume, bỏ qua nếu lần quét trước chưa tới 60 giây (vẫn hiện kết quả lần quét đó). Mỗi lượt quét tự động chỉ gửi trong 20 giây
  đầu để không giữ hook; worker còn lại được gửi ở lượt quét sau. Bảng việc có nhóm riêng "worker bị dừng vì
  lỗi API/mạng" (đã tự resume / đang chờ mạng / bị chặn cần người xử lý); các task đó không còn nằm trong "đang chạy". Mọi lỗi
  của bước này bị nuốt và ghi event, không làm hỏng hook. Tắt bằng `CODING_AGENT_AUTO_RESUME=off`.
- **`worker-resume [--task-id <id> ...] [--dry-run] [--watch [--interval GIÂY]] [--run ID]`:** quét và resume bằng tay. Mặc
  định quét mọi worker đang chạy của Run; `--task-id` chỉ một task (lặp lại được, và resume được cả task không do coordinator
  này tạo); `--dry-run` chỉ in; `--watch` lặp vô hạn (mặc định 60 giây một lần) để coordinator chạy nền, tự resume khi mạng trở
  lại, in một dòng mỗi khi trạng thái một worker đổi. Mã thoát 0 kể cả khi không có gì để làm.
- **Worker im lặng, chưa báo:** task còn `dispatched` mà màn hình worker ở trạng thái idle (lượt đã kết thúc, ô nhập trống,
  không spinner, không có lỗi) và không đổi suốt một ngưỡng (mặc định 10 phút), trong khi hộp thư orchestration chưa có
  `worker_done` của task đó (cùng dispatch), là **silent**. Worker được nhắc một dòng ngắn: gửi qua
  `orca orchestration send --to dispatch:<id> --type status` rồi gõ cùng dòng đó vào terminal (`Nhac viec: ...`: việc đã xong
  thì commit rồi gửi `worker_done` kèm tóm tắt, chưa xong thì làm tiếp). Tối đa 2 lần nhắc mỗi dispatch; sau một lần nhắc, worker
  phải im thêm đủ ngưỡng mới bị nhắc lại. Mỗi lần nhắc ghi vào `.coding-agent/resumes.jsonl` với `kind: nudge` (resume ghi
  `kind: resume`; hai loại có giới hạn riêng). Đồng hồ im lặng tính từ lần quét đầu tiên thấy màn hình idle (lưu ở
  `.coding-agent/state/idle-since.json`, quét bằng bảng việc, `worker-resume` hay `--watch` đều tính); màn hình đổi hoặc
  worker chạy lại thì đồng hồ đặt lại. Bảng việc có nhóm riêng "worker im lặng, chưa báo" kèm thời gian im và số lần đã nhắc
  (`đã nhắc 1/2 lần`), tách khỏi nhóm lỗi API/mạng; nhắc đủ 2 lần mà vẫn im thì ghi "cần người xử lý".
  `worker-resume --dry-run` liệt kê cả worker stalled ("would be resumed") lẫn worker silent ("would be reminded").
- **Biến môi trường:** `CODING_AGENT_SILENT_MINUTES` đổi ngưỡng im lặng (số phút, chấp nhận số lẻ, mặc định `10`); `off`
  hoặc `0` tắt việc nhắc. `CODING_AGENT_AUTO_RESUME=off` tắt cả bước tự động của bảng việc (resume lẫn nhắc).
  `CODING_AGENT_API_HOST` đổi đích kiểm mạng.
- **Event** (`guard: worker-resume`): `resumed`, `resume-failed`, `nudged`, `nudge-failed`, và khi trạng thái đổi:
  `waiting-network`, `blocked`, `limit`, `already-resumed`, `nudge-limit`, `inbox-unreadable`; `error` khi bước tự động gặp lỗi.

Giới hạn: phát hiện dựa trên chữ trên màn hình Claude Code; hai dòng lỗi ngắt lượt ở trên đã đo trên terminal thật, các dòng
lỗi mạng của sự cố 2026-10-12 và các mẫu ngắt lượt còn lại lấy theo mô tả và thông báo lỗi của Node/undici, và agent khác
(codex) có dạng lỗi khác thì chưa nhận ra. Worker im lặng chỉ được phát hiện khi có lượt quét. Không có coordinator nào đang chạy lượt mới
và không có `worker-resume --watch` chạy nền thì không ai quét.

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
- **Chung bộ cài coding-agent với coordinator:** cài bằng `install.sh` không commit gì, nên `harness/`, `integration.yaml`,
  `.claude/settings.json`, `.codex/hooks.json` và `.coding-agent/installed.json` chỉ có ở checkout của coordinator; worktree Orca
  tạo từ commit thì thiếu, và worker không chạy hook nào (không stop-gate, verify, ratchet, evaluator). Vì vậy ngay sau khi worktree
  có và trước khi agent khởi động (Claude Code và Codex chỉ đọc file hook lúc khởi động), coding-agent tạo link trong worktree trỏ
  về checkout của coordinator (`--root`, không có bộ cài thì repo chính): symlink trên macOS/Linux; trên Windows symlink nếu tài
  khoản được phép, không thì junction cho `harness/` và bản sao cho file. Đường dẫn worktree đã có (file, thư mục hay link), đường
  dẫn git đang track, hay đường dẫn coordinator không có thì để nguyên, không bao giờ ghi đè. Một bộ cài dùng chung cho mọi worker,
  nên nâng cấp bộ cài của coordinator là worker có ngay. Link đã tạo được ghi ở `.coding-agent/worktree-links.json` trong worktree.
  Có bộ cài thì mọi agent (cả codex) đi đường tạo worktree không kèm agent rồi mở agent trong terminal riêng. Event
  `worker-install` (`linked` / `skipped`). `worker-adopt` và `worker-kick` cũng link vào worktree chúng ghi sổ; agent đang chạy ở đó
  đọc hook ở lần khởi động sau.
- **Không commit nhầm bộ cài:** các đường dẫn trên, cùng `.claude/settings.json.bak` (installer tạo) và
  `.coding-agent/worktree-links.json`, được thêm vào `<git-common-dir>/info/exclude` (một khối mở đầu bằng
  `# coding-agent: install linked into worker worktrees`), file mọi worktree của repo cùng đọc, kể cả checkout của coordinator.
  Mẫu neo ở gốc và không có `/` cuối (`/harness`), để khớp cả symlink. Nên `git status` không hiện chúng và `git add -A` của
  worker không commit chúng. Đường dẫn git đang track thì không thêm.
- **Worker biết mình là worker:** worktree được ghi vào sổ worker trước khi agent khởi động, và trên macOS/Linux lệnh mở agent
  là `CODING_AGENT_ROLE=worker claude ...` (Orca gõ `--command` vào login shell). Nên `stop-gate` coi phiên là phiên tự ghi
  (`_writes_here`) và chạy ratchet, evaluator; `coordinator-guard` không chặn worker. Trên Windows shell có thể là PowerShell
  hoặc cmd, không nhận dạng đó, nên chỉ dựa vào sổ worker.
- **Gate trong worktree đọc file của worktree:** `coding_agent.gate --manifest integration.yaml` và các hook nạp manifest theo
  đường dẫn tuyệt đối, không theo đích của link, nên trong worker chúng so file hook của chính worktree.

## Dọn worktree của worker

Khi task của worker đã `completed` hoặc `failed` và worktree **sạch, đã gộp** (không file chưa commit, không commit nào của
worker mà cả HEAD của coordinator lẫn nhánh base của task đều chưa có), coding-agent **tự xoá** nó, không hỏi ai. Nhánh base là
nhánh worker được tạo từ đó (`delegate --base-branch`, `base:` của node plan), kèm nhánh upstream remote-tracking của nó; sổ worker
ghi nó thành `base_branch`. Nhánh khác không tính: nhánh tách từ nhánh của một worker chứa commit của worker đó mà không gộp.
Bản ghi cũ chưa có `base_branch`, hoặc nhánh base đã bị xoá, thì chỉ so với HEAD. Việc tự dọn chạy khi dựng bảng việc (mỗi prompt của
coordinator) và sau `plan-next`, `plan-status`, `inbox`, `delegate`, `worktree-list`; mỗi worktree xoá được in một dòng
`auto-clean: removed <tên> (<task>)` và ghi event `worktree-auto-removed`. Lỗi khi tự dọn chỉ là cảnh báo, không đổi mã thoát
của lệnh. Đặt `CODING_AGENT_AUTO_CLEAN=off` để tắt.

Worktree đã kiểm là an toàn được xoá với force: file runtime của harness (`.coding-agent/`) và nhiễu bit thực thi trên Windows
làm `git worktree remove` từ chối dù không còn việc gì trong đó. Thư mục sót lại khi việc xoá dừng giữa chừng
(`Directory not empty`):

- Chỉ còn rác của hệ điều hành (`.DS_Store`, `Thumbs.db`, `desktop.ini`, và thư mục chỉ chứa chúng) hoặc rỗng: coi như đã mất,
  bản ghi được đánh dấu đã xoá và thư mục bị xoá.
- Còn nguyên checkout nhưng file `.git` trỏ tới git dir không còn (đăng ký worktree đã mất): chỉ xoá khi chứng minh được không
  mất gì — file trong thư mục khớp đúng một commit đã gộp (commit cuối của worker mà lần dọn trước thấy, sổ worker ghi thành
  `head`; đỉnh nhánh trùng tên thư mục; hoặc commit nó bắt đầu từ), so bằng object store của repo chính với một index tạm,
  bỏ qua file bị `.gitignore` (như `node_modules`), rác hệ điều hành và file runtime của harness. Không chứng minh được thì
  giữ và ghi `đăng ký worktree đã mất, cần kiểm tay`.
- Còn thứ khác: để nguyên. Khi xoá không bao giờ đi theo symlink hay junction ra ngoài thư mục.

Worker đã báo xong mà nhánh chưa gộp thì worktree được giữ để coordinator còn kiểm; gộp nhánh vào HEAD hoặc vào nhánh base của
task xong thì lần chạy sau tự xoá nó. Bảng việc chỉ còn liệt kê worktree có việc chưa gộp hoặc chưa commit.

```sh
python3 -m coding_agent.cli worktree-list                                   # tự dọn trước, rồi liệt kê worktree còn việc
python3 -m coding_agent.cli worktree-clean --task-id <id>                   # xoá một worktree sạch, đã gộp (khi tự dọn bị tắt)
python3 -m coding_agent.cli worktree-clean --all                            # xoá mọi worktree sạch, đã gộp; giữ phần còn lại
python3 -m coding_agent.cli worktree-clean --task-id <id> --discard --yes   # bỏ cả việc chưa gộp (mất code)
```

- Không cần `--yes` để xoá worktree sạch, đã gộp. Chỉ `--discard` mới cần `--yes`: thiếu thì lệnh từ chối. Hook
  `coordinator-guard` trả `ask` cho `worktree-clean --discard`, nên Claude Code hỏi người dùng xác nhận trước khi lệnh chạy, ở
  mọi mode trừ `off` và trừ khi phiên đang `bypassPermissions`. Coordinator vẫn phải hỏi người dùng trong hội thoại trước.
- Worktree còn file chưa commit, hoặc còn commit của worker mà cả nhánh của coordinator lẫn nhánh base của task đều chưa có,
  được **giữ lại**. Gộp nghĩa là commit tới được từ nhánh đó: squash hay rebase merge vẫn để commit của worker là chưa gộp. Với
  `--task-id` thì lệnh thoát 1; với `--all` thì đó là chủ ý và lệnh thoát 0. Nếu git không so sánh được, worktree cũng được giữ.
- File harness tự ghi vào worktree (`.coding-agent/events.jsonl`, `.coding-agent/state/`, `orca-run`, `links.jsonl`,
  `workers.jsonl`, `plan.json`, `installed.json`, `backups/`, `roots.json`, `acked.jsonl`, `worktree-links.json`) không tính là việc; `.coding-agent/plan.yaml` thì có. Trên Windows,
  thay đổi chỉ ở bit thực thi (repo clone bằng git của WSL/Linux) cũng không tính.
- Tự dọn chỉ thấy task của Run đang dùng (`CODING_AGENT_ORCA_RUN`, hoặc Run đã lưu); worktree của task thuộc Run cũ phải xoá tay.
- Xoá gồm: nhả terminal của worker, gỡ link bộ cài (xem "Khởi động worker"), `orca worktree rm` (xoá cả nhánh), và ghi
  `removed` vào sổ worker. Link được gỡ trước, như link: symlink và junction bị xoá mà không đi vào thư mục chúng trỏ tới, bản sao
  chỉ bị xoá khi vẫn là file thường trong worktree, và đường dẫn không còn là thứ harness tạo thì để nguyên. Bộ cài ở checkout
  của coordinator không bị đụng. Link bộ cài cũng không tính là việc khi kiểm worktree sạch.

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

- Store là thư mục `<CODING_AGENT_ZEROMEM_HOME>/workspaces/<sha256(project)[:16]>` (mặc định
  `~/.coding-agent/zeromem`). Một project là một git repo: `project` là main checkout của repo, tìm từ
  `git rev-parse --git-common-dir` (thư mục cha khi nó kết thúc bằng `.git`). Vì vậy main checkout, workspace Orca
  và worktree của worker cùng một repo dùng chung một store, còn hai repo khác nhau không bao giờ chung, kể cả khi
  nằm trong cùng một folder project. Thư mục không phải git là project của chính nó, theo đường dẫn.
- Main checkout giữ nguyên key cũ (`sha256` của đường dẫn gốc repo), nên store đã có không mất.
- Phiên ở gốc folder project (không phải git) không ghi lượt chạy bên trong một repo con: lượt đó ghi event
  `memory` `other-project` và không vào store nào; worker của repo con ghi vào store của repo đó.
- `recall`, `stats`, `report`, `forget` dùng cùng cách tìm project từ `--root` (mặc định thư mục hiện tại).
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
