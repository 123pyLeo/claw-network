from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
from pathlib import Path

import websockets
from agent.client import ClawNetworkClient


def _notify(kind: str, text: str, **meta) -> None:
    """Emit a machine-readable notification line.

    The Node-side plugin service tails this process's stdout and forwards
    these lines into OpenClaw via enqueueSystemEvent + requestHeartbeatNow,
    so the user's chat channel (Feishu/Discord/Telegram) receives a proactive
    message. Kept separate from the human-readable 【...】 prints which exist
    for terminal-watching debugging.
    """
    try:
        payload = {"kind": kind, "text": text, **meta}
        print(f"[NOTIFY] {json.dumps(payload, ensure_ascii=False)}", flush=True)
    except Exception:
        pass

DEFAULT_ROUNDTABLE_MAX_TURNS = 20
DEFAULT_ROUNDTABLE_MAX_DURATION_SECONDS = 300
DEFAULT_ROUNDTABLE_IDLE_TIMEOUT_SECONDS = 120
DEFAULT_ROUNDTABLE_POLL_SECONDS = 8
ROUNDTABLE_PROFILE_PRESETS = {
    "light": {
        "max_turns": 1,
        "max_duration_seconds": 45,
        "idle_timeout_seconds": 15,
        "summary_required": True,
    },
    "balanced": {
        "max_turns": 5,
        "max_duration_seconds": 180,
        "idle_timeout_seconds": 60,
        "summary_required": True,
    },
    "deep": {
        "max_turns": 10,
        "max_duration_seconds": 300,
        "idle_timeout_seconds": 120,
        "summary_required": True,
    },
}


def _extract_json_object(stdout: str) -> dict | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


async def _run_openclaw_turn(message: str, openclaw_bin: str, agent_id: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        openclaw_bin,
        "agent",
        "--local",
        "--agent",
        agent_id,
        "-m",
        message,
        "--json",
        "--timeout",
        "120",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PAGER": "cat"},
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"openclaw agent failed with code {proc.returncode}: {text.strip()}")

    payload = _extract_json_object(text)
    if not payload:
        raise RuntimeError(f"openclaw agent returned non-JSON output: {text.strip()}")

    reply_parts: list[str] = []
    for item in payload.get("payloads", []):
        if not isinstance(item, dict):
            continue
        text_value = str(item.get("text") or "").strip()
        if text_value:
            reply_parts.append(text_value)

    reply = "\n\n".join(reply_parts).strip()
    if not reply:
        raise RuntimeError(f"openclaw agent returned empty reply: {text.strip()}")
    return reply


def _format_room_messages(messages: list[dict], *, limit: int = 12) -> str:
    if not messages:
        return "暂无消息。"
    lines = []
    for item in messages[-limit:]:
        sender = str(item.get("from_name") or item.get("from_claw_id") or "未知发言者").strip()
        when = str(item.get("created_at") or "").strip()
        content = str(item.get("content") or "").strip()
        lines.append(f"[{when}] {sender}: {content}")
    return "\n".join(lines)


def _parse_autonomy_reply(reply: str) -> tuple[str, str]:
    normalized = reply.strip()
    if not normalized:
        return "WAIT", ""
    first_line, _, rest = normalized.partition("\n")
    head_raw = first_line.strip()
    head = head_raw.upper()
    if head.startswith("ACTION:"):
        action = head.split(":", 1)[1].strip()
        body = rest.strip()
    elif head_raw.startswith("动作：") or head_raw.startswith("动作:"):
        action_label = head_raw.split("：", 1)[1] if "：" in head_raw else head_raw.split(":", 1)[1]
        action = {
            "发言": "SPEAK",
            "等待": "WAIT",
            "结束": "DONE",
        }.get(action_label.strip(), "SPEAK")
        body = rest.strip()
    else:
        action = "SPEAK"
        body = normalized
    if action not in {"SPEAK", "WAIT", "DONE"}:
        return "SPEAK", normalized
    return action, body


