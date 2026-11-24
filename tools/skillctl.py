#!/usr/bin/env python3
"""skillctl — 通用 Agent Skill 管理工具（只用 Python 標準庫，3.9+）

參考 Uber 的 agent skill 管理做法縮小成個人／小團隊版：
  * 單一 hub repo 當 marketplace，skills/ 下分 always-on / core / domain 三層
  * 每個 skill 有 spec.md（意圖）→ SKILL.md（給 agent 的指令）
  * 專案用分層設定（hub → 使用者 → 專案 → 本機）決定要裝哪些 skill
  * eval（golden / real / self 三種案例）+ ablation（有無 skill 對照）
  * hook 記錄使用量（不含 prompt 內容），stats 找出該改進或退場的 skill

常用指令：
  skillctl new <name> [--tier domain]        從範本建立新 skill
  skillctl lint [name ...]                    檢查格式（CI 用）
  skillctl list | find <關鍵字>               列出／搜尋 skill
  skillctl init [--project DIR]               在專案啟用框架（設定檔 + hook + 同步）
  skillctl sync [--project DIR]               依分層設定安裝／更新 skill
  skillctl install <skill|plugin:名稱>        額外安裝到目前專案
  skillctl eval <name> [--ablation] [--dry-run]
  skillctl stats [--days 30]                  使用量統計與退場候選
  skillctl papercut <name> "問題描述"          回報小問題（之後可轉成 eval case）
  skillctl compile <name> [--run]             由 spec.md 產生 SKILL.md 草稿
  skillctl promote <name> [--to core|always-on] [--dry-run]   檢查條件並升級
  skillctl demote <name> [--to ...] [--status deprecated|retired]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIERS = ("always-on", "core", "domain")
HUB_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
MAX_DESC = 1024
MAX_BODY_LINES = 500
# 安裝到專案時不複製的檔案：這些是「作者端」資料，agent 不需要
AUTHOR_ONLY = {"spec.md", "evals", "feedback.jsonl", "CHANGELOG.md"}
USAGE_LOG = Path(os.environ.get("SKILLHUB_LOG", Path.home() / ".skillhub" / "usage.jsonl"))


# ---------------------------------------------------------------- 共用工具
def die(msg: str, code: int = 1):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


def parse_frontmatter(text: str):
    """極簡 YAML frontmatter 解析：支援 key: value 與一層巢狀（如 metadata:）。"""
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    raw = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
    data: dict = {}
    parent = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        val = val.strip().strip('"').strip("'")
        if indent and parent is not None:
            data[parent][key.strip()] = val
        elif val == "":
            parent = key.strip()
            data[parent] = {}
        else:
            parent = None
            data[key.strip()] = val
    return data, body


def load_skill(path: Path) -> dict:
    md = path / "SKILL.md"
    fm, body = parse_frontmatter(md.read_text(encoding="utf-8")) if md.exists() else (None, "")
    meta = (fm or {}).get("metadata", {}) if isinstance((fm or {}).get("metadata"), dict) else {}
    return {
        "name": path.name,
        "path": path,
        "tier": path.parent.name,
        "fm": fm or {},
        "body": body,
        "description": (fm or {}).get("description", ""),
        "version": meta.get("version", "0.0.0"),
        "owner": meta.get("owner", ""),
        "status": meta.get("status", "active"),
    }


def all_skills(hub: Path = HUB_ROOT) -> dict:
    out = {}
    for tier in TIERS:
        d = hub / "skills" / tier
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                out[p.name] = load_skill(p)
    return out


def find_skill(name: str, hub: Path = HUB_ROOT) -> dict:
    s = all_skills(hub).get(name)
    if not s:
        die(f"找不到 skill：{name}")
    return s


def dir_hash(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(path.rglob("*")):
        rel = f.relative_to(path)
        if rel.parts[0] in AUTHOR_ONLY or not f.is_file():
            continue
        h.update(str(rel).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError as e:
        die(f"{p} 不是合法 JSON：{e}")


def write_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_usage(days: int | None = None):
    if not USAGE_LOG.exists():
        return []
    cutoff = None
    if days:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    rows = []
    for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cutoff and dt.datetime.fromisoformat(r["ts"]) < cutoff:
            continue
        rows.append(r)
    return rows


# ---------------------------------------------------------------- new / lint
def cmd_new(a):
    if not NAME_RE.match(a.name):
        die("名稱只能用小寫英數與連字號，例如 api-review")
    if a.name in all_skills():
        die(f"{a.name} 已存在")
    dest = HUB_ROOT / "skills" / a.tier / a.name
    shutil.copytree(HUB_ROOT / "templates" / "skill", dest)
    for f in dest.rglob("*"):
        if f.is_file():
            t = f.read_text(encoding="utf-8")
            t = t.replace("{{name}}", a.name).replace("{{owner}}", a.owner or os.environ.get("USER", "me"))
            t = t.replace("{{tier}}", a.tier).replace("{{date}}", dt.date.today().isoformat())
            f.write_text(t, encoding="utf-8")
    print(f"✓ 已建立 {dest.relative_to(HUB_ROOT)}")
    print("  下一步：1) 填 spec.md  2) skillctl compile", a.name, " 3) 補 evals/cases.jsonl  4) skillctl lint", a.name)


def lint_skill(s: dict) -> list[str]:
    errs, warns = [], []
    p = s["path"]
    if not (p / "SKILL.md").exists():
        return [f"ERROR 缺少 SKILL.md"]
    fm = s["fm"]
    if not fm:
        errs.append("SKILL.md 缺少 frontmatter（--- ... ---）")
    if fm.get("name") != p.name:
        errs.append(f"frontmatter name「{fm.get('name')}」必須等於資料夾名稱「{p.name}」")
    if not NAME_RE.match(p.name) or len(p.name) > 64:
        errs.append("名稱只能用小寫英數與連字號，且不超過 64 字元")
    desc = s["description"]
    if not desc:
        errs.append("缺少 description（這是 agent 決定是否載入的唯一依據）")
    elif desc.startswith("（"):
        errs.append("description 還是範本文字，請改寫")
    elif len(desc) > MAX_DESC:
        errs.append(f"description 超過 {MAX_DESC} 字元（{len(desc)}）")
    elif len(desc) < 40:
        warns.append("description 太短，建議寫清楚『做什麼』＋『什麼時候用』")
    n_lines = len(s["body"].splitlines())
    if n_lines > MAX_BODY_LINES:
        errs.append(f"SKILL.md 本文 {n_lines} 行，超過 {MAX_BODY_LINES} 行；把細節移到 references/")
    meta_tier = (fm.get("metadata") or {}).get("tier") if isinstance(fm.get("metadata"), dict) else None
    if meta_tier and meta_tier != s["tier"]:
        errs.append(f"metadata.tier「{meta_tier}」與所在資料夾「{s['tier']}」不一致")
    if not SEMVER_RE.match(s["version"]):
        errs.append("metadata.version 必須是 x.y.z")
    if not s["owner"]:
        errs.append("metadata.owner 必填（誰負責維護）")
    if not (p / "spec.md").exists():
        errs.append("缺少 spec.md（記錄目標、產出、工具限制）")
    cases = p / "evals" / "cases.jsonl"
    if not cases.exists():
        (errs if s["tier"] != "domain" else warns).append("缺少 evals/cases.jsonl")
    else:
        for i, line in enumerate(cases.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                errs.append(f"evals/cases.jsonl 第 {i} 行不是合法 JSON")
                continue
            if c.get("type") not in ("golden", "real", "self"):
                errs.append(f"evals 第 {i} 行 type 必須是 golden/real/self")
            if "prompt" not in c:
                errs.append(f"evals 第 {i} 行缺少 prompt")
    for ref in re.findall(r"\]\(((?:references|scripts)/[^)#]+)\)", s["body"]):
        if not (p / ref).exists():
            errs.append(f"SKILL.md 連到不存在的檔案 {ref}")
    if s["tier"] == "always-on" and len(desc) > 300:
        warns.append("always-on skill 的 description 每次都會載入，建議 300 字元內")
    return [f"ERROR {e}" for e in errs] + [f"WARN  {w}" for w in warns]


def cmd_lint(a):
    skills = all_skills()
    targets = [find_skill(n) for n in a.names] if a.names else list(skills.values())
    n_err = 0
    seen = {}
    for s in targets:
        msgs = lint_skill(s)
        if s["name"] in seen:
            msgs.append(f"ERROR 名稱與 {seen[s['name']]} 重複")
        seen[s["name"]] = s["tier"]
        n_err += sum(m.startswith("ERROR") for m in msgs)
        mark = "✗" if any(m.startswith("ERROR") for m in msgs) else ("!" if msgs else "✓")
        print(f"{mark} {s['tier']}/{s['name']} v{s['version']}")
        for m in msgs:
            print(f"    {m}")
    for pf in sorted((HUB_ROOT / "plugins").glob("*.json")):
        for n in read_json(pf).get("skills", []):
            if n not in skills:
                print(f"✗ plugins/{pf.name} 引用不存在的 skill：{n}")
                n_err += 1
    print(f"\n{len(targets)} 個 skill，{n_err} 個錯誤")
    sys.exit(1 if n_err else 0)


# ---------------------------------------------------------------- list / find
def eval_score(s: dict):
    f = s["path"] / "evals" / "last_result.json"
    return read_json(f).get("pass_rate") if f.exists() else None


def cmd_list(a):
    usage = {}
    for r in read_usage(30):
        usage[r["skill"]] = usage.get(r["skill"], 0) + 1
    rows = []
    q = (a.query or "").lower()
    terms = [t for t in re.split(r"\s+", q) if t]
    for s in all_skills().values():
        text = f"{s['name']} {s['description']}".lower()
        match = sum(text.count(t) for t in terms) + sum(3 for t in terms if t in s["name"])
        if terms and match == 0:
            continue
        ev = eval_score(s)
        # 排序依據：關鍵字符合度 > eval 分數 > 30 天使用量 > 層級
        tier_bonus = {"always-on": 0.2, "core": 0.1, "domain": 0}[s["tier"]]
        score = match * 10 + (ev or 0.5) * 5 + min(usage.get(s["name"], 0), 50) / 10 + tier_bonus
        rows.append((score, s, ev))
    rows.sort(key=lambda r: -r[0])
    if a.json:
        print(json.dumps([{"name": s["name"], "tier": s["tier"], "version": s["version"],
                           "description": s["description"], "eval_pass_rate": ev,
                           "uses_30d": usage.get(s["name"], 0)} for _, s, ev in rows],
                         ensure_ascii=False, indent=2))
        return
    if not rows:
        print("沒有符合的 skill。可以用 skillctl new 建一個。")
    for _, s, ev in rows[: a.limit]:
        evs = f"eval {ev:.0%}" if ev is not None else "eval -"
        flag = " [deprecated]" if s["status"] == "deprecated" else ""
        print(f"{s['name']:<28} {s['tier']:<9} v{s['version']:<7} {evs:<9} 用量30天 {usage.get(s['name'], 0):<4}{flag}")
        print(f"    {s['description'][:110]}")


# ---------------------------------------------------------------- 分層設定 / sync / install
def layered_config(project: Path) -> dict:
    """設定合併順序（後面覆蓋前面）：hub 預設 → 使用者 → 專案 → 本機。"""
    layers = [
        ("hub", HUB_ROOT / "skillhub.json"),
        ("user", Path(os.environ.get("SKILLHUB_USER_CONFIG", Path.home() / ".config" / "skillhub" / "config.json"))),
        ("project", project / ".skillhub.json"),
        ("local", project / ".skillhub.local.json"),
    ]
    cfg = {"always_on": True, "plugins": [], "include": [], "exclude": [], "pin": {}, "mode": "copy", "sources": []}
    for name, p in layers:
        d = read_json(p)
        if not d:
            continue
        cfg["sources"].append(f"{name}:{p}")
        for k in ("plugins", "include", "exclude"):
            for v in d.get(k, []):
                if v not in cfg[k]:
                    cfg[k].append(v)
        for k in ("always_on", "mode"):
            if k in d:
                cfg[k] = d[k]
        cfg["pin"].update(d.get("pin", {}))
    return cfg


def resolve_wanted(cfg: dict) -> dict:
    skills = all_skills()
    wanted = {}  # name -> 來源說明
    if cfg["always_on"]:
        for s in skills.values():
            if s["tier"] == "always-on":
                wanted[s["name"]] = "always-on"
    for pl in cfg["plugins"]:
        pf = HUB_ROOT / "plugins" / f"{pl}.json"
        if not pf.exists():
            die(f"找不到 plugin：{pl}（plugins/{pl}.json）")
        for n in read_json(pf).get("skills", []):
            wanted.setdefault(n, f"plugin:{pl}")
    for n in cfg["include"]:
        wanted.setdefault(n, "include")
    for n in cfg["exclude"]:
        wanted.pop(n, None)
    for n in wanted:
        if n not in skills:
            die(f"設定要求的 skill 不存在：{n}")
        if skills[n]["status"] == "retired":
            print(f"! {n} 已退場，略過")
    return {n: src for n, src in wanted.items() if skills[n]["status"] != "retired"}


def install_one(s: dict, project: Path, mode: str):
    dest = project / ".claude" / "skills" / s["name"]
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "link":
        dest.symlink_to(s["path"], target_is_directory=True)
    else:
        top = str(s["path"])
        shutil.copytree(s["path"], dest, ignore=lambda d, names: [n for n in names if d == top and (n in AUTHOR_ONLY or n.endswith(".draft"))])


def cmd_sync(a):
    project = Path(a.project).resolve()
    cfg = layered_config(project)
    wanted = resolve_wanted(cfg)
    skills = all_skills()
    lock_p = project / ".claude" / "skills.lock.json"
    lock = read_json(lock_p).get("skills", {})
    new_lock = {}
    for n, src in sorted(wanted.items()):
        s = skills[n]
        if n in cfg["pin"] and cfg["pin"][n] != s["version"]:
            if n in lock and lock[n]["version"] == cfg["pin"][n]:
                new_lock[n] = lock[n]
                print(f"= {n} 固定在 v{cfg['pin'][n]}（hub 已是 v{s['version']}）")
                continue
            print(f"! {n} 要求固定 v{cfg['pin'][n]}，但 hub 只有 v{s['version']}，改裝 hub 版本")
        h = dir_hash(s["path"])
        prev = lock.get(n)
        if prev and prev["hash"] == h and (project / ".claude/skills" / n).exists():
            new_lock[n] = prev
            continue
        if not a.dry_run:
            install_one(s, project, cfg["mode"])
        verb = "更新" if prev else "安裝"
        print(f"+ {verb} {n} v{s['version']}  ({src})")
        new_lock[n] = {"version": s["version"], "hash": h, "tier": s["tier"], "source": src}
    for n in lock:
        if n not in new_lock:
            d = project / ".claude" / "skills" / n
            print(f"- 移除 {n}")
            if not a.dry_run:
                if d.is_symlink():
                    d.unlink()
                elif d.exists():
                    shutil.rmtree(d)
    if not a.dry_run:
        write_json(lock_p, {"hub": str(HUB_ROOT), "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                            "skills": new_lock})
    print(f"✓ {len(new_lock)} 個 skill 已同步到 {project / '.claude/skills'}（設定來源：{', '.join(x.split(':')[0] for x in cfg['sources']) or '預設'}）")


def cmd_install(a):
    project = Path(a.project).resolve()
    cfg_p = project / ".skillhub.json"
    cfg = read_json(cfg_p) or {"hub": str(HUB_ROOT)}
    for item in a.items:
        key, val = ("plugins", item.split(":", 1)[1]) if item.startswith("plugin:") else ("include", item)
        if key == "include":
            find_skill(val)
        elif not (HUB_ROOT / "plugins" / f"{val}.json").exists():
            die(f"找不到 plugin：{val}")
        cfg.setdefault(key, [])
        if val not in cfg[key]:
            cfg[key].append(val)
        if val in cfg.get("exclude", []):
            cfg["exclude"].remove(val)
    write_json(cfg_p, cfg)
    a.dry_run = False
    cmd_sync(a)


HOOK_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/skillhub_usage.py"'


def cmd_init(a):
    project = Path(a.project).resolve()
    cfg_p = project / ".skillhub.json"
    if not cfg_p.exists():
        write_json(cfg_p, {"hub": str(HUB_ROOT), "plugins": a.plugins or [], "include": [], "exclude": []})
        print(f"✓ 建立 {cfg_p.name}")
    # .gitignore：本機覆寫檔不進版控
    gi = project / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if ".skillhub.local.json" not in lines:
        gi.write_text("\n".join(lines + [".skillhub.local.json"]) + "\n", encoding="utf-8")
    if not a.no_hook:
        hook_dir = project / ".claude" / "hooks"
        hook_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HUB_ROOT / "hooks" / "skillhub_usage.py", hook_dir / "skillhub_usage.py")
        settings_p = project / ".claude" / "settings.json"
        st = read_json(settings_p)
        hooks = st.setdefault("hooks", {})
        for event, matcher in (("PostToolUse", "Skill"), ("UserPromptSubmit", None)):
            entries = hooks.setdefault(event, [])
            if any(HOOK_CMD in json.dumps(e) for e in entries):
                continue
            e = {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 5}]}
            if matcher:
                e["matcher"] = matcher
            entries.append(e)
        write_json(settings_p, st)
        print("✓ 已在 .claude/settings.json 註冊使用量 hook")
    a.dry_run = False
    cmd_sync(a)


# ---------------------------------------------------------------- eval
def load_cases(s: dict):
    f = s["path"] / "evals" / "cases.jsonl"
    if not f.exists():
        die(f"{s['name']} 沒有 evals/cases.jsonl")
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


EVAL_TOOLS = "Skill,Read,Glob,Grep"  # 預設只給唯讀工具；需要更多用 --allow-tools 指定


def run_claude(prompt: str, workdir: Path, timeout: int, model: str | None, tools: str = EVAL_TOOLS):
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--allowedTools", tools, "--max-turns", "8"]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"text": "", "skills": [], "tools": [], "error": "timeout"}
    text, skills, tools = "", [], []
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    tools.append(c.get("name"))
                    if c.get("name") == "Skill":
                        skills.append((c.get("input") or {}).get("skill") or (c.get("input") or {}).get("command"))
        if ev.get("type") == "result":
            text = ev.get("result", "") or ""
    return {"text": text, "skills": skills, "tools": tools, "error": None if r.returncode == 0 else r.stderr[-300:]}


def check_case(c: dict, out: dict, skill: str) -> list[str]:
    """回傳失敗原因清單；空清單 = 通過。"""
    e = c.get("expect", {})
    fails = []
    if out.get("error"):
        fails.append(f"執行錯誤：{out['error']}")
    trig = e.get("triggers")
    if trig is True and skill not in out["skills"]:
        fails.append("預期觸發 skill 但沒有")
    if trig is False and skill in out["skills"]:
        fails.append("不該觸發卻觸發了（description 太寬）")
    for t in e.get("contains", []):
        if t.lower() not in out["text"].lower():
            fails.append(f"輸出缺少「{t}」")
    for t in e.get("not_contains", []):
        if t.lower() in out["text"].lower():
            fails.append(f"輸出不該包含「{t}」")
    for rx in e.get("regex", []):
        if not re.search(rx, out["text"], re.S):
            fails.append(f"輸出不符合 /{rx}/")
    order = e.get("tool_order")
    if order:
        it = iter(out["tools"])
        if not all(any(t == x for x in it) for t in order):
            fails.append(f"工具呼叫順序不符：預期 {order}，實際 {out['tools']}")
    return fails


def make_sandbox(with_skill: dict | None, base_skills: list[dict]) -> Path:
    d = Path(tempfile.mkdtemp(prefix="skilleval-"))
    for s in base_skills + ([with_skill] if with_skill else []):
        install_one(s, d, "copy")
    return d


def prepare(c: dict, sb: Path):
    """案例可帶 setup（shell 指令），在沙盒中先建立情境，例如建 git repo 並 stage 檔案。"""
    if c.get("setup"):
        subprocess.run(["bash", "-c", c["setup"]], cwd=sb, capture_output=True, timeout=60)


def cmd_eval(a):
    s = find_skill(a.name)
    cases = load_cases(s)
    mix = {t: sum(c.get("type") == t for c in cases) for t in ("golden", "real", "self")}
    total = len(cases)
    print(f"{s['name']}：{total} 個案例  golden {mix['golden']} / real {mix['real']} / self {mix['self']}")
    if total >= 10:
        pct = {k: v / total for k, v in mix.items()}
        if pct["real"] < 0.5:
            print("  ! 真實 prompt 比例偏低（Uber 約 80%）。用 skillctl papercut / stats 從實際使用補案例")
    if a.dry_run:
        print("✓ dry-run：案例格式正確，未實際呼叫模型")
        return
    if not shutil.which("claude"):
        die("找不到 claude CLI，無法跑 eval（可加 --dry-run 只檢查格式）")
    always_on = [x for x in all_skills().values() if x["tier"] == "always-on" and x["name"] != s["name"]]
    results = []
    for i, c in enumerate(cases, 1):
        runs = []
        for _ in range(a.repeat):
            sb = make_sandbox(s, always_on)
            prepare(c, sb)
            out = run_claude(c["prompt"], sb, a.timeout, a.model, a.allow_tools)
            runs.append(check_case(c, out, s["name"]))
            shutil.rmtree(sb, ignore_errors=True)
        passed = sum(not f for f in runs)
        ok = passed / a.repeat >= 0.5
        row = {"i": i, "type": c.get("type"), "passed": ok, "fails": runs[0] if not ok else []}
        if a.ablation:
            sb = make_sandbox(None, always_on)
            prepare(c, sb)
            out = run_claude(c["prompt"], sb, a.timeout, a.model, a.allow_tools)
            e2 = dict(c)
            e2["expect"] = {k: v for k, v in c.get("expect", {}).items() if k != "triggers"}
            row["baseline_passed"] = not check_case(e2, out, s["name"])
            shutil.rmtree(sb, ignore_errors=True)
        results.append(row)
        mark = "✓" if ok else "✗"
        extra = f"  baseline {'✓' if row.get('baseline_passed') else '✗'}" if a.ablation else ""
        print(f"  {mark} #{i} [{c.get('type')}] {c['prompt'][:60]}{extra}")
        for f in row["fails"]:
            print(f"      - {f}")
    rate = sum(r["passed"] for r in results) / max(len(results), 1)
    summary = {"skill": s["name"], "version": s["version"], "pass_rate": round(rate, 3),
               "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "cases": results}
    print(f"\n通過率 {rate:.0%}")
    if rate == 1.0 and total >= 5:
        print("  ! 100% 通過：案例可能太簡單，測不出改動造成的差異。加一些困難或邊界案例")
    elif rate < 0.7:
        print("  ! 低於 70%：先修 skill 或案例再上架")
    if a.ablation:
        base = sum(r.get("baseline_passed", False) for r in results) / max(len(results), 1)
        summary["baseline_pass_rate"] = round(base, 3)
        print(f"沒有這個 skill 時的通過率 {base:.0%}（差距 {rate - base:+.0%}）")
        if base >= rate:
            print("  → 模型不靠這個 skill 也做得一樣好，可考慮退場（metadata.status: deprecated）")
    write_json(s["path"] / "evals" / "last_result.json", summary)


# ---------------------------------------------------------------- stats / papercut / compile
def cmd_stats(a):
    rows = read_usage(a.days)
    skills = all_skills()
    agg = {}
    for r in rows:
        if r.get("event") != "skill_invoked":
            continue
        g = agg.setdefault(r["skill"], {"n": 0, "sessions": set(), "projects": set(), "last": r["ts"], "versions": set(), "errors": 0})
        g["n"] += 1
        g["sessions"].add(r.get("session"))
        g["projects"].add(r.get("project"))
        g["versions"].add(r.get("version", "?"))
        g["last"] = max(g["last"], r["ts"])
        g["errors"] += 1 if r.get("error") else 0
    print(f"最近 {a.days} 天（紀錄檔 {USAGE_LOG}）")
    print(f"{'skill':<28}{'次數':>6}{'session':>9}{'專案':>6}{'錯誤':>6}  最後使用   版本")
    for n, g in sorted(agg.items(), key=lambda x: -x[1]["n"]):
        print(f"{n:<28}{g['n']:>6}{len(g['sessions']):>9}{len(g['projects']):>6}{g['errors']:>6}  {g['last'][:10]}  {','.join(sorted(g['versions']))}")
    unused = [n for n, s in skills.items() if n not in agg and s["tier"] != "always-on" and s["status"] == "active"]
    if unused:
        print(f"\n{a.days} 天內沒被用過（退場候選，或 description 寫得讓 agent 找不到）：")
        for n in unused:
            print(f"  - {skills[n]['tier']}/{n}")
    papercuts = {n: len((s["path"] / "feedback.jsonl").read_text(encoding="utf-8").splitlines())
                 for n, s in skills.items() if (s["path"] / "feedback.jsonl").exists()}
    if papercuts:
        print("\n待處理 papercut：" + "、".join(f"{n}({c})" for n, c in papercuts.items()))


def cmd_papercut(a):
    s = find_skill(a.name)
    f = s["path"] / "feedback.jsonl"
    if a.to_eval:
        if not f.exists():
            die("沒有 papercut")
        items = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        cases = s["path"] / "evals" / "cases.jsonl"
        with cases.open("a", encoding="utf-8") as fh:
            for it in items:
                if it.get("prompt"):
                    fh.write(json.dumps({"type": "real", "prompt": it["prompt"], "expect": {"triggers": True},
                                         "note": f"from papercut: {it['issue']}"}, ensure_ascii=False) + "\n")
        f.rename(f.with_suffix(".done.jsonl"))
        print(f"✓ 已把 {sum(1 for i in items if i.get('prompt'))} 筆 papercut 轉成 eval 案例，記得補 expect 條件")
        return
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "version": s["version"],
                             "issue": a.issue, "prompt": a.prompt}, ensure_ascii=False) + "\n")
    print(f"✓ 已記錄到 {f.relative_to(HUB_ROOT)}")


COMPILE_PROMPT = """你是 Agent Skill 作者。請依下面的 spec.md 產生完整的 SKILL.md。
規則：
- 開頭是 YAML frontmatter，欄位：name（= {name}）、description（第三人稱，寫「做什麼＋什麼時候用」，含使用者會說的關鍵字，300 字元內）、metadata（保留 version/owner/tier/status）。
- 本文 500 行內，寫成 agent 可直接照做的步驟；大段參考資料改成連到 references/ 的連結。
- 遵守 spec 中的「必用工具」「禁用工具」與「產出格式」。
- 只輸出 SKILL.md 內容，不要加說明。

