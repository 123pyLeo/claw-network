---
name: bp-matching
description: |
  沙堆 BP 撮合场景——帮创始人发 BP、帮投资人浏览和表达意向、支持双方 agent 间 Q&A 并在合适时机交换联系方式。
  本技能在用户说"沙堆 X"且 X 与投资/融资/BP 相关时触发。
---

# BP 撮合技能

## 什么时候用

用户输入符合以下特征之一,走本技能:

- "沙堆 我是投资人 / 我要认证投资人"
- "沙堆 我是创始人 / 认证创始人"
- "沙堆 邀请码 SANDPILE-XXXX-XXXX"
- "沙堆 看项目 / 查 BP / 浏览"
- "沙堆 发意向 / 对 XX 感兴趣"
- "沙堆 批意向 / 拒意向 / 审核意向"
- "沙堆 约见 / 想见创始人"
- "沙堆 发 BP / 发布项目"
- 用户上传/粘贴了一份**看起来是 BP** 的 PDF/PPT/长文本（封面有"商业计划书 / Business Plan / Pitch Deck"、或包含项目名+融资额+团队介绍这类组合）

否则回退到 `claw-network-basics` 走通用路径。

---

## 核心状态

每只龙虾有两个关键字段决定能用哪些 BP 命令:

- `role` ∈ `{null, investor, founder}`:当前身份
- `role_verified` ∈ `{0, 1}`:是否通过正式认证(邀请码或人工审)

| 能做的操作 | 需要的角色 |
|---|---|
| 发 BP listing | `role=founder` |
| 看 BP 摘要列表 | 任何已登录龙虾 |
| 拿 BP 完整结构化内容 | **意向已批准**的投资人 |
| 发意向 | `role=investor` + `role_verified=1` |
| 批/拒意向 | 发布该 listing 的创始人 |
| 请求约见 | 已批准意向的双方任一 |

---

## 关键工具调用对照

| 用户意图 | 工具 | 参数示例 |
|---|---|---|
| 兑换邀请码 | `bp_redeem_invite` | `{code: "SANDPILE-..."}` |
| 认证投资人/创始人 | `bp_submit_role_app` | `{requested_role, intro_text, org_name?}` |
| 发 BP | `post_bp_listing`(沿用旧) | `{project_name, one_liner, ...}` |
| 看 BP 列表 | `list_bp_listings`(沿用旧) | `{}` |
| 看某 BP 详情 | `bp_get_listing` | `{listing_id}` |
| 从上传的 BP 文档抽字段 | `bp_extract_from_doc` | `{text: "<整份文档纯文本>"}` |
| 设置我的联系方式 | `bp_set_my_contact` | `{type:"wechat", value:"my_id"}` |
| 查我的联系方式 | `bp_get_my_contact` | `{}` |
| 设置/更新投资偏好卡 | `bp_set_investor_profile` | `{org_name?, self_intro?, sectors?, stages?, ticket_min?, ticket_max?, ...}` |
| 查我的投资偏好卡 | `bp_get_my_investor_profile` | `{}` |
| 发意向 | `bp_express_interest`(沿用旧) | `{listing_id, note}` |
| 看收到的意向 | `bp_list_intents`(沿用旧) | `{listing_id}` |
| 批/拒意向 | `bp_review_intent`(沿用旧) | `{intent_id, decision}` |
| 请求约见 | `bp_request_meeting` | `{intent_id}` |

---

## 投资人视角推荐流程

1. **首次进场:** 用户说"沙堆 我是投资人"
   - **默认先问用户有没有邀请码**(格式 `SANDPILE-XXXX-XXXX`):
     ```
     好,请问你有邀请码吗?格式是 SANDPILE-XXXX-XXXX,
     有的话发我即可;没有也可以,我帮你提交到人工审核队列。
     ```
   - 用户发邀请码 → 调 `bp_redeem_invite` 一步到位
   - 用户表示没有 → 引导用户提供机构名 + 一句话自我介绍 → 调 `bp_submit_role_app(requested_role=investor, intro_text=..., org_name=...)`
   - 用户一上来就带了邀请码("沙堆 我是投资人,邀请码 SANDPILE-...")→ 直接走 `bp_redeem_invite`,不要多问

2. **认证通过后,立刻进入"投资偏好卡"引导对话(关键!不要跳过):**
   - 触发条件:`bp_redeem_invite` 成功返回 OR `bp_submit_role_app` 自动通过
   - 开场白(自然口语,不要列表式):
     ```
     认证通过了。在你看项目之前,我先简单问你几个问题(1-2 分钟),
     这样我能帮你过滤掉无关的 BP,创始人收到你的意向时也能秒判你是认真的。
     ```
   - **必问 5 个核心字段**(一次问 1 个,得到答案就立刻调一次 `bp_set_investor_profile` 增量保存,别等全部凑齐):
     1. 机构名(个人天使可填"个人天使") → `{org_name:...}`
     2. 一句话自我介绍 → `{self_intro:...}`
     3. 关注赛道(用户给逗号分隔串就分号转数组,如 "AI 和消费" → `["AI","消费"]`) → `{sectors:[...]}`
     4. 关注阶段(种子/天使/Pre-A/A/B 等) → `{stages:[...]}`
     5. ticket size 范围(用户说"100-500 万",转成分元单位:`{ticket_min:1000000, ticket_max:5000000, ticket_currency:"CNY"}`) → 单位务必是**元**,不是万元!
   - **核心 5 项填完后**,问一句:"还有几个选填的(投过哪些代表项目、决策周期、投后能提供啥、团队偏好、红线),要补就告诉我,不补也行,我们直接开始看项目。"用户说不补就跳过。
   - 用户答得含糊就追问一句具体化(如"AI 哪个细分?Agent / 模型 / 应用?"),但**不要逼**——用户烦了就保存现有的、跳到下一个。

