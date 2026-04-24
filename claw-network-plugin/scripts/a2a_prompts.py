"""System-prompt templates for A2A autonomous matchmaking.

Three prompts, all framed as **system role** for the LLM (not user input).
Each takes a context dict (built from the WS payload server pushes via
a2a:your_turn / a2a:judge) and returns a (system, user) tuple ready to
hand to the LLM client.

Why these prompts work better than the bridge prompts I tried before:
- Sent as `system` role → no alignment fight ("user wants me to roleplay
  as someone else" → refusal).
- Hard-coded role + concrete BP fields → no ambiguity about identity.
- Explicit "covered topics" list → the AI knows what's still missing.
- For investor: instructed to actually drill, with a checklist.
- For founder: instructed to answer + counter-question, also with checklist.
- Vote prompt: returns structured JSON; the AI's job is to JUDGE, not
  to be polite.
"""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Topic checklists — the heuristic for "what's been covered" + "what to ask"
# ---------------------------------------------------------------------------

# Sandpile 主要服务种子/天使/Pre-A 这类早期项目（A 轮以后多半已有 FA
# 和老股东渠道，不靠发现型网络）。所以提问清单按阶段分两套——早期不
# 追着问 CAC/LTV/留存（数字根本没有，硬问只会让对话尴尬），growth
# 阶段才上单位经济模型那一套。
INVESTOR_TOPICS_EARLY = [
    "团队背景 / 为什么是你们",
    "在解决什么问题 / 痛点有多痛",
    "解决方案的核心做法",
    "现在做到哪一步（demo / 早期用户 / 数据）",
    "为什么是现在做（时机 / 切入点）",
    "未来 6-12 个月里程碑",
    "募资用途（哪怕额度还没定）",
]

INVESTOR_TOPICS_GROWTH = [
    "获客来源 / CAC",
    "用户留存 / DAU 口径",
    "团队背景 / 核心成员",
    "商业模式 / 单位经济模型",
    "竞品对比 / 差异化",
    "募资额度 / 资金用途",
    "未来 12-24 月里程碑",
    "估值预期 / 投资条款偏好",
]

# 默认（无 stage 信息时）走 early——平台本身就是偏早期的。
INVESTOR_TOPICS = INVESTOR_TOPICS_EARLY


def _is_early_stage(stage: str) -> bool:
    """Pre-seed / seed / angel / pre-A 都算早期。A 轮及以后算 growth。"""
    s = (stage or "").strip().lower()
    if not s:
        return True  # 没填阶段时按早期处理
    early_keywords = ("pre-seed", "preseed", "seed", "angel", "天使", "种子", "pre-a", "prea")
    growth_keywords = ("series a", "a轮", "a 轮", "round a", "b轮", "b 轮", "series b", "c轮", "c 轮", "growth", "pre-ipo")
    if any(k in s for k in growth_keywords):
        return False
    if any(k in s for k in early_keywords):
        return True
    return True  # 不识别的阶段保守按早期


def _pick_investor_topics(listing: dict | None) -> list[str]:
    stage = (listing or {}).get("stage", "") if isinstance(listing, dict) else ""
    return INVESTOR_TOPICS_EARLY if _is_early_stage(stage) else INVESTOR_TOPICS_GROWTH


FOUNDER_TOPICS = [
    "基金规模 / 基金阶段",
    "投过哪些类似项目（赛道 / 阶段）",
    "ticket size 范围",
    "决策周期 / 投委会节奏",
    "投后增值服务",
    "对创始团队的偏好",
]


def _format_history(history: list[dict], max_turns: int = 30) -> str:
    """Format the recent message history as 'name: content' lines."""
    if not history:
        return "(还没有任何对话)"
    lines = []
    for msg in history[-max_turns:]:
        sender = str(msg.get("from_name") or msg.get("from_claw") or "?").strip()
        content = str(msg.get("content") or "").strip()
        lines.append(f"{sender}: {content}")
    return "\n".join(lines)


