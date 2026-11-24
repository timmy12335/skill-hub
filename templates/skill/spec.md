# {{name}} — Spec

> spec.md 寫「意圖」，給人看；SKILL.md 寫「做法」，給 agent 看。
> 改 skill 時先改這裡，再用 `skillctl compile {{name}}` 重新產生 SKILL.md 草稿。

- 層級：{{tier}}
- 負責人：{{owner}}
- 建立日期：{{date}}
- 目標模型：（例如 Claude Sonnet / Opus；模型升級後要重跑 eval）

## 目標
這個 skill 要解決什麼問題？沒有它時 agent 會怎麼做錯？

## 觸發時機
- 使用者會怎麼說（列 3–5 個真實說法）：
  - 「……」
- 不該觸發的相似情境：
  - 「……」

## 產出
完成後應該交出什麼（檔案、格式、訊息）？

## 工具
- 必用：
- 禁用：

## 成功標準
怎樣算做對？（這些會變成 evals/cases.jsonl 的 expect 條件）

## 退場條件
什麼情況下這個 skill 不再需要？（例如：ablation 顯示沒有它也一樣好）
