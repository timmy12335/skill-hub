---
name: commit-message
description: Writes git commit messages in Conventional Commits format from the staged diff. Use when the user asks to commit, write a commit message, or describe staged changes.
metadata:
  version: 1.1.0
  owner: platform
  tier: core
  status: active
---

# commit-message

1. 執行 `git diff --staged --stat`，再看 `git diff --staged`。沒有 staged 變更就告訴使用者並停止。
2. 判斷類型：feat / fix / refactor / docs / test / chore / perf / build / ci。
3. 格式：
   ```
   <type>(<scope>): <祈使句摘要，72 字元內，不加句號>

   <為什麼要改；必要時列出重點>
   ```
4. 若專案根目錄有 `.commit-convention.md`，以它為準，覆蓋上述規則。
5. 破壞性變更在 footer 寫 `BREAKING CHANGE: ...`。
6. 先把訊息給使用者看，取得同意才執行 `git commit`。

範例與 scope 命名建議見 [references/examples.md](references/examples.md)。
