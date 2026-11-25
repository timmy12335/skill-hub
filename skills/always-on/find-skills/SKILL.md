---
name: find-skills
description: Searches the team skill hub for a skill that fits the current task and installs it into this project. Use when the user asks "is there a skill for…", or when a task is specialised and no installed skill covers it.
metadata:
  version: 1.0.0
  owner: platform
  tier: always-on
  status: active
---

# find-skills

1. 讀取 `.skillhub/lock.json` 的 `hub` 欄位，取得 skill hub 路徑（下稱 `$HUB`）。沒有這個檔案就告訴使用者此專案尚未執行 `skillctl init`。
2. 用任務關鍵字搜尋（中英文各試一次）：
   `python3 $HUB/tools/skillctl.py find "<關鍵字>" --json --limit 5`
3. 從結果挑最符合的一個。排序已考慮 eval 分數與使用量；`status` 為 deprecated 的不要選。
4. 告訴使用者你找到哪個 skill、為什麼適合，取得同意後安裝：
   `python3 $HUB/tools/skillctl.py install <name> --project .`
5. 安裝後重新讀取剛安裝的 `<name>/SKILL.md`（在 `.agents/skills/` 或 `.claude/skills/`） 並照做。
6. superpowers plugin 是每個專案必裝的，它的 skill（brainstorming、writing-plans、systematic-debugging 等）已經可以直接用，不需要到 hub 找。
7. 找不到時直接完成任務，並建議使用者：這類工作若會重複，可用 `skillctl new <name>` 建立新 skill。
