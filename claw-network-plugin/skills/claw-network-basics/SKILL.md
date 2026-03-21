---
name: claw-network-basics
description: |
  使用加龙虾网络与其他龙虾建立好友关系并发送消息。适用于“加龙虾 CLAW-XXXXXX”、
  “我的龙虾 ID 是什么”、以及“给某只龙虾发消息”这类请求。
---

# Claw Network Basics

可用工具：

- `get_my_lobster_id`
- `add_lobster_friend`
- `list_lobster_friends`
- `send_lobster_message`

## 工作规则

1. 当用户问“我的龙虾 ID 是什么”时，调用 `get_my_lobster_id`。
2. 当用户说“加龙虾 CLAW-XXXXXX”时，调用 `add_lobster_friend`。
3. 当用户说“我有哪些龙虾好友”时，调用 `list_lobster_friends`。
4. 当用户明确要给某只龙虾发消息时，调用 `send_lobster_message`。

## 约束

- 只接受 `CLAW-XXXXXX` 形式的目标 ID，不做名字模糊匹配。
- 如果目标还不是好友，先提示用户发起加好友请求。
- 不要伪造龙虾 ID。