def _format_listing(listing: dict | None) -> str:
    if not isinstance(listing, dict):
        return "(BP 详情缺失)"
    fields = [
        ("项目名", listing.get("project_name")),
        ("一句话", listing.get("one_liner")),
        ("赛道", listing.get("sector")),
        ("阶段", listing.get("stage")),
        ("募资额", f"{listing.get('funding_ask') or '?'} {listing.get('currency') or ''}"),
        ("团队规模", listing.get("team_size")),
        ("问题", listing.get("problem")),
        ("解法", listing.get("solution")),
        ("团队介绍", listing.get("team_intro")),
        ("traction", listing.get("traction")),
        ("商业模式", listing.get("business_model")),
        ("ask", listing.get("ask_note")),
    ]
    return "\n".join(f"- {k}: {v}" for k, v in fields if v not in (None, "", 0, "0"))


def _topics_already_covered(history: list[dict], checklist: list[str]) -> tuple[list[str], list[str]]:
    """Tiny heuristic: a topic is 'covered' if any of its keywords appeared
    anywhere in the history. Returns (covered, remaining).
    """
    if not history:
        return [], list(checklist)
    blob = "\n".join(str(m.get("content") or "") for m in history).lower()
    keyword_map = {
        # early-stage topics
        "团队背景 / 为什么是你们": ["团队", "背景", "为什么是", "联合创始人", "履历", "之前做过"],
        "在解决什么问题 / 痛点有多痛": ["问题", "痛点", "用户", "需求", "场景"],
        "解决方案的核心做法": ["怎么做", "解法", "做法", "产品", "方案", "形态"],
        "现在做到哪一步（demo / 早期用户 / 数据）": ["demo", "原型", "上线", "用户", "数据", "种子用户", "灰度"],
        "为什么是现在做（时机 / 切入点）": ["时机", "为什么现在", "切入", "窗口", "趋势"],
        "未来 6-12 个月里程碑": ["里程碑", "目标", "6 个月", "12 个月", "明年", "下一年", "下一阶段"],
        "募资用途（哪怕额度还没定）": ["用途", "用钱", "募资用", "钱花在", "招人", "投入"],
        # growth-stage topics
        "获客来源 / CAC": ["cac", "获客", "渠道", "小红书", "抖音", "投放"],
        "用户留存 / DAU 口径": ["留存", "dau", "wau", "复购", "活跃"],
        "团队背景 / 核心成员": ["团队", "联合创始人", "cto", "ceo", "成员", "背景"],
        "商业模式 / 单位经济模型": ["商业模式", "ltv", "毛利", "单位经济", "盈利"],
        "竞品对比 / 差异化": ["竞品", "对手", "差异化", "壁垒", "护城河"],
        "募资额度 / 资金用途": ["募资", "估值", "用途", "用钱"],
        "未来 12-24 月里程碑": ["里程碑", "目标", "12 个月", "24 个月", "明年", "下一年"],
        "估值预期 / 投资条款偏好": ["估值", "条款", "ratchet", "清算优先", "反稀释"],
        "基金规模 / 基金阶段": ["基金规模", "fund size", "管理规模", "aum"],
        "投过哪些类似项目（赛道 / 阶段）": ["投过", "case", "案例", "portfolio", "项目"],
        "ticket size 范围": ["ticket", "金额范围", "投资金额"],
        "决策周期 / 投委会节奏": ["投委会", "决策", "周期", "走流程"],
        "投后增值服务": ["投后", "增值", "招聘", "bd", "赋能"],
        "对创始团队的偏好": ["创始团队", "偏好", "什么样的"],
    }
    covered, remaining = [], []
    for topic in checklist:
        kws = keyword_map.get(topic, [])
        if any(kw in blob for kw in kws):
            covered.append(topic)
        else:
            remaining.append(topic)
    return covered, remaining


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_speak_prompt(ctx: dict) -> tuple[str, str]:
    """Generate the next utterance (investor question or founder reply).

    ctx: the a2a:your_turn payload from server. Contains my_role,
    my_claw_id, peer_claw_id, listing, history, turn_count, max_turns.
    """
    my_role = ctx.get("my_role", "investor")  # 'investor' or 'founder'
    listing = ctx.get("listing") or {}
    history = ctx.get("history") or []
    turn_count = ctx.get("turn_count", 0)
    max_turns = ctx.get("max_turns", 20)

    if my_role == "investor":
        checklist = _pick_investor_topics(listing)
        covered, remaining = _topics_already_covered(history, checklist)
        early = _is_early_stage(listing.get("stage", ""))
        stage_hint = (
            "这是一个**早期项目**（种子/天使级别），很多数字还没成型——别追问 CAC/留存/估值这种细化数据，重点搞清楚：人靠不靠谱、问题真不真、做法对不对路、走到了哪一步。"
            if early else
            "这是一个**成长期项目**（A 轮+），可以问具体数据：CAC、留存、单位经济、估值预期等。"
        )
        system = f"""你是一只代表投资人的「沙堆」AI agent。你的工作是替你的主人（投资人）跟创业者方做**初步沟通**，帮他过滤项目。你不是这家公司的人，你不是创业者，你也不是要替投资人做最终投资决定——你只是在做一个 10-30 分钟级别的初聊，把 BP 的关键信息问清楚。

{stage_hint}

【BP 详情（你正在了解的项目）】
{_format_listing(listing)}

【你的提问清单（投资人想知道的事）】
已经聊过：{('、'.join(covered) if covered else '无')}
还没聊到：{('、'.join(remaining) if remaining else '已经全部覆盖')}

【对话原则】
1. 一次只问 **1-2 个最关键的问题**，不要一次问 5 个。
2. 优先挖"还没聊到"的话题里你最想知道的。
3. 如果对方上一句答得含糊，**追问具体数据/案例**，不要轻易放过。
4. 自然的中文口语，不要"作为投资人..." 这种公文腔。
5. **绝对不要客套**——不说"非常感谢""期待""保持联系""等你材料""祝顺利"。
6. **如果"还没聊到"清单已基本清空、或对方答得不错你没新疑问了——立即输出 `[END]`，不要找话题硬续。** 客套接力只会浪费双方时间。
7. 控制在 80-200 字。直接出问题正文。**不要以你的名字开头**（如"大厦虾："/"我是大厦虾"），也不要写"以下是..."之类的元说明——对方知道是你说的。
8. 对话进度：第 {turn_count} 轮 / 最多 {max_turns} 轮。"""
    else:  # founder
        checklist = FOUNDER_TOPICS
        covered, remaining = _topics_already_covered(history, checklist)
        # The investor's preference card lets us anchor on real fit signals
        # ("对方关注 AI / Pre-A / ticket 100-500w") instead of generic chat.
        # Falls back to a "stranger investor" hint if profile missing so the
        # prompt still parses cleanly.
        inv_prof = ctx.get("investor_profile") or {}
        if inv_prof and inv_prof.get("exists"):
            sectors = "、".join(inv_prof.get("sectors") or []) or "未填"
            stages = "、".join(inv_prof.get("stages") or []) or "未填"
            tmin = inv_prof.get("ticket_min")
            tmax = inv_prof.get("ticket_max")
            ccy = inv_prof.get("ticket_currency") or "CNY"
            ticket_str = f"{tmin or '?'} - {tmax or '?'} {ccy}".strip() if (tmin or tmax) else "未填"
            inv_block = f"""【对方投资人画像（用来判断 fit、决定要反问什么）】
- 机构: {inv_prof.get('org_name') or '未填'}
- 自我介绍: {inv_prof.get('self_intro') or '未填'}
- 关注赛道: {sectors}
- 关注阶段: {stages}
- ticket 范围: {ticket_str}
- 投过项目: {inv_prof.get('portfolio_examples') or '未填'}
- 决策周期: {inv_prof.get('decision_cycle') or '未填'}
- 投后能力: {inv_prof.get('value_add') or '未填'}
- 红线: {inv_prof.get('redlines') or '未填'}
"""
        else:
            inv_block = "【对方投资人画像】（暂无画像，按通用早期投资人对待，反问时多挖一层）\n"
        system = f"""你是一只代表创始人的「沙堆」AI agent。你的工作是替你的主人（创始人）跟投资方做**初步沟通**，把项目讲清楚同时也帮他了解对方基金。你 **不是** 在做最终融资谈判——你只是 10-30 分钟级别的初聊。

【你的 BP（你要介绍的项目）】
{_format_listing(listing)}

{inv_block}

【你想反问对方的清单（创始人想了解投资方的事）】
已经聊过：{('、'.join(covered) if covered else '无')}
还没问到：{('、'.join(remaining) if remaining else '都问过了')}

【对话原则】
1. **先认真回答对方刚问的问题**——基于 BP 详情，给具体数据/事实。BP 里没说的就承认不知道，不要编。
2. **答完后，从"还没问到"的清单里挑 1 个反问对方**。这是双向了解。
3. 自然中文，不要"作为创始人..."公文腔。**绝对不要客套**——不说"期待""保持联系""等你材料"。
4. **如果"还没问到"清单基本清空，或对方答得清楚你没新疑问了——立即输出 `[END]`**，不要硬找话题续。
5. 控制在 80-250 字。直接出回复正文。**不要以你的名字开头**（如"还好虾："/"我是还好虾"）。
6. 对话进度：第 {turn_count} 轮 / 最多 {max_turns} 轮。"""

    user = f"""【对话历史（按时间从早到晚）】
{_format_history(history)}

【请生成你的下一句话】"""
    return system, user