目前的 SKILL.md（保留其 metadata）：
{current}

spec.md：
{spec}
"""


def cmd_compile(a):
    s = find_skill(a.name)
    spec = (s["path"] / "spec.md").read_text(encoding="utf-8")
    cur = (s["path"] / "SKILL.md").read_text(encoding="utf-8")
    prompt = COMPILE_PROMPT.format(name=s["name"], spec=spec, current=cur)
    if not a.run:
        print(prompt)
        print("\n（加 --run 會用 claude CLI 產生 SKILL.md.draft 供你比對）")
        return
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=300)
    out = s["path"] / "SKILL.md.draft"
    out.write_text(r.stdout.strip() + "\n", encoding="utf-8")
    print(f"✓ 草稿寫到 {out.relative_to(HUB_ROOT)}，確認後覆蓋 SKILL.md 並把 version 加一")


# ---------------------------------------------------------------- promote / demote
# 升級門檻：可在 skillhub.json 的 "promotion" 欄位覆蓋
PROMOTION_RULES = {
    "core": {"min_projects": 2, "min_uses": 10, "min_cases": 5, "min_pass": 0.7, "need_ablation": False, "max_desc": 1024},
    "always-on": {"min_projects": 3, "min_uses": 30, "min_cases": 8, "min_pass": 0.7, "need_ablation": True, "max_desc": 300},
}


def set_meta(s: dict, **kv):
    """改寫 SKILL.md frontmatter metadata 中的欄位（保留其他內容）。"""
    md = s["path"] / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    head, rest = text[:end], text[end:]
    for k, v in kv.items():
        pat = re.compile(rf"^(\s+){k}:.*$", re.M)
        if pat.search(head):
            head = pat.sub(lambda m: f"{m.group(1)}{k}: {v}", head, count=1)
        else:
            head = re.sub(r"^metadata:\s*$", f"metadata:\n  {k}: {v}", head, count=1, flags=re.M)
    md.write_text(head + rest, encoding="utf-8")


def bump(version: str, part: str) -> str:
    ma, mi, pa = (int(x) for x in (version if SEMVER_RE.match(version) else "0.0.0").split("."))
    return {"major": f"{ma + 1}.0.0", "minor": f"{ma}.{mi + 1}.0", "patch": f"{ma}.{mi}.{pa + 1}"}[part]


def changelog(s: dict, version: str, line: str):
    f = s["path"] / "CHANGELOG.md"
    old = f.read_text(encoding="utf-8") if f.exists() else "# Changelog\n"
    title, _, body = old.partition("\n")
    f.write_text(f"{title}\n\n## {version} — {dt.date.today().isoformat()}\n- {line}\n{body}", encoding="utf-8")


def move_skill(s: dict, tier: str) -> Path:
    dest = HUB_ROOT / "skills" / tier / s["name"]
    in_git = subprocess.run(["git", "rev-parse"], cwd=HUB_ROOT, capture_output=True).returncode == 0
    tracked = in_git and subprocess.run(["git", "ls-files", "--error-unmatch", str(s["path"] / "SKILL.md")],
                                        cwd=HUB_ROOT, capture_output=True).returncode == 0
    if tracked:
        subprocess.run(["git", "mv", str(s["path"]), str(dest)], cwd=HUB_ROOT, check=True)
    else:
        shutil.move(str(s["path"]), str(dest))
    return dest


def promotion_checks(s: dict, target: str, days: int) -> list[tuple[bool, str]]:
    rules = dict(PROMOTION_RULES[target])
    rules.update(read_json(HUB_ROOT / "skillhub.json").get("promotion", {}).get(target, {}))
    checks = []
    errs = [m for m in lint_skill(s) if m.startswith("ERROR")]
    checks.append((not errs, "lint 無錯誤" + (f"（{len(errs)} 個錯誤）" if errs else "")))
    checks.append((s["status"] == "active", f"狀態為 active（目前 {s['status']}）"))
    checks.append((bool(s["owner"]), f"有負責人（{s['owner'] or '未填'}）"))
    try:
        n_cases = len(load_cases(s)) if (s["path"] / "evals" / "cases.jsonl").exists() else 0
    except json.JSONDecodeError:
        n_cases = 0
    checks.append((n_cases >= rules["min_cases"], f"eval 案例 ≥ {rules['min_cases']}（目前 {n_cases}）"))
    res = read_json(s["path"] / "evals" / "last_result.json")
    rate = res.get("pass_rate")
    fresh = res.get("version") == s["version"]
    checks.append((rate is not None and fresh and rate >= rules["min_pass"],
                   f"目前版本的 eval 通過率 ≥ {rules['min_pass']:.0%}（"
                   + ("沒跑過 eval" if rate is None else f"{rate:.0%}" + ("" if fresh else f"，但跑的是 v{res.get('version')}")) + "）"))
    if rules["need_ablation"]:
        base = res.get("baseline_pass_rate")
        checks.append((base is not None and rate is not None and rate > base,
                       "ablation 顯示 skill 有幫助（" + ("沒跑過 --ablation" if base is None else f"有 {rate:.0%} / 無 {base:.0%}") + "）"))
    checks.append((len(s["description"]) <= rules["max_desc"], f"description ≤ {rules['max_desc']} 字元（目前 {len(s['description'])}）"))
    rows = [r for r in read_usage(days) if r.get("skill") == s["name"] and r.get("event") == "skill_invoked"]
    projects = {r.get("project") for r in rows}
    checks.append((len(projects) >= rules["min_projects"], f"{days} 天內被 ≥ {rules['min_projects']} 個專案使用（目前 {len(projects)}）"))
    checks.append((len(rows) >= rules["min_uses"], f"{days} 天內使用 ≥ {rules['min_uses']} 次（目前 {len(rows)}）"))
    return checks


def cmd_promote(a):
    s = find_skill(a.name)
    order = list(TIERS)[::-1]  # domain → core → always-on
    if s["tier"] == "always-on":
        die(f"{s['name']} 已經是 always-on")
    target = a.to or order[order.index(s["tier"]) + 1]
    if order.index(target) <= order.index(s["tier"]):
        die(f"{s['tier']} → {target} 不是升級；降級請用 skillctl demote")
    print(f"{s['name']}：{s['tier']} → {target}")
    checks = promotion_checks(s, target, a.days)
    for ok, msg in checks:
        print(f"  {'✓' if ok else '✗'} {msg}")
    failed = [m for ok, m in checks if not ok]
    if failed and not a.force:
        print(f"\n{len(failed)} 項未通過，沒有變更。補齊後再試，或加 --force 強制升級（請在 PR 說明理由）")
        sys.exit(1)
    if a.dry_run:
        print("\n（dry-run，沒有變更）")
        return
    new_v = bump(s["version"], "minor")
    dest = move_skill(s, target)
    s2 = load_skill(dest)
    set_meta(s2, tier=target, version=new_v)
    changelog(s2, new_v, f"升級為 {target}" + ("（--force：" + "；".join(failed) + "）" if failed else ""))
    print(f"\n✓ 已搬到 skills/{target}/{s['name']}，版本 {s['version']} → {new_v}")
    print("  下一步：git checkout -b promote-" + s["name"] + " && git add -A && git commit，開 PR（需 Code Owner 審查）")


def cmd_demote(a):
    s = find_skill(a.name)
    order = list(TIERS)[::-1]
    if not a.to and not a.status:
        die("請指定 --to <層級> 或 --status deprecated|retired|active")
    path, notes = s["path"], []
    if a.to:
        if order.index(a.to) >= order.index(s["tier"]):
            die(f"{s['tier']} → {a.to} 不是降級；升級請用 skillctl promote")
        path = move_skill(s, a.to)
        notes.append(f"降級為 {a.to}")
    s2 = load_skill(path)
    new_v = bump(s["version"], "minor" if a.to else "patch")
    meta = {"version": new_v}
    if a.to:
        meta["tier"] = a.to
    if a.status:
        meta["status"] = a.status
        notes.append(f"狀態改為 {a.status}")
    set_meta(s2, **meta)
    changelog(s2, new_v, "；".join(notes) + (f"。原因：{a.reason}" if a.reason else ""))
    print(f"✓ {s['name']}：{'；'.join(notes)}，版本 {s['version']} → {new_v}")
    if a.status == "retired":
        print("  retired 的 skill 會在各專案下次 skillctl sync 時被移除")


# ---------------------------------------------------------------- main
def main(argv=None):
    p = argparse.ArgumentParser(prog="skillctl", description="通用 Agent Skill 管理工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("new", help="從範本建立 skill")
    x.add_argument("name")
    x.add_argument("--tier", choices=TIERS, default="domain")
    x.add_argument("--owner")
    x.set_defaults(fn=cmd_new)

    x = sub.add_parser("lint", help="檢查 skill 格式")
    x.add_argument("names", nargs="*")
    x.set_defaults(fn=cmd_lint)

    for nm in ("list", "find"):
        x = sub.add_parser(nm, help="列出／搜尋 skill")
        x.add_argument("query", nargs="?")
        x.add_argument("--json", action="store_true")
        x.add_argument("--limit", type=int, default=20)
        x.set_defaults(fn=cmd_list)

    for nm, fn in (("sync", cmd_sync), ("init", cmd_init), ("install", cmd_install)):
        x = sub.add_parser(nm)
        x.add_argument("--project", default=".")
        if nm == "sync":
            x.add_argument("--dry-run", action="store_true")
        if nm == "init":
            x.add_argument("--plugins", nargs="*")
            x.add_argument("--no-hook", action="store_true")
        if nm == "install":
            x.add_argument("items", nargs="+", help="skill 名稱或 plugin:名稱")
        x.set_defaults(fn=fn)

    x = sub.add_parser("eval", help="跑 eval")
    x.add_argument("name")
    x.add_argument("--ablation", action="store_true", help="同時跑『沒有這個 skill』的對照組")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--repeat", type=int, default=1, help="每案例跑幾次（取多數）")
    x.add_argument("--timeout", type=int, default=240)
    x.add_argument("--model")
    x.add_argument("--allow-tools", default=EVAL_TOOLS, help=f"eval 時允許的工具（預設 {EVAL_TOOLS}）")
    x.set_defaults(fn=cmd_eval)

    x = sub.add_parser("stats", help="使用量統計")
    x.add_argument("--days", type=int, default=30)
    x.set_defaults(fn=cmd_stats)

    x = sub.add_parser("papercut", help="回報 skill 小問題")
    x.add_argument("name")
    x.add_argument("issue", nargs="?", default="")
    x.add_argument("--prompt", help="觸發問題的使用者請求（之後可轉成 eval）")
    x.add_argument("--to-eval", action="store_true")
    x.set_defaults(fn=cmd_papercut)

    x = sub.add_parser("compile", help="由 spec.md 產生 SKILL.md")
    x.add_argument("name")
    x.add_argument("--run", action="store_true")
    x.set_defaults(fn=cmd_compile)

    x = sub.add_parser("promote", help="檢查升級條件並升級 skill（domain → core → always-on）")
    x.add_argument("name")
    x.add_argument("--to", choices=("core", "always-on"))
    x.add_argument("--days", type=int, default=30, help="看幾天內的使用量")
    x.add_argument("--dry-run", action="store_true", help="只檢查，不變更")
    x.add_argument("--force", action="store_true", help="條件未全過也升級")
    x.set_defaults(fn=cmd_promote)

    x = sub.add_parser("demote", help="降級或標記 deprecated / retired")
    x.add_argument("name")
    x.add_argument("--to", choices=("core", "domain"))
    x.add_argument("--status", choices=("active", "deprecated", "retired"))
    x.add_argument("--reason", default="")
    x.set_defaults(fn=cmd_demote)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
