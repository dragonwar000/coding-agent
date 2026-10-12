"""`python -m coding_agent.cli wiki-sync` — neo đồng bộ CODE→WIKI, tất định, 0 token (distill từ openwiki, GH langchain-ai).

Bịt lỗ "code đổi mà wiki không hay": stale.json của wiki_ledger chỉ lan truyền
stale WIKI→WIKI (trang đổi → trang link tới nó); nếu code đổi ngoài phiên có kỷ
luật wiki (人 sửa tay, tool khác, session quên) thì không gì phát hiện. Script này
giữ một NEO (<wiki>/.last-sync.json: gitHead + content-hash của wiki) và:

  --check        cổng no-op tất định: code không đổi kể từ neo → exit 0, KHÔNG cần
                 gọi LLM (0 token). Có đổi → map file-code-đổi → trang wiki nhắc tới
                 file đó, ghi vào stale.json (action="code-drift") rồi exit 3.
  --mark-synced  chốt neo sau khi wiki đã được rà/cập nhật: chỉ ghi khi (gitHead,
                 content-hash) thực sự đổi — cron chạy lại không tự churn metadata.
                 Đồng thời xoá các cờ code-drift (đã được rà theo định nghĩa).
  --json         output máy đọc cho CI.

Exit: 0 = wiki current / đã chốt neo · 2 = chưa có neo hoặc neo mất hiệu lực
      (sau rebase/squash — chạy --mark-synced để neo lại) · 3 = có drift cần rà.
Khác hook (fail-open), đây là công cụ CLI/CI: lỗi bất ngờ phải nổi (exit ≠ 0).

Chuyển từ harness/scripts/wiki-sync.py của setup; định dạng .last-sync.json,
.last-sync-tree.json và stale.json giữ nguyên để neo cũ vẫn dùng được.
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
import time

try:
    import fcntl  # POSIX only — cùng quy ước wiki_ledger
except ImportError:
    fcntl = None

# Cùng danh sách thư mục nội dung với wiki_ledger.py — đổi một nơi phải đổi nơi kia
# (harness-lint bắt hằng-số-lệch giữa script).
CONTENT_DIRS = ("concepts/", "entities/", "sources/", "draft/", "architecture/", "tours/")
ANCHOR_NAME = ".last-sync.json"
MAX_CHANGED_LIST = 200  # cap hiển thị — vượt thì nói rõ, không cắt im lặng

# Trang chỉ-ghi-lịch-sử: bản ghi ĐÚNG-TẠI-THỜI-ĐIỂM, /lint có luật không sửa chúng
# (progression, không phải drift). Cờ code-drift ở đây chỉ sinh việc không ai được
# làm → không quét luôn.
ARCHIVE_PREFIXES = ("sources/draft/archive/", "sources/handover/")

# Basename chỉ được làm needle khi nó ĐỊNH DANH được. Trần đo trên chính wiki đang
# quét: một tên khớp quá nhiều trang thì nó không chỉ ra trang nào đang trích file đó.
BASENAME_PAGE_CAP = 8


def run_git(root: pathlib.Path, *args: str) -> str:
    """Trả stdout RAW (không strip) — `git status --short` mã hoá trạng thái bằng
    khoảng trắng ĐẦU DÒNG; strip toàn cục sẽ cắt lệch path của dòng đầu."""
    out = subprocess.run(["git", "--no-pager", *args], cwd=root,
                         capture_output=True, text=True)
    return out.stdout


def detect_wiki_dir(root: pathlib.Path, arg) -> pathlib.Path:
    if arg:
        return (root / arg).resolve() if not pathlib.Path(arg).is_absolute() else pathlib.Path(arg)
    for cand in (root / "llmwiki" / "wiki", root / "wiki"):
        if cand.is_dir():
            return cand
    sys.exit("wiki-sync: không tìm thấy thư mục wiki (llmwiki/wiki hay wiki/) — chỉ định --wiki-dir")


def read_anchor(wiki_dir: pathlib.Path):
    p = wiki_dir / ANCHOR_NAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data if (data.get("gitHead") or data.get("mode") == "tree") else None
    except (OSError, ValueError):
        return None


def content_hash(wiki_dir: pathlib.Path) -> str:
    """sha256 nội dung wiki (chỉ CONTENT_DIRS, path-sorted) — metadata vận hành
    (neo, stale.json, ledger, log) không tham gia hash nên không tự làm 'đổi'."""
    h = hashlib.sha256()
    for d in CONTENT_DIRS:
        base = wiki_dir / d.rstrip("/")
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md"), key=lambda x: x.as_posix()):
            rel = p.relative_to(wiki_dir).as_posix()
            try:
                body = p.read_bytes()
            except OSError:
                continue  # file biến mất giữa scan — bỏ qua, lần sau hash lại
            h.update(f"file:{rel}\0".encode())
            h.update(body)
            h.update(b"\0")
    return h.hexdigest()


def resolve_roots(arg_root: str, wiki_arg):
    """(project_root, git_root|None). Neo theo THƯ MỤC DỰ ÁN (nơi có llmwiki/), không
    theo git toplevel: dự án có thể là thư mục con của repo cha (hoặc thư mục cha
    gitignore/untracked chứa nhiều repo con). Không có wiki dưới --root thì đi ngược lên
    tới git toplevel tìm thư mục có llmwiki/wiki — giữ hành vi cũ khi chạy từ thư mục
    con của một repo thường."""
    start = pathlib.Path(arg_root).resolve()
    top = run_git(start, "rev-parse", "--show-toplevel").strip()
    git_root = pathlib.Path(top).resolve() if top else None
    if wiki_arg:
        return start, git_root
    cur = start
    while True:
        if cur.name == "llmwiki" and (cur / "wiki").is_dir():
            # Chạy từ bên trong llmwiki/ (có thể là repo git riêng) → dự án là thư mục cha.
            proj = cur.parent
            ptop = run_git(proj, "rev-parse", "--show-toplevel").strip()
            return proj, (pathlib.Path(ptop).resolve() if ptop else None)
        if (cur / "llmwiki" / "wiki").is_dir() or (cur / "wiki").is_dir():
            return cur, git_root
        if git_root is None or cur == git_root or cur.parent == cur:
            break
        cur = cur.parent
    return (git_root or start), git_root


def detect_mode(root: pathlib.Path, git_root) -> tuple[str, str]:
    """('git'|'tree', subpath). git = dự án nằm trong cây git được track (gốc repo,
    hoặc thư mục con có trong HEAD); tree = không có git / thư mục chưa track hay bị
    ignore (vd thư mục meta chứa nhiều repo con) → so dấu vân tay cây file."""
    if git_root is None:
        return "tree", ""
    sub = root.relative_to(git_root).as_posix() if root != git_root else ""
    if not sub:
        return "git", ""
    ok = subprocess.run(["git", "rev-parse", "--verify", "-q", f"HEAD:{sub}"],
                        cwd=git_root, capture_output=True)
    return ("git" if ok.returncode == 0 else "tree"), sub


# Thư mục build/vendor/cache không phải "nguồn" — chỉ dùng ở chế độ tree.
TREE_SKIP_DIRS = {".git", "node_modules", "target", "out", ".venv", "venv",
                  "__pycache__", ".next", ".nuxt", ".gradle", ".idea", ".vscode",
                  ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".cache",
                  ".coding-agent", ".turbo", "coverage"}
TREE_SKIP_DIR_PREFIXES = ("dist", "build")  # dist/, dist-electron/, build/, build-out/...
TREE_SIDECAR = ".last-sync-tree.json"


def _skip_dir(name: str) -> bool:
    return name in TREE_SKIP_DIRS or name.startswith(TREE_SKIP_DIR_PREFIXES)


def _runtime_file(rel: str) -> bool:
    """File do runtime/tool ghi mỗi phiên (log audit, *.jsonl trong .claude/) — đổi liên
    tục nên nếu tính vào dấu vân tay thì cây file drift mãi mãi."""
    parts = rel.split("/")
    dirs = parts[:-1]
    if any(_skip_dir(d) for d in dirs):
        return True
    for i, d in enumerate(dirs):
        if d == ".claude" and (dirs[i + 1:i + 2] == ["audit"] or rel.endswith(".jsonl")):
            return True
    return False


def _excluded_rels(root: pathlib.Path, wiki_dir: pathlib.Path) -> tuple[str, ...]:
    wiki_rel = wiki_dir.relative_to(root).as_posix()
    html_rel = wiki_rel.rsplit("/", 1)[0] + "/html" if "/" in wiki_rel else "html"
    # harness/metrics: chính các tool (kể cả cổng này — sync-log.jsonl) ghi vào đó.
    return (wiki_rel + "/", html_rel + "/", "harness/metrics/")


def _check_ignore(base: list[str], paths: list[str], no_index: bool = False):
    """[(source, line, path)] các path bị .gitignore loại. Bỏ khớp bởi pattern rỗng
    (dòng CRLF trống — git đôi khi báo khớp chính thư mục) và pattern phủ định "!"
    (-v báo cả chúng dù path KHÔNG bị ignore)."""
    cmd = [*base, "check-ignore", "--stdin", "-z", "-v"] + (["--no-index"] if no_index else [])
    out = subprocess.run(cmd, input="\0".join(paths) + "\0", capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    f = out.stdout.split("\0")
    res = []
    for i in range(0, len(f) - 3, 4):
        src, line, pat, path = f[i:i + 4]
        if pat.strip() and not pat.startswith("!"):
            res.append((src, line, path))
    return res


def _git_ignored(root: pathlib.Path, rels: list[str]) -> set[str]:
    """Tập rel (tương đối root) bị .gitignore loại — của repo cha (kể cả .gitignore lồng
    trong dự án và trong các repo con) qua `git check-ignore`. Nếu chính thư mục dự án
    bị repo cha ignore (rule thư mục che mọi rule cụ thể hơn bên dưới) hoặc không có repo
    cha: dựng work-tree tạm = root để đọc riêng các .gitignore BÊN TRONG dự án.
    Fail-open: không có git / lỗi → không lọc gì thêm."""
    if not rels:
        return set()
    import tempfile
    ignored: set[str] = set()
    try:
        top = run_git(root, "rev-parse", "--show-toplevel").strip()
        own = True
        if top:
            git_root = pathlib.Path(top).resolve()
            sub = root.relative_to(git_root).as_posix() if root != git_root else ""
            pre = sub + "/" if sub else ""
            base = ["git", "-C", str(git_root)]
            root_rules = ({(s, n) for s, n, _ in _check_ignore(base, [sub, pre])}
                          if sub else set())
            for s, n, path in _check_ignore(base, [pre + r for r in rels]):
                if (s, n) not in root_rules and path.startswith(pre):
                    ignored.add(path[len(pre):])
            own = bool(root_rules)
        if own:
            with tempfile.TemporaryDirectory() as g:
                subprocess.run(["git", "init", "-q", "--bare", g], capture_output=True,
                               check=True)
                base = ["git", f"--git-dir={g}", f"--work-tree={root}"]
                ignored.update(p for _, _, p in _check_ignore(base, rels, no_index=True))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return ignored


def _drop_excluded(root: pathlib.Path, wiki_dir: pathlib.Path, rels) -> set[str]:
    """Lọc rels còn lại là "nguồn": bỏ wiki/html/metrics, file runtime, và file .gitignore."""
    excl = _excluded_rels(root, wiki_dir)
    keep = [r for r in rels if not r.startswith(excl) and not _runtime_file(r)]
    return set(keep) - _git_ignored(root, keep)


def tree_manifest(root: pathlib.Path, wiki_dir: pathlib.Path) -> dict[str, list[int]]:
    """{relpath: [size, mtime_ns]} cho mọi file "nguồn" dưới root — trừ wiki/html,
    build/vendor/cache, file runtime (.claude/audit, .claude/*.jsonl) và file bị
    .gitignore loại. Chỉ stat (không đọc nội dung) — ~22k file < 1s."""
    import os
    excl = _excluded_rels(root, wiki_dir)
    found: dict[str, list[int]] = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not _skip_dir(d)]
        rel_dir = pathlib.Path(dp).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        if rel_dir and any(rel_dir.startswith(e) for e in excl):
            dns[:] = []
            continue
        for fn in fns:
            rel = rel_dir + fn
            if any(rel.startswith(e) for e in excl):
                continue
            try:
                st = os.stat(os.path.join(dp, fn))
            except OSError:
                continue
            found[rel] = [st.st_size, st.st_mtime_ns]
    keep = _drop_excluded(root, wiki_dir, found)
    return {k: v for k, v in found.items() if k in keep}


def manifest_hash(man: dict) -> str:
    return hashlib.sha256(json.dumps(man, sort_keys=True).encode()).hexdigest()


def read_sidecar(wiki_dir: pathlib.Path):
    try:
        data = json.loads((wiki_dir / TREE_SIDECAR).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def changed_since(root: pathlib.Path, wiki_dir: pathlib.Path, anchor: dict, mode: str):
    """File đổi kể từ neo (path TƯƠNG ĐỐI thư mục dự án), LOẠI phần thuộc wiki/html —
    còn lại coi là code/nguồn. None = neo mất hiệu lực (rebase/gc, hoặc đổi chế độ)."""
    if (anchor.get("mode") or "git") != mode:
        return None
    if mode == "tree":
        old = read_sidecar(wiki_dir)
        if old is None or manifest_hash(old) != anchor.get("treeHash"):
            return None
        new = tree_manifest(root, wiki_dir)
        diff = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
        # Neo cũ có thể chứa file mà luật lọc hiện tại đã loại (runtime/.gitignore) —
        # "biến mất" khỏi manifest mới không phải là code đổi.
        gone = {k for k in diff if k not in new}
        return sorted((diff - gone) | _drop_excluded(root, wiki_dir, gone))
    head_anchor = anchor["gitHead"]
    ok = subprocess.run(["git", "cat-file", "-e", f"{head_anchor}^{{commit}}"],
                        cwd=root, capture_output=True)
    if ok.returncode != 0:
        return None
    files: set[str] = set()
    # cwd=root + pathspec "." + --relative: chỉ phần dưới thư mục dự án, path tương đối
    # thư mục đó (ở gốc repo thì y hệt hành vi cũ).
    for line in run_git(root, "diff", "--name-only", "--relative",
                        f"{head_anchor}..HEAD", "--", ".").splitlines():
        if line.strip():
            files.add(line.strip())
    for line in run_git(root, "-c", "status.relativePaths=true", "status", "--short",
                        "--untracked-files=all", "--", ".").splitlines():
        if len(line) > 3:  # "XY <path>" — path bắt đầu cột 3 (dòng KHÔNG được strip)
            files.add(line[3:].rstrip().split(" -> ")[-1].strip('"'))
    excl = _excluded_rels(root, wiki_dir)
    wiki_rel = excl[0].rstrip("/")
    keep = []
    for f in sorted(files):
        if f == wiki_rel or f.startswith(excl[:2]):
            continue
        keep.append(f)
    return keep


def map_suspects(wiki_dir: pathlib.Path, changed: list[str]) -> dict[str, list[str]]:
    """Trang wiki nhắc tới file code đã đổi (theo path đầy đủ, hoặc basename khi
    basename ĐỊNH DANH được). Heuristic tất định, thiên về recall — /lint là người
    phán xử cuối.

    Hai bộ lọc giữ recall mà bỏ cờ-sai-lý-do (đo 2026-09-04, neo 6 tuần / 337 file
    đổi: 159 → 114 cờ; hai cờ LIVE bị gỡ đúng là hai cờ đã rà tay ra false positive):
      1. Trang trong ARCHIVE_PREFIXES: không quét (xem hằng số).
      2. Basename phải có đuôi VÀ khớp ≤ BASENAME_PAGE_CAP trang. Không đạt thì chỉ
         khớp path đầy đủ. Đo thật trên wiki này: "serve" khớp 30/240 trang vì là từ
         tiếng Anh; "SKILL.md" khớp 42/240 vì tên đó có ở 85 chỗ trong repo.
    """
    docs: dict[str, str] = {}
    for d in CONTENT_DIRS:
        basedir = wiki_dir / d.rstrip("/")
        if not basedir.is_dir():
            continue
        for p in basedir.rglob("*.md"):
            rel = p.relative_to(wiki_dir).as_posix()
            if rel.startswith(ARCHIVE_PREFIXES):
                continue
            try:
                docs[rel] = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

    base_pages: dict[str, int] = {}  # memo: basename → số trang khớp
    needles = {}
    for f in changed:
        base = f.rsplit("/", 1)[-1]
        ns = {f}
        if len(base) >= 5 and "." in base:
            if base not in base_pages:
                base_pages[base] = sum(1 for t in docs.values() if base in t)
            if base_pages[base] <= BASENAME_PAGE_CAP:
                ns.add(base)
        needles[f] = ns

    suspects: dict[str, list[str]] = {}
    for rel, text in docs.items():
        hits = [f for f, ns in needles.items() if any(n in text for n in ns)]
        if hits:
            suspects[rel] = hits
    return suspects


def flag_stale(wiki_dir: pathlib.Path, suspects: dict[str, list[str]]) -> None:
    """Ghi cờ code-drift vào stale.json — cùng schema + flock với wiki_ledger."""
    stale_path = wiki_dir / "stale.json"
    lock_path = wiki_dir / ".stale.lock"
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with open(lock_path, "w") as lk:
        if fcntl is not None:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            try:
                stale = json.loads(stale_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stale = {}
            for rel, hits in suspects.items():
                stale[rel] = {"by": hits[0] if len(hits) == 1 else f"{hits[0]} (+{len(hits) - 1})",
                              "action": "code-drift", "ts": ts, "session": None}
            stale_path.write_text(json.dumps(stale, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
        finally:
            if fcntl is not None:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


def clear_code_drift(wiki_dir: pathlib.Path) -> int:
    stale_path = wiki_dir / "stale.json"
    lock_path = wiki_dir / ".stale.lock"
    with open(lock_path, "w") as lk:
        if fcntl is not None:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            try:
                stale = json.loads(stale_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return 0
            keep = {k: v for k, v in stale.items()
                    if not (isinstance(v, dict) and v.get("action") == "code-drift")}
            removed = len(stale) - len(keep)
            if removed:
                stale_path.write_text(json.dumps(keep, ensure_ascii=False, indent=1) + "\n",
                                      encoding="utf-8")
            return removed
        finally:
            if fcntl is not None:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


def log_sync_cost(root: pathlib.Path, status: str, ms: int, suspects: int) -> None:
    """Ghi chi phí một lượt --check vào harness/metrics/sync-log.jsonl (fail-open).

    Vì sao: kiến trúc này cố ý KHÔNG embedding — cổng no-op trả lời "có cần rà không" mà
    không gọi model (0 token). Nhưng một lợi thế không đo được thì không phải lợi thế, chỉ
    là niềm tin: không có số thì vừa không chứng minh được, vừa không phát hiện được lúc nó
    thoái hoá. `sync_tokens` giữ chỗ cho đủ trường — ở cổng no-op nó luôn 0 theo thiết kế.

    Guardrail thật nằm ở `sync_ms`: cổng chậm thì dev tắt hook, mà cổng bị tắt là cổng chết.
    KHÔNG tự tạo thư mục — downstream không có harness/ trong repo (ADR-017) thì im lặng bỏ qua.
    """
    try:
        d = root / "harness" / "metrics"
        if not d.is_dir():
            return
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "status": status, "sync_ms": ms, "sync_tokens": 0, "suspects": suspects}
        with open(d / "sync-log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — đo lường không bao giờ được làm gãy cổng
        pass


def anchor_label(anchor: dict) -> str:
    if anchor.get("mode") == "tree":
        return "tree:" + str(anchor.get("treeHash", ""))[:10]
    return str(anchor.get("gitHead", ""))[:10]


def cmd_check(root: pathlib.Path, wiki_dir: pathlib.Path, as_json: bool,
              mode: str = "git") -> int:
    t0 = time.perf_counter()
    head = run_git(root, "rev-parse", "HEAD").strip()
    anchor = read_anchor(wiki_dir)
    if anchor is None:
        msg = {"status": "no-anchor", "head": head,
               "hint": "chưa có neo — chạy: python -m coding_agent.cli wiki-sync --mark-synced sau lần rà wiki kế tiếp"}
        print(json.dumps(msg, ensure_ascii=False) if as_json else
              f"⚠ wiki-sync: chưa có neo {ANCHOR_NAME} — sau lần rà wiki kế tiếp chạy "
              f"`python -m coding_agent.cli wiki-sync --mark-synced` để bật cổng no-op.")
        log_sync_cost(root, "no-anchor", int((time.perf_counter() - t0) * 1000), 0)
        return 2
    changed = changed_since(root, wiki_dir, anchor, mode)
    if changed is None:
        msg = {"status": "anchor-invalid", "anchor": anchor.get("gitHead"), "head": head,
               "mode": mode,
               "hint": "neo trỏ commit không còn (rebase/squash) hoặc khác chế độ neo "
                       "(git/tree) — --mark-synced để neo lại"}
        print(json.dumps(msg, ensure_ascii=False) if as_json else
              f"⚠ wiki-sync: neo {anchor_label(anchor)} không còn hiệu lực (rebase/squash, "
              f"hoặc đổi chế độ neo → {mode}) — rà wiki một lượt rồi `--mark-synced` để neo lại.")
        log_sync_cost(root, "anchor-invalid", int((time.perf_counter() - t0) * 1000), 0)
        return 2
    if not changed:
        msg = {"status": "current", "head": head, "anchor": anchor.get("gitHead"),
               "mode": mode, "since": anchor.get("updatedAt")}
        print(json.dumps(msg, ensure_ascii=False) if as_json else
              f"✓ wiki current — code không đổi kể từ neo {anchor_label(anchor)} "
              f"({anchor.get('updatedAt', '?')}). No-op, 0 token.")
        log_sync_cost(root, "current", int((time.perf_counter() - t0) * 1000), 0)
        return 0
    suspects = map_suspects(wiki_dir, changed)
    if suspects:
        flag_stale(wiki_dir, suspects)
    if as_json:
        print(json.dumps({"status": "drift", "head": head, "anchor": anchor.get("gitHead"),
                          "mode": mode,
                          "changed": changed[:MAX_CHANGED_LIST],
                          "changed_total": len(changed),
                          "suspects": suspects}, ensure_ascii=False, indent=1))
    else:
        print(f"⟳ wiki-sync: {len(changed)} file code/nguồn đổi kể từ neo "
              f"{anchor_label(anchor)} ({anchor.get('updatedAt', '?')}):")
        for f in changed[:MAX_CHANGED_LIST]:
            print(f"   · {f}")
        if len(changed) > MAX_CHANGED_LIST:
            print(f"   … và {len(changed) - MAX_CHANGED_LIST} file nữa (cap hiển thị "
                  f"{MAX_CHANGED_LIST} — danh sách đầy đủ qua --json).")
        if suspects:
            print(f"  → {len(suspects)} trang wiki nhắc tới file đổi — đã cờ code-drift "
                  f"vào stale.json:")
            for rel, hits in sorted(suspects.items()):
                print(f"   ⚑ {rel}  ⇐ {', '.join(hits[:3])}{' …' if len(hits) > 3 else ''}")
        else:
            print("  → không trang wiki nào nhắc trực tiếp tới file đổi — vẫn nên /lint "
                  "nếu thay đổi mang tính kiến trúc.")
        print("  Rà xong chạy: python -m coding_agent.cli wiki-sync --mark-synced")
    log_sync_cost(root, "drift", int((time.perf_counter() - t0) * 1000), len(suspects))
    return 3


def cmd_flags_for(wiki_dir: pathlib.Path, pages: str, as_json: bool) -> int:
    """Trang nào TRONG SỐ pages đang mang cờ code-drift — dùng trên ĐƯỜNG ĐỌC (/query).

    Vì sao tồn tại: `--check` ghi cờ vào stale.json nhưng tới 2026-07-19 chỉ `/lint` đọc
    cờ đó, mà lint chạy theo chu kỳ → giữa hai lần lint, /query trả trang đã lệch code mà
    KHÔNG cảnh báo (đúng ca "graph là source-of-truth nhưng nội dung lệch" → model lập
    luận trên tri thức cũ). Đây là đầu tiêu thụ cờ cho đường đọc.

    FAIL-OPEN TUYỆT ĐỐI — khác mọi subcommand khác của file này (CLI/CI, lỗi phải nổi).
    Lý do: nó nằm trên đường ĐỌC. stale.json thiếu/hỏng/khoá không bao giờ được phép làm
    gãy một câu trả lời; mất cảnh báo là suy giảm chấp nhận được, mất đường đọc thì không.
    """
    try:
        wanted = [s.strip() for s in pages.split(",") if s.strip()]
        if not wanted:
            return 0
        try:
            stale = json.loads((wiki_dir / "stale.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stale = {}
        hits = {}
        for rel, meta in (stale.items() if isinstance(stale, dict) else []):
            if not isinstance(meta, dict) or meta.get("action") != "code-drift":
                continue
            stem = rel.rsplit("/", 1)[-1].removesuffix(".md")
            for w in wanted:
                wstem = w.rsplit("/", 1)[-1].removesuffix(".md")
                if w == rel or wstem == stem:
                    hits[rel] = meta
                    break
        if as_json:
            print(json.dumps({"flagged": hits}, ensure_ascii=False, indent=1))
        elif hits:
            for rel, meta in sorted(hits.items()):
                print(f"⚑ CẢNH BÁO DRIFT: `{rel}` có thể đã lỗi thời — code đổi kể từ lần "
                      f"rà cuối (⇐ {meta.get('by', '?')}, cờ {meta.get('ts', '?')}). "
                      f"Kiểm lại trước khi tin; chạy /lint để rà.")
    except Exception:  # noqa: BLE001 — fail-open có chủ đích, xem docstring
        pass
    return 0


def cmd_mark(root: pathlib.Path, wiki_dir: pathlib.Path, as_json: bool,
             mode: str = "git", git_root=None, sub: str = "") -> int:
    head = run_git(root, "rev-parse", "HEAD").strip() if git_root is not None else ""
    if mode == "git" and not head:
        sys.exit("wiki-sync: không lấy được git HEAD — đây có phải git repo?")
    chash = content_hash(wiki_dir)
    man = tree_manifest(root, wiki_dir) if mode == "tree" else None
    thash = manifest_hash(man) if man is not None else None
    anchor = read_anchor(wiki_dir)
    if (anchor and (anchor.get("mode") or "git") == mode and anchor.get("contentHash") == chash
            and (anchor.get("treeHash") == thash if mode == "tree"
                 else anchor.get("gitHead") == head)):
        print(json.dumps({"status": "unchanged", "head": head, "mode": mode}, ensure_ascii=False)
              if as_json else f"= neo giữ nguyên ({anchor_label(anchor)} + nội dung wiki không đổi).")
        return 0
    cleared = clear_code_drift(wiki_dir)
    rec = {"gitHead": head or None,
           "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
           "contentHash": chash,
           "tool": "wiki-sync"}
    if mode == "tree" or sub:
        # Dự án không phải gốc repo: gitHead là HEAD của repo CHA (chỉ để tham chiếu),
        # kèm đường dẫn con; ở chế độ tree, drift đo bằng dấu vân tay cây file (sidecar).
        rec.update({"mode": mode, "gitRoot": str(git_root) if git_root else None,
                    "subpath": sub})
    if mode == "tree":
        (wiki_dir / TREE_SIDECAR).write_text(json.dumps(man, sort_keys=True, separators=(",", ":")) + "\n",
                                             encoding="utf-8")
        rec.update({"treeHash": thash, "treeFiles": len(man)})
    (wiki_dir / ANCHOR_NAME).write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n",
                                        encoding="utf-8")
    label = anchor_label(rec)
    print(json.dumps({"status": "marked", "head": head, "mode": mode,
                      "clearedCodeDrift": cleared}, ensure_ascii=False) if as_json else
          f"✓ đã chốt neo tại {label} ({mode}{', ' + sub if sub else ''})" +
          (f" — xoá {cleared} cờ code-drift (đã rà)." if cleared else "."))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coding_agent.cli wiki-sync",
                                 description="neo đồng bộ code→wiki (no-op gate 0 token)")
    ap.add_argument("--root", default=".", help="thư mục dự án chứa llmwiki/ (mặc định: ., "
                    "không có wiki thì đi ngược lên tới git toplevel)")
    ap.add_argument("--wiki-dir", default=None, help="thư mục wiki (mặc định: llmwiki/wiki hoặc wiki/)")
    ap.add_argument("--check", action="store_true", help="kiểm drift (mặc định)")
    ap.add_argument("--mark-synced", action="store_true", help="chốt neo sau khi rà wiki")
    ap.add_argument("--flags-for", default=None, metavar="PAGES",
                    help="đường ĐỌC: trang nào trong danh sách (slug/relpath, phân tách bằng dấu "
                         "phẩy) đang mang cờ code-drift — luôn exit 0, fail-open")
    ap.add_argument("--json", action="store_true", help="output máy đọc")
    a = ap.parse_args(argv)
    if a.flags_for is not None:
        # Fail-open ngay từ khâu định vị: không có git/wiki cũng không được gãy đường đọc.
        try:
            root, _ = resolve_roots(a.root, a.wiki_dir)
            wiki_dir = detect_wiki_dir(root, a.wiki_dir)
        except (Exception, SystemExit):  # noqa: BLE001
            return 0
        return cmd_flags_for(wiki_dir, a.flags_for, a.json)
    try:
        root, git_root = resolve_roots(a.root, a.wiki_dir)
        wiki_dir = detect_wiki_dir(root, a.wiki_dir)
        mode, sub = detect_mode(root, git_root)
        return (cmd_mark(root, wiki_dir, a.json, mode, git_root, sub) if a.mark_synced
                else cmd_check(root, wiki_dir, a.json, mode))
    except SystemExit as stop:
        if isinstance(stop.code, str):
            print(stop.code, file=sys.stderr)
            return 1
        return stop.code or 0


if __name__ == "__main__":
    sys.exit(main())
