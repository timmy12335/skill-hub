# Skill Hub — 通用 Agent Skill 管理框架

把所有專案共用的 Claude Code skill 放在這一個 repo 管理，任何專案只要 `init` 一次，
之後 `sync` 就會依設定自動安裝、更新、移除 skill。做法參考
[Uber 的 agent skill 管理方式](https://www.uber.com/us/en/blog/optimizing-ubers-agent-skills/)，縮小成個人／小團隊可用的版本。
只需要 Python 3.9+，沒有其他相依套件。

完整中文教學見 [docs/guide.html](docs/guide.html)。

## 結構

```
skill-hub/
├── skills/
│   ├── always-on/      每個專案預設安裝（description 要短）
│   ├── core/           常用、有負責人、修改需平台審查
│   └── domain/         任何人可發佈，1 人 review + CI
│       └── <name>/
│           ├── spec.md          意圖（給人看）
│           ├── SKILL.md         指令（給 agent 看，≤500 行）
│           ├── references/      需要時才讀的細節
│           ├── scripts/         需要時才執行的腳本
│           ├── evals/cases.jsonl
│           └── CHANGELOG.md
├── plugins/*.json      skill 組合包
├── templates/skill/    新 skill 範本
├── hooks/skillhub_usage.py   使用量紀錄 hook
├── tools/skillctl.py   CLI
└── skillhub.json       hub 預設設定（第 1 層）
```

## 快速開始

```bash
# 1. 把 hub 放在固定位置，並設 alias
git clone <your-repo> ~/skill-hub
alias skillctl="python3 ~/skill-hub/tools/skillctl.py"

# 2. 在任一專案啟用
cd ~/projects/my-app
skillctl init --plugins starter     # 建 .skillhub.json、註冊 hook、安裝 skill

# 3. 之後 hub 有更新時
skillctl sync
```

## 分層設定（後者覆蓋前者）

| 層 | 檔案 | 用途 |
|---|---|---|
| 1 hub | `skill-hub/skillhub.json` | 全部專案的預設 |
| 2 使用者 | `~/.config/skillhub/config.json` | 你個人在每個專案都想要的 |
| 3 專案 | `<project>/.skillhub.json`（進版控） | 這個專案需要的 plugin / skill |
| 4 本機 | `<project>/.skillhub.local.json`（gitignore） | 只在你這台電腦試用 |

欄位：`plugins`、`include`、`exclude`（清單會累加，exclude 最後套用）；
`always_on`、`mode`（`copy` 或 `link`）後者覆蓋前者；`pin: {"name": "1.0.0"}` 固定版本。

## Skill 生命週期

1. **撰寫**：`skillctl new api-review --tier domain` → 填 `spec.md` → `skillctl compile api-review --run` 產生 SKILL.md 草稿
2. **測試**：`skillctl lint api-review`、`skillctl eval api-review`（目標 70–90%）
3. **上架**：開 PR，CI 跑 lint；core/always-on 由 CODEOWNERS 審查；改動必須升版號
4. **改進**：`skillctl stats` 看用量；`skillctl papercut ... --to-eval` 把回報轉成 eval
5. **退場**：`skillctl eval <name> --ablation` 顯示沒差 → `status: deprecated` → `retired`

## Eval 案例格式

```json
{"type": "golden|real|self", "prompt": "...", "setup": "（選填）先在沙盒執行的 shell 指令",
 "expect": {
  "triggers": true, "contains": [], "not_contains": [], "regex": [], "tool_order": ["Bash", "Edit"]}}
```

建議比例 golden 5% / real 80% / self 15%。real 案例來自真實請求與 papercut。

## 遙測

hook 只記錄：時間、skill 名稱、版本、觸發來源（agent / 使用者 `/指令`）、session 與專案的雜湊值。
不記錄 prompt 與檔案內容。設 `SKILLHUB_OTLP_ENDPOINT` 可另外送到 OpenTelemetry collector。
