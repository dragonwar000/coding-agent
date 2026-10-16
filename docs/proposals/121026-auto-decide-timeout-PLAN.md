---
type: draft
title: 121026-auto-decide-timeout-PLAN
status: proposed
timestamp: 2026-10-12
task:
---

# Auto-decide — PLAN thi hành

**Goal:** Khi user bật auto-decide, mỗi lần agent gọi `AskUserQuestion` thì câu hỏi hiện trong một cửa sổ đếm ngược; user trả lời thì dùng câu đó, hết giờ thì Fable chọn (lùi về phương án được gợi ý), và agent được báo lại đã chọn gì.
**Architecture:** Một module mới `src/coding_agent/autodecide.py` giữ toàn bộ logic (cấu hình hiệu lực, chọn phương án, cửa sổ tkinter chạy subprocess, gọi Fable qua `claude -p`). Một hook PreToolUse `auto-decide` (matcher `AskUserQuestion`) gọi `autodecide.decide` rồi trả `permissionDecision: allow` + `updatedInput.answers`. Cấu hình mặc định ở `integration.yaml` (`auto_decide:`), user ghi đè bằng `cli auto-decide`, lưu ở `.coding-agent/state/auto-decide.json`.
**Tech stack:** Python ≥ 3.10 (máy dev: 3.12), stdlib (`tkinter`, `subprocess`, `json`, `re`), PyYAML ≥ 6, pytest ≥ 8. Không thêm dependency.
**SPEC nguồn:** `docs/proposals/121026-auto-decide-timeout.md` (đã duyệt 2026-10-12)

## Origin
- **SPEC:** `docs/proposals/121026-auto-decide-timeout.md`
- **Commit:** _(verify-before-commit điền)_

## Global constraints
- Manifest duy nhất: mọi hook và cấu hình mới khai trong `integration.yaml` + `src/coding_agent/template.yaml`; `.claude/settings.json` chỉ được sinh bằng `python -m coding_agent.gen`, không sửa tay; `python -m coding_agent.gate --manifest integration.yaml` phải thoát 0.
- Mọi hook không `off` bắt buộc có `assumption`. Hook mới bắt đầu ở `mode: shadow` (FR-010) và manifest giữ `verified: false`.
- Cấu hình sai bị từ chối ngay lúc nạp manifest (`manifest.py`), không âm thầm thay bằng mặc định.
- Đường dẫn trong cấu hình sinh ra là tương đối, không tuyệt đối (`gen.py`).
- Mỗi quyết định của hook ghi qua `ctx.note(guard=..., kind=..., applied=..., detail=...)`.
- Không thêm dependency ngoài `pyyaml>=6` (runtime) và `pytest>=8` (test); `tkinter` là stdlib, được phép nhưng phải có đường lùi khi thiếu.
- Verify: `python3 -m pytest -q` xanh trước khi commit.

**Ghi chú môi trường (không phải ràng buộc mới):** trên Windows lệnh là `python`, không phải `python3`. Repo này không có `.claude/settings.json` nên `coding_agent.gate` ở chính repo này thoát 1 ("settings.json is missing") cả trước khi đổi gì; cổng của từng task là `python -m pytest -q`. Hành vi chung của mọi hook: fail-open (exception → exit 0, xem `src/coding_agent/hooks/__init__.py:run`).

## File structure
- Tạo `src/coding_agent/autodecide.py` — toàn bộ logic auto-decide: cấu hình hiệu lực + lưu state, chọn phương án gợi ý, kiểm câu trả lời, cửa sổ đếm ngược (subprocess `python -m coding_agent.autodecide window`), gọi Fable, dựng output hook.
- Sửa `src/coding_agent/manifest.py` — dataclass `AutoDecide`, hàm `auto_decide_from`, trường `Manifest.auto_decide`, trường `Hook.timeout_s`.
- Sửa `src/coding_agent/gen.py` — ghi `"timeout"` vào entry hook khi `Hook.timeout_s` có giá trị.
- Sửa `src/coding_agent/cli.py` — lệnh `auto-decide`.
- Sửa `src/coding_agent/hooks/handlers.py` — handler `auto_decide`, `_turn_context`, đăng ký `REGISTRY`, truyền cờ vào `coordinator.contract`.
- Sửa `src/coding_agent/coordinator.py` — `contract(python_src, auto_decide=False)` thêm một dòng khi bật.
- Sửa `integration.yaml`, `src/coding_agent/template.yaml` — khối `auto_decide:` + hook `auto-decide`.
- Sửa `README.md` — mục "Auto-decide".
- Tạo `tests/test_auto_decide.py` — cấu hình, CLI, logic thuần, cửa sổ (không mở GUI), Fable (giả lập).
- Tạo `tests/test_auto_decide_hook.py` — hook chạy như host gọi (subprocess, JSON stdin).

## Thứ tự & phụ thuộc
Task 1 → Task 2 → (Task 3 ∥ Task 4) → Task 5. Task 3 và Task 4 cùng sửa `autodecide.py` ở các hàm khác nhau; gộp Task 3 trước rồi rebase Task 4 nếu chạy song song.

| Task | Agent (CLI) | Thoả | Phụ thuộc |
|---|---|---|---|
| 1 Cấu hình + CLI | codex | FR-001, FR-002 | — |
| 2 Hook end-to-end với cửa sổ giả lập | claude | FR-003, FR-005 (nhánh gợi ý), FR-006, FR-008 | 1 |
| 3 Cửa sổ đếm ngược tkinter | claude | FR-004, FR-007 | 2 |
| 4 Fable chọn khi hết giờ | claude | FR-005, FR-010 | 2 |
| 5 Hợp đồng coordinator + README | codex | FR-009 | 2 |

---

### Task 1: Cấu hình `auto_decide` + lệnh `cli auto-decide`

**Thoả:** FR-001, FR-002

**Files:**
- Sửa: `src/coding_agent/manifest.py` (dataclass `Hook` dòng 32-43, `Manifest` dòng 66-90, `_hook` dòng 111-133, `load` dòng 136-220)
- Sửa: `src/coding_agent/gen.py:42-51` (`_render_host`)
- Tạo: `src/coding_agent/autodecide.py`
- Sửa: `src/coding_agent/cli.py` (import dòng 61-65; parser trong `main` sau dòng 288; `_command` ngay đầu khối `try:`)
- Sửa: `integration.yaml`, `src/coding_agent/template.yaml` (khối `auto_decide:` ngay trên `hooks:`)
- Test: `tests/test_auto_decide.py`

**Interfaces:**
- Consumes: `manifest.ManifestError`, `manifest._require_int`, `state._write_json(path: Path, data: dict) -> None`.
- Produces:
  - `manifest.AutoDecide(enabled: bool = False, timeout_s: int = 60, fable: bool = True, fable_timeout_s: int = 90)` — frozen dataclass.
  - `manifest.auto_decide_from(raw: Any, where: str = "auto_decide") -> AutoDecide` — raise `ManifestError` khi sai.
  - `manifest.Manifest.auto_decide: AutoDecide` (mặc định `AutoDecide()`), `manifest.Hook.timeout_s: int | None` (mặc định `None`).
  - `autodecide.STATE_FILE = Path(".coding-agent") / "state" / "auto-decide.json"`
  - `autodecide.effective(root: Path, base: AutoDecide | None = None) -> AutoDecide`
  - `autodecide.save(root: Path, settings: AutoDecide) -> None`
  - `autodecide.manifest_settings(root: Path) -> AutoDecide`
  - CLI: `python -m coding_agent.cli [--root R] auto-decide [on|off|status] [--timeout N] [--fable|--no-fable] [--fable-timeout N]` → in JSON `{"enabled", "timeout_s", "fable", "fable_timeout_s"}`, exit 0; giá trị sai → stderr `auto-decide: <lỗi>`, exit 1, không ghi state.

- [ ] **Step 1: viết test fail** — tạo `tests/test_auto_decide.py`:

