#!/usr/bin/env python3
"""skillctl — 通用 Agent Skill 管理工具（只用 Python 標準庫，3.9+）

參考 Uber 的 agent skill 管理做法縮小成個人／小團隊版：
  * 單一 hub repo 當 marketplace，skills/ 下分 always-on / core / domain 三層
  * 每個 skill 有 spec.md（意圖）→ SKILL.md（給 agent 的指令）
  * 專案用分層設定（hub → 團隊 → 使用者 → 專案 → 本機）決定要裝哪些 skill
  * eval（golden / real / self 三種案例）+ ablation（有無 skill 對照）
  * hook 記錄使用量（不含 prompt 內容），stats 找出該改進或退場的 skill

常用指令：
  skillctl new <name> [--tier domain]        從範本建立新 skill
  skillctl lint [name ...]                    檢查格式（CI 用）
  skillctl list | find <關鍵字>               列出／搜尋 skill
  skillctl init [--team 團隊] [--project DIR]  在專案啟用框架（設定檔 + hook + 同步）
  skillctl sync [--project DIR]               依分層設定安裝／更新 skill
  skillctl install <skill|plugin:名稱>        額外安裝到目前專案
  skillctl eval <name> [--ablation] [--dry-run]
  skillctl stats [--days 30]                  使用量統計與退場候選
  skillctl papercut <name> "問題描述"          回報小問題（之後可轉成 eval case）
  skillctl compile <name> [--run]             由 spec.md 產生 SKILL.md 草稿
  skillctl adopt [name ...]                   把專案內自建的 skill 送進 hub domain 層並開 PR
  skillctl scan [目標 ...] [--llm]             安全掃描（SkillSpector 或 Cisco；skill、路徑、tool:/plugin:/bundle:）
  skillctl tools [--run]                      外部工具（graphify、archify…）的狀態與安裝指令
  skillctl doctor [--project DIR]              檢查專案狀態（skill、hook、必裝 plugin）
  skillctl promote <name> [--to core|always-on] [--dry-run]   檢查條件並升級
  skillctl demote <name> [--to ...] [--status deprecated|retired]
  skillctl teams                              列出各團隊的必裝與建議
  skillctl report [--pr]                      匯出匿名使用彙總到 hub 的 telemetry/
  skillctl dashboard [--out site/index.html]  產生靜態看板（CI 每天發佈到 GitHub Pages）
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
    clash = reserved_names().get(p.name)
    if clash:
        errs.append(f"名稱與 plugin {clash} 的 skill 重複，請改名（例如加上領域前綴）")
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
        for ext in read_json(pf).get("extends", []):
            if not (HUB_ROOT / "plugins" / f"{ext}.json").exists():
                print(f"✗ plugins/{pf.name} extends 不存在的組合包：{ext}")
                n_err += 1
        for ap in read_json(pf).get("agent_plugins", []):
            aid = ap if isinstance(ap, str) else ap.get("id", "")
            if "@" not in aid:
                print(f"✗ plugins/{pf.name} 的 agent_plugins「{aid}」要寫成 名稱@marketplace")
                n_err += 1
        for n in read_json(pf).get("skills", []):
            if n not in skills:
                print(f"✗ plugins/{pf.name} 引用不存在的 skill：{n}")
                n_err += 1
    for t, d in all_teams().items():
        for sec in ("required", "recommended"):
            for n in d.get(sec, {}).get("skills", []):
                if n not in skills:
                    print(f"✗ teams/{t}.json 的 {sec} 引用不存在的 skill：{n}")
                    n_err += 1
            for b in d.get(sec, {}).get("plugins", []):
                if not (HUB_ROOT / "plugins" / f"{b}.json").exists():
                    print(f"✗ teams/{t}.json 的 {sec} 引用不存在的組合包：{b}")
                    n_err += 1
        for ap in d.get("required", {}).get("agent_plugins", []):
            if "@" not in (ap if isinstance(ap, str) else ap.get("id", "")):
                print(f"✗ teams/{t}.json 的 agent_plugins 要寫成 名稱@marketplace")
                n_err += 1
    print(f"\n{len(targets)} 個 skill，{n_err} 個錯誤")
    sys.exit(1 if n_err else 0)


# ---------------------------------------------------------------- list / find
def eval_result(s: dict) -> dict:
    for f in ("last_result.json", "score.json"):
        d = read_json(s["path"] / "evals" / f)
        if d:
            return d
    return {}


def eval_score(s: dict):
    return eval_result(s).get("pass_rate")


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
TEAMS_DIR = HUB_ROOT / "teams"


def all_teams() -> dict:
    return {p.stem: read_json(p) for p in sorted(TEAMS_DIR.glob("*.json"))} if TEAMS_DIR.exists() else {}


def config_teams(project: Path) -> list:
    """專案屬於哪些團隊：專案或本機設定的 "team"（字串或清單）。"""
    teams = []
    for p in (project / ".skillhub.json", project / ".skillhub.local.json"):
        t = read_json(p).get("team")
        for x in ([t] if isinstance(t, str) else (t or [])):
            if x not in teams:
                teams.append(x)
    return teams


def layered_config(project: Path) -> dict:
    """設定合併順序（後面覆蓋前面）：hub 預設 → 團隊 → 使用者 → 專案 → 本機。
    團隊設定在 hub 的 teams/<團隊>.json，專案用 "team" 指定；團隊的 required 跟 hub 的必裝一樣不能被排除。"""
    teams = config_teams(project)
    layers = [("hub", HUB_ROOT / "skillhub.json")]
    layers += [(f"team:{t}", TEAMS_DIR / f"{t}.json") for t in teams]
    layers += [
        ("user", Path(os.environ.get("SKILLHUB_USER_CONFIG", Path.home() / ".config" / "skillhub" / "config.json"))),
        ("project", project / ".skillhub.json"),
        ("local", project / ".skillhub.local.json"),
    ]
    cfg = {"always_on": True, "agents": ["claude"], "hooks": True, "plugins": [], "include": [], "exclude": [], "pin": {}, "mode": "copy", "sources": [],
           "agent_plugins": [], "exclude_agent_plugins": [], "teams": teams, "team_required": {}, "missing_teams": []}
    for name, p in layers:
        d = read_json(p)
        if name.startswith("team:"):
            if not p.exists():
                cfg["missing_teams"].append(name[5:])
                continue
            req = d.get("required", {})
            for n in req.get("skills", []):
                cfg["team_required"].setdefault(n, name)
            d = {"plugins": req.get("plugins", []) + d.get("recommended", {}).get("plugins", []),
                 "include": d.get("recommended", {}).get("skills", []),
                 "agent_plugins": [dict({"id": ap} if isinstance(ap, str) else ap, required=True, team=name[5:])
                                   for ap in req.get("agent_plugins", [])]}
            cfg.setdefault("team_plugins", {}).update({b: name for b in req.get("plugins", [])})
            for b in req.get("plugins", []):
                for n in read_json(HUB_ROOT / "plugins" / f"{b}.json").get("skills", []):
                    cfg["team_required"].setdefault(n, name)
        if not d:
            continue
        cfg["sources"].append(f"{name}:{p}")
        for k in ("plugins", "include", "exclude"):
            for v in d.get(k, []):
                if v not in cfg[k]:
                    cfg[k].append(v)
        for k in ("always_on", "mode", "agents", "hooks"):
            if k in d and not name.startswith("team:"):
                cfg[k] = d[k]
        cfg["pin"].update(d.get("pin", {}))
        for ap in d.get("agent_plugins", []):
            ap = {"id": ap} if isinstance(ap, str) else dict(ap)
            ap["required"] = bool(ap.get("required")) and (name == "hub" or name.startswith("team:"))  # 只有 hub 和團隊能設必裝
            if not any(x["id"] == ap["id"] for x in cfg["agent_plugins"]):
                cfg["agent_plugins"].append(ap)
        cfg["exclude_agent_plugins"] += d.get("exclude_agent_plugins", [])
        cfg.setdefault("adopted", {}).update(d.get("adopted", {}))
        if "auto_adopt" in d:
            cfg["auto_adopt"] = d["auto_adopt"]
    # 團隊必裝的組合包不能被排除：exclude 只能排除 skill，這裡確保組合包仍在
    for b in cfg.get("team_plugins", {}):
        if b not in cfg["plugins"]:
            cfg["plugins"].append(b)
    return cfg


def bundle_names(cfg: dict) -> list:
    """專案選的組合包，加上它們 extends 的組合包（去重、防循環）。"""
    out, todo = [], list(cfg["plugins"])
    while todo:
        b = todo.pop(0)
        if b in out:
            continue
        pf = HUB_ROOT / "plugins" / f"{b}.json"
        if not pf.exists():
            die(f"找不到組合包：{b}（plugins/{b}.json）")
        out.append(b)
        todo += read_json(pf).get("extends", [])
    return out


def all_bundles() -> dict:
    return {pf.stem: read_json(pf) for pf in sorted((HUB_ROOT / "plugins").glob("*.json"))}


def active_tools(cfg: dict) -> list:
    """選用組合包帶進來的外部工具（自己有安裝程式、會自己把 skill 裝到 agent 資料夾的那種）。"""
    out, seen = [], set()
    for b in bundle_names(cfg):
        for tl in read_json(HUB_ROOT / "plugins" / f"{b}.json").get("tools", []):
            if tl["id"] not in seen:
                seen.add(tl["id"])
                out.append(dict(tl, bundle=b))
    return out


def external_skill_names() -> set:
    """所有組合包裡的外部工具會安裝的 skill 名稱：這些不是自建 skill，不送 adopt。"""
    names = set()
    for b in all_bundles().values():
        for tl in b.get("tools", []):
            names |= set(tl.get("skills", []))
    for group in read_json(HUB_ROOT / "skillhub.json").get("known_tool_groups", {}).values():
        names |= set(group)
    return names


USER_SKILL_DIRS = ("~/.claude/skills", "~/.agents/skills", "~/.cursor/skills", "~/.codex/skills")


def skill_present(project: Path, name: str) -> bool:
    dirs = [project / d for d in SKILL_DIRS] + [Path(os.path.expanduser(d)) for d in USER_SKILL_DIRS]
    return any((d / name).exists() for d in dirs)


def tool_status(tl: dict, project: Path, agents) -> dict:
    cli_ok = True if not tl.get("check") else bool(shutil.which(tl["check"]))
    skills = tl.get("skills", [])
    skill_ok = not skills or skill_present(project, skills[0])
    return {"cli": cli_ok, "skill": skill_ok}


def group_conflicts(cfg: dict, project: Path) -> dict:
    """同一類工具（例如 code-graph）同時開了不只一個。"""
    groups = {}
    for tl in active_tools(cfg):
        if tl.get("group"):  # 用工具的主要 skill 名稱比對，才能和已安裝的 skill 對上
            groups.setdefault(tl["group"], set()).add((tl.get("skills") or [tl["id"]])[0])
    for g, members in read_json(HUB_ROOT / "skillhub.json").get("known_tool_groups", {}).items():
        for m in members:
            if skill_present(project, m):
                groups.setdefault(g, set()).add(m)
    return {g: sorted(m) for g, m in groups.items() if len(m) > 1}


def tool_commands(tl: dict, agents) -> list:
    ins = tl.get("install", {})
    cmds = []
    if ins.get("cli") and tl.get("check") and not shutil.which(tl["check"]):
        cmds.append(("cli", ins["cli"]))
    if ins.get("project"):
        cmds.append(("project", ins["project"]))
    cmds += [(a, ins[a]) for a in agents if a in ins]
    return cmds


def cmd_tools(a):
    project = Path(a.project).resolve()
    cfg = layered_config(project)
    agents = cfg["agents"]
    tools = active_tools(cfg)
    if not tools:
        print("這個專案選的組合包沒有外部工具。可用的：" + "、".join(
            f"{n}（{', '.join(x['id'] for x in b.get('tools', []))}）" for n, b in all_bundles().items() if b.get("tools")))
        return
    for tl in tools:
        st = tool_status(tl, project, agents)
        ok = st["cli"] and st["skill"]
        print(f"{'✓' if ok else '○'} {tl['id']}（組合包 {tl['bundle']}，{tl.get('license', '?')}）  {tl.get('homepage', '')}")
        if tl.get("warning"):
            print(f"    ! {tl['warning']}")
        if tl.get("usage"):
            print(f"    用法：{tl['usage']}")
        res = read_json(SCAN_RESULTS).get(f"tool:{tl['id']}")
        if res:
            acc = " 已接受" if res.get("accepted") else ""
            print(f"    安全掃描：{res.get('score')}/100 {res.get('recommendation')}{acc}（{res.get('scanned_at', '')[:10]}）")
        if ok and not a.run:
            continue
        if a.run and not ok and not scan_gate(f"tool:{tl['id']}", tl.get("scan", tl["homepage"]), recursive=True):
            print(f"    ✗ 安全掃描未通過，不安裝。確認誤判後：skillctl scan --accept tool:{tl['id']} --reason …")
            continue
        for kind, c in tool_commands(tl, agents):
            print(f"    {kind:<8} {c}")
            if a.run:
                r = subprocess.run(c.split("（")[0], shell=True, cwd=project)
                print(f"    {'✓' if r.returncode == 0 else '✗'} 結束碼 {r.returncode}")
    for g, members in group_conflicts(cfg, project).items():
        print(f"! {g} 類工具同時有 {', '.join(members)}，功能重疊，建議只留一個")
    if not a.run:
        print("\n要我直接執行上面的安裝指令：skillctl tools --run（會執行第三方安裝程式，請先確認來源）")


def bundle_agent_plugins(cfg: dict) -> list:
    """組合包（plugins/<名稱>.json）也可以帶外部 agent plugin，選了組合包才會啟用，屬於選用。"""
    out = []
    for pl in bundle_names(cfg):
        for ap in read_json(HUB_ROOT / "plugins" / f"{pl}.json").get("agent_plugins", []):
            ap = {"id": ap} if isinstance(ap, str) else dict(ap)
            ap["required"] = False
            ap["bundle"] = pl
            out.append(ap)
    return out


def plugin_conflicts(aps: list) -> list:
    """回傳 [(plugin, 另一個 plugin, 重疊說明)]：兩個功能重疊的 plugin 同時啟用時提醒。"""
    ids = {ap["id"] for ap in aps}
    out = []
    for ap in aps:
        for other, why in (ap.get("overlaps") or {}).items():
            if other in ids:
                out.append((ap["id"], other, why))
    return out


def wanted_agent_plugins(cfg: dict, warn: bool = False) -> list:
    out = []
    seen = {ap["id"] for ap in cfg["agent_plugins"]}
    extra = [ap for ap in bundle_agent_plugins(cfg) if ap["id"] not in seen]
    for ap in cfg["agent_plugins"] + extra:
        if ap["id"] in cfg["exclude_agent_plugins"]:
            if ap.get("required"):
                if warn:
                    print(f"! {ap['id']} 是{('團隊 ' + ap['team']) if ap.get('team') else ' hub '}規定的必裝 plugin，不能排除")
            else:
                continue
        out.append(ap)
    return out


def ensure_agent_plugins(project: Path, cfg: dict, dry_run: bool = False) -> list:
    """把 Claude Code plugin 寫進專案的 .claude/settings.json（enabledPlugins + extraKnownMarketplaces）。
    專案被信任後，Claude Code 會提示安裝並啟用；不會把 plugin 的檔案複製進專案。"""
    aps = wanted_agent_plugins(cfg, warn=True)
    wanted_skills = resolve_wanted(cfg)
    for a_id, b_id, why in plugin_conflicts(aps):
        router = next((ap.get("router") for ap in aps if ap["id"] == a_id), None)
        if router and router in wanted_skills:
            print(f"… {a_id.split('@')[0]} 與 {b_id.split('@')[0]} 功能重疊（{why}），已由 {router} 分工")
        else:
            print(f"! {a_id.split('@')[0]} 和 {b_id.split('@')[0]} 功能重疊（{why}），可能互搶觸發")
    if "claude" not in cfg["agents"]:
        return aps  # 只有 Claude Code 能用 settings.json 宣告 plugin；其他 agent 由 doctor 提示手動安裝
    settings_p = project / ".claude" / "settings.json"
    st = read_json(settings_p)
    before = json.dumps(st, sort_keys=True)
    enabled = st.setdefault("enabledPlugins", {})
    for ap in aps:
        enabled[ap["id"]] = True
        mk = ap.get("marketplace")
        if mk:
            st.setdefault("extraKnownMarketplaces", {})[mk["name"]] = {"source": {"source": "github", "repo": mk["repo"]}}
    if json.dumps(st, sort_keys=True) != before:
        for ap in aps:
            print(f"+ 啟用 Claude Code plugin {ap['id']}" + ("（必裝）" if ap.get("required") else ""))
        if not dry_run:
            write_json(settings_p, st)
    return aps


def reserved_names() -> dict:
    """agent plugin 已提供的 skill 名稱，hub 內不能重名，避免兩份指令互相打架。"""
    out = {}
    aps = list(read_json(HUB_ROOT / "skillhub.json").get("agent_plugins", []))
    for pf in sorted((HUB_ROOT / "plugins").glob("*.json")):  # 組合包裡的選用 plugin 也算
        aps += read_json(pf).get("agent_plugins", [])
    for ap in aps:
        if isinstance(ap, dict):
            for n in ap.get("provides", []):
                out.setdefault(n, ap["id"])
    for b in all_bundles().values():
        for tl in b.get("tools", []):
            for n in tl.get("skills", []):
                out.setdefault(n, tl["id"])
    return out


_WARNED: set = set()


def resolve_wanted(cfg: dict) -> dict:
    skills = all_skills()
    wanted = {}  # name -> 來源說明
    if cfg["always_on"]:
        for s in skills.values():
            if s["tier"] == "always-on":
                wanted[s["name"]] = "always-on"
    for pl in bundle_names(cfg):
        pf = HUB_ROOT / "plugins" / f"{pl}.json"
        for n in read_json(pf).get("skills", []):
            wanted.setdefault(n, f"plugin:{pl}")
    for n in cfg["include"]:
        wanted.setdefault(n, "include")
    for n in cfg["exclude"]:
        if n in cfg.get("team_required", {}):
            if n not in _WARNED:
                _WARNED.add(n)
                print(f"! {n} 是 {cfg['team_required'][n]} 的必裝 skill，不能排除")
            continue
        wanted.pop(n, None)
    for n, team in cfg.get("team_required", {}).items():
        wanted[n] = team
    for t in cfg.get("missing_teams", []):
        print(f"! 找不到團隊設定 teams/{t}.json")
    out = {}
    for n, src in wanted.items():
        if n not in skills:
            if n in cfg.get("adopted", {}):
                continue  # 收編 PR 還在審核，先用專案內的版本
            die(f"設定要求的 skill 不存在：{n}")
        if skills[n]["status"] == "retired":
            print(f"! {n} 已退場，略過")
            continue
        out[n] = src
    return out


# ---------------------------------------------------------------- 多 agent：Claude Code / Codex / Cursor
AGENTS = ("claude", "codex", "cursor")
# 各 agent 會讀的 skill 資料夾（依官方文件）：
#   Claude Code：.claude/skills
#   Codex      ：.agents/skills（從 cwd 往上找到 repo root）
#   Cursor     ：.agents/skills、.cursor/skills，也相容 .claude/skills、.codex/skills
SKILL_DIRS = (".agents/skills", ".claude/skills", ".cursor/skills", ".codex/skills")
LOCK_REL = ".skillhub/lock.json"
HOOK_REL = ".skillhub/hooks/skillhub_usage.py"


def layout(agents) -> tuple:
    """回傳 (實體資料夾, [symlink 資料夾])。每個 skill 只有一份實體檔案，其他 agent 用 symlink 指過去。
    - 只有 Claude（或 Claude + Cursor）：放 .claude/skills（Cursor 本來就讀得到）
    - 有 Codex、或沒有 Claude：放跨 agent 標準位置 .agents/skills；有 Claude 時再在 .claude/skills 建 symlink"""
    agents = set(agents or ["claude"])
    if "codex" in agents or "claude" not in agents:
        return ".agents/skills", ([".claude/skills"] if "claude" in agents else [])
    return ".claude/skills", []


def remove_path(d: Path):
    if d.is_symlink() or d.is_file():
        d.unlink()
    elif d.exists():
        shutil.rmtree(d)


def install_one(s: dict, project: Path, mode: str, agents=("claude",)):
    main_dir, links = layout(agents)
    dest = project / main_dir / s["name"]
    remove_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "link":
        dest.symlink_to(s["path"], target_is_directory=True)
    else:
        top = str(s["path"])
        shutil.copytree(s["path"], dest, ignore=lambda d, names: [n for n in names if d == top and (n in AUTHOR_ONLY or n.endswith(".draft"))])
    for ld in links:
        l = project / ld / s["name"]
        remove_path(l)
        l.parent.mkdir(parents=True, exist_ok=True)
        l.symlink_to(os.path.relpath(dest, l.parent), target_is_directory=True)


def uninstall_one(name: str, project: Path):
    for d in SKILL_DIRS:
        p = project / d / name
        # symlink 一律移除；實體資料夾只在 skillctl 會安裝的位置移除，不碰 .cursor/.codex 裡的自建同名 skill
        if p.is_symlink() or (p.exists() and d in (".agents/skills", ".claude/skills")):
            remove_path(p)


def find_local(project: Path, name: str):
    """回傳專案內這個名稱的實體 skill 資料夾（非 symlink）。"""
    for d in SKILL_DIRS:
        p = project / d / name
        if p.is_dir() and not p.is_symlink() and (p / "SKILL.md").exists():
            return p
    return None


PROJECTS_FILE = USAGE_LOG.parent / "projects.json"


def project_id(project: Path) -> str:
    """和 hook 一樣的專案代號：預設是路徑雜湊，設 SKILLHUB_PROJECT_NAMES=1 才用資料夾名稱。"""
    return project.name if os.environ.get("SKILLHUB_PROJECT_NAMES") else hashlib.sha256(str(project).encode()).hexdigest()[:10]


def register_project(project: Path):
    """記下這台電腦有哪些專案用 skill-hub，skillctl report 會讀它們的 lock 檔。"""
    try:
        reg = json.loads(PROJECTS_FILE.read_text(encoding="utf-8")) if PROJECTS_FILE.exists() else {}
        reg[str(project)] = {"id": project_id(project), "seen": dt.date.today().isoformat()}
        PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROJECTS_FILE.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def cmd_sync(a):
    project = Path(a.project).resolve()
    cfg = layered_config(project)
    wanted = resolve_wanted(cfg)
    skills = all_skills()
    lock_p = project / LOCK_REL
    lock = read_json(lock_p).get("skills", {})
    agents = cfg["agents"]
    main_dir, _ = layout(agents)
    if read_json(lock_p).get("layout") not in (None, main_dir):
        lock = {k: dict(v, hash="relayout") for k, v in lock.items()}  # agent 組合改變：全部重裝到新位置
        for n in lock:
            uninstall_one(n, project)
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
        local = find_local(project, n)
        if not prev and local:
            if dir_hash(local) == h:
                print(f"= {n} 已收編進 hub，改由 hub 管理")
                if not a.dry_run:
                    remove_path(local)  # 移除自建的那份，避免同一個 skill 在不同資料夾各一份
            else:
                print(f"! {n}：專案內有同名的自建 skill，內容和 hub 不同，保留專案版本（確認後刪掉它再 sync，或用 adopt 更新 hub）")
                continue
        if prev and prev["hash"] == h and (project / main_dir / n).exists():
            new_lock[n] = prev
            continue
        if not a.dry_run:
            install_one(s, project, cfg["mode"], agents)
        verb = "更新" if prev else "安裝"
        print(f"+ {verb} {n} v{s['version']}  ({src})")
        new_lock[n] = {"version": s["version"], "hash": h, "tier": s["tier"], "source": src}
    for n in lock:
        if n not in new_lock:
            print(f"- 移除 {n}")
            if not a.dry_run:
                uninstall_one(n, project)
    if not a.dry_run:
        write_json(lock_p, {"hub": str(HUB_ROOT), "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                            "agents": agents, "layout": main_dir, "teams": cfg["teams"], "skills": new_lock,
                            "external_skills": sorted(external_skill_names())})
        # 舊版 lock 位置
        old = project / ".claude" / "skills.lock.json"
        if old.exists():
            old.unlink()
    aps = ensure_agent_plugins(project, cfg, a.dry_run)
    if not a.dry_run:
        lock_data = read_json(lock_p)
        lock_data["agent_plugins"] = [x["id"] for x in aps]
        write_json(lock_p, lock_data)
    un = [x for x in unmanaged_skills(project, new_lock) if x not in cfg.get("adopted", {})]
    pend = [x for x in unmanaged_skills(project, new_lock) if x in cfg.get("adopted", {})]
    if pend:
        print(f"… 審核中，暫用專案內版本：{', '.join(pend)}")
    if un:
        print(f"! 專案內有 {len(un)} 個 hub 還沒有的 skill：{', '.join(un)}。執行 skillctl adopt 送進 hub 審核")
    missing_tools = [tl["id"] for tl in active_tools(cfg) if not all(tool_status(tl, project, agents).values())]
    if missing_tools:
        print(f"! 組合包需要的外部工具尚未安裝：{', '.join(missing_tools)}。執行 skillctl tools 查看安裝指令")
    for g, members in group_conflicts(cfg, project).items():
        print(f"! {g} 類工具同時有 {', '.join(members)}，建議只留一個")
    if not a.dry_run:
        register_hooks(project, agents, cfg)
        register_project(project)
    _, links = layout(agents)
    where = main_dir + (f"（{', '.join(links)} 以 symlink 指向）" if links else "")
    print(f"✓ {len(new_lock)} 個 skill 已同步到 {where}，agent：{', '.join(agents)}（設定來源：{', '.join(x.split(':')[0] for x in cfg['sources']) or '預設'}）")


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


# 每個 agent 的 hook 指令都指向同一支腳本，用 --agent 區分事件格式
HOOK_CMDS = {
    "claude": f'python3 "$CLAUDE_PROJECT_DIR/{HOOK_REL}" --agent claude',
    "codex": f'python3 "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/{HOOK_REL}" --agent codex',
    "cursor": f'python3 {HOOK_REL} --agent cursor',  # Cursor 專案層 hook 在專案根目錄執行
}
LEGACY_HOOK_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/skillhub_usage.py"'


def has_hook(entries, cmd) -> bool:
    for e in entries or []:
        for h in [e] + list(e.get("hooks", [])):
            if h.get("command") == cmd:
                return True
    return False


def _add_nested(hooks: dict, event: str, cmd: str, matcher=None, timeout=5):
    """Claude Code / Codex 格式：{"matcher":…, "hooks":[{"type":"command","command":…}]}"""
    entries = hooks.setdefault(event, [])
    for e in entries:  # 移除舊版路徑
        e["hooks"] = [h for h in e.get("hooks", []) if h.get("command") != LEGACY_HOOK_CMD]
    entries[:] = [e for e in entries if e.get("hooks")]
    if has_hook(entries, cmd):
        return
    e = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
    if matcher:
        e["matcher"] = matcher
    entries.append(e)


def register_hooks(project: Path, agents, cfg: dict):
    """在每個 agent 的設定檔註冊同一支使用量／自建 skill 偵測 hook。"""
    if not cfg.get("hooks", True):
        return
    src = HUB_ROOT / "hooks" / "skillhub_usage.py"
    dst = project / HOOK_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() or dst.read_bytes() != src.read_bytes():
        shutil.copy2(src, dst)
    legacy = project / ".claude" / "hooks" / "skillhub_usage.py"
    if legacy.exists():
        legacy.unlink()
    done = []
    if "claude" in agents:
        p = project / ".claude" / "settings.json"
        st = read_json(p)
        hk = st.setdefault("hooks", {})
        c = HOOK_CMDS["claude"]
        _add_nested(hk, "PostToolUse", c, "Skill")
        _add_nested(hk, "UserPromptSubmit", c)
        _add_nested(hk, "SessionEnd", c)
        write_json(p, st)
        done.append(".claude/settings.json")
    if "codex" in agents:
        p = project / ".codex" / "hooks.json"
        st = read_json(p)
        hk = st.setdefault("hooks", {})
        c = HOOK_CMDS["codex"]
        _add_nested(hk, "PostToolUse", c, ".*")
        _add_nested(hk, "UserPromptSubmit", c)
        _add_nested(hk, "SessionEnd", c, timeout=3)  # Codex 的 SessionEnd 上限 3 秒
        write_json(p, st)
        done.append(".codex/hooks.json")
    if "cursor" in agents and "claude" not in agents:
        # Cursor 會載入 Claude Code 的 hook；兩者都啟用時不重複註冊，避免同一次使用記兩筆
        p = project / ".cursor" / "hooks.json"
        st = read_json(p)
        st.setdefault("version", 1)
        hk = st.setdefault("hooks", {})
        c = HOOK_CMDS["cursor"]
        for ev in ("postToolUse", "beforeSubmitPrompt", "sessionEnd"):
            entries = hk.setdefault(ev, [])
            if not has_hook(entries, c):
                entries.append({"type": "command", "command": c, "timeout": 5})
        write_json(p, st)
        done.append(".cursor/hooks.json")
    return done


def cmd_init(a):
    project = Path(a.project).resolve()
    if a.team and not (TEAMS_DIR / f"{a.team}.json").exists():
        die(f"找不到團隊 {a.team}（hub 的 teams/{a.team}.json）。現有團隊：{', '.join(all_teams()) or '無'}")
    cfg_p = project / ".skillhub.json"
    if not cfg_p.exists():
        write_json(cfg_p, {"hub": str(HUB_ROOT), **({"team": a.team} if a.team else {}),
                           "agents": a.agents or ["claude"], "plugins": a.plugins or [], "include": [], "exclude": []})
        print(f"✓ 建立 {cfg_p.name}")
    elif a.team:
        pc = read_json(cfg_p)
        pc["team"] = a.team
        write_json(cfg_p, pc)
    # .gitignore：本機覆寫檔不進版控
    gi = project / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if ".skillhub.local.json" not in lines:
        gi.write_text("\n".join(lines + [".skillhub.local.json"]) + "\n", encoding="utf-8")
    if a.no_hook:
        pc = read_json(cfg_p)
        pc["hooks"] = False
        write_json(cfg_p, pc)
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
    # 精簡版成績會進版控，看板和 promote 都讀得到（last_result.json 含完整輸出，不進版控）
    write_json(s["path"] / "evals" / "score.json", {k: summary[k] for k in ("version", "pass_rate", "ran_at")}
               | ({"baseline_pass_rate": summary["baseline_pass_rate"]} if "baseline_pass_rate" in summary else {}))


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
    res = eval_result(s)
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


# ---------------------------------------------------------------- adopt：把專案內自建的 skill 收進 hub
def unmanaged_skills(project: Path, lock: dict | None = None) -> list:
    """專案 .claude/skills/ 裡不是由 hub 安裝的 skill。"""
    if lock is None:
        lock = read_json(project / LOCK_REL).get("skills", {})
    names = set()
    ext = external_skill_names()
    for rel in SKILL_DIRS:
        d = project / rel
        if d.is_dir():
            names |= {p.name for p in d.iterdir()
                      if p.is_dir() and not p.is_symlink() and (p / "SKILL.md").exists()
                      and p.name not in lock and p.name not in ext and not p.name.startswith("gitnexus")}
    return sorted(names)


def git_owner() -> str:
    r = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    return (r.stdout.strip() or os.environ.get("USER", "me")).replace(" ", "-").lower()


def normalize_skill_md(path: Path, name: str, owner: str):
    """補齊 frontmatter：name 與資料夾一致，metadata 帶 version/owner/tier/status。"""
    md = path / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    if fm is None:
        first = next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), name)
        text = f"---\nname: {name}\ndescription: {first}\n---\n\n{body}"
        fm, _ = parse_frontmatter(text)
    end = text.find("\n---", 3)
    head, rest = text[:end], text[end:]
    if fm.get("name") != name:
        head = re.sub(r"^name:.*$", f"name: {name}", head, count=1, flags=re.M) if "name" in fm else head + f"\nname: {name}"
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else None
    want = {"version": "0.1.0", "owner": owner, "tier": "domain", "status": "active"}
    if meta is None:
        head += "\nmetadata:" + "".join(f"\n  {k}: {v}" for k, v in want.items())
    else:
        for k, v in want.items():
            if k == "tier":
                head = re.sub(r"^(\s+)tier:.*$", r"\1tier: domain", head, count=1, flags=re.M)
            if k not in meta:
                head = re.sub(r"^metadata:\s*$", f"metadata:\n  {k}: {v}", head, count=1, flags=re.M)
    md.write_text(head + rest, encoding="utf-8")


def run(cmd, cwd, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} 失敗：{(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout.strip()


def cmd_adopt(a):
    project = Path(a.project).resolve()
    cfg_p = project / ".skillhub.json"
    pcfg = read_json(cfg_p)
    adopted = pcfg.setdefault("adopted", {})
    names = a.names or unmanaged_skills(project)
    names = [n for n in names if n not in adopted or a.again]
    if not names:
        print("沒有需要收編的 skill")
        return
    existing, reserved = all_skills(), reserved_names()
    owner = a.owner or git_owner()
    for n in names:
        src = find_local(project, n)
        if not src:
            print(f"✗ {n}：在 {', '.join(SKILL_DIRS)} 都找不到 {n}/SKILL.md")
            continue
        if not NAME_RE.match(n):
            print(f"✗ {n}：名稱只能用小寫英數與連字號，請先改資料夾名稱")
            continue
        if n in reserved:
            print(f"✗ {n}：和必裝 plugin {reserved[n]} 的 skill 同名，請先改名")
            continue
        if n in existing and not a.again:
            print(f"✗ {n}：hub 已有同名 skill（{existing[n]['tier']}），請改名或用 --again 送更新")
            continue
        if not scan_gate(f"skill:{n}", str(src)):
            print(f"✗ {n}：安全掃描未通過，沒有送審。看過結果後可用 skillctl scan --accept skill:{n} --reason … 再送")
            continue
        normalize_skill_md(src, n, owner)  # 專案內版本也補齊，合併後 sync 才認得是同一份
        if a.no_pr:
            dest = HUB_ROOT / "skills" / "domain" / n
            _stage_into(src, dest, n, project)
            print(f"✓ {n} 已複製到 {dest.relative_to(HUB_ROOT)}（未開 PR）")
            url = "local"
        else:
            try:
                url = _adopt_pr(src, n, project)
            except RuntimeError as e:
                print(f"✗ {n}：{e}")
                continue
            print(f"✓ {n} 已送審：{url}")
        adopted[n] = url
        if n not in pcfg.setdefault("include", []):
            pcfg["include"].append(n)
    write_json(cfg_p, pcfg)


def _stage_into(src: Path, dest: Path, name: str, project: Path):
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    s = load_skill(dest)
    if not (dest / "spec.md").exists():
        spec = (HUB_ROOT / "templates" / "skill" / "spec.md").read_text(encoding="utf-8")
        spec = spec.replace("{{name}}", name).replace("{{owner}}", s["owner"]).replace("{{tier}}", "domain")
        spec = spec.replace("{{date}}", dt.date.today().isoformat())
        spec = spec.replace("這個 skill 要解決什麼問題？沒有它時 agent 會怎麼做錯？",
                            f"（由專案 {project.name} 收編，請補完）\n\n{s['description']}")
        (dest / "spec.md").write_text(spec, encoding="utf-8")
    if not (dest / "CHANGELOG.md").exists():
        (dest / "CHANGELOG.md").write_text(f"# Changelog\n\n## {s['version']} — {dt.date.today().isoformat()}\n"
                                           f"- 從專案 {project.name} 收編\n", encoding="utf-8")


def _adopt_pr(src: Path, name: str, project: Path) -> str:
    """在暫存 worktree 從 origin 預設分支開 adopt/<name> 分支、推上去並開 PR，不動你目前的 hub checkout。"""
    if not shutil.which("gh"):
        raise RuntimeError("需要 GitHub CLI（gh）並先 gh auth login；或改用 --no-pr")
    run(["git", "fetch", "-q", "origin"], HUB_ROOT)
    base = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], HUB_ROOT, check=False) or "origin/main"
    branch = f"adopt/{name}"
    wt = Path(tempfile.mkdtemp(prefix="skillhub-adopt-"))
    try:
        run(["git", "worktree", "add", "-q", "-B", branch, str(wt), base], HUB_ROOT)
        _stage_into(src, wt / "skills" / "domain" / name, name, project)
        run(["git", "add", "-A", "skills/domain/" + name], wt)
        run(["git", "commit", "-q", "-m", f"Adopt {name} from {project.name} into domain tier"], wt)
        run(["git", "push", "-q", "-f", "-u", "origin", branch], wt)
        body = (f"從專案 `{project.name}` 收編的 skill，放在 `skills/domain/{name}`。\n\n"
                "- [ ] 補完 spec.md\n- [ ] 補 evals/cases.jsonl（至少 golden 1、real 3）\n\n"
                "由 `skillctl adopt` 建立。domain 層只改動 `skills/domain/` 時，CI 通過後會自動合併。")
        existing = run(["gh", "pr", "list", "--head", branch, "--json", "url", "-q", ".[0].url"], wt, check=False)
        url = existing or run(["gh", "pr", "create", "--base", base.split("/", 1)[1], "--head", branch,
                               "--title", f"Adopt skill: {name}", "--body", body], wt)
        return url
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], HUB_ROOT, check=False)


# ---------------------------------------------------------------- scan：NVIDIA SkillSpector 或 Cisco skill-scanner
SCAN_RESULTS = HUB_ROOT / "security" / "scan-results.json"


SCANNERS = {"skillspector": "skillspector", "cisco": "skill-scanner"}
SEV_SCORE = {"SAFE": 0, "INFO": 0, "LOW": 20, "MEDIUM": 40, "HIGH": 70, "CRITICAL": 90}
SEV_ORDER = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


def install_hint(sc: dict) -> str:
    return sc["cisco"]["install"] if sc["scanner"] == "cisco" else sc["install"]


def sec_cfg() -> dict:
    d = {"scanner": "skillspector", "max_score": 50, "block": ["DO_NOT_INSTALL"], "llm": False,
         "baseline": ".skillspector-baseline.yaml",
         "install": "uv tool install --python 3.13 git+https://github.com/NVIDIA/skillspector.git",
         "cisco": {"install": "uv tool install cisco-ai-skill-scanner", "policy": "low-noise",
                   "fail_on_severity": "high"}}
    user = read_json(HUB_ROOT / "skillhub.json").get("security", {})
    d.update({k: v for k, v in user.items() if k != "cisco"})
    d["cisco"].update(user.get("cisco", {}))
    if os.environ.get("SKILLHUB_SCANNER"):
        d["scanner"] = os.environ["SKILLHUB_SCANNER"]
    if d["scanner"] not in SCANNERS:
        die(f"security.scanner 只能是 {' / '.join(SCANNERS)}，目前是 {d['scanner']}")
    return d


def _local_target(target: str, tmp: Path) -> str:
    """遠端 repo 先淺層 clone 到暫存資料夾再掃（GitHub 的 /tree/<branch>/<子目錄> 也支援）。"""
    if not re.match(r"^(https?://|git@)", target):
        return target
    m = re.match(r"^(https://github\.com/[^/]+/[^/]+)/tree/([^/]+)/?(.*)$", target)
    repo, branch, sub = (m.group(1), m.group(2), m.group(3)) if m else (target, None, "")
    dest = tmp / "src"
    cmd = ["git", "clone", "-q", "--depth", "1"] + (["--branch", branch] if branch else []) + [repo, str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError(f"clone 失敗：{r.stderr.strip()[-200:]}")
    return str(dest / sub) if sub else str(dest)


def _skills_root(local: str) -> str:
    """SkillSpector 的 --recursive 只看下一層；找出直接子資料夾含最多 SKILL.md 的那一層。"""
    best, n_best = local, 0
    for d, dirs, _ in os.walk(local):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "node_modules"]
        if Path(d).relative_to(local).parts.__len__() > 4:
            dirs[:] = []
            continue
        n = sum(1 for x in dirs if (Path(d) / x / "SKILL.md").exists())
        if n > n_best:
            best, n_best = d, n
    return best


def _run_cisco(exe: str, target: str, sc: dict, llm: bool, recursive: bool) -> dict:
    cc = sc["cisco"]
    with tempfile.TemporaryDirectory() as td:
        try:
            local = _local_target(target, Path(td))
        except RuntimeError as e:
            return {"error": str(e), "blocked": True}
        multi = recursive or not (Path(local) / "SKILL.md").exists()
        cmd = [exe, "scan-all" if multi else "scan", local, "--format", "json", "--policy", cc["policy"], "--lenient"]
        if multi:
            cmd.append("--recursive")
        if llm:
            cmd.append("--use-llm")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": (r.stderr or r.stdout).strip()[-300:] or f"exit {r.returncode}", "blocked": True}
    results = d.get("results", [d]) if multi else [d]
    findings = [f for res in results for f in res.get("findings", [])]
    sevs = [str(f.get("severity", "INFO")).upper() for f in findings]
    top = max(sevs, key=lambda x: SEV_ORDER.index(x) if x in SEV_ORDER else 0, default="SAFE")
    fail = SEV_ORDER.index(cc["fail_on_severity"].upper())
    hit = any(SEV_ORDER.index(x) >= fail for x in sevs if x in SEV_ORDER)
    rec = "DO_NOT_INSTALL" if hit else ("CAUTION" if top == "MEDIUM" else "SAFE")
    issues = [{"category": f.get("category"), "severity": f.get("severity"), "file": f.get("file_path"),
               "line": f.get("line_number")} for f in findings if str(f.get("severity")).upper() != "INFO"]
    issues.sort(key=lambda i: -SEV_ORDER.index(str(i["severity"]).upper()) if str(i["severity"]).upper() in SEV_ORDER else 0)
    return {"score": SEV_SCORE.get(top, 0), "severity": top, "recommendation": rec, "issues": issues,
            "complete": True, "version": f"cisco {cc['policy']}", "blocked": rec in sc["block"]}


def run_scan(target: str, llm: bool | None = None, recursive: bool = False) -> dict | None:
    """回傳 {score, severity, recommendation, issues:[…], blocked}；沒裝掃描器回傳 None。
    兩種後端（security.scanner）輸出統一成同一格式：skillspector（預設）或 cisco。"""
    sc = sec_cfg()
    exe = shutil.which(SCANNERS[sc["scanner"]])
    if not exe:
        return None
    use_llm = sc["llm"] if llm is None else llm
    if sc["scanner"] == "cisco":
        return _run_cisco(exe, target, sc, use_llm, recursive)
    with tempfile.TemporaryDirectory() as td:
        try:
            local = _local_target(target, Path(td))
        except RuntimeError as e:
            return {"error": str(e), "blocked": True}
        multi = recursive and not (Path(local) / "SKILL.md").exists()
        if multi:
            local = _skills_root(local)
        cmd = [exe, "scan", local, "--format", "json"]
        if not use_llm:
            cmd.append("--no-llm")
        if multi:
            cmd += ["--recursive", "--max-skills", "50"]
        bl = HUB_ROOT / sc["baseline"]
        if bl.exists():
            cmd += ["--baseline", str(bl)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": (r.stderr or r.stdout).strip()[-300:] or f"exit {r.returncode}", "blocked": True}
    if d.get("multi_skill"):  # --recursive：每個子 skill 各自一份結果，取最高分
        ra = {"score": d.get("max_risk_score", 0), "severity": d.get("risk_severity"),
              "recommendation": d.get("risk_recommendation")}
        raw = [(sk.get("name", "") + "/", i) for sk in d.get("skills", []) for i in sk.get("issues", [])]
    else:
        ra = d.get("risk_assessment", {})
        raw = [("", i) for i in d.get("issues", [])]
    issues = [{"category": i.get("category"), "severity": i.get("severity"),
               "file": pre + str((i.get("location") or {}).get("file") or ""), "line": (i.get("location") or {}).get("start_line")}
              for pre, i in raw]
    issues.sort(key=lambda i: -SEV_ORDER.index(str(i["severity"]).upper()) if str(i["severity"]).upper() in SEV_ORDER else 0)
    score = ra.get("score", 0)
    return {"score": score, "severity": ra.get("severity"), "recommendation": ra.get("recommendation"),
            "issues": issues, "complete": (d.get("analysis_completeness") or {}).get("is_complete"),
            "version": (d.get("metadata") or {}).get("skillspector_version"),
            "blocked": score > sc["max_score"] or ra.get("recommendation") in sc["block"]}


def scan_target(spec: str) -> list:
    """把 skill 名稱 / 路徑 / URL / tool:<id> / bundle:<名稱> 轉成 [(結果 key, 掃描目標, 是否遞迴)]。"""
    if spec.startswith("bundle:"):
        b = read_json(HUB_ROOT / "plugins" / f"{spec[7:]}.json")
        if not b:
            die(f"找不到組合包：{spec[7:]}")
        out = [(f"skill:{n}", str(all_skills()[n]["path"]), False) for n in b.get("skills", []) if n in all_skills()]
        out += [(f"tool:{tl['id']}", tl.get("scan", tl["homepage"]), True) for tl in b.get("tools", []) if tl.get("homepage")]
        for ap in b.get("agent_plugins", []):
            if isinstance(ap, dict) and ap.get("homepage"):
                out.append((f"plugin:{ap['id']}", ap["homepage"], True))
        return out
    if spec.startswith("tool:"):
        for b in all_bundles().values():
            for tl in b.get("tools", []):
                if tl["id"] == spec[5:]:
                    return [(spec, tl.get("scan", tl["homepage"]), True)]  # scan：只掃實際會裝的部分
        die(f"找不到工具：{spec[5:]}")
    if spec.startswith("plugin:"):
        pid = spec[7:]
        aps = list(read_json(HUB_ROOT / "skillhub.json").get("agent_plugins", []))
        aps += [ap for b in all_bundles().values() for ap in b.get("agent_plugins", [])]
        for ap in aps:
            if isinstance(ap, dict) and ap.get("id") == pid and ap.get("homepage"):
                return [(spec, ap["homepage"], True)]
        die(f"找不到 plugin 或它沒有 homepage：{pid}")
    if spec in all_skills():
        return [(f"skill:{spec}", str(all_skills()[spec]["path"]), False)]
    return [(f"path:{spec}", spec, not spec.endswith("SKILL.md"))]


def _rel(target: str) -> str:
    try:
        return str(Path(target).resolve().relative_to(HUB_ROOT))
    except (ValueError, OSError):
        return target


def save_scan(key: str, target: str, res: dict):
    data = read_json(SCAN_RESULTS)
    prev = data.get(key, {})
    scanner = sec_cfg()["scanner"]
    same = prev.get("score") == res.get("score") and prev.get("scanner", "skillspector") == scanner
    data[key] = {**{k: res.get(k) for k in ("score", "severity", "recommendation", "blocked", "complete", "version", "error")},
                 "scanner": scanner,
                 "target": _rel(target), "issues": res.get("issues", [])[:20],
                 "scanned_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "accepted": prev.get("accepted") if same else None}
    write_json(SCAN_RESULTS, data)


def print_scan(key: str, res: dict):
    if res.get("error"):
        print(f"✗ {key}：掃描失敗（{res['error']}）")
        return
    mark = "✗" if res["blocked"] else ("!" if res["recommendation"] == "CAUTION" else "✓")
    print(f"{mark} {key}：風險 {res['score']}/100（{res['severity']}，{res['recommendation']}），{len(res['issues'])} 個發現")
    for i in res["issues"][:8]:
        print(f"    - [{i['severity']}] {i['category']}  {i['file'] or ''}{':' + str(i['line']) if i['line'] else ''}")


def cmd_scan(a):
    if a.scanner:
        os.environ["SKILLHUB_SCANNER"] = a.scanner
    sc = sec_cfg()
    if a.accept:
        data = read_json(SCAN_RESULTS)
        if a.accept not in data:
            die(f"{a.accept} 沒有掃描紀錄，先執行 skillctl scan")
        if not a.reason:
            die("接受風險請用 --reason 說明理由（會寫進 security/scan-results.json，PR 審查看得到）")
        data[a.accept]["accepted"] = {"reason": a.reason, "by": git_owner(), "score": data[a.accept].get("score"),
                                      "at": dt.date.today().isoformat()}
        write_json(SCAN_RESULTS, data)
        print(f"✓ 已接受 {a.accept} 的掃描結果：{a.reason}（分數改變後需重新接受）")
        return
    if not shutil.which(SCANNERS[sc["scanner"]]):
        die(f"找不到 {SCANNERS[sc['scanner']]}。安裝：{install_hint(sc)}")
    targets = []
    for spec in a.targets or [f"skill:{n}" for n in all_skills()]:
        targets += scan_target(spec.replace("skill:", "", 1) if spec.startswith("skill:") else spec)
    blocked = 0
    for key, target, rec in targets:
        res = run_scan(target, llm=a.llm or None, recursive=rec)
        print_scan(key, res)
        save_scan(key, target, res)
        acc = read_json(SCAN_RESULTS).get(key, {}).get("accepted")
        if res.get("blocked") and not acc:
            blocked += 1
    if blocked:
        limit = (f"嚴重度 ≥ {sc['cisco']['fail_on_severity'].upper()}" if sc["scanner"] == "cisco"
                 else f"分數 > {sc['max_score']} 或 {'/'.join(sc['block'])}")
        print(f"\n{blocked} 個目標超過門檻（{limit}）。確認是誤判可用：skillctl scan --accept <key> --reason \"…\"")
    sys.exit(1 if blocked else 0)


def scan_gate(key: str, target: str, recursive: bool = False, quiet: bool = False) -> bool:
    """安裝或送審前的關卡：沒裝掃描器時放行（CI 會再擋），被擋且未接受時回傳 False。"""
    res = run_scan(target, recursive=recursive)
    if res is None:
        if not quiet:
            sc = sec_cfg()
            print(f"! 沒有安裝 {SCANNERS[sc['scanner']]}，略過 {key} 的安全掃描（{install_hint(sc)}）")
        return True
    save_scan(key, target, res)
    if not quiet:
        print_scan(key, res)
    acc = read_json(SCAN_RESULTS).get(key, {}).get("accepted")
    return not res.get("blocked") or bool(acc)


# ---------------------------------------------------------------- doctor
def cmd_doctor(a):
    project = Path(a.project).resolve()
    cfg = layered_config(project)
    st = read_json(project / ".claude" / "settings.json")
    lock = read_json(project / LOCK_REL)
    agents = cfg["agents"]
    main_dir, links = layout(agents)
    skills = all_skills()
    checks = [((project / ".skillhub.json").exists(), "有 .skillhub.json（已執行 skillctl init）")]
    checks.append((bool(lock), f"有 {LOCK_REL}"))
    stale = [n for n, v in lock.get("skills", {}).items() if n in skills and dir_hash(skills[n]["path"]) != v["hash"]]
    checks.append((not stale, "已安裝的 skill 與 hub 一致" + (f"（過期：{', '.join(stale)}，請執行 skillctl sync）" if stale else "")))
    missing = [n for n in resolve_wanted(cfg) if not (project / main_dir / n).exists()]
    for ld in links:
        missing += [f"{ld}/{n}" for n in resolve_wanted(cfg) if not (project / ld / n).is_symlink()]
    checks.append((not missing, "設定要求的 skill 都已安裝" + (f"（缺：{', '.join(missing)}）" if missing else "")))
    for t in cfg.get("missing_teams", []):
        checks.append((False, f"團隊設定 teams/{t}.json 存在（hub 裡沒有這個團隊）"))
    for t in cfg.get("teams", []):
        if t in cfg.get("missing_teams", []):
            continue
        req = [n for n, src in cfg["team_required"].items() if src == f"team:{t}"]
        miss = [n for n in req if not (project / main_dir / n).exists()]
        reqp = [ap["id"] for ap in cfg["agent_plugins"] if ap.get("team") == t]
        checks.append((not miss, f"團隊 {t} 的必裝 skill 都已安裝（{len(req)} 個" + (f"，缺：{', '.join(miss)}" if miss else "")
                       + (f"；必裝 plugin：{', '.join(reqp)}" if reqp else "") + "）"))
    adopted = cfg.get("adopted", {})
    un = [n for n in unmanaged_skills(project) if n not in adopted]
    checks.append((not un, "專案內沒有未送進 hub 的自建 skill" + (f"（{', '.join(un)}，執行 skillctl adopt）" if un else "")))
    pending = [f"{n} {u}" for n, u in adopted.items() if n not in skills]
    if pending:
        print("審核中：" + "；".join(pending))
    if cfg.get("hooks", True):
        checks.append(((project / HOOK_REL).exists(), f"hook 腳本存在（{HOOK_REL}）"))
        if "claude" in agents:
            hk = st.get("hooks", {})
            c = HOOK_CMDS["claude"]
            checks.append((all(has_hook(hk.get(e), c) for e in ("PostToolUse", "UserPromptSubmit", "SessionEnd")),
                           "Claude Code hook 已註冊（.claude/settings.json）"))
        if "codex" in agents:
            hk = read_json(project / ".codex" / "hooks.json").get("hooks", {})
            c = HOOK_CMDS["codex"]
            checks.append((all(has_hook(hk.get(e), c) for e in ("PostToolUse", "UserPromptSubmit", "SessionEnd")),
                           "Codex hook 已註冊（.codex/hooks.json，第一次需在 Codex 用 /hooks 信任）"))
        if "cursor" in agents and "claude" not in agents:
            hk = read_json(project / ".cursor" / "hooks.json").get("hooks", {})
            c = HOOK_CMDS["cursor"]
            checks.append((all(has_hook(hk.get(e), c) for e in ("postToolUse", "beforeSubmitPrompt", "sessionEnd")),
                           "Cursor hook 已註冊（.cursor/hooks.json）"))
    others = [x for x in agents if x != "claude"]
    for ap in wanted_agent_plugins(cfg):
        if "claude" in agents:
            on = st.get("enabledPlugins", {}).get(ap["id"]) is True
            checks.append((on, f"Claude Code plugin {ap['id']} 已在 settings.json 啟用" + ("（必裝）" if ap.get("required") else "")))
        if others and (ap.get("required") or ap.get("bundle")):
            home = ap.get("homepage") or (f"https://github.com/{ap['marketplace']['repo']}" if ap.get("marketplace") else "")
            how = "；".join(f"{x}：{ap['install'][x]}" for x in others if x in (ap.get("install") or {}))
            print(f"提醒：{ap['id'].split('@')[0]} 在 {', '.join(others)} 需要手動安裝一次（每台電腦一次）"
                  + (f"，{how}" if how else "") + f"，見 {home}")
    aps_all = wanted_agent_plugins(cfg)
    for a_id, b_id, why in plugin_conflicts(aps_all):
        router = next((ap.get("router") for ap in aps_all if ap["id"] == a_id), None)
        ok = bool(router) and (project / main_dir / router).exists()
        checks.append((ok, f"{a_id.split('@')[0]} 與 {b_id.split('@')[0]} 的重疊（{why}）已由 {router or '路由 skill'} 分工"))
    for tl in active_tools(cfg):
        stt = tool_status(tl, project, agents)
        if tl.get("check"):
            checks.append((stt["cli"], f"外部工具 {tl['id']} 已安裝（指令 {tl['check']}）"))
        if tl.get("skills"):
            checks.append((stt["skill"], f"{tl['id']} 的 skill 已裝到 agent（{tl['skills'][0]}）"))
        if tl.get("noncommercial"):
            print(f"注意：{tl['id']} 是非商業授權（{tl['license']}），商業專案請移除組合包 {tl['bundle']}")
    for g, members in group_conflicts(cfg, project).items():
        checks.append((False, f"{g} 類工具只開一個（目前 {', '.join(members)}）"))
    scans = read_json(SCAN_RESULTS)
    ext_keys = [f"tool:{tl['id']}" for tl in active_tools(cfg)]
    ext_keys += [f"plugin:{ap['id']}" for ap in wanted_agent_plugins(cfg) if ap.get("homepage")]
    for k in ext_keys:
        r = scans.get(k)
        if not r:
            checks.append((False, f"{k} 已做安全掃描（尚未掃描：skillctl scan {k}）"))
        else:
            ok = not r.get("blocked") or bool(r.get("accepted"))
            note = "，已接受風險：" + r["accepted"]["reason"] if r.get("blocked") and r.get("accepted") else ""
            checks.append((ok, f"{k} 安全掃描 {r.get('score')}/100 {r.get('recommendation')}{note}"))
    if active_tools(cfg) and any(not ok for ok, m in checks if "外部工具" in m or "的 skill 已裝" in m):
        print("提示：skillctl tools 會列出缺少的安裝指令")
    for ok, msg in checks:
        print(f"{'✓' if ok else '✗'} {msg}")
    bad = sum(not ok for ok, _ in checks)
    if "claude" in agents and any(ap.get("required") for ap in wanted_agent_plugins(cfg)):
        print("\n提醒：settings.json 只負責宣告。第一次在專案開 Claude Code 時要同意安裝 plugin，可用 /plugin 確認已安裝。")
    print(f"agent：{', '.join(agents)}；skill 實體放在 {main_dir}" + (f"，{', '.join(links)} 為 symlink" if links else ""))
    print(f"\n{len(checks) - bad}/{len(checks)} 項通過")
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------- teams / report / dashboard：團隊必裝與使用看板
TELEMETRY_DIR = HUB_ROOT / "telemetry"


def cmd_teams(a):
    teams = all_teams()
    if not teams:
        print("hub 還沒有團隊設定。新增 teams/<團隊>.json，格式見 README「團隊必裝」。")
        return
    for t, d in teams.items():
        req = d.get("required", {})
        print(f"{t}：{d.get('description', '')}（負責人 {', '.join(d.get('owners', [])) or '未設定'}）")
        for k, label in (("skills", "必裝 skill"), ("plugins", "必裝組合包"), ("agent_plugins", "必裝 plugin")):
            if req.get(k):
                print(f"  {label}：{', '.join(x if isinstance(x, str) else x['id'] for x in req[k])}")
        rec = d.get("recommended", {})
        if rec.get("skills") or rec.get("plugins"):
            print(f"  建議：{', '.join(rec.get('skills', []) + ['plugin:' + b for b in rec.get('plugins', [])])}")


def reporter_id() -> str:
    who = run(["git", "config", "user.email"], HUB_ROOT, check=False) or os.environ.get("USER", "unknown")
    return hashlib.sha256(who.encode()).hexdigest()[:10]


def build_report(days: int) -> dict:
    """本機的匿名彙總：各專案裝了什麼（讀 lock 檔），以及每天每個 skill 用了幾次。不含 prompt 或檔案內容。"""
    projects = []
    reg = read_json(PROJECTS_FILE)
    for path, info in reg.items():
        lock = read_json(Path(path) / LOCK_REL)
        if not lock:
            continue
        cfg = layered_config(Path(path))
        main_dir, _ = layout(lock.get("agents", ["claude"]))
        miss = [n for n in cfg.get("team_required", {}) if not (Path(path) / main_dir / n).exists()]
        projects.append({"id": info.get("id") or project_id(Path(path)), "teams": lock.get("teams", []),
                         "agents": lock.get("agents", []), "synced_at": lock.get("synced_at"),
                         "skills": {n: v.get("version") for n, v in lock.get("skills", {}).items()},
                         "external_skills": lock.get("external_skills", []),
                         "agent_plugins": lock.get("agent_plugins", []), "missing_required": miss})
    agg = {}
    for r in read_usage(days):
        if r.get("event") != "skill_invoked":
            continue
        k = (r["skill"], r.get("version"), r.get("team"), r.get("project"), r.get("agent"), r.get("source"), r["ts"][:10])
        g = agg.setdefault(k, {"count": 0, "errors": 0, "sessions": set()})
        g["count"] += 1
        g["errors"] += 1 if r.get("error") else 0
        g["sessions"].add(r.get("session"))
    usage = [{"skill": k[0], "version": k[1], "team": k[2], "project": k[3], "agent": k[4], "source": k[5], "day": k[6],
              "count": g["count"], "errors": g["errors"], "sessions": len(g["sessions"])} for k, g in sorted(agg.items(), key=lambda x: x[0][6])]
    return {"schema": 1, "reporter": reporter_id(), "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "days": days, "projects": projects, "usage": usage}


def _pr_with_files(branch: str, files: dict, title: str, body: str) -> str:
    """在暫存 worktree 從 origin 預設分支開分支、寫入檔案、推上去並開 PR，不動目前的 hub checkout。"""
    if not shutil.which("gh"):
        raise RuntimeError("需要 GitHub CLI（gh）並先 gh auth login")
    run(["git", "fetch", "-q", "origin"], HUB_ROOT)
    base = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], HUB_ROOT, check=False) or "origin/main"
    wt = Path(tempfile.mkdtemp(prefix="skillhub-pr-"))
    try:
        run(["git", "worktree", "add", "-q", "-B", branch, str(wt), base], HUB_ROOT)
        for rel, content in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(content, encoding="utf-8")
        run(["git", "add", "-A", *files], wt)
        if not run(["git", "status", "--porcelain"], wt):
            return "（沒有變更）"
        run(["git", "commit", "-q", "-m", title], wt)
        run(["git", "push", "-q", "-f", "-u", "origin", branch], wt)
        existing = run(["gh", "pr", "list", "--head", branch, "--json", "url", "-q", ".[0].url"], wt, check=False)
        return existing or run(["gh", "pr", "create", "--base", base.split("/", 1)[1], "--head", branch,
                                "--title", title, "--body", body], wt)
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], HUB_ROOT, check=False)


def cmd_report(a):
    rep = build_report(a.days)
    rel = f"telemetry/{rep['reporter']}.json"
    text = json.dumps(rep, ensure_ascii=False, indent=1) + "\n"
    n_use = sum(u["count"] for u in rep["usage"])
    print(f"彙總：{len(rep['projects'])} 個專案、{a.days} 天內 {n_use} 次 skill 使用（匿名代號 {rep['reporter']}）")
    if a.pr:
        try:
            url = _pr_with_files(f"telemetry/{rep['reporter']}", {rel: text}, f"Telemetry report {rep['reporter']}",
                                 "由 `skillctl report --pr` 建立的匿名使用彙總（不含 prompt 或檔案內容）。"
                                 "只改動 `telemetry/` 時 CI 會自動合併，看板會在合併後更新。")
            print(f"✓ 已送出：{url}")
        except RuntimeError as e:
            die(str(e))
        return
    out = Path(a.out) if a.out else HUB_ROOT / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"✓ 寫入 {out}（commit 後看板會用到；或改用 --pr 自動開 PR）")


def collect_dashboard(days: int, local: bool) -> dict:
    skills = all_skills()
    scans = read_json(SCAN_RESULTS)
    reports = [read_json(p) for p in sorted(TELEMETRY_DIR.glob("*.json"))] if TELEMETRY_DIR.exists() else []
    if local:
        mine = build_report(days)
        reports = [r for r in reports if r.get("reporter") != mine["reporter"]] + [mine]
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    usage = [u for r in reports for u in r.get("usage", []) if u.get("day", "") >= cutoff]
    projects = {}
    for r in reports:
        for p in r.get("projects", []):
            projects[p["id"]] = p  # 同一專案多人回報時取最後一份
    teams = all_teams()
    sk = []
    for n, s in sorted(skills.items()):
        us = [u for u in usage if u["skill"] == n]
        ev = eval_result(s)
        sc = scans.get(f"skill:{n}", {})
        fb = s["path"] / "feedback.jsonl"
        sk.append({"name": n, "tier": s["tier"], "status": s["status"], "version": s["version"], "owner": s.get("owner"),
                   "description": s["description"][:240],
                   "uses": sum(u["count"] for u in us), "errors": sum(u["errors"] for u in us),
                   "projects": len({u["project"] for u in us}), "teams": sorted({t for u in us for t in (u.get("team") or "").split(",") if t}),
                   "installed_in": sum(1 for p in projects.values() if n in p.get("skills", {})),
                   "last_used": max((u["day"] for u in us), default=None),
                   "daily": {d: sum(u["count"] for u in us if u["day"] == d) for d in sorted({u["day"] for u in us})},
                   "eval": ev.get("pass_rate"), "eval_version": ev.get("version"), "baseline": ev.get("baseline_pass_rate"),
                   "scan": {k: sc.get(k) for k in ("score", "recommendation", "blocked", "scanner")} | {"accepted": bool(sc.get("accepted"))} if sc else None,
                   "papercuts": len(fb.read_text(encoding="utf-8").splitlines()) if fb.exists() else 0,
                   "required_by": sorted(t for t, d in teams.items() if n in d.get("required", {}).get("skills", [])
                                         or any(n in read_json(HUB_ROOT / "plugins" / f"{b}.json").get("skills", [])
                                                for b in d.get("required", {}).get("plugins", [])))})
    tm = []
    for t, d in teams.items():
        ps = [p for p in projects.values() if t in p.get("teams", [])]
        tm.append({"name": t, "description": d.get("description", ""), "owners": d.get("owners", []),
                   "required": d.get("required", {}), "recommended": d.get("recommended", {}),
                   "projects": len(ps), "compliant": sum(1 for p in ps if not p.get("missing_required")),
                   "uses": sum(u["count"] for u in usage if t in (u.get("team") or "").split(","))})
    hub = read_json(HUB_ROOT / "skillhub.json")
    ext = [{"key": k, **{x: v.get(x) for x in ("score", "recommendation", "blocked", "scanner", "scanned_at")},
            "accepted": (v.get("accepted") or {}).get("reason")} for k, v in scans.items() if not k.startswith("skill:")]
    commit = run(["git", "rev-parse", "--short", "HEAD"], HUB_ROOT, check=False)
    return {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "days": days, "commit": commit,
            "reporters": len(reports), "skills": sk, "teams": tm, "projects": list(projects.values()),
            "bundles": [{"name": b, "tier": d.get("tier", "domain"), "description": d.get("description", ""),
                         "skills": d.get("skills", []), "tools": [tl["id"] for tl in d.get("tools", [])],
                         "extends": d.get("extends", [])} for b, d in all_bundles().items()],
            "required_plugins": [ap if isinstance(ap, str) else ap["id"] for ap in hub.get("agent_plugins", [])
                                 if isinstance(ap, str) or ap.get("required")],
            "external_scans": ext, "promotion": hub.get("promotion", {})}


def cmd_dashboard(a):
    data = collect_dashboard(a.days, a.local)
    tpl = (HUB_ROOT / "tools" / "dashboard.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tpl.replace("/*__DATA__*/null", payload), encoding="utf-8")
    print(f"✓ 看板寫入 {out}（{len(data['skills'])} 個 skill、{len(data['teams'])} 個團隊、"
          f"{len(data['projects'])} 個專案、{data['reporters']} 份回報）")


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
            x.add_argument("--agents", nargs="+", choices=AGENTS, help="要支援的 agent，預設 claude")
            x.add_argument("--no-hook", action="store_true")
            x.add_argument("--team", help="專案所屬團隊（hub 的 teams/<團隊>.json），會套用該團隊的必裝")
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

    x = sub.add_parser("teams", help="列出各團隊的必裝與建議 skill")
    x.set_defaults(fn=cmd_teams)

    x = sub.add_parser("report", help="匯出本機匿名使用彙總到 hub 的 telemetry/（給看板用）")
    x.add_argument("--days", type=int, default=30)
    x.add_argument("--out", help="輸出檔（預設 hub 的 telemetry/<代號>.json）")
    x.add_argument("--pr", action="store_true", help="直接開 PR 送進 hub（只改 telemetry/ 時 CI 自動合併）")
    x.set_defaults(fn=cmd_report)

    x = sub.add_parser("dashboard", help="產生靜態看板：分級、團隊、專案、用量、eval、安全掃描")
    x.add_argument("--out", default="site/index.html")
    x.add_argument("--days", type=int, default=30)
    x.add_argument("--local", action="store_true", help="把本機還沒回報的使用紀錄也算進去")
    x.set_defaults(fn=cmd_dashboard)

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

    x = sub.add_parser("scan", help="安全掃描（security.scanner：skillspector / cisco）")
    x.add_argument("targets", nargs="*", help="skill 名稱、路徑、URL、tool:<id>、plugin:<id>、bundle:<名稱>；不填 = hub 全部 skill")
    x.add_argument("--llm", action="store_true", help="加上 LLM 語意分析（預設只做本機靜態分析）")
    x.add_argument("--accept", metavar="KEY", help="接受某個目標的目前掃描結果（例如 tool:archify）")
    x.add_argument("--reason", help="接受的理由")
    x.add_argument("--scanner", choices=["skillspector", "cisco"], help="這次改用哪個掃描器（預設看 skillhub.json security.scanner）")
    x.set_defaults(fn=cmd_scan)

    x = sub.add_parser("tools", help="列出組合包需要的外部工具與安裝指令")
    x.add_argument("--project", default=".")
    x.add_argument("--run", action="store_true", help="直接執行缺少的安裝指令")
    x.set_defaults(fn=cmd_tools)

    x = sub.add_parser("adopt", help="把專案內自建的 skill 送進 hub 的 domain 層（開 PR）")
    x.add_argument("names", nargs="*", help="不填 = 所有尚未收編的自建 skill")
    x.add_argument("--project", default=".")
    x.add_argument("--owner")
    x.add_argument("--no-pr", action="store_true", help="只複製到本機 hub，不開分支與 PR")
    x.add_argument("--again", action="store_true", help="已收編過也再送一次（更新 hub 版本）")
    x.set_defaults(fn=cmd_adopt)

    x = sub.add_parser("doctor", help="檢查專案的 skill 與必裝 plugin 狀態")
    x.add_argument("--project", default=".")
    x.set_defaults(fn=cmd_doctor)

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