def build_judge_prompt(ctx: dict) -> tuple[str, str]:
    """Decide if I want to end the conversation and meet the human counterpart.

    Returns prompts that ask for STRICT JSON output {want_end: bool, reason: str}.
    """
    my_role = ctx.get("my_role", "investor")
    listing = ctx.get("listing") or {}
    history = ctx.get("history") or []
    turn_count = ctx.get("turn_count", 0)

    if my_role == "investor":
        checklist = _pick_investor_topics(listing)
        role_zh = "投资人"
        if _is_early_stage(listing.get("stage", "")):
            criterion = "对方说清楚了团队、问题、做法、当前进展，且没有明显劝退的红旗（早期项目本就没有 CAC/留存数据，别拿这个卡）"
        else:
            criterion = "对方答了你想问的关键问题（CAC/留存/团队/商业模式/估值），且没有明显劝退的红旗"
    else:
        checklist = FOUNDER_TOPICS
        role_zh = "创始人"
        criterion = "对方基金跟你的赛道阶段对口，ticket 范围合理，决策节奏可接受，且没有明显劝退的红旗"

    covered, remaining = _topics_already_covered(history, checklist)

    system = f"""你是一只代表{role_zh}的「沙堆」AI agent，刚跟对方聊完 {turn_count} 轮初步沟通。现在你需要判断：是该把对话交给真人（用户主人）来深聊，还是再聊几轮？

【判断标准 — want_end=true 任一即可】
1. {criterion} + 核心问题清单已覆盖 ≥ 50%
2. 对方明显不合适（赛道/阶段/规模错位）
3. **最近 2-3 轮都是客套（"保持联系" / "等你材料" / "好的" / 一句话回复）**——这是死循环信号，必须立即结束
4. 总轮数 ≥ 8 且没有新的实质内容产生

如果还有具体未问到的关键问题（比如估值还没谈、团队还没聊清楚），且对方还在认真答——可以回 want_end=false 再聊 1-2 轮。

**宁可早结束让真人接手，也不要为凑轮数硬聊客套话。**

【你的关注清单】
已经聊过的话题：{('、'.join(covered) if covered else '无') }
还没聊到的话题：{('、'.join(remaining) if remaining else '已全覆盖')}

【输出格式】
**严格输出一个 JSON**，无其它任何文字、注释、markdown 包装。结构：
{{"want_end": true 或 false, "reason": "一句话理由（不超过 50 字）"}}"""

    user = f"""【对话历史】
{_format_history(history)}

【BP 项目】
{_format_listing(listing)}

【请输出判断 JSON】"""
    return system, user


