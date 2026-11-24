# find-skills — Spec
- 層級：always-on　負責人：platform
## 目標
讓 agent 在專案沒裝對應 skill 時，主動到 hub 搜尋並安裝，而不是自己硬做。
## 觸發時機
「有沒有處理 X 的 skill」「幫我找工具做 X」；或任務很專門但沒有已安裝的 skill。
## 產出
推薦一個 skill（附理由）並在使用者同意後安裝。
## 工具
必用：skillctl find / install。禁用：直接複製 hub 檔案到專案（會繞過 lock 檔）。
## 成功標準
搜尋時使用 --json；安裝前先徵求同意；找不到時不卡住任務。