```python
"""Auto-decide: configuration, the CLI switch, the choice rules, the window and Fable (no GUI is opened)."""

import json
import re
from pathlib import Path

import pytest

from coding_agent import autodecide, cli, gen
from coding_agent.manifest import AutoDecide, ManifestError, load

BASE = """schema: 1
verified: false
python_src: src
hooks:
  - id: auto-decide
    host: [claude_code]
    event: PreToolUse
    matcher: AskUserQuestion
    mode: shadow
    timeout_s: 4215
    assumption: "a question nobody answers in time can be decided for the user"
"""


def write(root: Path, text: str) -> Path:
    path = root / "integration.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_the_manifest_has_no_auto_decide(tmp_path):
    assert load(write(tmp_path, BASE)).auto_decide == AutoDecide(enabled=False, timeout_s=60, fable=True, fable_timeout_s=90)


@pytest.mark.parametrize("block, message", [
    ("auto_decide: {timeout_s: 5}\n", "auto_decide.timeout_s must be an integer from 10 to 3600"),
    ("auto_decide: {timeout_s: 3601}\n", "auto_decide.timeout_s must be an integer from 10 to 3600"),
    ("auto_decide: {fable_timeout_s: 601}\n", "auto_decide.fable_timeout_s must be an integer from 10 to 600"),
    ("auto_decide: {enabled: yes-please}\n", "auto_decide.enabled must be true or false"),
    ("auto_decide: [1, 2]\n", "auto_decide must be a mapping"),
])
def test_rejects_a_bad_auto_decide_block(tmp_path, block, message):
    with pytest.raises(ManifestError, match=re.escape(message)):
        load(write(tmp_path, BASE + block))


def test_the_hook_timeout_is_rendered(tmp_path):
    rendered = gen.render(load(write(tmp_path, BASE)), ["claude_code"])
    entry = rendered["claude_code"]["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "AskUserQuestion"
    assert entry["hooks"][0]["timeout"] == 4215


def test_the_cli_turns_it_on_changes_the_wait_and_turns_it_off(tmp_path, capsys):
    write(tmp_path, BASE + "auto_decide: {timeout_s: 60}\n")
    assert cli.main(["--root", str(tmp_path), "auto-decide", "on", "--timeout", "90"]) == 0
    assert json.loads(capsys.readouterr().out)["timeout_s"] == 90
    base = load(tmp_path / "integration.yaml").auto_decide
    assert autodecide.effective(tmp_path, base) == AutoDecide(enabled=True, timeout_s=90, fable=True, fable_timeout_s=90)
    assert cli.main(["--root", str(tmp_path), "auto-decide", "--no-fable"]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": True, "timeout_s": 90, "fable": False, "fable_timeout_s": 90}
    assert cli.main(["--root", str(tmp_path), "auto-decide", "off"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False


def test_the_cli_refuses_a_wait_out_of_range_and_writes_nothing(tmp_path, capsys):
    write(tmp_path, BASE)
    assert cli.main(["--root", str(tmp_path), "auto-decide", "on", "--timeout", "5"]) == 1
    assert "from 10 to 3600" in capsys.readouterr().err
    assert not (tmp_path / autodecide.STATE_FILE).exists()


def test_a_broken_state_file_falls_back_to_the_manifest(tmp_path):
    (tmp_path / autodecide.STATE_FILE).parent.mkdir(parents=True)
    (tmp_path / autodecide.STATE_FILE).write_text('{"enabled": true, "timeout_s": 2}', encoding="utf-8")
    assert autodecide.effective(tmp_path, AutoDecide()) == AutoDecide()
```

- [ ] **Step 2: chạy cho THẤY nó fail**

Chạy: `python -m pytest tests/test_auto_decide.py -q`
Mong đợi: FAIL ở bước collect — `ImportError: cannot import name 'autodecide' from 'coding_agent'`

- [ ] **Step 3: code tối thiểu cho pass**

3a. `src/coding_agent/manifest.py` — thêm hằng ngay dưới `VERIFY_SCOPES = ("root", "changed-repos")`:

```python
# auto-decide (FR-001): how long a question to the user waits, and how long Fable may take to decide it.
AUTO_DECIDE_TIMEOUT_S = (10, 3600)
FABLE_TIMEOUT_S = (10, 600)
```

Thêm trường cuối vào `Hook` (sau `assumption: str`):

```python
    # Seconds the host lets this hook run; None leaves the host's default.
    timeout_s: int | None = None
```

Thêm dataclass ngay trên `class Manifest`:

```python
@dataclass(frozen=True)
class AutoDecide:
    enabled: bool = False
    timeout_s: int = 60
    fable: bool = True
    fable_timeout_s: int = 90
```

Thêm trường cuối vào `Manifest` (sau `hooks: ...`):

```python
    auto_decide: AutoDecide = field(default_factory=AutoDecide)
```

Thêm hai hàm ngay dưới `_str_list`:

```python
def _require_range(value: Any, field_name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ManifestError(f"{field_name} must be an integer from {low} to {high}")
    return value


def auto_decide_from(raw: Any, where: str = "auto_decide") -> AutoDecide:
    """The auto-decide settings in `raw`; a missing block is the defaults, a wrong value raises ManifestError."""
    if raw is None:
        return AutoDecide()
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be a mapping")
    enabled = raw.get("enabled", False)
    fable = raw.get("fable", True)
    for name, value in (("enabled", enabled), ("fable", fable)):
        if not isinstance(value, bool):
            raise ManifestError(f"{where}.{name} must be true or false")
    return AutoDecide(
        enabled=enabled,
        timeout_s=_require_range(raw.get("timeout_s", 60), f"{where}.timeout_s", *AUTO_DECIDE_TIMEOUT_S),
        fable=fable,
        fable_timeout_s=_require_range(raw.get("fable_timeout_s", 90), f"{where}.fable_timeout_s", *FABLE_TIMEOUT_S),
    )
```

Trong `_hook`, ngay trước `return Hook(...)`:

```python
    timeout_s = item.get("timeout_s")
    if timeout_s is not None:
        timeout_s = _require_int(timeout_s, f"hooks[{hook_id}].timeout_s", 1)
```

và đổi dòng return thành:

```python
    return Hook(id=hook_id, host=host, event=event, matcher=matcher, mode=mode, assumption=str(assumption), timeout_s=timeout_s)
```

Trong `load`, thêm đối số cuối của `return Manifest(...)` (sau `hooks=hooks,`):

```python
        auto_decide=auto_decide_from(raw.get("auto_decide")),
```

3b. `src/coding_agent/gen.py` — thay thân vòng lặp của `_render_host`:

```python
def _render_host(manifest: Manifest, host: str, root_expr: str) -> dict[str, Any]:
    events: dict[str, list[dict[str, Any]]] = {}
    for hook in manifest.hooks:
        if host not in hook.host:
            continue
        command: dict[str, Any] = {"type": "command", "command": command_for(manifest, hook, root_expr)}
        if hook.timeout_s is not None:
            command["timeout"] = hook.timeout_s
        entry: dict[str, Any] = {"hooks": [command]}
        if hook.matcher is not None:
            entry = {"matcher": hook.matcher, **entry}
        events.setdefault(hook.event, []).append(entry)
    return {"hooks": events}
```

3c. Tạo `src/coding_agent/autodecide.py`:

```python
"""Auto-decide: a question to the user waits `timeout_s` seconds, then the agent decides (FR-001 to FR-010).

The manifest's `auto_decide:` block holds the defaults; `cli auto-decide` writes the user's choice to
`.coding-agent/state/auto-decide.json`, which wins over the manifest and applies from the next question.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from coding_agent import state
from coding_agent.manifest import AutoDecide, ManifestError, auto_decide_from, load

STATE_FILE = Path(".coding-agent") / "state" / "auto-decide.json"


def effective(root: Path, base: AutoDecide | None = None) -> AutoDecide:
    """`base` (the manifest's settings) overridden by the state file. A state file that fails validation is ignored."""
    base = base or AutoDecide()
    try:
        data: Any = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict):
        return base
    merged = {**asdict(base), **{key: value for key, value in data.items() if key in AutoDecide.__dataclass_fields__}}
    try:
        return auto_decide_from(merged, "auto-decide.json")
    except ManifestError:
        return base


def save(root: Path, settings: AutoDecide) -> None:
    state._write_json(root / STATE_FILE, asdict(settings))


def manifest_settings(root: Path) -> AutoDecide:
    """The `auto_decide:` block of `root/integration.yaml`, or the defaults when there is no usable manifest."""
    path = root / "integration.yaml"
    if not path.exists():
        return AutoDecide()
    try:
        return load(path.resolve()).auto_decide
    except (ManifestError, OSError, ImportError, ValueError):
        return AutoDecide()
```