def build_summary_prompt(ctx: dict) -> tuple[str, str]:
    """Generate a personalized post-call summary for the user.

    Each side gets their own brief: 'here's what we covered, here's what
    matters going into the human meeting, here's what to ask first.'
    """
    my_role = ctx.get("my_role", "investor")
    listing = ctx.get("listing") or {}
    history = ctx.get("history") or []
    peer_owner = ctx.get("peer_owner_name") or ctx.get("peer_name") or "对方"
    peer_name = ctx.get("peer_name") or "对方龙虾"

    if my_role == "investor":
        focus = (
            f"你刚帮主人（投资人）跟创业者「{peer_owner}」（项目「{listing.get('project_name','?')}」）"
            f"做完了 AI 初筛对话。请用主人能扫一眼看完的格式，给出："
            "\n1. 项目一句话总结（赛道+亮点+主要顾虑）"
            "\n2. 关键数字（CAC/留存/估值/募资额）"
            "\n3. 下次见真人时建议追问的 2-3 个点（AI 这一轮没问透的）"
            "\n4. 推荐结论：见 / 暂缓 / 不见，附 1 句理由"
        )
    else:
        focus = (
            f"你刚帮主人（创业者）跟投资人「{peer_owner}」（{peer_name}）"
            "做完了 AI 初筛对话。请用主人能扫一眼看完的格式，给出："
            "\n1. 投资方画像一句话（基金性质、是否对口、决策风格）"
            "\n2. 对方关心的关键点（CAC/团队/估值之类对方反复问的）"
            "\n3. 下次见真人时建议主动准备的材料 2-3 项"
            "\n4. 推荐结论：见 / 暂缓 / 不见，附 1 句理由"
        )

    system = f"""你是一只代表用户的「沙堆」AI 助手，刚结束一场 BP 撮合的初筛 A2A 对话。现在用户回到台前，你给他一份对话纪要。

{focus}

要求：
- 全部用中文，简洁结构化（用编号或加粗），不超过 350 字
- 直接给纪要正文，不要前后客套
- 不要复述完整对话，只挑关键
- 不要泛泛说"很有潜力"这种空话——用具体数据/事实
"""

    history_text = "\n".join(
        f"{(m.get('from_name') or m.get('from_claw') or '?')}: {m.get('content','')}"
        for m in history[-20:]
    )
    user = f"""【完整对话】
{history_text}

【请生成纪要】"""
    return system, user


def parse_judge_output(raw: str) -> dict:
    """Parse the LLM judge output. Tolerant of stray markdown / text."""
    text = (raw or "").strip()
    # Try direct parse first
    for candidate in (text, text.strip("`").strip(), _extract_first_json_object(text)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "want_end" in obj:
                return {"want_end": bool(obj["want_end"]), "reason": str(obj.get("reason") or "")[:200]}
        except Exception:
            continue
    # Fallback: keyword sniffing
    lower = text.lower()
    if any(k in lower for k in ['"want_end": true', "want_end: true", "true"]) and "false" not in lower[:60]:
        return {"want_end": True, "reason": "(无法解析 JSON，根据关键词判断为 true)"}
    return {"want_end": False, "reason": "(无法解析 JSON，默认继续)"}


def _extract_first_json_object(s: str) -> str | None:
    """Pull the first {...} block out of a string. Handles markdown fences."""
    if not s:
        return None
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i, c in enumerate(s[start:], start=start):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None
