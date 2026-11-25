# Skill Hub — 通用 Agent Skill 管理框架

把所有專案共用的 agent skill 放在這一個 repo 管理，同一份可給 Claude Code、Codex、Cursor 使用，任何專案只要 `init` 一次，
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
├── teams/*.json        各團隊的必裝與建議
├── telemetry/*.json    成員送進來的匿名使用彙總（看板用）
├── security/scan-results.json  安全掃描結果與已接受的風險
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
# 也用 Codex / Cursor：skillctl init --plugins starter --agents claude codex cursor

# 3. 之後 hub 有更新時
skillctl sync
```

## 分層設定（後者覆蓋前者）

| 層 | 檔案 | 用途 |
|---|---|---|
| 1 hub | `skill-hub/skillhub.json` | 全部專案的預設與必裝 |
| 2 團隊 | `skill-hub/teams/<團隊>.json` | 該團隊的必裝與建議（專案用 `"team"` 指定） |
| 3 使用者 | `~/.config/skillhub/config.json` | 你個人在每個專案都想要的 |
| 4 專案 | `<project>/.skillhub.json`（進版控） | 這個專案需要的 plugin / skill |
| 5 本機 | `<project>/.skillhub.local.json`（gitignore） | 只在你這台電腦試用 |

欄位：`plugins`、`include`、`exclude`（清單會累加，exclude 最後套用）；
`always_on`、`mode`（`copy` 或 `link`）後者覆蓋前者；`pin: {"name": "1.0.0"}` 固定版本。

## 在專案裡自建的 skill 會自動送進 hub

在任何專案的 skill 資料夾（`.claude/skills/`、`.agents/skills/`、`.cursor/skills/`）自己新增 skill 後，不用手動搬：

1. **偵測**：`init` 會註冊 SessionEnd hook。Claude Code 結束時，hook 發現 hub 還沒有的 skill，就在背景執行 `skillctl adopt`（紀錄在 `~/.skillhub/adopt.log`）。`sync` 和 `doctor` 也會列出這些 skill，`sync` 不會覆蓋它們。
2. **送審**：`adopt` 補齊 frontmatter（版號、owner、`tier: domain`），在暫存 worktree 從 hub 的 `origin/main` 開 `adopt/<name>` 分支，加上 spec.md 與 CHANGELOG 草稿，推上去並用 `gh` 開 PR。不會動到你目前的 hub checkout。
3. **審核**：送出前 `adopt` 先做安全掃描，被擋的不送。hub CI 跑 lint 與安全掃描（有開的話再加 LLM 判讀，見[安全掃描](#安全掃描)）；PR 只改動 `skills/domain/` 時，全部通過就自動合併。動到其他地方的一律等人審。
4. **接手**：合併後在 hub `git pull`，專案再 `skillctl sync`，這個 skill 就改由 hub 管理，之後其他專案也能 `install`。

需要本機有 [GitHub CLI](https://cli.github.com/) 並已 `gh auth login`。在 `.skillhub.json`（或 `.skillhub.local.json`）設定 `"auto_adopt"`：

| 值 | 行為 |
|---|---|
| `"pr"`（預設） | 自動開 PR 送審 |
| `"notify"` | 只寫進 adopt.log，等你手動 `skillctl adopt` |
| `"off"` | 不偵測 |

手動用法：`skillctl adopt`（全部）、`skillctl adopt sql-style`、`--no-pr`（只複製到本機 hub）、`--again`（已收編過，再送一次更新）。

## 團隊必裝

全部專案都要的放在 hub（`skillhub.json` 的必裝 plugin、`always-on` skill）。只有某個團隊要的，寫在 hub 的 `teams/<團隊>.json`：

```json
{
  "description": "前端團隊：查最新框架文件，commit 訊息統一格式。",
  "owners": ["@timmy12335"],
  "required":    { "skills": ["commit-message"], "plugins": ["library-docs"], "agent_plugins": [] },
  "recommended": { "skills": [], "plugins": ["docs-visual"] }
}
```

專案在 `.skillhub.json` 寫 `"team": "frontend"`（可以是清單），或 `skillctl init --team frontend`。之後：

- `sync` 會裝上 `required` 和 `recommended`。`required` 的 skill、組合包、plugin 跟 hub 必裝一樣，專案的 `exclude` 排除不掉。`recommended` 可以排除。
- `doctor` 會檢查團隊必裝是否到位；`skillctl teams` 列出每個團隊的設定。
- `lint`（CI）會檢查團隊檔引用的 skill 和組合包都存在。
- 改 `teams/` 要經過 PR，CODEOWNERS 可以指定各團隊的負責人審。

## 使用看板

參考 Uber 的做法，用一個看板讓大家看到：hub 有哪些 skill、各團隊必裝什麼、哪些專案裝了什麼、用了多少次、eval 成績、安全掃描結果，以及「需要處理」清單（掃描被擋、eval 未達標、沒人用的退場候選、專案缺團隊必裝、待處理 papercut）。

資料流：

1. **記錄**：hook 每次使用寫一行到 `~/.skillhub/usage.jsonl`（含團隊名稱，不含 prompt）。`sync` 會記下這台電腦有哪些專案用 skill-hub。
2. **回報**：成員在 hub 執行 `skillctl report --pr`，把本機的匿名彙總（每天每個 skill 幾次、各專案裝了哪些 skill、缺哪些團隊必裝）寫成 `telemetry/<代號>.json` 開 PR。只改 `telemetry/` 的 PR 由 CI 自動合併。可以排進每週的 cron。
3. **發佈**：`.github/workflows/dashboard.yml` 在 main 更新時和每天一次執行 `skillctl dashboard`，把單一 HTML 頁發佈到 GitHub Pages。第一次要到 repo **Settings → Pages → Source** 選 **GitHub Actions**。私有 repo 的 Pages 需要付費方案；沒有的話在本機看：

```bash
skillctl dashboard --local --out site/index.html   # --local 把自己還沒回報的紀錄也算進去
```

eval 成績來自各 skill 的 `evals/score.json`（`skillctl eval` 會寫，進版控）；完整輸出 `last_result.json` 不進版控。

## 支援 Claude Code、Codex、Cursor

在 `.skillhub.json` 設定 `"agents": ["claude", "codex", "cursor"]`（預設只有 `claude`），或 `skillctl init --agents claude codex cursor`。

| Agent | skill 資料夾 | hook 設定檔 |
|---|---|---|
| Claude Code | `.claude/skills/` | `.claude/settings.json` |
| Codex | `.agents/skills/` | `.codex/hooks.json`（第一次要用 `/hooks` 信任） |
| Cursor | `.agents/skills/`、`.cursor/skills/`，也讀 `.claude/skills/` | `.cursor/hooks.json` |

每個 skill 在專案裡只有一份實體檔案：只有 Claude（或 Claude＋Cursor）時放 `.claude/skills/`；有 Codex 或沒有 Claude 時放跨 agent 標準位置 `.agents/skills/`，有 Claude 再在 `.claude/skills/` 建 symlink。改了 agents 後 `sync` 會自動搬移。

- hook 是同一支 `.skillhub/hooks/skillhub_usage.py`，以 `--agent` 區分。Codex／Cursor 沒有 Skill 工具，以「讀取 `skills/<name>/SKILL.md`」或輸入 `$name`／`/name` 計為一次使用。
- 同時啟用 Claude 與 Cursor 時不寫 `.cursor/hooks.json`，因為 Cursor 會沿用 Claude Code 的 hook。
- superpowers 只有 Claude Code 能自動宣告；Codex 和 Cursor 要在每台電腦手動安裝一次，`doctor` 會提醒。

## 必裝的 Claude Code plugin

hub 的 `skillhub.json` 用 `agent_plugins` 宣告每個專案都要有的 Claude Code plugin。目前必裝的是
[superpowers](https://github.com/obra/superpowers)（`superpowers@superpowers-marketplace`）。

- `init` / `sync` 會把它寫進專案的 `.claude/settings.json`（僅 Claude Code）（`enabledPlugins` 與 `extraKnownMarketplaces`）。第一次在專案開 Claude Code 時同意安裝即可，隊友 clone 後也會被提示安裝。
- plugin 的檔案不會複製進專案，由 Claude Code 自己安裝與更新。
- `required: true` 只能在 hub 設定，專案或本機用 `exclude_agent_plugins` 也排除不掉。
- 專案可以在 `.skillhub.json` 的 `agent_plugins` 加自己需要的其他 plugin。
- `provides` 列出 plugin 內建的 skill 名稱，hub 裡的 skill 不能同名（lint 會擋），避免兩份指令互相衝突。
- `skillctl doctor` 檢查專案的 skill、hook、必裝 plugin 是否都到位。

## 選用組合包：pstack

[pstack](https://github.com/cursor/plugins/tree/main/pstack) 是 poteto（Lauren Tan）的工程方法 plugin，約 60 個 skill 加 2 個 agent。它和必裝的 superpowers 在 TDD、規劃、驗證、review 上重疊，所以**不放 always-on**，做成選用組合包：

```bash
skillctl install plugin:pstack     # 或 skillctl init --plugins starter pstack
```

- 在 Claude Code 啟用 `pstack@cursor-plugins`（marketplace `cursor/plugins`），指令帶前綴，例如 `/pstack:interrogate`。Cursor 用 `/add-plugin pstack` 手動安裝；目前沒有 Codex 版本。
- 同時安裝 domain skill `pstack-routing` 負責分工：TDD、規劃、除錯、驗證、review 交給 superpowers；`interrogate`、`why`、`how`、`unslop`、`reflect`、`swarm`/`arena` 交給 pstack；`/poteto-mode` 只在你明確要求時用。
- `sync` 和 `doctor` 會檢查重疊是否已由路由 skill 分工；pstack 的 skill 名稱也列入保留名，hub 不能用（例如 `tdd`、`why`）。
- plugin 無法只開其中幾個 skill。若想只用少數幾個，得把它們複製進 hub（MIT 授權，需註明來源並固定版本），但那會變成要自己同步的副本，所以預設不這麼做。

組合包格式也因此擴充：`plugins/<名稱>.json` 可以加 `agent_plugins`，選了組合包才會啟用那些外部 plugin；`overlaps` 標示與哪個 plugin 重疊，`router` 指定負責分工的 skill。

## 外部工具組合包：graphify、archify、GitNexus

有些工具有自己的安裝程式，會自己把 skill 裝進 agent 資料夾。skill-hub 不複製它們，只在組合包裡宣告「要用哪個工具、怎麼裝、裝好會出現哪些 skill」：

| 組合包 | 工具 | 層級 | 用途 | 授權 |
|---|---|---|---|---|
| `code-graph`（`starter` 已包含） | [graphify](https://github.com/Graphify-Labs/graphify) | core | 本機 tree-sitter 程式碼／文件知識圖譜，query / path / explain | Apache-2.0 |
| `library-docs` | [Context7](https://github.com/upstash/context7) | core（不在 starter） | 依版本查最新的函式庫、框架、SDK 文件；用 CLI＋skill（`find-docs`），不裝 MCP | MIT |
| `docs-visual` | [archify](https://github.com/tt-a1i/archify) | domain | 把需求、Mermaid、程式或 PR 畫成互動式 HTML 圖表 | Apache-2.0 |
| `gitnexus` | [GitNexus](https://github.com/abhigyanpatwari/GitNexus) | domain（選用） | 影響分析、diff 影響、安全改名 | **PolyForm Noncommercial** |

```bash
skillctl install plugin:docs-visual
skillctl tools          # 列出缺少的工具與各 agent 的安裝指令
skillctl tools --run    # 直接執行（會跑第三方安裝程式）
```

- 組合包可以用 `extends` 引用其他組合包，`starter` 透過它帶入 `code-graph`。
- 工具安裝的 skill（例如 `graphify`、`archify`、`gitnexus-*`）列入保留名，也不會被當成自建 skill 送去 adopt。
- **同類工具只開一個**：graphify、GitNexus、understand-anything 都是程式碼圖譜，同時開會互搶；Context7 的 CLI 模式（`find-docs`）和 MCP 模式（`context7-mcp`）也一樣。`sync` 和 `doctor` 會警告。
- **Context7**：雲端服務，查詢內容會送到 context7.com，第一次安裝要登入（或 `--api-key`）。因此是 core 但不預設，需要的專案 `skillctl install plugin:library-docs`。CLI 和 MCP 模式只開一個，`doctor` 會警告。安全掃描只掃實際安裝的 `skills/find-docs`（組合包的 `scan` 欄位），不掃整個 monorepo。
- **GitNexus**：非商業授權，商業專案不要用。一律用 `gitnexus analyze --index-only`；不加這個參數它會改寫 CLAUDE.md／AGENTS.md、註冊 hook、安裝 skill，和 skill-hub 搶著管專案設定。

## 安全掃描

skill 本身就是會被 agent 執行的指令和腳本，所以 hub 在三個地方掃描：

| 時機 | 誰執行 | 擋不擋 |
|---|---|---|
| `skillctl adopt` 送審前 | 本機 | 被擋的 skill 不送 PR |
| `skillctl tools --run` 安裝外部工具前 | 本機 | 被擋的工具不安裝 |
| 每個改到 `skills/` 的 PR（CI `security` job） | GitHub Actions | 會擋，自動合併也要它通過 |
| `adopt/` 自動合併 PR（CI `llm-judge` job，選用） | GitHub Actions | 會擋自動合併 |

`skillctl doctor` 會列出每個外部工具與 plugin 的最近掃描結果；沒掃過的會提醒。

### 兩個掃描器，一次用一個

`skillhub.json` 的 `security.scanner` 決定 `skillctl scan` 和 CI `security` job 用哪個（臨時切換：`skillctl scan --scanner cisco` 或 `SKILLHUB_SCANNER=cisco`）：

| | [NVIDIA SkillSpector](https://github.com/NVIDIA/skillspector)（預設） | [Cisco skill-scanner](https://github.com/cisco-ai-defense/skill-scanner) |
|---|---|---|
| 安裝 | `uv tool install --python 3.13 git+https://github.com/NVIDIA/skillspector.git` | `uv tool install cisco-ai-skill-scanner` |
| 判斷方式 | 0–100 風險分數 + SAFE / CAUTION / DO_NOT_INSTALL | 最高嚴重度（INFO…CRITICAL）+ 政策預設 |
| 門檻設定 | `max_score`（預設 50）、`block` | `cisco.fail_on_severity`（預設 high）、`cisco.policy`（預設 low-noise） |
| 輸出 | 兩者都統一成同一格式寫進 `security/scan-results.json` | |

兩個都預設只做本機靜態分析，不送資料給 LLM、不用 API key。`skillctl scan --llm` 或 `"llm": true` 才會加上 LLM 分析（要自己設供應商的 key）。

```bash
skillctl scan                    # 掃 hub 全部 skill
skillctl scan sql-style ./x      # skill 名稱或路徑
skillctl scan tool:archify       # 外部工具（掃它的 repo）
skillctl scan bundle:docs-visual # 組合包裡的 skill、工具、plugin
```

### 誤判怎麼辦

靜態規則會誤判。例如 archify 的 `archify-pr` 在 SkillSpector 拿 60 分（DO_NOT_INSTALL），在 Cisco 被標成 CRITICAL `command_injection`，其實是它本來就要執行 `gh` 和 git 指令。確認後：

```bash
skillctl scan --accept tool:archify --reason "archify-pr 呼叫 gh/git 是預期行為，已讀過 bin/archify-pr.mjs"
```

接受紀錄（理由、誰、日期、當時分數）寫進 `security/scan-results.json`，跟著 PR 讓人審。分數改變或換掃描器後要重新接受。改到這個檔案的 PR 不在 `skills/domain/` 範圍內，所以**一定要人審**，不會被自動合併。

### 自動合併的第二層：Cisco + LLM 判讀（選用）

`adopt/` PR 沒有人看就會合併，而靜態規則只抓到約 8% 的惡意 skill（Cisco 公布的數據；加上 LLM 判讀可到約 67%）。所以這種 PR 可以多跑一層 Cisco 的 [GitHub Action](https://github.com/cisco-ai-defense/skill-scanner/blob/main/docs/github-actions.md)，用 LLM 判讀：

1. repo **Settings → Secrets and variables → Actions**：
   - Secret `SKILL_SCANNER_LLM_API_KEY`：LLM 供應商的 key
   - Variable `SKILL_SCANNER_LLM=true`
   - （選用）Variable `SKILL_SCANNER_LLM_MODEL`，預設 `anthropic/claude-sonnet-5-5`
2. 之後每個 `adopt/` PR 會多一個 `llm-judge` job，HIGH 以上就不自動合併，留給人審。

沒開時這層略過，自動合併只看第一層。一般 PR 不跑這層，避免兩個掃描器重複擋。

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
3. Require status checks to pass：選 `lint` 和 `security`

`teams/` 可以在 CODEOWNERS 加一行讓各團隊負責人審自己的檔案，例如 `/teams/frontend.json @frontend-lead`。

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

hook 只記錄：時間、skill 名稱、版本、觸發來源（agent / 使用者 `/指令`）、團隊名稱、session 與專案的雜湊值。
不記錄 prompt 與檔案內容。設 `SKILLHUB_OTLP_ENDPOINT` 可另外送到 OpenTelemetry collector。
