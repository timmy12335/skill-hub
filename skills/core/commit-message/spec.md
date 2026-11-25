# commit-message — Spec
- 層級：core　負責人：platform　目標模型：任一 Claude 模型
## 目標
讓不同專案的 commit 訊息一致，方便產生 changelog。
## 觸發時機
「幫我 commit」「寫 commit message」「描述一下這次改了什麼」。不觸發：「幫我看 git log」。
## 產出
Conventional Commits 格式訊息；使用者確認後才 commit。
## 工具
必用：git diff --staged。禁用：未經同意直接 git commit、git push。
## 成功標準
有 type 與摘要；摘要 72 字元內；專案若有 .commit-convention.md 以其為準。
## 退場條件
若 ablation 顯示模型不靠此 skill 也能穩定產生合規訊息，改為 deprecated。