3d. `src/coding_agent/cli.py`:
- Thêm vào docstring đầu file, sau dòng `- \`plan-apply FILE\` ...`:

```text
- `auto-decide [on|off|status] [--timeout N] [--fable|--no-fable] [--fable-timeout N]`: turn auto-decide on or off and set how
  long a question to the user waits (10 to 3600 s) before Fable or the suggested option decides it. Stored in
  `.coding-agent/state/auto-decide.json`, which wins over `auto_decide:` in integration.yaml.
```

- Đổi import dòng 61 thành `from coding_agent import autodecide, cleanup, coordinator, events, orca, orca_cli, roots`, và thêm `from dataclasses import asdict` cạnh `import argparse`; đổi dòng 65 thành `from coding_agent.manifest import ManifestError, auto_decide_from, load`.
- Trong `main`, ngay sau `check.add_argument("--strict", action="store_true")`:

```python
    decide = sub.add_parser("auto-decide", help="turn auto-decide on or off, or change how long a question waits")
    decide.add_argument("action", nargs="?", choices=("on", "off", "status"), default="status")
    decide.add_argument("--timeout", type=int, help="seconds a question waits for the user (10 to 3600)")
    decide.add_argument("--fable", action=argparse.BooleanOptionalAction, default=None, help="let Fable decide when the wait runs out")
    decide.add_argument("--fable-timeout", type=int, help="seconds Fable may take (10 to 600)")
```

- Thêm hàm ngay trên `def _command`:

```python
def _auto_decide(args: argparse.Namespace, root: Path) -> int:
    current = autodecide.effective(root, autodecide.manifest_settings(root))
    changes: dict[str, object] = {}
    if args.action in ("on", "off"):
        changes["enabled"] = args.action == "on"
    if args.timeout is not None:
        changes["timeout_s"] = args.timeout
    if args.fable is not None:
        changes["fable"] = args.fable
    if args.fable_timeout is not None:
        changes["fable_timeout_s"] = args.fable_timeout
    if changes:
        try:
            current = auto_decide_from({**asdict(current), **changes}, "auto-decide")
        except ManifestError as error:
            print(f"auto-decide: {error}", file=sys.stderr)
            return 1
        autodecide.save(root, current)
    print(json.dumps(asdict(current), ensure_ascii=False))
    return 0
```

- Trong `_command`, dòng đầu tiên bên trong `try:` (trước `if args.command == "report":`):

```python
        if args.command == "auto-decide":
            return _auto_decide(args, root)
```

3e. `integration.yaml` và `src/coding_agent/template.yaml` — chèn ngay trên dòng `# Hook. \`mode\` có ba giá trị...`:

```yaml
# Auto-decide (FR-001): agent hỏi user bằng AskUserQuestion thì chờ timeout_s giây; hết giờ thì Fable chọn
# (fable: true, tối đa fable_timeout_s giây), lùi về phương án được gợi ý. User bật/tắt bằng
# `python -m coding_agent.cli auto-decide on|off [--timeout N]`; giá trị đó ghi đè khối này.
# ASSUMPTION: 60 giây là đủ để một user đang ngồi máy kịp trả lời. Chưa đo trên phiên thật.
auto_decide:
  enabled: false
  timeout_s: 60
  fable: true
  fable_timeout_s: 90
```

- [ ] **Step 4: chạy lại — PASS**

Chạy: `python -m pytest tests/test_auto_decide.py -q` → Mong đợi: `8 passed`
Chạy: `python -m pytest -q` → Mong đợi: không có test nào fail (số skipped tuỳ máy có `zm`).

- [ ] **Step 5: commit**

```bash
git add src/coding_agent/manifest.py src/coding_agent/gen.py src/coding_agent/autodecide.py src/coding_agent/cli.py integration.yaml src/coding_agent/template.yaml tests/test_auto_decide.py
git commit -m "feat(auto-decide): cau hinh auto_decide va lenh cli auto-decide"
```

---

### Task 2: Hook `auto-decide` end-to-end (cửa sổ giả lập, hết giờ → phương án gợi ý)

**Thoả:** FR-003, FR-005 (nhánh phương án gợi ý), FR-006, FR-008

**Files:**
- Sửa: `src/coding_agent/autodecide.py` (thêm hàm; giữ nguyên phần Task 1)
- Sửa: `src/coding_agent/hooks/handlers.py` (import dòng 9-19; handler mới trên `REGISTRY`; `REGISTRY` dòng ~398-408)
- Sửa: `integration.yaml`, `src/coding_agent/template.yaml` (thêm hook cuối danh sách `hooks:`)
- Test: `tests/test_auto_decide.py` (thêm), `tests/test_auto_decide_hook.py` (tạo)

**Interfaces:**
- Consumes (Task 1): `AutoDecide`, `autodecide.effective(root, base)`, `autodecide.save(root, settings)`, `Manifest.auto_decide`.
- Produces:
  - `autodecide.FAKE_ENV = "CODING_AGENT_AUTODECIDE_FAKE"` — giá trị `timeout` | `error` | `dismissed` | `choice:<n>` (n bắt đầu từ 1, áp cho mọi câu).
  - `autodecide.NESTED_ENV = "CODING_AGENT_AUTODECIDE_NESTED"` — có giá trị thì hook không làm gì (phiên Fable lồng nhau).
  - `autodecide.MULTI_SEP = ", "`
  - `autodecide.questions_of(tool_input: Any) -> list[dict[str, Any]]`
  - `autodecide.labels_of(question: dict[str, Any]) -> list[str]`
  - `autodecide.suggested(questions: list[dict]) -> dict[str, str]`
  - `autodecide.valid_answer(questions: list[dict], answers: Any) -> bool`
  - `autodecide.is_sensitive(questions: list[dict]) -> bool`
  - `autodecide.Reply(outcome: str, answers: dict[str, str] = {}, detail: str = "")` — `outcome` ∈ `answered|timeout|error`.
  - `autodecide.ask_window(questions: list[dict], timeout_s: int) -> Reply`
  - `autodecide.window_main() -> int` — Task 2 là bản tạm trả 3; Task 3 thay bằng cửa sổ thật.
  - `autodecide.Decision(answers: dict[str, str] | None, by: str, note: str = "", detail: dict = {})` — `by` ∈ `user|fable|suggested|fallback`.
  - `autodecide.decide(tool_input: dict, settings: AutoDecide, context: str = "") -> Decision`
  - `autodecide.hook_output(tool_input: dict, answers: dict[str, str], note: str) -> str`
  - `handlers.auto_decide(ctx: Context) -> HookResult`, đăng ký `REGISTRY["auto-decide"]`.
  - Event: `guard="auto-decide"`, `kind` ∈ `auto-decided` | `answered-by-user` | `auto-decide-fallback`; `detail` có `questions`, `answers`, `by`, `waited_s`, `sensitive`, `reason`, `error` tuỳ nhánh.

- [ ] **Step 1: viết test fail**

Thêm vào cuối `tests/test_auto_decide.py`:

