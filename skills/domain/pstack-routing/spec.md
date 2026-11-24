# pstack-routing — Spec
- 層級：domain　負責人：platform
## 目標
pstack 與必裝的 superpowers 在 TDD、規劃、驗證、review 上重疊。沒有分工時 agent 會兩邊都觸發或來回切換，浪費 context 也讓流程混亂。
## 觸發時機
選用 pstack 組合包的專案中，任何寫程式、除錯、審查任務的開始。
## 產出
選定由哪個 plugin 的哪個 skill 處理，然後照做。
## 工具
禁用：未經使用者要求的 /poteto-mode；同一任務同時跑兩套 TDD 或規劃流程。
## 成功標準
TDD 走 superpowers 的 test-driven-development；審查設計走 interrogate；不主動呼叫 poteto-mode。
## 退場條件
其中一個 plugin 不再使用，或兩者不再重疊。