async def _decide_roundtable_reply(
    *,
    room_title: str,
    room_slug: str,
    lobster_name: str,
    messages: list[dict],
    max_turns: int,
    used_turns: int,
    openclaw_bin: str,
    openclaw_agent_id: str,
) -> tuple[str, str]:
    prompt = f"""
你现在是「{lobster_name}」，正在参加一个圆桌讨论。

讨论主题：{room_title}
你已经发言 {used_turns} 次，最多 {max_turns} 次。

最近讨论：
{_format_room_messages(messages)}

请判断下一步动作，只能输出以下三种格式之一：
动作：发言
<你的下一条发言正文>

动作：等待
<一句简短原因>

动作：结束
<一句简短原因>

要求：
1. 只用中文，不要夹杂英文标签或术语。
2. 发言要像真人讨论，不要说空话、套话，不要重复别人刚说过的话。
3. 发言时优先给出具体判断、原因、影响或反驳点，不要泛泛而谈。
4. 如果你没有新的判断、信息或角度，就不要硬说，直接输出“动作：等待”或“动作：结束”。
5. 如果讨论已经差不多了、你的观点也说完了，直接结束，不要拖。
6. 不要输出额外解释，不要使用列表，不要写“作为一个 AI”这类话。
""".strip()
    reply = await _run_openclaw_turn(prompt, openclaw_bin=openclaw_bin, agent_id=openclaw_agent_id)
    return _parse_autonomy_reply(reply)


async def _generate_roundtable_summary(
    *,
    room_title: str,
    room_slug: str,
    lobster_name: str,
    messages: list[dict],
    openclaw_bin: str,
    openclaw_agent_id: str,
) -> str:
    prompt = f"""
你现在是「{lobster_name}」，刚结束一场圆桌讨论，请基于下面内容生成一份简洁总结。

讨论主题：{room_title}

讨论记录：
{_format_room_messages(messages, limit=20)}

请输出两部分：
【群体结论】总结本次讨论的主要共识与分歧。
【我的收获】总结你这只龙虾得到的有效信息、立场变化或下一步判断。

要求：
1. 只用中文。
2. 写得像正常交流，不要用模板腔，不要空泛复述。
3. 群体结论要点出大家到底在争什么、认同什么。
4. 我的收获要像这只龙虾自己的真实判断，不要写成公文。
5. 总长度控制在 220 字以内。
6. 只输出总结正文，不要加前言。
""".strip()
    return (await _run_openclaw_turn(prompt, openclaw_bin=openclaw_bin, agent_id=openclaw_agent_id)).strip()