```python
CLEAN = [{"question": "Dùng thư viện nào?", "header": "Lib", "multiSelect": False,
          "options": [{"label": "requests", "description": ""}, {"label": "httpx (Recommended)", "description": ""}]}]
MULTI = [{"question": "Bật gì?", "header": "Bật", "multiSelect": True,
          "options": [{"label": "a"}, {"label": "b (Khuyến nghị)"}, {"label": "c (Recommended)"}]}]


def test_suggested_takes_the_recommended_option_else_the_first():
    assert autodecide.suggested(CLEAN) == {"Dùng thư viện nào?": "httpx (Recommended)"}
    assert autodecide.suggested(MULTI) == {"Bật gì?": "b (Khuyến nghị), c (Recommended)"}
    plain = [{"question": "Q", "options": [{"label": "x"}, {"label": "y"}]}]
    assert autodecide.suggested(plain) == {"Q": "x"}


def test_valid_answer_refuses_a_label_outside_the_list():
    assert autodecide.valid_answer(CLEAN, {"Dùng thư viện nào?": "requests"})
    assert not autodecide.valid_answer(CLEAN, {"Dùng thư viện nào?": "aiohttp"})
    assert not autodecide.valid_answer(CLEAN, {})
    assert autodecide.valid_answer(MULTI, {"Bật gì?": "a, c (Recommended)"})


def test_sensitive_questions_are_flagged_not_blocked():
    assert not autodecide.is_sensitive(CLEAN)
    assert autodecide.is_sensitive([{"question": "Xoá nhánh cũ?", "options": [{"label": "Có"}]}])
    assert autodecide.is_sensitive([{"question": "Tiếp theo?", "options": [{"label": "git push lên main"}]}])
```

Tạo `tests/test_auto_decide_hook.py`:

```python
"""The auto-decide hook as Claude Code runs it: a subprocess per call, JSON on stdin (window and Fable faked)."""

import json
import os
import subprocess
import sys
from pathlib import Path

from coding_agent import autodecide, events
from coding_agent.manifest import AutoDecide

SRC = Path(__file__).resolve().parents[1] / "src"
MANIFEST = """schema: 1
verified: false
python_src: src
hooks:
  - id: auto-decide
    host: [claude_code]
    event: PreToolUse
    matcher: AskUserQuestion
    mode: shadow
    timeout_s: 4215
    assumption: "a question nobody answers in time can be decided for the user"
"""
ASK = {"questions": [{"question": "Xoá nhánh cũ?", "header": "Dọn", "multiSelect": False,
                      "options": [{"label": "Giữ lại", "description": ""}, {"label": "Xoá (Recommended)", "description": ""}]}]}


def setup(repo: Path, *, enabled: bool = True) -> None:
    (repo / "integration.yaml").write_text(MANIFEST, encoding="utf-8")
    autodecide.save(repo, AutoDecide(enabled=enabled, timeout_s=10, fable=False, fable_timeout_s=90))


def call(repo: Path, *, tool: str = "AskUserQuestion", fake: str = "timeout", **env: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(repo),
                   "CODING_AGENT_MODE_AUTO_DECIDE": "shadow", autodecide.FAKE_ENV: fake, **env}
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ASK}
    return subprocess.run([sys.executable, "-m", "coding_agent.hooks", "auto-decide"], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", env=environment, check=False, timeout=60)


def last_event(repo: Path) -> dict:
    return json.loads(events.log_path(repo).read_text(encoding="utf-8").splitlines()[-1])


def test_turned_off_it_leaves_the_question_to_the_host(repo):
    setup(repo, enabled=False)
    done = call(repo)
    assert done.returncode == 0 and done.stdout.strip() == ""


def test_other_tools_are_ignored(repo):
    setup(repo)
    done = call(repo, tool="Bash")
    assert done.returncode == 0 and done.stdout.strip() == ""


def test_a_timeout_picks_the_suggested_option_and_flags_a_sensitive_question(repo):
    setup(repo)
    done = call(repo, fake="timeout")
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["answers"] == {"Xoá nhánh cũ?": "Xoá (Recommended)"}
    assert out["updatedInput"]["questions"] == ASK["questions"]
    assert "sau 10 giây" in out["additionalContext"] and "[sensitive]" in out["additionalContext"]
    event = last_event(repo)
    assert event["kind"] == "auto-decided" and event["applied"] is True
    assert event["detail"]["by"] == "suggested" and event["detail"]["sensitive"] is True


def test_the_users_choice_wins(repo):
    setup(repo)
    done = call(repo, fake="choice:1")
    assert json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]["answers"] == {"Xoá nhánh cũ?": "Giữ lại"}
    assert last_event(repo)["kind"] == "answered-by-user"


def test_a_window_that_cannot_open_falls_back_to_asking(repo):
    setup(repo)
    done = call(repo, fake="error")
    assert done.returncode == 0 and done.stdout.strip() == ""
    assert last_event(repo)["kind"] == "auto-decide-fallback"


def test_a_nested_fable_session_is_left_alone(repo):
    setup(repo)
    done = call(repo, **{autodecide.NESTED_ENV: "1"})
    assert done.stdout.strip() == ""
```

- [ ] **Step 2: chạy cho THẤY nó fail**

Chạy: `python -m pytest tests/test_auto_decide.py tests/test_auto_decide_hook.py -q`
Mong đợi: FAIL — `AttributeError: module 'coding_agent.autodecide' has no attribute 'suggested'` và các test hook fail ở `json.loads(done.stdout)` (stdout rỗng vì `auto-decide` chưa có trong `REGISTRY`).

- [ ] **Step 3: code tối thiểu cho pass**

3a. `src/coding_agent/autodecide.py` — đổi khối import thành:

```python
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
```

Thêm dưới `STATE_FILE = ...`:

```python
FAKE_ENV = "CODING_AGENT_AUTODECIDE_FAKE"
NESTED_ENV = "CODING_AGENT_AUTODECIDE_NESTED"
MULTI_SEP = ", "
RECOMMENDED = re.compile(r"\((?:recommended|khuyến nghị|khuyen nghi)\)", re.IGNORECASE)
# Flags a question about a destructive or outward action. It only marks the decision; it never stops one (FR-008).
SENSITIVE = re.compile(
    r"xoá|xóa|\bxoa\b|delete|remove|\brm\b|ghi đè|overwrite|force|commit|push|merge|deploy|publish|gửi|\bsend\b|drop|reset|discard",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Reply:
    """What the window returned: `answered` (with answers), `timeout`, or `error` (with detail)."""

    outcome: str
    answers: dict[str, str] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True)
class Decision:
    """`answers` None means the host asks the user as usual. `by` is user, fable, suggested or fallback."""

    answers: dict[str, str] | None
    by: str
    note: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
```

Thêm cuối file:

