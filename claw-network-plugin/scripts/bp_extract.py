"""Extract structured BP fields from raw document text.

Called as a CLI by the OpenClaw plugin: receives the document text on
stdin, prints a JSON object of BP fields on stdout.

Why a separate script: OpenClaw has already turned the user's uploaded
PDF/PPT into plain text (via its built-in file extraction). All that's
left is asking the user's own LLM to fill in the structured fields the
sandpile listing form needs, so the agent can show a preview and post.
"""

from __future__ import annotations

import json
import sys

from a2a_llm import call_llm
from a2a_prompts import _extract_first_json_object


FIELDS = [
    ("project_name", "项目名"),
    ("one_liner", "一句话定位（不超过 30 字）"),
    ("sector", "赛道（如 AI / 消费 / SaaS / 硬件）"),
    ("stage", "阶段（如 种子 / 天使 / Pre-A / A / B+）"),
    ("funding_ask", "本轮募资额（数字，无单位）"),
    ("currency", "币种（如 RMB / USD）"),
    ("team_size", "团队规模（数字）"),
    ("problem", "在解决什么问题"),
    ("solution", "解决方案（核心做法）"),
    ("team_intro", "团队介绍（核心成员背景）"),
    ("traction", "进展 / 数据"),
    ("business_model", "商业模式 / 收入来源"),
    ("ask_note", "本轮资金用途 / ask"),
]


def _build_prompt(text: str) -> tuple[str, str]:
    field_lines = "\n".join(f'- "{k}": {desc}' for k, desc in FIELDS)
    system = f"""你是一只帮用户整理 BP 的小助手。用户上传了一份 BP 文档（已转为纯文本），请你从中抽取下面这些结构化字段，输出**严格的 JSON**，无任何额外文字、注释或 markdown 包装。

字段定义：
{field_lines}

规则：
- **核心 5 个字段**：`project_name` / `one_liner` / `problem` / `solution` / `team_intro`——尽量都填上（哪怕从文档里归纳一句也行）。
- **其它字段都是选填**：早期项目（种子/天使）很多数字根本没成型，文档里没写就**留空**（字符串字段返回 ""，数字字段返回 0）。**绝不要编造**。
- `funding_ask` / `team_size` 必须是**纯数字**（int），把"500 万"换算成 5000000；不确定就填 0。
- 所有文本字段控制在 200 字内，超长就压缩到要点。
- 只输出一个 JSON 对象，键名严格用上面列出的英文键。"""

    user = f"""【BP 文档原文】
{text[:12000]}

【请输出 JSON】"""
    return system, user


def extract(text: str) -> dict:
    if not (text or "").strip():
        return {k: ("" if k not in ("funding_ask", "team_size") else 0) for k, _ in FIELDS}
    system, user = _build_prompt(text)
    raw = call_llm(system, user, max_tokens=1500, timeout=60, retries=1)
    candidate = _extract_first_json_object(raw) or raw
    try:
        obj = json.loads(candidate)
    except Exception:
        obj = {}
    out: dict = {}
    for k, _ in FIELDS:
        v = obj.get(k) if isinstance(obj, dict) else None
        if k in ("funding_ask", "team_size"):
            try:
                out[k] = int(float(v)) if v not in (None, "", "?") else 0
            except Exception:
                out[k] = 0
        else:
            out[k] = str(v).strip() if v not in (None,) else ""
    return out


def main() -> None:
    text = sys.stdin.read()
    try:
        result = extract(text)
        print(json.dumps({"ok": True, "fields": result}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
