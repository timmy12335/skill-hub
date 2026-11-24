---
name: skill-papercut
description: Reports a "papercut" when an installed skill's instructions were wrong, outdated or missing a step, so the owner can fix it. Use right after you had to deviate from a skill or the user corrected output produced by a skill.
metadata:
  version: 1.0.0
  owner: platform
  tier: always-on
  status: active
---

# skill-papercut

在下列情況回報，一次任務最多回報一筆：
- 你照某個 skill 做，結果被使用者糾正。
- skill 裡的指令、路徑或指令列參數已經不正確。
- 你必須跳過或改寫 skill 的步驟才能完成任務。

做法：
1. 從 `.claude/skills.lock.json` 取得 `hub` 路徑（`$HUB`）。
2. 執行（issue 用一句話描述問題，不要貼程式碼、密碼或個資）：
   `python3 $HUB/tools/skillctl.py papercut <skill-name> "<一句話問題>" --prompt "<使用者原本的請求，去除敏感內容>"`
3. 用一行告訴使用者你已回報，然後繼續原本的工作。