```python
def questions_of(tool_input: Any) -> list[dict[str, Any]]:
    """The AskUserQuestion questions that carry a question text and at least one labelled option."""
    raw = tool_input.get("questions") if isinstance(tool_input, dict) else None
    return [q for q in raw or [] if isinstance(q, dict) and q.get("question") and labels_of(q)]


def labels_of(question: dict[str, Any]) -> list[str]:
    return [str(o["label"]) for o in question.get("options") or [] if isinstance(o, dict) and o.get("label")]


def suggested(questions: list[dict[str, Any]]) -> dict[str, str]:
    """The option the agent suggested for each question: those marked recommended, else the first one."""
    answers: dict[str, str] = {}
    for question in questions:
        labels = labels_of(question)
        picked = [label for label in labels if RECOMMENDED.search(label)]
        if not question.get("multiSelect"):
            picked = picked[:1]
        answers[str(question["question"])] = MULTI_SEP.join(picked or labels[:1])
    return answers


def valid_answer(questions: list[dict[str, Any]], answers: Any) -> bool:
    """True when every question has an answer made only of its own labels."""
    if not isinstance(answers, dict):
        return False
    for question in questions:
        value = answers.get(str(question["question"]))
        if not isinstance(value, str) or not value:
            return False
        chosen = value.split(MULTI_SEP) if question.get("multiSelect") else [value]
        if any(label not in labels_of(question) for label in chosen):
            return False
    return True


def is_sensitive(questions: list[dict[str, Any]]) -> bool:
    for question in questions:
        options = question.get("options") or []
        text = " ".join([str(question.get("question") or "")] + [
            f"{o.get('label', '')} {o.get('description', '')}" for o in options if isinstance(o, dict)])
        if SENSITIVE.search(text):
            return True
    return False


def _fake_reply(fake: str, questions: list[dict[str, Any]]) -> Reply:
    if fake.startswith("choice:"):
        index = max(int(fake.split(":", 1)[1]) - 1, 0)
        answers = {str(q["question"]): labels_of(q)[min(index, len(labels_of(q)) - 1)] for q in questions}
        return Reply("answered", answers)
    if fake in ("timeout", "error"):
        return Reply(fake, detail="fake")
    return Reply("error", detail="dismissed")


def ask_window(questions: list[dict[str, Any]], timeout_s: int) -> Reply:
    """Show the questions with a countdown in a separate process (`python -m coding_agent.autodecide window`)."""
    fake = os.environ.get(FAKE_ENV)
    if fake:
        return _fake_reply(fake, questions)
    request = json.dumps({"questions": questions, "timeout_s": timeout_s}, ensure_ascii=False)
    try:
        done = subprocess.run([sys.executable, "-m", "coding_agent.autodecide", "window"], input=request,
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout_s + 5, check=False)
    except subprocess.TimeoutExpired:
        return Reply("timeout", detail="window did not close in time")
    except OSError as error:
        return Reply("error", detail=str(error)[:300])
    if done.returncode != 0:
        return Reply("error", detail=(done.stderr or f"window exited {done.returncode}").strip()[:300])
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError:
        return Reply("error", detail=f"window printed {done.stdout[:200]!r}")
    if isinstance(data, dict) and data.get("timeout") is True:
        return Reply("timeout")
    if isinstance(data, dict) and data.get("dismissed") is True:
        return Reply("error", detail="dismissed")
    answers = data.get("answers") if isinstance(data, dict) else None
    if valid_answer(questions, answers):
        return Reply("answered", answers)
    return Reply("error", detail=f"window answers not in the options: {str(answers)[:200]}")


def window_main() -> int:
    """The countdown window. Replaced in Task 3; until then it reports that no window exists, so the host asks as usual."""
    print("auto-decide window: not available", file=sys.stderr)
    return 3


def decide(tool_input: dict[str, Any], settings: AutoDecide, context: str = "") -> Decision:
    """Ask in the window; when the wait runs out, decide for the user. A window that cannot open is a fallback."""
    questions = questions_of(tool_input)
    if not questions:
        return Decision(None, "fallback", detail={"error": "no question with options"})
    asked = [str(q["question"]) for q in questions]
    reply = ask_window(questions, settings.timeout_s)
    if reply.outcome == "error":
        return Decision(None, "fallback", detail={"questions": asked, "error": reply.detail})
    if reply.outcome == "answered":
        note = "[auto-decide] Người dùng đã trả lời trong cửa sổ auto-decide."
        return Decision(reply.answers, "user", note, {"questions": asked, "answers": reply.answers, "by": "user"})
    answers, by, reason = suggested(questions), "suggested", ""
    sensitive = is_sensitive(questions)
    chosen = "; ".join(f"{question} → {answer}" for question, answer in answers.items())
    who = "Fable" if by == "fable" else "gợi ý của agent"
    note = (f"[auto-decide] Người dùng không trả lời sau {settings.timeout_s} giây; đã tự chọn: {chosen} "
            f"(người chọn: {who}{'; lý do: ' + reason if reason else ''}){' [sensitive]' if sensitive else ''}. "
            "Hãy báo lại lựa chọn này cho người dùng.")
    detail = {"questions": asked, "answers": answers, "by": by, "waited_s": settings.timeout_s,
              "sensitive": sensitive, "reason": reason}
    return Decision(answers, by, note, detail)


def hook_output(tool_input: dict[str, Any], answers: dict[str, str], note: str) -> str:
    """PreToolUse output that lets AskUserQuestion run with the answers filled in."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": note,
        "updatedInput": {**tool_input, "answers": answers},
        "additionalContext": note,
    }}, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["window"]:
        return window_main()
    print("usage: python -m coding_agent.autodecide window", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

3b. `src/coding_agent/hooks/handlers.py`:
- Thêm `import os` sau `import json`; đổi dòng `from coding_agent import coordinator, events, orca, state, transcript` thành `from coding_agent import autodecide, coordinator, events, orca, state, transcript`.
- Thêm handler ngay trên `REGISTRY`:

```python
def auto_decide(ctx: Context) -> HookResult:
    """With auto-decide on, an AskUserQuestion waits in a countdown window; when nobody answers, the agent decides.

    The user switched this on, so it acts in `shadow` too: the mode only turns the hook off. A window that cannot
    open leaves the question to the host, never a choice the user did not see.
    """
    if ctx.payload.get("tool_name") != "AskUserQuestion" or os.environ.get(autodecide.NESTED_ENV):
        return HookResult()
    settings = autodecide.effective(ctx.root, ctx.manifest.auto_decide if ctx.manifest is not None else None)
    if not settings.enabled:
        return HookResult()
    tool_input = ctx.payload.get("tool_input") if isinstance(ctx.payload.get("tool_input"), dict) else {}
    decision = autodecide.decide(tool_input, settings)
    if decision.answers is None:
        ctx.note(guard="auto-decide", kind="auto-decide-fallback", applied=False, detail=decision.detail)
        return HookResult()
    kind = "answered-by-user" if decision.by == "user" else "auto-decided"
    ctx.note(guard="auto-decide", kind=kind, applied=True, detail=decision.detail)
    return HookResult(stdout=autodecide.hook_output(tool_input, decision.answers, decision.note))
```

- Thêm vào `REGISTRY` (dòng cuối trước `}`): `    "auto-decide": auto_decide,`

3c. `integration.yaml` và `src/coding_agent/template.yaml` — thêm cuối danh sách `hooks:`:

```yaml
  - id: auto-decide
    host: [claude_code]
    event: PreToolUse
    matcher: AskUserQuestion
    mode: shadow
    # Phải lớn hơn timeout_s + fable_timeout_s tối đa (3600 + 600) để Claude Code không giết hook đang chờ.
    timeout_s: 4215
    assumption: "User đã bật auto-decide chấp nhận để agent tự chọn khi không trả lời trong thời gian chờ; phương án Fable chọn hoặc agent gợi ý là đủ tốt để chạy tiếp."
```

- [ ] **Step 4: chạy lại — PASS**

Chạy: `python -m pytest tests/test_auto_decide.py tests/test_auto_decide_hook.py -q` → Mong đợi: `17 passed`
Chạy: `python -m pytest -q` → Mong đợi: không có test nào fail (`test_shipped_manifest` vẫn pass: hook mới ở `shadow`).

- [ ] **Step 5: commit**

```bash
git add src/coding_agent/autodecide.py src/coding_agent/hooks/handlers.py integration.yaml src/coding_agent/template.yaml tests/test_auto_decide.py tests/test_auto_decide_hook.py
git commit -m "feat(auto-decide): hook AskUserQuestion tu chon phuong an goi y khi het gio"
```

---

### Task 3: Cửa sổ đếm ngược tkinter

**Thoả:** FR-004, FR-007

**Files:**
- Sửa: `src/coding_agent/autodecide.py` (thay hàm `window_main` của Task 2; thêm import `time`)
- Test: `tests/test_auto_decide.py` (thêm)

**Interfaces:**
- Consumes (Task 2): `questions_of`, `labels_of`, `suggested`, `valid_answer`, `MULTI_SEP`, `ask_window` (gọi `python -m coding_agent.autodecide window`).
- Produces: `autodecide.window_main() -> int` — đọc stdin `{"questions": [...], "timeout_s": N}`; in một dòng JSON ra stdout rồi exit 0: `{"answers": {...}}` (user bấm Gửi hoặc "Chọn gợi ý ngay"), `{"timeout": true}` (hết giờ), `{"dismissed": true}` (user đóng cửa sổ → host hỏi như thường). Stdin sai → exit 2. Không mở được tkinter (thiếu module, không display) → stderr `auto-decide window: <lỗi>`, exit 3.

- [ ] **Step 1: viết test fail** — thêm vào cuối `tests/test_auto_decide.py`:

```python
import io
import subprocess as _subprocess
import sys as _sys


