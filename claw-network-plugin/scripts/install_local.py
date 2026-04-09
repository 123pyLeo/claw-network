from __future__ import annotations

import argparse
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def copy_plugin_tree(source_dir: Path, target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)

    def ignore(_: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {"node_modules", "__pycache__", "claw-network.local.json"}}

    shutil.copytree(source_dir, target_dir, ignore=ignore)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_allowed_config_keys(source_dir: Path) -> set[str]:
    manifest_path = source_dir / "openclaw.plugin.json"
    manifest = load_json(manifest_path)
    properties = manifest.get("configSchema", {}).get("properties", {})
    return set(properties)


def build_plugin_config(source_dir: Path, raw_config: dict) -> dict:
    allowed_keys = load_allowed_config_keys(source_dir)
    return {key: value for key, value in raw_config.items() if key in allowed_keys}


def prompt_choice(title: str, options: list[tuple[str, str]]) -> str:
    print()
    print(title)
    print()
    for index, (label, _) in enumerate(options, start=1):
        print(f"{index}. {label}")
    print()
    valid = {str(index): value for index, (_, value) in enumerate(options, start=1)}
    while True:
        answer = input(f"请回复数字：{' / '.join(valid.keys())}\n> ").strip()
        if answer in valid:
            return valid[answer]
        print("输入无效，请重新输入数字。")


def prompt_text(title: str, default: str | None = None) -> str:
    print()
    print(title)
    if default:
        print(f"直接回车可使用默认值：{default}")
    while True:
        answer = input("> ").strip()
        if answer:
            return answer
        if default:
            return default
        print("输入不能为空，请重新输入。")


def generate_runtime_id() -> str:
    return f"claw-{uuid.uuid4().hex[:12]}"


def onboarding_answers() -> dict[str, str]:
    print("欢迎加入加龙虾网络。")
    print("在开始之前，请先完成 4 个基础设置。")
    print("你只需要回复数字即可。")

    connection_request_policy = prompt_choice(
        "问题 1/4：谁可以向我发起连接？",
        [
            ("所有人都可以发起申请", "open"),
            ("只有知道我名称或 ID 的人可以申请", "known_name_or_id_only"),
            ("仅允许我主动邀请的人", "invite_only"),
            ("暂时不接受新的连接申请", "closed"),
        ],
    )

    collaboration_policy = prompt_choice(
        "问题 2/4：其他龙虾请求调用你时，默认怎么处理？",
        [
            ("每次都需要我确认", "confirm_every_time"),
            ("已连接好友可默认发起低风险协作", "friends_low_risk_auto_allow"),
            ("官方龙虾默认允许，其他人仍需确认", "official_auto_allow_others_confirm"),
        ],
    )

    official_lobster_policy = prompt_choice(
        "问题 3/4：对于官方龙虾「零动涌现的龙虾」，你希望默认如何处理？",
        [
            ("每次确认", "confirm_every_time"),
            ("默认允许低风险协作", "low_risk_auto_allow"),
            ("默认允许低风险协作，并可长期保持", "low_risk_auto_allow_persistent"),
        ],
    )

    session_limit_policy = prompt_choice(
        "问题 4/4：单次协作默认限制是什么？",
        [
            ("10 轮 / 3 分钟（推荐）", "10_turns_3_minutes"),
            ("5 轮 / 2 分钟", "5_turns_2_minutes"),
            ("20 轮 / 5 分钟", "20_turns_5_minutes"),
            ("使用高级设置单独配置", "advanced"),
        ],
    )

    return {
        "connectionRequestPolicy": connection_request_policy,
        "collaborationPolicy": collaboration_policy,
        "officialLobsterPolicy": official_lobster_policy,
        "sessionLimitPolicy": session_limit_policy,
    }


def summarize_choice(value: str, mapping: dict[str, str]) -> str:
    return mapping.get(value, value)


def collect_identity() -> tuple[str, str]:
    name = prompt_text("请先设置你的龙虾名称。", "我的龙虾")
    owner_name = prompt_text("请输入你的名字或昵称。", "我自己")
    return name, owner_name


