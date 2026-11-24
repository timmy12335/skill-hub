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

## 在專案裡自建的 skill 會自動送進 hub

在任何專案的 `.claude/skills/` 自己新增 skill 後，不用手動搬：

1. **偵測**：`init` 會註冊 SessionEnd hook。Claude Code 結束時，hook 發現 hub 還沒有的 skill，就在背景執行 `skillctl adopt`（紀錄在 `~/.skillhub/adopt.log`）。`sync` 和 `doctor` 也會列出這些 skill，`sync` 不會覆蓋它們。
2. **送審**：`adopt` 補齊 frontmatter（版號、owner、`tier: domain`），在暫存 worktree 從 hub 的 `origin/main` 開 `adopt/<name>` 分支，加上 spec.md 與 CHANGELOG 草稿，推上去並用 `gh` 開 PR。不會動到你目前的 hub checkout。
3. **審核**：hub CI 跑 lint；PR 只改動 `skills/domain/` 時，CI 通過就自動合併。動到其他地方的一律等人審。
4. **接手**：合併後在 hub `git pull`，專案再 `skillctl sync`，這個 skill 就改由 hub 管理，之後其他專案也能 `install`。

需要本機有 [GitHub CLI](https://cli.github.com/) 並已 `gh auth login`。在 `.skillhub.json`（或 `.skillhub.local.json`）設定 `"auto_adopt"`：

| 值 | 行為 |
|---|---|
| `"pr"`（預設） | 自動開 PR 送審 |
| `"notify"` | 只寫進 adopt.log，等你手動 `skillctl adopt` |
| `"off"` | 不偵測 |

手動用法：`skillctl adopt`（全部）、`skillctl adopt sql-style`、`--no-pr`（只複製到本機 hub）、`--again`（已收編過，再送一次更新）。

## 必裝的 Claude Code plugin

hub 的 `skillhub.json` 用 `agent_plugins` 宣告每個專案都要有的 Claude Code plugin。目前必裝的是
[superpowers](https://github.com/obra/superpowers)（`superpowers@superpowers-marketplace`）。

- `init` / `sync` 會把它寫進專案的 `.claude/settings.json`（`enabledPlugins` 與 `extraKnownMarketplaces`）。第一次在專案開 Claude Code 時同意安裝即可，隊友 clone 後也會被提示安裝。
- plugin 的檔案不會複製進專案，由 Claude Code 自己安裝與更新。
- `required: true` 只能在 hub 設定，專案或本機用 `exclude_agent_plugins` 也排除不掉。
- 專案可以在 `.skillhub.json` 的 `agent_plugins` 加自己需要的其他 plugin。
- `provides` 列出 plugin 內建的 skill 名稱，hub 裡的 skill 不能同名（lint 會擋），避免兩份指令互相衝突。
- `skillctl doctor` 檢查專案的 skill、hook、必裝 plugin 是否都到位。

## Skill 生命週期

1. **撰寫**：`skillctl new api-review --tier domain` → 填 `spec.md`（可搭配 superpowers 的 `writing-skills`） → `skillctl compile api-review --run` 產生 SKILL.md 草稿
2. **測試**：`skillctl lint api-review`、`skillctl eval api-review`（目標 70–90%）
3. **上架**：開 PR，CI 跑 lint；core/always-on 由 CODEOWNERS 審查；改動必須升版號
4. **改進**：`skillctl stats` 看用量；`skillctl papercut ... --to-eval` 把回報轉成 eval
5. **升級**：`skillctl promote <name>` 檢查條件後搬到上一層
6. **退場**：`skillctl eval <name> --ablation` 顯示沒差 → `skillctl demote <name> --status deprecated` → `retired`

## 分級、升級與退場

skill 在哪個資料夾就是哪一級，新 skill 一律從 `domain` 開始。

```bash
skillctl promote api-review --dry-run   # 只列出升級條件是否通過
skillctl promote api-review             # 全過才會 git mv 到上一層、改 tier、升 minor 版號、寫 CHANGELOG
skillctl demote api-review --status deprecated --reason "ablation 無差異"
skillctl demote api-review --status retired   # 各專案下次 sync 時移除
skillctl demote api-review --to domain        # 降一層
```

| 升級 | 條件（預設） |
|---|---|
| domain → core | lint 無錯、active、有 owner、≥5 個 eval 案例、**目前版本** eval 通過率 ≥70%、30 天內 ≥2 個專案共 ≥10 次使用 |
| core → always-on | 上述全部，加上 ≥8 個案例、跑過 `--ablation` 且有 skill 比沒有好、description ≤300 字元、≥3 個專案共 ≥30 次使用 |

門檻可在 `skillhub.json` 覆蓋，例如 `"promotion": {"core": {"min_uses": 5}}`。條件不全過時可加 `--force`，原因會寫進 CHANGELOG，請在 PR 說明。
使用量來自你本機的 `~/.skillhub/usage.jsonl`，多人團隊要合併紀錄可改送 OTLP（見「遙測」）。

### 讓審查跟著分級（GitHub 設定）

到 repo 的 **Settings → Branches**（或 Rules → Rulesets）替 `main` 開啟：

1. Require a pull request before merging，Required approvals 設 1
2. Require review from Code Owners
3. Require status checks to pass：選 `lint`

這樣 `domain/` 有 1 人 review 就能合併，`core/`、`always-on/`、`tools/` 則一定要 CODEOWNERS 裡的人審過。
個人 repo 無法核准自己的 PR，只有自己一人時可先只開第 3 項。

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