def test_the_window_reports_a_missing_tkinter_with_exit_3(monkeypatch, capsys):
    monkeypatch.setitem(_sys.modules, "tkinter", None)
    monkeypatch.setattr(_sys, "stdin", io.StringIO(json.dumps({"questions": CLEAN, "timeout_s": 10})))
    assert autodecide.window_main() == 3
    assert "auto-decide window:" in capsys.readouterr().err


def test_the_window_refuses_bad_input_with_exit_2(monkeypatch):
    monkeypatch.setattr(_sys, "stdin", io.StringIO("not json"))
    assert autodecide.window_main() == 2


@pytest.mark.parametrize("stdout, outcome", [
    ('{"answers": {"Dùng thư viện nào?": "requests"}}', "answered"),
    ('{"timeout": true}', "timeout"),
    ('{"dismissed": true}', "error"),
    ('{"answers": {"Dùng thư viện nào?": "aiohttp"}}', "error"),
])
def test_ask_window_reads_what_the_window_printed(monkeypatch, stdout, outcome):
    monkeypatch.delenv(autodecide.FAKE_ENV, raising=False)
    monkeypatch.setattr(autodecide.subprocess, "run",
                        lambda *a, **k: _subprocess.CompletedProcess(a[0], 0, stdout=stdout, stderr=""))
    assert autodecide.ask_window(CLEAN, 10).outcome == outcome
```

- [ ] **Step 2: chạy cho THẤY nó fail**

Chạy: `python -m pytest tests/test_auto_decide.py -q -k window`
Mong đợi: FAIL — `test_the_window_refuses_bad_input_with_exit_2` trả 3 thay vì 2 (bản tạm của Task 2 luôn trả 3).

- [ ] **Step 3: code tối thiểu cho pass** — thêm `import time` vào khối import của `autodecide.py`, rồi thay toàn bộ hàm `window_main`:

```python
def window_main() -> int:
    """The countdown window: one line of JSON on stdout, `answers`, `timeout` or `dismissed`. Exit 3 when no window can open."""
    try:
        request = json.loads(sys.stdin.read())
        questions = questions_of(request)
        timeout_s = int(request.get("timeout_s", 60))
    except (ValueError, TypeError, AttributeError):
        return 2
    if not questions:
        return 2
    try:
        import tkinter as tk

        window = tk.Tk()
    except Exception as error:  # ImportError, or TclError when there is no display
        print(f"auto-decide window: {error}", file=sys.stderr)
        return 3

    result: dict[str, Any] = {"timeout": True}
    window.title("coding-agent: auto-decide")
    window.attributes("-topmost", True)
    choices: list[tuple[dict[str, Any], Any]] = []
    for question in questions:
        frame = tk.LabelFrame(window, text=str(question.get("header") or ""), padx=8, pady=6)
        frame.pack(fill="x", padx=10, pady=6)
        tk.Label(frame, text=str(question["question"]), wraplength=480, justify="left", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        options = [o for o in question.get("options") or [] if isinstance(o, dict) and o.get("label")]
        if question.get("multiSelect"):
            checks = []
            for option in options:
                var = tk.BooleanVar(value=False)
                text = str(option["label"]) + (f" — {option['description']}" if option.get("description") else "")
                tk.Checkbutton(frame, text=text, variable=var, wraplength=480, justify="left", anchor="w").pack(anchor="w")
                checks.append((str(option["label"]), var))
            choices.append((question, checks))
        else:
            var = tk.StringVar(value="")
            for option in options:
                text = str(option["label"]) + (f" — {option['description']}" if option.get("description") else "")
                tk.Radiobutton(frame, text=text, value=str(option["label"]), variable=var, wraplength=480, justify="left", anchor="w").pack(anchor="w")
            choices.append((question, var))

    status = tk.Label(window, text="", fg="#b45309")
    status.pack(padx=10, pady=(4, 0))

    def collect() -> dict[str, str]:
        answers: dict[str, str] = {}
        for question, var in choices:
            if question.get("multiSelect"):
                answers[str(question["question"])] = MULTI_SEP.join(label for label, check in var if check.get())
            else:
                answers[str(question["question"])] = var.get()
        return answers

    def submit() -> None:
        answers = collect()
        if not valid_answer(questions, answers):
            status.config(text="Chọn ít nhất một phương án cho mỗi câu hỏi.")
            return
        result.clear()
        result["answers"] = answers
        window.destroy()

    def take_suggested() -> None:
        result.clear()
        result["answers"] = suggested(questions)
        window.destroy()

    def dismiss() -> None:
        result.clear()
        result["dismissed"] = True
        window.destroy()

    buttons = tk.Frame(window)
    buttons.pack(fill="x", padx=10, pady=8)
    tk.Button(buttons, text="Gửi", command=submit, width=12).pack(side="right")
    tk.Button(buttons, text="Chọn gợi ý ngay", command=take_suggested).pack(side="right", padx=6)
    countdown = tk.Label(buttons, text="")
    countdown.pack(side="left")
    window.protocol("WM_DELETE_WINDOW", dismiss)

    deadline = time.monotonic() + timeout_s

    def tick() -> None:
        left = int(deadline - time.monotonic() + 0.999)
        if left <= 0:
            window.destroy()
            return
        countdown.config(text=f"Tự chọn sau {left} giây")
        window.after(250, tick)

    tick()
    window.mainloop()
    print(json.dumps(result, ensure_ascii=False))
    return 0
```

- [ ] **Step 4: chạy lại — PASS**

Chạy: `python -m pytest tests/test_auto_decide.py -q` → Mong đợi: `17 passed` (11 cũ + 6 mới).
Chạy: `python -m pytest -q` → Mong đợi: không có test nào fail.
Thử tay (máy có màn hình; cửa sổ sẽ mở):
`echo '{"questions":[{"question":"Thử?","options":[{"label":"A"},{"label":"B (Recommended)"}]}],"timeout_s":10}' | PYTHONPATH=src python -m coding_agent.autodecide window`
Mong đợi: cửa sổ đếm ngược 10 giây. Không bấm gì → in `{"timeout": true}`. Bấm "A" rồi "Gửi" → in `{"answers": {"Thử?": "A"}}`. Đóng cửa sổ → in `{"dismissed": true}`.

- [ ] **Step 5: commit**

```bash
git add src/coding_agent/autodecide.py tests/test_auto_decide.py
git commit -m "feat(auto-decide): cua so dem nguoc tkinter"
```

---

### Task 4: Fable chọn khi hết giờ

**Thoả:** FR-005, FR-010

**Files:**
- Sửa: `src/coding_agent/autodecide.py` (thêm hàm + import `shutil`, `tempfile`; sửa 3 dòng trong `decide`)
- Sửa: `src/coding_agent/hooks/handlers.py` (thêm `_turn_context`; truyền context vào `autodecide.decide` trong `auto_decide`)
- Test: `tests/test_auto_decide.py`, `tests/test_auto_decide_hook.py` (thêm)

**Interfaces:**
- Consumes (Task 2): `valid_answer`, `suggested`, `decide`, `NESTED_ENV`, `MULTI_SEP`, `AutoDecide.fable`, `AutoDecide.fable_timeout_s`; `transcript.read_turn(path: Path, change_tools: tuple[str, ...]) -> Turn` (`Turn.prompt`, `Turn.response`); `handlers._path(value) -> Path`.
- Produces:
  - `autodecide.FABLE_MODEL = "claude-fable-5-1"`, `autodecide.FABLE_FAKE_ENV = "CODING_AGENT_AUTODECIDE_FABLE_FAKE"` (giá trị = stdout giả của `claude`; `error` = lệnh lỗi), `autodecide.CONTEXT_CHARS = 4000`.
  - `autodecide.fable_prompt(questions: list[dict], context: str) -> str`
  - `autodecide.parse_fable(stdout: str) -> tuple[dict[str, str], str] | None`
  - `autodecide.ask_fable(questions: list[dict], context: str, timeout_s: int) -> tuple[dict[str, str], str] | None`
  - `handlers._turn_context(ctx: Context) -> str`

- [ ] **Step 1: viết test fail**

Thêm vào cuối `tests/test_auto_decide.py`:

```python
def test_parse_fable_reads_the_claude_json_envelope():
    envelope = json.dumps({"type": "result", "result": 'Đây: {"answers": {"Dùng thư viện nào?": "requests"}, "reason": "đã có sẵn"}'})
    assert autodecide.parse_fable(envelope) == ({"Dùng thư viện nào?": "requests"}, "đã có sẵn")
    assert autodecide.parse_fable("không có json") is None


def test_ask_fable_refuses_a_label_outside_the_list(monkeypatch):
    monkeypatch.setenv(autodecide.FABLE_FAKE_ENV, json.dumps({"result": '{"answers": {"Dùng thư viện nào?": "aiohttp"}, "reason": "x"}'}))
    assert autodecide.ask_fable(CLEAN, "", 30) is None
    monkeypatch.setenv(autodecide.FABLE_FAKE_ENV, "error")
    assert autodecide.ask_fable(CLEAN, "", 30) is None


def test_the_prompt_lists_the_options_and_the_context():
    prompt = autodecide.fable_prompt(CLEAN, "user muốn giảm dependency")
    assert "httpx (Recommended)" in prompt and "user muốn giảm dependency" in prompt and '"answers"' in prompt
```

Thêm vào cuối `tests/test_auto_decide_hook.py`:

```python
def setup_fable(repo: Path) -> None:
    (repo / "integration.yaml").write_text(MANIFEST, encoding="utf-8")
    autodecide.save(repo, AutoDecide(enabled=True, timeout_s=10, fable=True, fable_timeout_s=30))


def test_fable_decides_when_the_wait_runs_out(repo):
    setup_fable(repo)
    fable = json.dumps({"result": '{"answers": {"Xoá nhánh cũ?": "Giữ lại"}, "reason": "nhánh chưa gộp"}'})
    done = call(repo, fake="timeout", **{autodecide.FABLE_FAKE_ENV: fable})
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["updatedInput"]["answers"] == {"Xoá nhánh cũ?": "Giữ lại"}
    assert "người chọn: Fable" in out["additionalContext"] and "nhánh chưa gộp" in out["additionalContext"]
    assert last_event(repo)["detail"]["by"] == "fable"


def test_a_failing_fable_falls_back_to_the_suggested_option(repo):
    setup_fable(repo)
    done = call(repo, fake="timeout", **{autodecide.FABLE_FAKE_ENV: "error"})
    assert json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]["answers"] == {"Xoá nhánh cũ?": "Xoá (Recommended)"}
    assert last_event(repo)["detail"]["by"] == "suggested"
