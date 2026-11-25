# skill-papercut — Spec
- 層級：always-on　負責人：platform
## 目標
把「skill 指令有小問題」的訊號自動收集回 hub，之後用 `skillctl papercut <name> --to-eval` 轉成 eval 案例。
## 觸發時機
使用者糾正了由 skill 產生的結果；skill 指令過時；agent 需要偏離 skill。
## 產出
feedback.jsonl 中新增一筆，並告知使用者。
## 工具
必用：skillctl papercut。禁用：在 issue 中寫入程式碼、憑證或個資。
## 成功標準
只在真的偏離或被糾正時回報；一次任務至多一筆。
