#!/usr/bin/env python3
"""skill-hub 的 agent hook：記錄 skill 使用量（不記錄 prompt 或檔案內容），並在 session 結束時偵測自建 skill。

同一支腳本給三種 agent 用，用 --agent 指定事件格式（skillctl sync 會自動註冊）：
  claude  .claude/settings.json  PostToolUse(Skill) / UserPromptSubmit(/name) / SessionEnd
  codex   .codex/hooks.json      PostToolUse（讀了 skills/<name>/SKILL.md）/ UserPromptSubmit($name) / SessionEnd
  cursor  .cursor/hooks.json     postToolUse / beforeSubmitPrompt(/name) / sessionEnd

使用紀錄寫到 ~/.skillhub/usage.jsonl（可用 SKILLHUB_LOG 改位置）；設定 SKILLHUB_OTLP_ENDPOINT 會另外送 OTLP。
SessionEnd 時若發現 hub 還沒有的自建 skill，依 auto_adopt 設定在背景執行 skillctl adopt。
任何錯誤都吞掉，絕不擋住 agent。
"""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

LOG = Path(os.environ.get("SKILLHUB_LOG", Path.home() / ".skillhub" / "usage.jsonl"))
LOCK_REL = Path(".skillhub") / "lock.json"
SKILL_DIRS = (".agents/skills", ".claude/skills", ".cursor/skills", ".codex/skills")
SKILL_PATH_RE = re.compile(r"skills/([a-z0-9][a-z0-9-]*)/SKILL\.md")


def h(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:10] if s else None


def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_project(agent, data):
    env = os.environ.get("CLAUDE_PROJECT_DIR") if agent == "claude" else os.environ.get("CURSOR_PROJECT_DIR")
    cand = env or (data.get("workspace_roots") or [None])[0] or data.get("cwd") or os.getcwd()
    p = Path(cand).resolve()
    for d in [p, *p.parents]:  # 往上找 .skillhub.json（Codex 可能在子資料夾執行）
        if (d / ".skillhub.json").exists():
            return d
    return p


def installed(project, name):
    return any((project / d / name / "SKILL.md").exists() for d in SKILL_DIRS)


def detect_use(agent, event, data, project):
    """回傳 (skill 名稱, 來源, 是否錯誤)；不是 skill 使用就回傳 None。"""
    if event in ("PostToolUse", "postToolUse"):
        ti = data.get("tool_input") or {}
        if agent == "claude":
            if data.get("tool_name") != "Skill":
                return None
            name = ti.get("skill") or ti.get("command") or ti.get("name")
            resp = data.get("tool_response")
            err = isinstance(resp, dict) and bool(resp.get("error") or resp.get("success") is False)
            return (name, "agent", err) if name else None
        # Codex / Cursor 沒有專用的 Skill 工具：agent 讀取 skills/<name>/SKILL.md 就算一次使用
        m = SKILL_PATH_RE.search(json.dumps(ti, ensure_ascii=False))
        if m and installed(project, m.group(1)):
            return m.group(1), "agent", None
        return None
    if event in ("UserPromptSubmit", "beforeSubmitPrompt"):
        # 只取開頭的 /name 或 $name（Codex 用 $ 明確呼叫 skill），不記錄其他文字
        m = re.match(r"^[/$]([a-z0-9][a-z0-9-]*)", (data.get("prompt") or "").strip())
        if m and installed(project, m.group(1)):
            return m.group(1), "user", None
    return None


def log_use(agent, project, data, name, source, error):
    lock = read_json(project / LOCK_REL)
    name = name.split(":")[-1]  # plugin:skill → skill
    rec = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "skill_invoked",
        "skill": name,
        "version": (lock.get("skills", {}).get(name) or {}).get("version"),
        "agent": agent,
        "source": source,
        "session": h(data.get("session_id") or data.get("conversation_id")),
        "project": project.name if os.environ.get("SKILLHUB_PROJECT_NAMES") else h(str(project)),
        "error": error,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    endpoint = os.environ.get("SKILLHUB_OTLP_ENDPOINT")
    if endpoint:
        attrs = [{"key": f"skill.{k}", "value": {"stringValue": str(v)}} for k, v in rec.items() if v is not None]
        body = {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "skillhub"}}]},
                                  "scopeLogs": [{"logRecords": [{"body": {"stringValue": "skill_invoked"}, "attributes": attrs}]}]}]}
        req = urllib.request.Request(endpoint.rstrip("/") + "/v1/logs", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2)


def on_session_end(project):
    """auto_adopt：pr（預設，背景開 PR）/ notify（只記錄）/ off。"""
    cfg = {**read_json(project / ".skillhub.json"), **read_json(project / ".skillhub.local.json")}
    mode = cfg.get("auto_adopt", "pr")
    lock = read_json(project / LOCK_REL)
    hub = lock.get("hub")
    if mode == "off" or not hub or Path(hub).resolve() == project.resolve():
        return
    managed = set(lock.get("skills", {})) | set(cfg.get("adopted", {}))
    new = set()
    for rel in SKILL_DIRS:
        d = project / rel
        if d.is_dir():
            new |= {p.name for p in d.iterdir() if p.is_dir() and not p.is_symlink()
                    and (p / "SKILL.md").exists() and p.name not in managed}
    if not new:
        return
    log = Path.home() / ".skillhub" / "adopt.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n[{dt.datetime.now().isoformat(timespec='seconds')}] {project.name}: 發現新 skill {', '.join(sorted(new))}（{mode}）\n")
        if mode == "pr":
            # 背景執行，不拖慢 agent 結束（Codex 的 SessionEnd 最多只等 3 秒）
            subprocess.Popen([sys.executable, str(Path(hub) / "tools" / "skillctl.py"), "adopt", "--project", str(project)],
                             stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)


EVENT = {"name": ""}


def main():
    agent = sys.argv[sys.argv.index("--agent") + 1] if "--agent" in sys.argv else "claude"
    data = json.load(sys.stdin)
    event = EVENT["name"] = data.get("hook_event_name") or ""
    project = find_project(agent, data)
    if event in ("SessionEnd", "sessionEnd"):
        on_session_end(project)
        return
    hit = detect_use(agent, event, data, project)
    if hit:
        log_use(agent, project, data, *hit)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    if EVENT["name"] == "beforeSubmitPrompt":
        print(json.dumps({"continue": True}))  # Cursor：明確允許送出 prompt
    sys.exit(0)