```

- [ ] **Step 2: chạy cho THẤY nó fail**

Chạy: `python -m pytest tests/test_auto_decide.py tests/test_auto_decide_hook.py -q -k "fable or prompt"`
Mong đợi: FAIL — `AttributeError: module 'coding_agent.autodecide' has no attribute 'parse_fable'` (và `FABLE_FAKE_ENV` ở test hook).

- [ ] **Step 3: code tối thiểu cho pass**

3a. `src/coding_agent/autodecide.py` — thêm `import shutil` và `import tempfile` vào khối import; thêm dưới `SENSITIVE = ...`:

```python
FABLE_MODEL = "claude-fable-5-1"
FABLE_FAKE_ENV = "CODING_AGENT_AUTODECIDE_FABLE_FAKE"
CONTEXT_CHARS = 4000
```

Thêm ngay trên `def decide`:

```python
def fable_prompt(questions: list[dict[str, Any]], context: str) -> str:
    listed = [{"question": q["question"], "multiSelect": bool(q.get("multiSelect")),
               "options": [{"label": o.get("label"), "description": o.get("description", "")}
                           for o in q.get("options") or [] if isinstance(o, dict) and o.get("label")]}
              for q in questions]
    return "\n".join([
        "Bạn quyết định thay một người dùng đang vắng mặt cho một coding agent: họ không trả lời các câu hỏi dưới đây trong thời gian chờ.",
        f"Cho MỖI câu hỏi, chọn đúng một nhãn trong danh sách của nó (câu multiSelect: một hoặc nhiều nhãn, nối bằng '{MULTI_SEP}'). Không bịa nhãn mới.",
        'Chỉ trả về một object JSON: {"answers": {"<câu hỏi>": "<nhãn>"}, "reason": "<một câu lý do>"}.',
        "",
        "Ngữ cảnh của phiên:",
        context[-CONTEXT_CHARS:] or "(không có)",
        "",
        "Câu hỏi:",
        json.dumps(listed, ensure_ascii=False, indent=1),
    ])


def parse_fable(stdout: str) -> tuple[dict[str, str], str] | None:
    """The answers and reason in `claude -p --output-format json` output (or in plain text), or None."""
    try:
        envelope = json.loads(stdout)
        text = envelope.get("result") if isinstance(envelope, dict) else None
    except json.JSONDecodeError:
        text = stdout
    if not isinstance(text, str):
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        return None
    return {str(k): str(v) for k, v in data["answers"].items()}, str(data.get("reason") or "")[:300]