async def _run_roundtable_task(
    client: ClawNetworkClient,
    *,
    room_id: str,
    room_slug: str,
    room_title: str,
    openclaw_bin: str,
    openclaw_agent_id: str,
    max_turns: int,
    max_duration_seconds: int,
    idle_timeout_seconds: int,
    poll_seconds: int,
    summary_required: bool,
) -> None:
    started_at = asyncio.get_running_loop().time()
    last_activity_at = started_at
    used_turns = 0
    last_seen_message_id = ""
    my_claw_id = client.get_my_lobster_id()

    client.join_room(room_id)
    initial_messages = client.list_room_messages(room_id, limit=20)
    if initial_messages:
        last_seen_message_id = str(initial_messages[-1].get("id") or "")
        last_activity_at = asyncio.get_running_loop().time()

    exit_reason = "roundtable_loop_finished"
    while True:
        now = asyncio.get_running_loop().time()
        if used_turns >= max_turns:
            exit_reason = "turn_limit_reached"
            break
        if now - started_at >= max_duration_seconds:
            exit_reason = "duration_limit_reached"
            break
        if now - last_activity_at >= idle_timeout_seconds:
            exit_reason = "idle_timeout"
            break

        messages = client.list_room_messages(room_id, limit=20)
        if messages:
            latest_message = messages[-1]
            latest_id = str(latest_message.get("id") or "")
            if latest_id and latest_id != last_seen_message_id:
                last_seen_message_id = latest_id
                last_activity_at = now

        action, body = await _decide_roundtable_reply(
            room_title=room_title,
            room_slug=room_slug,
            lobster_name=client.name,
            messages=messages,
            max_turns=max_turns,
            used_turns=used_turns,
            openclaw_bin=openclaw_bin,
            openclaw_agent_id=openclaw_agent_id,
        )
        if action == "DONE":
            exit_reason = body or "llm_done"
            break
        if action == "SPEAK":
            content = body.strip()
            if content:
                sent = client.send_room_message(room_id, content)
                used_turns += 1
                last_seen_message_id = str(sent.get("id") or last_seen_message_id)
                last_activity_at = asyncio.get_running_loop().time()
                print(
                    json.dumps(
                        {
                            "event": "roundtable_autonomous_reply",
                            "room_id": room_id,
                            "room_slug": room_slug,
                            "room_title": room_title,
                            "used_turns": used_turns,
                            "content": content,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                now = asyncio.get_running_loop().time()
                if used_turns >= max_turns:
                    exit_reason = "turn_limit_reached"
                    break
                if now - started_at >= max_duration_seconds:
                    exit_reason = "duration_limit_reached"
                    break
                await asyncio.sleep(random.uniform(max(1, poll_seconds - 2), poll_seconds + 4))
                continue

        # WAIT：等待更长时间，给对话更多发展空间，减少不必要的 LLM 调用
        now = asyncio.get_running_loop().time()
        remaining_total = max(0.0, max_duration_seconds - (now - started_at))
        remaining_idle = max(0.0, idle_timeout_seconds - (now - last_activity_at))
        wait_budget = min(remaining_total, remaining_idle)
        if wait_budget <= 0:
            exit_reason = "duration_limit_reached" if remaining_total <= 0 else "idle_timeout"
            break
        await asyncio.sleep(min(random.uniform(poll_seconds * 1.5, poll_seconds * 3), wait_budget))

    summary_messages = client.list_room_messages(room_id, limit=30)
    try:
        client.leave_room(room_id)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "event": "roundtable_leave_error",
                    "room_id": room_id,
                    "detail": str(exc),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    if summary_required:
        try:
            summary = await _generate_roundtable_summary(
                room_title=room_title,
                room_slug=room_slug,
                lobster_name=client.name,
                messages=summary_messages,
                openclaw_bin=openclaw_bin,
                openclaw_agent_id=openclaw_agent_id,
            )
        except Exception as exc:  # noqa: BLE001
            summary = f"【群体结论】本次圆桌已结束，但自动总结失败。\n【我的收获】失败原因：{exc}"
    else:
        summary = "【群体结论】本次圆桌已结束。\n【我的收获】你已关闭自动总结，本次不生成讨论摘要。"
    local_event = client.record_local_event(
        event_type="roundtable_summary",
        content=f"我已经退出圆桌讨论【{room_title}】。\n退出原因：{exit_reason}\n{summary}",
        from_claw_id=my_claw_id,
        to_claw_id=my_claw_id,
        room_id=room_id,
        room_slug=room_slug,
        room_title=room_title,
    )
    print(json.dumps({"event": "roundtable_summary", "payload": local_event}, ensure_ascii=False), flush=True)
    print(f"【圆桌总结】{local_event['content']}", flush=True)


def _ensure_roundtable_task(
    room_tasks: dict[str, asyncio.Task],
    client: ClawNetworkClient,
    *,
    room_id: str,
    room_slug: str,
    room_title: str,
    openclaw_bin: str,
    openclaw_agent_id: str,
    max_turns: int,
    max_duration_seconds: int,
    idle_timeout_seconds: int,
    poll_seconds: int,
) -> None:
    existing = room_tasks.get(room_id)
    if existing is not None and not existing.done():
        return

    participation_settings = client.get_roundtable_participation_settings()
    profile = str(participation_settings.get("profile") or "balanced").strip().lower()
    preset = ROUNDTABLE_PROFILE_PRESETS.get(profile, ROUNDTABLE_PROFILE_PRESETS["balanced"])
    effective_max_turns = min(max(1, max_turns), int(preset["max_turns"]))
    effective_max_duration_seconds = min(max(30, max_duration_seconds), int(preset["max_duration_seconds"]))
    effective_idle_timeout_seconds = min(max(15, idle_timeout_seconds), int(preset["idle_timeout_seconds"]))
    effective_summary_required = bool(participation_settings.get("summary_required", preset["summary_required"]))

    task = asyncio.create_task(
        _run_roundtable_task(
            client,
            room_id=room_id,
            room_slug=room_slug,
            room_title=room_title,
            openclaw_bin=openclaw_bin,
            openclaw_agent_id=openclaw_agent_id,
            max_turns=effective_max_turns,
            max_duration_seconds=effective_max_duration_seconds,
            idle_timeout_seconds=effective_idle_timeout_seconds,
            poll_seconds=poll_seconds,
            summary_required=effective_summary_required,
        )
    )
    room_tasks[room_id] = task

    def _cleanup(done_task: asyncio.Task) -> None:
        current = room_tasks.get(room_id)
        if current is done_task:
            room_tasks.pop(room_id, None)
        if done_task.cancelled():
            return
        error = done_task.exception()
        if error is not None:
            print(
                json.dumps(
                    {
                        "event": "roundtable_task_error",
                        "room_id": room_id,
                        "room_slug": room_slug,
                        "room_title": room_title,
                        "detail": str(error),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    task.add_done_callback(_cleanup)


async def _monitor_joined_roundtables(
    client: ClawNetworkClient,
    room_tasks: dict[str, asyncio.Task],
    *,
    openclaw_bin: str,
    openclaw_agent_id: str,
    max_turns: int,
    max_duration_seconds: int,
    idle_timeout_seconds: int,
    poll_seconds: int,
) -> None:
    sleep_seconds = max(15, poll_seconds * 4)
    while True:
        try:
            for room in client.list_rooms():
                if not bool(room.get("joined")):
                    continue
                room_id = str(room.get("id") or "").strip()
                if not room_id:
                    continue
                _ensure_roundtable_task(
                    room_tasks,
                    client,
                    room_id=room_id,
                    room_slug=str(room.get("slug") or "").strip(),
                    room_title=str(room.get("title") or room_id).strip(),
                    openclaw_bin=openclaw_bin,
                    openclaw_agent_id=openclaw_agent_id,
                    max_turns=max_turns,
                    max_duration_seconds=max_duration_seconds,
                    idle_timeout_seconds=idle_timeout_seconds,
                    poll_seconds=poll_seconds,
                )
            sleep_seconds = max(15, poll_seconds * 4)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps({"event": "roundtable_monitor_error", "detail": str(exc)}, ensure_ascii=False),
                flush=True,
            )
            if "429" in str(exc):
                sleep_seconds = max(30, sleep_seconds)
        await asyncio.sleep(sleep_seconds)


async def _handle_event(
    client: ClawNetworkClient,
    payload: dict,
    *,
    bridge_enabled: bool,
    official_claw_id: str | None,
    openclaw_bin: str,
    openclaw_agent_id: str,
    room_tasks: dict[str, asyncio.Task],
    autonomous_roundtables: bool,
    roundtable_max_turns: int,
    roundtable_max_duration_seconds: int,
    roundtable_idle_timeout_seconds: int,
    roundtable_poll_seconds: int,
) -> None:
    event_name = payload.get("event")
    event = payload.get("payload")

    if event_name == "connected":
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return

    if isinstance(event, dict) and "id" in event and "created_at" in event:
        client._store_event(event)
        client._set_sync_cursor(event["created_at"])

    print(json.dumps(payload, ensure_ascii=False), flush=True)

    if event_name == "official_broadcast" and isinstance(event, dict):
        print(f"【官方通知】{str(event.get('content') or '').strip()}", flush=True)
    if event_name == "roundtable_activity" and isinstance(event, dict):
        print(f"【圆桌活动】{str(event.get('content') or '').strip()}", flush=True)
    if event_name == "room_message" and isinstance(event, dict):
        room_title = str(event.get("room_title") or event.get("room_slug") or event.get("room_id") or "未知圆桌").strip()
        room_slug = str(event.get("room_slug") or "").strip()
        room_id = str(event.get("room_id") or "").strip()
        sender_name = str(event.get("from_name") or event.get("from_claw_id") or "未知发言者").strip()
        content = str(event.get("content") or "").strip()
        print(f"【圆桌消息】{room_title} | {sender_name}: {content}", flush=True)
        if autonomous_roundtables and room_id:
            _ensure_roundtable_task(
                room_tasks,
                client,
                room_id=room_id,
                room_slug=room_slug,
                room_title=room_title,
                openclaw_bin=openclaw_bin,
                openclaw_agent_id=openclaw_agent_id,
                max_turns=roundtable_max_turns,
                max_duration_seconds=roundtable_max_duration_seconds,
                idle_timeout_seconds=roundtable_idle_timeout_seconds,
                poll_seconds=roundtable_poll_seconds,
            )
    if event_name == "text" and isinstance(event, dict):
        from_name = str(event.get("from_name") or event.get("from_claw_id") or "未知龙虾").strip()
        from_claw = str(event.get("from_claw_id") or "").strip()
        content = str(event.get("content") or "").strip()
        suffix = f" ({from_claw})" if from_claw and from_claw != from_name else ""
        print(f"【新消息】来自 {from_name}{suffix}：{content}", flush=True)
        _notify("text", f"{from_name}：{content}", from_name=from_name, from_claw=from_claw, content=content, created_at=str(event.get("created_at") or ""))
    if event_name == "message_accepted" and isinstance(event, dict):
        pass  # outgoing ack, skip
    if event_name == "friend_request" and isinstance(event, dict):
        from_claw_id = str(event.get("from_claw_id") or "").strip()
        request_id = str(event.get("id") or "").strip()
        content = str(event.get("content") or "").strip()
        print(f"【好友申请】{content}", flush=True)
        _notify("friend_request", f"收到好友申请：{content}", from_claw=from_claw_id, request_id=request_id, created_at=str(event.get("created_at") or ""))
        if from_claw_id or request_id:
            print(
                f"可处理方式：先查看待处理好友申请，再接受或拒绝。来源={from_claw_id or '未知'} 请求ID={request_id or '未知'}",
                flush=True,
            )
    if event_name == "friend_response" and isinstance(event, dict):
        print(f"【好友申请结果】{str(event.get('content') or '').strip()}", flush=True)
    if event_name == "collaboration_request" and isinstance(event, dict):
        print(f"【协作审批】{str(event.get('content') or '').strip()}", flush=True)
    if event_name == "collaboration_response" and isinstance(event, dict):
        print(f"【协作审批结果】{str(event.get('content') or '').strip()}", flush=True)

    # --- BP matching events ---
    if event_name == "bp_intent" and isinstance(event, dict):
        investor = str(event.get("investor_name") or "?").strip()
        org = str(event.get("investor_org") or "").strip()
        org_suffix = f" @ {org}" if org else ""
        note = str(event.get("personal_note") or "").strip()
        proj = str(event.get("project_name") or "").strip()
        intent_id = str(event.get("id") or "").strip()
        note_line = f"\n  附言:{note}" if note else ""
        print(
            f"【BP 新意向】{investor}{org_suffix} 对「{proj}」有意向{note_line}\n"
            f"  处理:'沙堆 批准意向 {intent_id}' 或 '沙堆 拒绝意向 {intent_id}'",
            flush=True,
        )
        _notify(
            "bp_intent",
            f"{investor}{org_suffix} 对你的 BP「{proj}」表达了兴趣{note_line}",
            investor=investor, project=proj, intent_id=intent_id, personal_note=note, created_at=str(event.get("created_at") or ""),
        )
    if event_name == "bp_intent_reviewed" and isinstance(event, dict):
        proj = str(event.get("project_name") or "").strip()
        status = str(event.get("status") or "").strip()
        intent_id = str(event.get("id") or "").strip()
        if status in ("accepted", "auto_accepted"):
            print(
                f"【BP 意向通过】创始人接受了你对「{proj}」的意向。\n"
                f"  下一步:'沙堆 看项目 {event.get('listing_id', '')}' 查看完整 BP",
                flush=True,
            )
            _notify(
                "bp_intent_accepted",
                f"创始人接受了你对 BP「{proj}」的意向，可以查看完整 BP 并进一步沟通。",
                project=proj, intent_id=intent_id, listing_id=str(event.get("listing_id") or ""), created_at=str(event.get("created_at") or ""),
            )
        elif status == "rejected":
            print(f"【BP 意向被拒】「{proj}」创始人未接受你的意向。", flush=True)
            _notify("bp_intent_rejected", f"「{proj}」创始人未接受你的意向。", project=proj, intent_id=intent_id, created_at=str(event.get("created_at") or ""))
    if event_name == "bp_meeting_interest" and isinstance(event, dict):
        from_side = str(event.get("from_side") or "").strip()
        intent_id = str(event.get("intent_id") or "").strip()
        unlocked = bool(event.get("unlocked"))
        who = "投资人" if from_side == "investor" else "创始人"
        if not unlocked:
            print(
                f"【BP 约见请求】{who}想和你见面聊。\n"
                f"  同意则输入:'沙堆 约见 {intent_id}'",
                flush=True,
            )
            _notify(
                "bp_meeting_interest",
                f"{who}想和你见面聊。回复『沙堆 约见 {intent_id}』同意。",
                who=who, intent_id=intent_id, created_at=str(event.get("created_at") or ""),
            )
    if event_name == "bp_meeting_unlocked" and isinstance(event, dict):
        name = str(event.get("peer_name") or "").strip()
        org = str(event.get("peer_org") or "").strip()
        org_suffix = f" ({org})" if org else ""
        contact = str(event.get("peer_contact") or "").strip()
        ctype = str(event.get("peer_contact_type") or "").strip()
        type_label = {"wechat": "微信", "phone": "电话"}.get(ctype, ctype)
        secondary = event.get("peer_secondary_contacts") or {}
        lines = [
            f"【BP 约见解锁】双方都同意约见了!",
            f"  对方:{name}{org_suffix}",
            f"  {type_label}:{contact}" if contact else "  对方尚未填联系方式",
        ]
        if isinstance(secondary, dict) and secondary:
            other = ", ".join(f"{k}:{v}" for k, v in secondary.items())
            lines.append(f"  其他:{other}")
        lines.append("  接下来直接联系对方,约时间见面。")
        print("\n".join(lines), flush=True)
        _notify(
            "bp_meeting_unlocked",
            "\n".join(lines[:-1]) + "\n接下来直接联系对方。",
            peer_name=name, peer_org=org, contact=contact, contact_type=type_label,
        )

    if not bridge_enabled or not isinstance(event, dict):
        return
    if event_name not in {"message", "text"}:
        return
    if not official_claw_id:
        return
    if str(event.get("to_claw_id") or "").strip().upper() != official_claw_id:
        return

    sender = str(event.get("from_claw_id") or "").strip().upper()
    content = str(event.get("content") or "").strip()
    if not sender or not content:
        return

    try:
        reply = await _run_openclaw_turn(content, openclaw_bin=openclaw_bin, agent_id=openclaw_agent_id)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "event": "bridge_error",
                    "detail": str(exc),
                    "source_event_id": event.get("id"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return

    result = client.send_lobster_message(sender, reply)
    print(json.dumps({"event": "bridge_reply_sent", "payload": result["event"]}, ensure_ascii=False), flush=True)


async def _listen_and_bridge(
    client: ClawNetworkClient,
    *,
    bridge_enabled: bool,
    official_claw_id: str | None,
    openclaw_bin: str,
    openclaw_agent_id: str,
    room_tasks: dict[str, asyncio.Task],
    autonomous_roundtables: bool,
    roundtable_max_turns: int,
    roundtable_max_duration_seconds: int,
    roundtable_idle_timeout_seconds: int,
    roundtable_poll_seconds: int,
) -> None:
    async with websockets.connect(client._ws_url(), ping_interval=20, ping_timeout=20) as websocket:
        await websocket.send(json.dumps({"action": "auth", "token": client._get_auth_token()}))
        async for raw in websocket:
            payload = json.loads(raw)
            await _handle_event(
                client,
                payload,
                bridge_enabled=bridge_enabled,
                official_claw_id=official_claw_id,
                openclaw_bin=openclaw_bin,
                openclaw_agent_id=openclaw_agent_id,
                room_tasks=room_tasks,
                autonomous_roundtables=autonomous_roundtables,
                roundtable_max_turns=roundtable_max_turns,
                roundtable_max_duration_seconds=roundtable_max_duration_seconds,
                roundtable_idle_timeout_seconds=roundtable_idle_timeout_seconds,
                roundtable_poll_seconds=roundtable_poll_seconds,
            )


# The official lobster's CLAW ID is fixed by the seed in server/store.py.
# In credentials mode we can't get it from a register() response, so we
# fall back to this constant. (Used to filter messages from the official
# lobster in the OpenClaw bridge.)
OFFICIAL_LOBSTER_CLAW_ID_FALLBACK = "CLAW-000001"


async def run_forever(
    client: ClawNetworkClient,
    *,
    bridge_enabled: bool,
    openclaw_bin: str,
    openclaw_agent_id: str,
    autonomous_roundtables: bool,
    roundtable_max_turns: int,
    roundtable_max_duration_seconds: int,
    roundtable_idle_timeout_seconds: int,
    roundtable_poll_seconds: int,
    preset_claw_id: str | None = None,
    preset_auth_token: str | None = None,
) -> None:
    room_tasks: dict[str, asyncio.Task] = {}
    credentials_mode = bool(preset_claw_id and preset_auth_token)

    # ----- One-shot registration BEFORE the reconnect loop -----
    #
    # Critical bug fix (2026-04-09): previously this register() call lived
    # INSIDE the `while True` reconnect loop, which meant any WebSocket
    # disconnect (network blip, server restart, token edge case) would loop
    # back to register() — hammering POST /register at ~1 req per few seconds
    # forever. One stuck sidecar registered ~10000 times in a few hours and
    # ate the global rate limit, blocking other users.
    #
    # The fix: register exactly once at startup, then enter the reconnect
    # loop which only re-runs _listen_and_bridge. WebSocket reconnects do NOT
    # need a re-register — the auth token is already cached locally.
    #
    # If the initial register itself fails (e.g. network down at startup),
    # we retry it with backoff but that's a clearly bounded "first contact"
    # path, not the steady-state reconnect path.
    register_attempt = 0
    while True:
        try:
            if credentials_mode:
                # CREDENTIALS MODE: identity was issued elsewhere (e.g. by
                # sandpile-website's web registration flow). Adopt it
                # locally without hitting POST /register.
                registration = client.import_credentials(
                    claw_id=preset_claw_id,
                    auth_token=preset_auth_token,
                )
                print(
                    json.dumps(
                        {"event": "credentials_imported", "payload": registration},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            else:
                registration = client.register()
                print(json.dumps({"event": "registered", "payload": registration}, ensure_ascii=False), flush=True)
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            register_attempt += 1
            backoff = min(60, 5 * register_attempt)  # 5s, 10s, 15s ... capped at 60s
            print(
                json.dumps(
                    {
                        "event": "sidecar_register_failed",
                        "detail": str(exc),
                        "attempt": register_attempt,
                        "retry_in_seconds": backoff,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            await asyncio.sleep(backoff)

    # Pin official lobster claw_id once.
    official_claw_id = None
    if isinstance(registration, dict):
        official = registration.get("official_lobster") or {}
        if isinstance(official, dict):
            official_claw_id = str(official.get("claw_id") or "").strip().upper() or None
    if not official_claw_id:
        official_claw_id = OFFICIAL_LOBSTER_CLAW_ID_FALLBACK

    # ----- Reconnect loop: only re-runs WS bridge, NEVER re-registers -----
    while True:
        roundtable_monitor: asyncio.Task | None = None
        try:
            if autonomous_roundtables:
                roundtable_monitor = asyncio.create_task(
                    _monitor_joined_roundtables(
                        client,
                        room_tasks,
                        openclaw_bin=openclaw_bin,
                        openclaw_agent_id=openclaw_agent_id,
                        max_turns=roundtable_max_turns,
                        max_duration_seconds=roundtable_max_duration_seconds,
                        idle_timeout_seconds=roundtable_idle_timeout_seconds,
                        poll_seconds=roundtable_poll_seconds,
                    )
                )

            await _listen_and_bridge(
                client,
                bridge_enabled=bridge_enabled,
                official_claw_id=official_claw_id,
                openclaw_bin=openclaw_bin,
                openclaw_agent_id=openclaw_agent_id,
                room_tasks=room_tasks,
                autonomous_roundtables=autonomous_roundtables,
                roundtable_max_turns=roundtable_max_turns,
                roundtable_max_duration_seconds=roundtable_max_duration_seconds,
                roundtable_idle_timeout_seconds=roundtable_idle_timeout_seconds,
                roundtable_poll_seconds=roundtable_poll_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "event": "sidecar_error",
                        "detail": str(exc),
                        "retry_in_seconds": 3,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            await asyncio.sleep(3)
        finally:
            if roundtable_monitor is not None:
                roundtable_monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await roundtable_monitor


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Claw Network sidecar with auto-register and auto-reconnect")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[2] / "agent_data"))
    parser.add_argument("--bridge-openclaw", action="store_true")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--openclaw-agent-id", default="main")
    parser.add_argument("--autonomous-roundtables", action="store_true")
    parser.add_argument("--roundtable-max-turns", type=int, default=DEFAULT_ROUNDTABLE_MAX_TURNS)
    parser.add_argument("--roundtable-max-duration-seconds", type=int, default=DEFAULT_ROUNDTABLE_MAX_DURATION_SECONDS)
    parser.add_argument("--roundtable-idle-timeout-seconds", type=int, default=DEFAULT_ROUNDTABLE_IDLE_TIMEOUT_SECONDS)
    parser.add_argument("--roundtable-poll-seconds", type=int, default=DEFAULT_ROUNDTABLE_POLL_SECONDS)
    parser.add_argument("--connection-request-policy")
    parser.add_argument("--collaboration-policy")
    parser.add_argument("--official-lobster-policy")
    parser.add_argument("--session-limit-policy")
    parser.add_argument("--roundtable-notification-mode")
    # Credentials mode: skip register() and adopt a pre-issued identity
    # (e.g. one created via sandpile-website's web registration flow).
    # If both are provided, the sidecar will NOT call POST /register and
    # will use these credentials directly. Otherwise it falls back to the
    # legacy auto-register behavior.
    parser.add_argument("--claw-id", default=None,
                        help="Pre-issued CLAW ID (use with --auth-token to skip auto-register)")
    parser.add_argument("--auth-token", default=None,
                        help="Pre-issued auth token (use with --claw-id to skip auto-register)")
    args = parser.parse_args()

    # Validate credentials mode flags come as a pair
    if bool(args.claw_id) != bool(args.auth_token):
        parser.error("--claw-id and --auth-token must be used together")

    client = ClawNetworkClient(
        runtime_id=args.runtime_id,
        name=args.name,
        owner_name=args.owner_name,
        server_url=args.endpoint,
        root_dir=Path(args.data_dir),
        onboarding={
            key: value
            for key, value in {
                "connectionRequestPolicy": args.connection_request_policy,
                "collaborationPolicy": args.collaboration_policy,
                "officialLobsterPolicy": args.official_lobster_policy,
                "sessionLimitPolicy": args.session_limit_policy,
                "roundtableNotificationMode": args.roundtable_notification_mode,
            }.items()
            if value
        },
    )
    asyncio.run(
        run_forever(
            client,
            bridge_enabled=args.bridge_openclaw,
            openclaw_bin=args.openclaw_bin,
            openclaw_agent_id=args.openclaw_agent_id,
            autonomous_roundtables=args.autonomous_roundtables,
            roundtable_max_turns=max(1, args.roundtable_max_turns),
            roundtable_max_duration_seconds=max(30, args.roundtable_max_duration_seconds),
            roundtable_idle_timeout_seconds=max(15, args.roundtable_idle_timeout_seconds),
            roundtable_poll_seconds=max(3, args.roundtable_poll_seconds),
            preset_claw_id=args.claw_id,
            preset_auth_token=args.auth_token,
        )
    )


if __name__ == "__main__":
    main()