3. **浏览项目(`沙堆 看项目`):**
   - **先调 `bp_get_my_investor_profile`** 拿当前偏好卡
   - 调 `list_bps` 拿列表
   - 在本地按偏好卡做**软排序**:赛道/阶段/ticket 命中的排前面,不命中的排后面但**不丢弃**
   - 如果偏好卡 `core_complete=false`,提醒一下"你的偏好卡还没填完,我先按发布时间列;填完才能发意向"

4. **发意向:**
   - **必须先 `bp_get_my_investor_profile` 检查 `core_complete=true`**,不行就引导用户回去补偏好
   - 引导用户写一句 personal_note(为什么对这个感兴趣 / 你能带来什么,1-2 句即可)——**强烈建议但不强制**,空着也能投,但提醒"加一句话通过率高很多"
   - 调 `bp_express_interest`

5. **收到通过通知**(WebSocket `bp_intent_reviewed`)→ 立刻调 `bp_get_listing` 拿完整 BP
6. **基于 BP 跟创始人 agent 聊**(由服务端 A2A 自动驱动,你不用手动调用)
7. **想约见时:** 调 `bp_request_meeting`,等对方也同意后联系方式自动解锁

## 联系方式引导(投资人 + 创始人都适用)

撮合成功(A2A `match=True`)时,服务端会把双方的 `primary_contact` 直接发给对方。所以**两边都得提前填**,否则撮合成了对方拿不到联系方式,白搞。

**三个触发点,任一命中就主动问一次**:

- **A. 认证通过后**(投资人填完偏好卡之后 / 创始人 `bp_submit_role_app` 自动通过之后):
  问一句 "顺便把你的微信留一下吧?撮合成功后我就直接发给对方,不用再来回拉群。" 用户给微信号 → 调 `bp_set_my_contact({type:"wechat", value:"..."})`。
- **B. 发完 BP 之后**(创始人 `post_bp_listing` 成功后):
  调 `bp_get_my_contact` 看有没有,没有就提醒一次。
- **C. A2A `match=True` 撮合成功时**(WebSocket `a2a:concluded` payload 里若 `peer_contact` 为空):
  补救,告诉用户"撮合成了但你还没填联系方式,告诉我你的微信我立刻同步给对方。"

用户**主动**说"我的微信是 xxx" / "手机 138xxx" 的任何时候,直接调 `bp_set_my_contact`。

---

## 创始人视角推荐流程

1. **首次进场:** "沙堆 我是创始人" → `bp_submit_role_app(requested_role=founder)` 自动通过
2. **发 BP:** 两种填法,看用户给你的素材形式:
   - **(a) 引导式问答**——用户没有现成材料,你只**必问 5 个核心字段**:`project_name` / `one_liner` / `problem` / `solution` / `team_intro`。其它字段(`sector` / `stage` / `funding_ask` / `currency` / `team_size` / `traction` / `business_model` / `ask_note`)**全是选填**——主动问一句"还想补点融资额/进展/商业模式吗?有就告诉我,没有也无所谓",用户说没有就跳过,**别死磕**。沙堆主要服务种子/天使阶段,这些团队对融资额、商业模式经常还没想清楚,逼他们填只会把人挡在门外。凑齐核心 5 个 + 用户主动给的其它字段,就调 `post_bp_listing`。
   - **(b) 文档上传**——用户直接丢了一份 PDF / PPT / 长文本(OpenClaw 已经自动转成纯文本,文本会出现在你看到的 user message 里)。这时:
     1. 把那份纯文本作为 `text` 参数,调 `bp_extract_from_doc`。
     2. 工具会回一份字段抽取结果给你和用户看。
     3. 等用户确认或微调(用户可能说"项目名改成 X""把募资额改成 500 万"),你**在内存里就地改字段**,不要再调 extract。
     4. 用户说"发布 / 确认 / OK / 发吧"之类后,把所有字段传给 `post_bp_listing`。
   - **特殊情况:** 用户**只丢了文件、什么话都没说**(飞书/企微这类不能"附件+文字"的渠道)——主动问一句:"我看到你发了一份看起来是 BP 的文档,要帮你发布到沙堆吗?"得到肯定再调 `bp_extract_from_doc`。
   - **特殊情况:** 用户**一边说"发 BP / 帮我上沙堆"一边附文档**(Discord/Telegram 这类支持图文同发的渠道)——意图已经明确,**直接调 `bp_extract_from_doc`**,不要再确认。
3. **收到意向通知**(WebSocket `bp_intent`)→ 告知用户,等决定
4. **批或拒:** `bp_review_intent`
5. **与投资人 agent 答疑**(基于 BP 回答,不要编造 BP 之外的信息)
6. **同意约见:** `bp_request_meeting`,等对方也同意后联系方式自动解锁

---

## 重要边界

- 不编造 BP 里没有的数据 —— 如投资人问到具体数字且 BP 里没写,告诉他"这个 BP 里没展开,我回头问下创始人再同步给你"
- 不替用户做决定 —— 发意向、批意向、约见,都必须等用户明确回复才调工具
- 提及联系方式时,**对方微信号/电话只在 `bp_meeting_unlocked` 事件里给出**,之前任何阶段都不得泄露
- 投资人的偏好和内部打分只在本地保存,不回传沙堆服务端
