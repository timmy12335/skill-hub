#!/usr/bin/env python3
"""Claude Code hook：記錄 skill 使用量（不記錄 prompt 或檔案內容）。

註冊在 .claude/settings.json：
  PostToolUse (matcher: Skill) → agent 自動呼叫 skill
  UserPromptSubmit             → 使用者手動輸入 /skill-name（只取指令名稱）
  SessionEnd                   → 檢查專案內是否有 hub 還沒有的自建 skill，
                                 依 auto_adopt 設定在背景執行 skillctl adopt（開 PR 送審）

每筆紀錄寫一行 JSON 到 ~/.skillhub/usage.jsonl（可用 SKILLHUB_LOG 改位置）。
若設定 SKILLHUB_OTLP_ENDPOINT，另外送一份到 OpenTelemetry collector（/v1/logs）。
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


def h(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:10] if s else None


def skill_version(project, name):
    try:
        lock = json.loads((Path(project) / ".claude" / "skills.lock.json").read_text())
        return lock["skills"][name]["version"]
    except Exception:
        return None


def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def on_session_end(project):
    """auto_adopt：pr（預設，背景開 PR）/ notify（只記錄）/ off。"""
    proj = Path(project)
    cfg = {**read_json(proj / ".skillhub.json"), **read_json(proj / ".skillhub.local.json")}
    mode = cfg.get("auto_adopt", "pr")
    lock = read_json(proj / ".claude" / "skills.lock.json")
    hub = lock.get("hub")
    if mode == "off" or not hub or Path(hub).resolve() == proj.resolve():
        return
    managed = set(lock.get("skills", {})) | set(cfg.get("adopted", {}))
    d = proj / ".claude" / "skills"
    new = [p.name for p in d.iterdir() if d.is_dir() and p.is_dir() and not p.is_symlink()
           and (p / "SKILL.md").exists() and p.name not in managed] if d.is_dir() else []
    if not new:
        return
    log = Path.home() / ".skillhub" / "adopt.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n[{dt.datetime.now().isoformat(timespec='seconds')}] {proj.name}: 發現新 skill {', '.join(new)}（{mode}）\n")
        if mode == "pr":
            # 背景執行，不拖慢 Claude Code 結束
            subprocess.Popen([sys.executable, str(Path(hub) / "tools" / "skillctl.py"), "adopt", "--project", str(proj)],
                             stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)


def main():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ""
    if event == "SessionEnd":
        on_session_end(project)
        return
    name, source, error = None, None, None
    if event == "PostToolUse" and data.get("tool_name") == "Skill":
        ti = data.get("tool_input") or {}
        name = ti.get("skill") or ti.get("command") or ti.get("name")
        source = "agent"
        resp = data.get("tool_response")
        if isinstance(resp, dict) and (resp.get("error") or resp.get("success") is False):
            error = True
    elif event == "UserPromptSubmit":
        m = re.match(r"^/([a-z0-9][a-z0-9-]*)", (data.get("prompt") or "").strip())
        if m and (Path(project) / ".claude" / "skills" / m.group(1)).is_dir():
            name, source = m.group(1), "user"
    if not name:
        return
    name = name.split(":")[-1]  # plugin:skill → skill
    rec = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "skill_invoked",
        "skill": name,
        "version": skill_version(project, name),
        "source": source,
        "session": h(data.get("session_id")),
        "project": Path(project).name if os.environ.get("SKILLHUB_PROJECT_NAMES") else h(project),
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


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