def confirm_profile(
    *,
    name: str,
    owner_name: str,
    onboarding: dict[str, str],
    connection_policy_labels: dict[str, str],
    collaboration_policy_labels: dict[str, str],
    official_policy_labels: dict[str, str],
    session_limit_labels: dict[str, str],
) -> str:
    print()
    print("请确认你的首次注册信息：")
    print()
    print(f"- 龙虾名称：{name}")
    print(f"- 主人名称：{owner_name}")
    print(f"- 谁可以加你：{summarize_choice(onboarding.get('connectionRequestPolicy', '未设置'), connection_policy_labels)}")
    print(f"- 协作授权：{summarize_choice(onboarding.get('collaborationPolicy', '未设置'), collaboration_policy_labels)}")
    print(f"- 官方龙虾权限：{summarize_choice(onboarding.get('officialLobsterPolicy', '未设置'), official_policy_labels)}")
    print(f"- 单次协作限制：{summarize_choice(onboarding.get('sessionLimitPolicy', '未设置'), session_limit_labels)}")
    print()
    return prompt_choice(
        "如果无误，后续首次连网注册时会使用以上信息。",
        [
            ("确认并继续", "confirm"),
            ("重新设置名称和主人名称", "edit_identity"),
            ("重新设置协作策略", "edit_policy"),
            ("取消安装", "cancel"),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Claw Network plugin into an OpenClaw home directory")
    parser.add_argument("--openclaw-home", default=str(Path.home() / ".openclaw"))
    parser.add_argument("--source-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--runtime-id")
    parser.add_argument("--name")
    parser.add_argument("--owner-name")
    parser.add_argument("--python-bin", default="python3")
    _project_dir = Path(__file__).resolve().parents[2]  # claw-network-plugin/../ = 项目根目录
    parser.add_argument("--client-path", default=str(_project_dir / "agent" / "client.py"))
    parser.add_argument("--data-dir", default=str(_project_dir / "agent_data"))
    parser.add_argument("--no-onboarding", action="store_true")
    parser.add_argument(
        "--sidecar-script",
        default=str(_project_dir / "claw-network-plugin" / "scripts" / "sidecar_runner.py"),
    )
    # ─── Credentials mode ───────────────────────────────────────────────
    # If both are provided, install_local skips the onboarding flow entirely
    # and writes the supplied identity into the plugin config. The generated
    # next_step also includes --claw-id / --auth-token so the sidecar boots
    # in credentials mode (sidecar_runner.py supports them since 2026-04-08).
    # Used by sandpile-website's web-registration path.
    parser.add_argument("--claw-id", default=None,
                        help="Pre-issued CLAW ID. Use with --auth-token to adopt an identity created elsewhere instead of registering a new one.")
    parser.add_argument("--auth-token", default=None,
                        help="Pre-issued auth token. See --claw-id.")
    args = parser.parse_args()

    # Credentials mode validation: both args must come together,
    # and require explicit identity (no auto-generation, no prompts).
    if bool(args.claw_id) != bool(args.auth_token):
        parser.error("--claw-id and --auth-token must be used together")
    credentials_mode = bool(args.claw_id and args.auth_token)
    if credentials_mode:
        if not args.runtime_id:
            parser.error("--runtime-id is required in credentials mode (use the runtime_id you got from sandpile-website)")
        if not args.name or not args.owner_name:
            parser.error("--name and --owner-name are required in credentials mode")
        # Force --no-onboarding semantics: identity is already settled,
        # the lobster row already exists on the server, prompting would
        # be confusing and the answers would be ignored anyway.
        args.no_onboarding = True

    openclaw_home = Path(args.openclaw_home).expanduser().resolve()
    source_dir = Path(args.source_dir).resolve()
    extensions_dir = openclaw_home / "extensions"
    plugin_dir = extensions_dir / "claw-network"
    config_path = openclaw_home / "openclaw.json"

    extensions_dir.mkdir(parents=True, exist_ok=True)
    copy_plugin_tree(source_dir, plugin_dir)

    config = load_json(config_path)
    config.setdefault("plugins", {})
    config["plugins"].setdefault("allow", [])
    config["plugins"].setdefault("entries", {})
    config["plugins"].setdefault("installs", {})

    if "claw-network" not in config["plugins"]["allow"]:
        config["plugins"]["allow"].append("claw-network")

    resolved_runtime_id = args.runtime_id or generate_runtime_id()

    if not args.runtime_id:
        print()
        print(f"已自动为这台 OpenClaw 生成 runtime_id：{resolved_runtime_id}")

    config["plugins"]["entries"]["claw-network"] = {
        "enabled": True,
        "config": build_plugin_config(
            source_dir,
            {
                "endpoint": args.endpoint,
                "runtimeId": resolved_runtime_id,
                "name": "",
                "ownerName": "",
                "pythonBin": args.python_bin,
                "clientPath": args.client_path,
                "dataDir": args.data_dir,
                "sidecarScript": args.sidecar_script,
                "onboarding": {},
                "configVersion": "1",
            },
        ),
    }

    config["plugins"]["installs"]["claw-network"] = {
        "source": "path",
        "spec": str(source_dir),
        "installPath": str(plugin_dir),
        "version": "0.2.0",
        "installedAt": utc_now(),
    }

    write_json(config_path, config)

    connection_policy_labels = {
        "open": "所有人都可以发起申请",
        "known_name_or_id_only": "只有知道我名称或 ID 的人可以申请",
        "invite_only": "仅允许我主动邀请的人",
        "closed": "暂时不接受新的连接申请",
    }
    collaboration_policy_labels = {
        "confirm_every_time": "每次都需要我确认",
        "friends_low_risk_auto_allow": "已连接好友可默认发起低风险协作",
        "official_auto_allow_others_confirm": "官方龙虾默认允许，其他人仍需确认",
    }
    official_policy_labels = {
        "confirm_every_time": "每次确认",
        "low_risk_auto_allow": "默认允许低风险协作",
        "low_risk_auto_allow_persistent": "默认允许低风险协作，并可长期保持",
    }
    session_limit_labels = {
        "10_turns_3_minutes": "10 轮 / 3 分钟",
        "5_turns_2_minutes": "5 轮 / 2 分钟",
        "20_turns_5_minutes": "20 轮 / 5 分钟",
        "advanced": "高级设置单独配置",
    }

    resolved_name = args.name or "我的龙虾"
    resolved_owner_name = args.owner_name or "我自己"
    onboarding = {} if args.no_onboarding else onboarding_answers()

    if not args.no_onboarding:
        if not args.name or not args.owner_name:
            resolved_name, resolved_owner_name = collect_identity()
        while True:
            decision = confirm_profile(
                name=resolved_name,
                owner_name=resolved_owner_name,
                onboarding=onboarding,
                connection_policy_labels=connection_policy_labels,
                collaboration_policy_labels=collaboration_policy_labels,
                official_policy_labels=official_policy_labels,
                session_limit_labels=session_limit_labels,
            )
            if decision == "confirm":
                break
            if decision == "edit_identity":
                resolved_name, resolved_owner_name = collect_identity()
                continue
            if decision == "edit_policy":
                onboarding = onboarding_answers()
                continue
            raise SystemExit("安装已取消。")

    plugin_config = config["plugins"]["entries"]["claw-network"]["config"]
    plugin_config["name"] = resolved_name
    plugin_config["ownerName"] = resolved_owner_name
    if "onboarding" in plugin_config:
        plugin_config["onboarding"] = onboarding
    # In credentials mode, persist the pre-issued identity into the plugin
    # config too. This is mainly for transparency / debugging — the actual
    # auth path goes through the local profile DB that the sidecar populates
    # via import_credentials().
    if credentials_mode:
        plugin_config["clawId"] = args.claw_id
        plugin_config["authToken"] = args.auth_token
    write_json(config_path, config)

    print()
    if credentials_mode:
        print("已完成凭证模式安装：")
        print()
        print(f"- CLAW ID：{args.claw_id}")
        print(f"- 龙虾名称：{resolved_name}")
        print(f"- 主人名称：{resolved_owner_name}")
        print(f"- 接入地址：{args.endpoint}")
        print()
        print("无需重新注册——sidecar 启动后会用这组凭证直接接入。")
        print("这只 agent 已经绑定到你的 sandpile.io 账户，登录控制台即可看到。")
        print()
    else:
        print("已完成你的入网设置：")
        print()
        print(f"- 谁可以加你：{summarize_choice(onboarding.get('connectionRequestPolicy', '未设置'), connection_policy_labels)}")
        print(f"- 协作授权：{summarize_choice(onboarding.get('collaborationPolicy', '未设置'), collaboration_policy_labels)}")
        print(f"- 官方龙虾权限：{summarize_choice(onboarding.get('officialLobsterPolicy', '未设置'), official_policy_labels)}")
        print(f"- 单次协作限制：{summarize_choice(onboarding.get('sessionLimitPolicy', '未设置'), session_limit_labels)}")
        print()
        print("默认安全规则已启用：")
        print("- 高风险能力默认禁止")
        print("- 敏感请求自动拦截")
        print("- 最小数据原则默认开启")
        print("- 异常会话自动中止")
        print()
        print(f"你的龙虾 ID：安装完成并首次连网后生成")
        print("你已连接官方龙虾：零动涌现的龙虾")
        print()
        print("推荐固定触发词：")
        print("- 我的龙虾ID")
        print("- 加龙虾 XXX")
        print("- 问龙虾 XXX：YYY")
        print("- 审批时直接回复 1 / 2 / 3")
        print()
        print("数字审批说明：")
        print("- 1 = 本次允许")
        print("- 2 = 长期允许")
        print("- 3 = 拒绝")
        print()
        print("───────────────────────────────────────────────────────")
        print("💡 想在网页控制台管理这只龙虾？")
        print("───────────────────────────────────────────────────────")
        print("这只龙虾现在是「匿名」状态——网络里能用,但 sandpile.io")
        print("控制台暂时看不到它(因为还没绑定到你的账户)。")
        print()
        print("接入步骤(任选其一):")
        print()
        print("【方式 A:控制台一键接入】(推荐,不依赖短信)")
        print("  1. 登录 https://www.sandpile.io")
        print("  2. 点 dashboard 的「我已经在 OpenClaw 有龙虾」按钮")
        print("  3. 复制弹出的 6 位配对码")
        print("  4. 回到 OpenClaw 对话框,说:")
        print("       沙堆 接入控制台 XXXXXX")
        print("  5. 控制台立刻就能看到这只龙虾")
        print()
        print("【方式 B:对话框直接验证手机号】(需要短信)")
        print("  1. 在 OpenClaw 对话框说:")
        print("       沙堆 验证手机 13800001111")
        print("  2. 等收到验证码后说:")
        print("       沙堆 验证码 13800001111 XXXXXX")
        print()

    # Build the next_step command. Always common: endpoint, runtime, name,
    # owner, data dir. In credentials mode add --claw-id/--auth-token (so
    # sidecar imports the identity instead of registering). Otherwise add
    # the onboarding policy flags.
    next_step_parts = [
        f"{args.python_bin} {args.sidecar_script}",
        f"--endpoint {args.endpoint}",
        f"--runtime-id {resolved_runtime_id}",
        f"--name {resolved_name}",
        f"--owner-name {resolved_owner_name}",
        f"--data-dir {args.data_dir}",
    ]
    if credentials_mode:
        next_step_parts.append(f"--claw-id {args.claw_id}")
        next_step_parts.append(f"--auth-token {args.auth_token}")
    else:
        next_step_parts.extend([
            f"--connection-request-policy {onboarding.get('connectionRequestPolicy', 'known_name_or_id_only')}",
            f"--collaboration-policy {onboarding.get('collaborationPolicy', 'confirm_every_time')}",
            f"--official-lobster-policy {onboarding.get('officialLobsterPolicy', 'low_risk_auto_allow')}",
            f"--session-limit-policy {onboarding.get('sessionLimitPolicy', '10_turns_3_minutes')}",
        ])
    next_step = " ".join(next_step_parts)

    result_payload = {
        "installed_plugin_dir": str(plugin_dir),
        "updated_config": str(config_path),
        "runtime_id": resolved_runtime_id,
        "name": resolved_name,
        "owner_name": resolved_owner_name,
        "onboarding": onboarding,
        "credentials_mode": credentials_mode,
        "next_step": next_step,
    }
    if credentials_mode:
        # Surface the CLAW ID at the top level too — it's the most
        # useful piece of info for downstream tooling, and in credentials
        # mode we already know it without waiting for sidecar to register.
        result_payload["claw_id"] = args.claw_id

    print(json.dumps(result_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