def ask_fable(questions: list[dict[str, Any]], context: str, timeout_s: int) -> tuple[dict[str, str], str] | None:
    """Fable's answers and reason, or None when `claude` is missing, fails, times out or answers outside the options."""
    fake = os.environ.get(FABLE_FAKE_ENV)
    if fake is not None:
        if fake == "error":
            return None
        stdout = fake
    else:
        claude = shutil.which("claude")
        if claude is None:
            return None
        try:
            # Run outside the project so its hooks do not load; NESTED_ENV keeps auto-decide out of Fable's own session.
            done = subprocess.run([claude, "-p", "--model", FABLE_MODEL, "--output-format", "json"],
                                  input=fable_prompt(questions, context), capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout_s, cwd=tempfile.gettempdir(), env={**os.environ, NESTED_ENV: "1"}, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0:
            return None
        stdout = done.stdout
    parsed = parse_fable(stdout)
    if parsed is None or not valid_answer(questions, parsed[0]):
        return None
    return parsed
```

Trong `decide`, ngay dưới dòng `answers, by, reason = suggested(questions), "suggested", ""` thêm:

```python
    if settings.fable:
        picked = ask_fable(questions, context, settings.fable_timeout_s)
        if picked is not None:
            answers, reason = picked
            by = "fable"
```

3b. `src/coding_agent/hooks/handlers.py` — thêm ngay trên `def auto_decide`:

```python
def _turn_context(ctx: Context) -> str:
    """The user's latest prompt and the agent's latest text in this turn, for Fable to decide with."""
    path = ctx.payload.get("transcript_path")
    if not path or ctx.manifest is None:
        return ""
    turn = transcript.read_turn(_path(path), ctx.manifest.memory.change_tools)
    parts = [f"Prompt gần nhất của người dùng: {turn.prompt}" if turn.prompt else "",
             f"Trả lời gần nhất của agent: {turn.response}" if turn.response else ""]
    return "\n".join(part for part in parts if part)[-autodecide.CONTEXT_CHARS:]
```

và trong `auto_decide` đổi `decision = autodecide.decide(tool_input, settings)` thành:

```python
    decision = autodecide.decide(tool_input, settings, _turn_context(ctx))
```

- [ ] **Step 4: chạy lại — PASS**

Chạy: `python -m pytest tests/test_auto_decide.py tests/test_auto_decide_hook.py -q` → Mong đợi: không có test nào fail.
Chạy: `python -m pytest -q` → Mong đợi: không có test nào fail.
Thử tay Fable (cần `claude` đã đăng nhập): `python -c "from coding_agent import autodecide as a; print(a.ask_fable([{'question':'Q','options':[{'label':'A'},{'label':'B'}]}], '', 90))"` với `PYTHONPATH=src` → Mong đợi: một tuple `({'Q': 'A' hoặc 'B'}, '<lý do>')`; `None` nghĩa là `claude` thiếu/lỗi — ghi lại stderr của lệnh `claude -p --model claude-fable-5-1 --output-format json` chạy tay để báo cáo.

- [ ] **Step 5: commit**

```bash
git add src/coding_agent/autodecide.py src/coding_agent/hooks/handlers.py tests/test_auto_decide.py tests/test_auto_decide_hook.py
git commit -m "feat(auto-decide): Fable chon khi het gio, lui ve phuong an goi y"
```

---

### Task 5: Hợp đồng coordinator khi auto-decide bật + README

**Thoả:** FR-009

**Files:**
- Sửa: `src/coding_agent/coordinator.py:239-262` (`contract`)
- Sửa: `src/coding_agent/hooks/handlers.py` (`coordinator_context`, dòng gọi `coordinator.contract(ctx.manifest.python_src)`)
- Sửa: `README.md` (mục mới "Auto-decide" ngay trước "## Cài vào một dự án"; thêm dòng `src/coding_agent/autodecide.py` vào bảng "Cấu trúc")
- Test: `tests/test_auto_decide.py` (thêm)

**Interfaces:**
- Consumes (Task 1): `autodecide.effective(root, base) -> AutoDecide`, `Manifest.auto_decide`.
- Produces: `coordinator.AUTO_DECIDE_LINE: str`; `coordinator.contract(python_src: str, auto_decide: bool = False) -> str` — khi `auto_decide` là `False` trả đúng chuỗi như trước (test hợp đồng hiện có không đổi).

- [ ] **Step 1: viết test fail** — thêm vào cuối `tests/test_auto_decide.py`:

```python
from coding_agent import coordinator


def test_the_contract_asks_through_ask_user_question_only_when_auto_decide_is_on():
    assert coordinator.contract("src") == coordinator.contract("src", auto_decide=False)
    assert "AskUserQuestion" not in coordinator.contract("src")
    on = coordinator.contract("src", auto_decide=True)
    assert on.endswith(coordinator.AUTO_DECIDE_LINE)
    assert "AskUserQuestion" in on and "(Recommended)" in on
```

- [ ] **Step 2: chạy cho THẤY nó fail**

Chạy: `python -m pytest tests/test_auto_decide.py -q -k contract`
Mong đợi: FAIL — `TypeError: contract() got an unexpected keyword argument 'auto_decide'`

- [ ] **Step 3: code tối thiểu cho pass**

3a. `src/coding_agent/coordinator.py` — thêm hằng ngay trên `def contract`:

```python
AUTO_DECIDE_LINE = (
    "- Auto-decide đang bật: khi cần người dùng quyết (kể cả câu hỏi worker leo thang), hỏi bằng tool AskUserQuestion, "
    "không hỏi bằng văn bản rồi kết thúc lượt. Đặt phương án bạn gợi ý lên đầu, nhãn kết thúc bằng \"(Recommended)\". "
    "Người dùng không trả lời trong thời gian chờ thì hệ thống tự chọn và báo lại; hãy nói lại lựa chọn đó cho người dùng "
    "rồi làm tiếp (với câu hỏi của worker: trả lời worker theo lựa chọn đó)."
)
```

Đổi chữ ký và phần đầu/cuối của `contract`:

```python
def contract(python_src: str, auto_decide: bool = False) -> str:
    """The coordinator contract injected at session start and after a compaction."""
    lines = [
```

(giữ nguyên mọi dòng trong danh sách), và thay `    ])` đóng danh sách bằng:

```python
    ]
    if auto_decide:
        lines.append(AUTO_DECIDE_LINE)
    return "\n".join(lines)
```

3b. `src/coding_agent/hooks/handlers.py` — trong `coordinator_context`, đổi:

```python
        text = coordinator.contract(ctx.manifest.python_src) + "\n" + text
```

thành:

```python
        auto = autodecide.effective(ctx.root, ctx.manifest.auto_decide).enabled
        text = coordinator.contract(ctx.manifest.python_src, auto_decide=auto) + "\n" + text
```

3c. `README.md` — thêm vào bảng "Cấu trúc" sau dòng `src/coding_agent/brief.py`:

```markdown
| `src/coding_agent/autodecide.py` | Auto-decide: câu hỏi `AskUserQuestion` chờ N giây trong cửa sổ đếm ngược, hết giờ thì Fable hoặc phương án gợi ý chọn |
```

và thêm mục ngay trước `## Cài vào một dự án`:

````markdown
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
````

- [ ] **Step 4: chạy lại — PASS**

Chạy: `python -m pytest tests/test_auto_decide.py tests/test_coordinator.py tests/test_board_roots.py -q` → Mong đợi: không có test nào fail.
Chạy: `python -m pytest -q` → Mong đợi: không có test nào fail.

- [ ] **Step 5: commit**

```bash
git add src/coding_agent/coordinator.py src/coding_agent/hooks/handlers.py README.md tests/test_auto_decide.py
git commit -m "feat(auto-decide): hop dong coordinator hoi qua AskUserQuestion khi bat, README"
```

---

## Kiểm chứng sau cùng (người chạy, sau khi gộp cả 5 task)
1. `python -m pytest -q` xanh.
2. Trong một dự án đã cài (`bash install.sh <dự án> --clean` hoặc bootstrap với `--add-new-hooks`): `python -m coding_agent.gate --manifest integration.yaml` thoát 0 và `.claude/settings.json` có entry `AskUserQuestion` với `"timeout": 4215`.
3. Thử thật trên Claude Code (SC-001, SC-002): bật `auto-decide on --timeout 15`, nhờ agent hỏi một câu có 2 lựa chọn; (a) không chạm → sau ~15 s + thời gian Fable, agent báo đã tự chọn; (b) lần nữa, bấm lựa chọn trong cửa sổ → agent dùng lựa chọn đó. Nếu Claude Code bỏ qua `updatedInput.answers` và vẫn hiện hộp hỏi: ghi lại phiên bản Claude Code và mở issue — đường lùi là `permissionDecision: "deny"` với lý do chứa lựa chọn (SPEC, Assumptions).
4. Chỉ đổi `verified` hay mode sau khi (3) đạt và người duyệt đồng ý.

## Self-review
1. **Phủ SPEC:** FR-001 → T1 · FR-002 → T1 · FR-003 → T2 · FR-004 → T3 · FR-005 → T2 (nhánh gợi ý) + T4 (Fable) · FR-006 → T2 (ghi chú + event), T4 (người chọn + lý do) · FR-007 → T2 (fallback khi cửa sổ lỗi) + T3 (exit 3, dismissed) · FR-008 → T2 (`is_sensitive`, cờ `sensitive`) · FR-009 → T5 · FR-010 → T1 (cờ `fable`, `--no-fable`) + T4 (áp dụng). Không FR nào bị bỏ.
2. **Placeholder:** không có TBD/TODO/"tương tự Task N". `window_main` bản Task 2 là bản tạm có hành vi xác định (exit 3 → host hỏi như thường) và được Task 3 thay toàn bộ.
3. **Nhất quán tên:** `AutoDecide`, `auto_decide_from`, `effective`, `save`, `manifest_settings`, `questions_of`, `labels_of`, `suggested`, `valid_answer`, `is_sensitive`, `ask_window`, `window_main`, `ask_fable`, `parse_fable`, `fable_prompt`, `decide`, `hook_output`, `Reply`, `Decision`, `FAKE_ENV`, `FABLE_FAKE_ENV`, `NESTED_ENV`, `MULTI_SEP`, `CONTEXT_CHARS`, `AUTO_DECIDE_LINE` — mỗi tên định nghĩa đúng một lần và dùng cùng chữ ở các task sau. Event kind: `auto-decided`, `answered-by-user`, `auto-decide-fallback` khớp SPEC.
4. **Lệch so với SPEC (ghi rõ):** SPEC T1–T8 được gộp thành 5 lát cắt dọc (T3 logic thuần nằm trong Task 2; T7 test nằm trong từng task). Đóng cửa sổ = trả câu hỏi về terminal (`dismissed`) — chi tiết SPEC không nói, chọn theo FR-007 "không tự chọn khi user không thấy câu hỏi" vì đóng cửa sổ là user chủ động muốn trả lời ở chỗ khác.
