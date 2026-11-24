---
name: pstack-routing
description: Decides whether pstack or superpowers handles a task when both plugins are installed, so they do not compete. Use at the start of any coding task in a project that has the pstack bundle enabled.
metadata:
  version: 0.1.0
  owner: platform
  tier: domain
  status: active
---

# pstack-routing

這個專案同時裝了 superpowers（必裝）和 pstack（選用）。兩者都有流程型 skill，依下表分工，同一件事只走一邊。

## 交給 superpowers
- 規劃與發想：`brainstorming`、`writing-plans`、`executing-plans`
- 測試驅動：`test-driven-development`（不要再呼叫 pstack 的 `tdd`）
- 除錯：`systematic-debugging`
- 完成前驗證：`verification-before-completion`
- code review 與分支收尾：`requesting-code-review`、`receiving-code-review`、`finishing-a-development-branch`

## 交給 pstack
- 對抗式審查設計或計畫：`interrogate`
- 追設計理由、看懂程式怎麼運作：`why`、`how`
- 去除 AI 腔的文字：`unslop`、`technical-writing`
- 把這次的教訓寫回 skill：`reflect`
- 平行嘗試多個方案再挑最好的：`swarm`、`arena`（成本高，先問使用者）
- `principle-*` 只當參考原則，不當流程入口

## 不要用
- `/poteto-mode`：它會自己挑 playbook 再串其他 skill，會和 superpowers 的流程互搶。使用者明確要求時才用，用了就整段交給它，不再混用 superpowers 的流程 skill。

在 Claude Code 中 pstack 的指令帶前綴，例如 `/pstack:interrogate`。
