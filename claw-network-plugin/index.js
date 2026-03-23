import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { emptyPluginConfigSchema } from 'openclaw/plugin-sdk';

// 插件根目录（claw-network-plugin/）的上一级，即项目根目录
const __filename = fileURLToPath(import.meta.url);
const __pluginDir = path.dirname(__filename);
const __projectDir = path.resolve(__pluginDir, '..');

const execFileAsync = promisify(execFile);

function jsonResult(data) {
  return {
    content: [
      {
        type: 'text',
        text: JSON.stringify(data, null, 2),
      },
    ],
    details: data,
  };
}

function toolTextResult(text, details) {
  return {
    content: [
      {
        type: 'text',
        text,
      },
    ],
    details,
  };
}

function getPluginConfig(api) {
  const cfg =
    api.pluginConfig ??
    api.config?.plugins?.entries?.['claw-network']?.config ??
    api.config?.plugins?.entries?.['claw-network'] ??
    api.config?.plugins?.entries?.clawNetwork?.config ??
    api.config?.plugins?.entries?.clawNetwork ??
    api.config?.clawNetwork;
  return cfg ?? {};
}

function buildBaseArgs(config) {
  const args = [
    config.clientPath ?? path.join(__projectDir, 'agent', 'client.py'),
    '--runtime-id',
    config.runtimeId,
    '--name',
    config.name,
    '--owner-name',
    config.ownerName,
    '--server-url',
    config.endpoint,
  ];
  if (config.dataDir) {
    args.push('--data-dir', config.dataDir);
  }
  const onboarding = config.onboarding ?? {};
  if (onboarding.connectionRequestPolicy) {
    args.push('--connection-request-policy', onboarding.connectionRequestPolicy);
  }
  if (onboarding.collaborationPolicy) {
    args.push('--collaboration-policy', onboarding.collaborationPolicy);
  }
  if (onboarding.officialLobsterPolicy) {
    args.push('--official-lobster-policy', onboarding.officialLobsterPolicy);
  }
  if (onboarding.sessionLimitPolicy) {
    args.push('--session-limit-policy', onboarding.sessionLimitPolicy);
  }
  return args;
}

async function runClient(api, extraArgs) {
  const config = getPluginConfig(api);
  const required = ['endpoint', 'runtimeId', 'name', 'ownerName'];
  for (const key of required) {
    if (!config[key]) {
      throw new Error(`Missing claw-network config field: ${key}`);
    }
  }

  const pythonBin = config.pythonBin ?? 'python3';
  const args = [...buildBaseArgs(config), ...extraArgs];
  const { stdout, stderr } = await execFileAsync(pythonBin, args, {
    cwd: config.projectDir ?? __projectDir,
    maxBuffer: 1024 * 1024,
  });
  if (stderr && stderr.trim()) {
    api.logger?.warn?.(stderr.trim());
  }
  const output = stdout.trim();
  if (!output) {
    return { ok: true };
  }
  try {
    return JSON.parse(output);
  } catch {
    return { output };
  }
}

function latestPendingRequest(requests) {
  if (!Array.isArray(requests) || requests.length === 0) {
    return null;
  }
  return [...requests].sort((a, b) => {
    const aTime = String(a.created_at ?? '');
    const bTime = String(b.created_at ?? '');
    return bTime.localeCompare(aTime);
  })[0];
}

function decisionFromNumericChoice(choice) {
  const normalized = String(choice ?? '').trim();
  if (normalized === '1') {
    return 'approved_once';
  }
  if (normalized === '2') {
    return 'approved_persistent';
  }
  if (normalized === '3') {
    return 'rejected';
  }
  throw new Error('审批数字只能是 1、2、3。');
}

// 找到 openclaw.json 的路径并读取/写入
function getOpenclaConfigPath(api) {
  const config = getPluginConfig(api);
  if (config.opeclawConfigPath) return config.opeclawConfigPath;
  const home = process.env.HOME || process.env.USERPROFILE || '';
  return path.join(home, '.openclaw', 'openclaw.json');
}

function loadOpenclaConfig(configPath) {
  try {
    return JSON.parse(readFileSync(configPath, 'utf8'));
  } catch {
    return {};
  }
}

function saveOpenclaConfig(configPath, data) {
  writeFileSync(configPath, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

// setup_lobster 用到的标签映射（与用户展示保持一致）
const CONNECTION_LABELS = {
  open: '所有人都可以发起申请',
  known_name_or_id_only: '只有知道我名称或 ID 的人',
  invite_only: '仅允许我主动邀请的人',
  closed: '暂时不接受新的连接申请',
};
const COLLAB_LABELS = {
  confirm_every_time: '每次都需要我确认',
  friends_low_risk_auto_allow: '已连接好友可自动发起低风险协作',
  official_auto_allow_others_confirm: '官方龙虾自动允许，其他人仍需确认',
};
const OFFICIAL_LABELS = {
  confirm_every_time: '每次确认',
  low_risk_auto_allow: '默认允许低风险协作',
  low_risk_auto_allow_persistent: '默认允许低风险协作并长期保持',
};
const SESSION_LABELS = {
  '10_turns_3_minutes': '10 轮 / 3 分钟（推荐）',
  '5_turns_2_minutes': '5 轮 / 2 分钟',
  '20_turns_5_minutes': '20 轮 / 5 分钟',
};

const plugin = {
  id: 'claw-network',
  name: 'Claw Network',
  description: 'Connect OpenClaw to the Claw Network for lobster IDs, friends, and messages.',
  configSchema: emptyPluginConfigSchema(),
  register(api) {
    api.registerTool({
      name: 'get_my_lobster_id',
      label: 'Get My Lobster ID',
      description: 'Return this OpenClaw instance public CLAW-XXXXXX ID. Registers first if needed.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {}
      },
      async execute() {
        try {
          const register = await runClient(api, ['register']);
          const clawId = register?.lobster?.claw_id ?? register?.output;
          const name = register?.lobster?.name ?? '';
          const owner = register?.lobster?.owner_name ?? '';
          return toolTextResult(
            `你的龙虾 ID 是：${clawId}\n龙虾名称：${name}\n主人名称：${owner}`,
            { success: true, claw_id: clawId, registration: register }
          );
        } catch (error) {
          return toolTextResult(`获取龙虾 ID 失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'find_lobster',
      label: 'Find Lobster',
      description: 'Find a lobster by name, nickname-like query, owner name, or CLAW-XXXXXX.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['query'],
        properties: {
          query: { type: 'string' },
          limit: { type: 'number' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const extraArgs = ['find-lobster', params.query];
          if (params.limit) {
            extraArgs.push('--limit', String(params.limit));
          }
          const result = await runClient(api, extraArgs);
          const list = Array.isArray(result) ? result : (result?.results ?? []);
          if (list.length === 0) {
            return toolTextResult(`没有找到「${params.query}」相关的龙虾。`, { success: true, result });
          }
          const lines = list.map((r, i) => {
            const id = r.claw_id ?? '';
            const name = r.name ?? '';
            const owner = r.owner_name ?? '';
            return `${i + 1}. ${name}（主人：${owner}）— ${id}`;
          });
          return toolTextResult(
            `找到 ${list.length} 只龙虾：\n${lines.join('\n')}`,
            { success: true, result }
          );
        } catch (error) {
          return toolTextResult(`查找失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'add_lobster_friend',
      label: 'Add Lobster Friend',
      description: 'Send a friend request to another lobster by name or CLAW-XXXXXX.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['target'],
        properties: {
          target: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['add-lobster', params.target]);
          const status = result?.status ?? result?.friendship?.status ?? '';
          if (status === 'accepted' || status === 'already_friends') {
            return toolTextResult(
              `已成功与「${params.target}」建立好友关系。`,
              { success: true, result }
            );
          }
          return toolTextResult(
            `已向「${params.target}」发送好友申请，等待对方接受。`,
            { success: true, result }
          );
        } catch (error) {
          return toolTextResult(`添加好友失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'list_lobster_friends',
      label: 'List Lobster Friends',
      description: 'List current lobster friends for this OpenClaw instance.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {}
      },
      async execute() {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['list-friends']);
          const list = Array.isArray(result) ? result : [];
          if (list.length === 0) {
            return toolTextResult('你还没有龙虾好友，用「加龙虾 XXX」发出好友申请吧。', { success: true, friends: list });
          }
          const lines = list.map((f, i) => {
            const name = f.friend_name ?? f.name ?? '';
            const owner = f.friend_owner_name ?? f.owner_name ?? '';
            const id = f.friend_claw_id ?? f.claw_id ?? '';
            return `${i + 1}. ${name}（主人：${owner}）— ${id}`;
          });
          return toolTextResult(
            `你有 ${list.length} 个龙虾好友：\n${lines.join('\n')}`,
            { success: true, friends: list }
          );
        } catch (error) {
          return toolTextResult(`获取好友列表失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'list_official_notifications',
      label: 'List Official Notifications',
      description: 'Show recent official broadcast notifications received by this lobster.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          limit: { type: 'number' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const extraArgs = ['list-official-notifications'];
          if (params?.limit) {
            extraArgs.push('--limit', String(params.limit));
          }
          const result = await runClient(api, extraArgs);
          if (Array.isArray(result) && result.length > 0) {
            const lines = result.map((item, idx) => {
              const when = String(item.created_at ?? '');
              const content = String(item.content ?? '');
              return `${idx + 1}. ${when} ${content}`.trim();
            });
            return toolTextResult(lines.join('\n'), { success: true, result });
          }
          return toolTextResult('当前没有官方通知。', { success: true, result });
        } catch (error) {
          return toolTextResult(`获取通知失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'rename_lobster',
      label: 'Rename Lobster',
      description: 'Update the current lobster display name and owner name without re-registering or changing CLAW-ID.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['name'],
        properties: {
          name: { type: 'string' },
          owner_name: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const extraArgs = ['rename-lobster', params.name];
          if (params.owner_name) {
            extraArgs.push('--owner-name', params.owner_name);
          }
          const result = await runClient(api, extraArgs);
          const newName = result?.name ?? params.name;
          const newOwner = result?.owner_name ?? params.owner_name ?? '';
          const ownerPart = newOwner ? `，主人名称：${newOwner}` : '';
          return toolTextResult(
            `龙虾信息已更新。新名称：${newName}${ownerPart}`,
            { success: true, result }
          );
        } catch (error) {
          return toolTextResult(`改名失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'list_collaboration_requests',
      label: 'List Collaboration Requests',
      description: 'List pending collaboration approval requests for this lobster.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          direction: { type: 'string', enum: ['incoming', 'outgoing'] }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const extraArgs = ['list-collaboration-requests'];
          if (params.direction) {
            extraArgs.push('--direction', params.direction);
          }
          const result = await runClient(api, extraArgs);
          const list = Array.isArray(result) ? result : [];
          if (list.length === 0) {
            return toolTextResult('当前没有待处理的协作请求。', { success: true, requests: list });
          }
          const dir = params.direction === 'outgoing' ? '发出' : '收到';
          const lines = list.map((r, i) => {
            const from = r.from_name ?? r.from_claw_id ?? '';
            const to = r.to_name ?? r.to_claw_id ?? '';
            const id = r.id ?? '';
            return `${i + 1}. ID:${id}  ${from} → ${to}`;
          });
          return toolTextResult(
            `你有 ${list.length} 条${dir}的协作请求：\n${lines.join('\n')}\n\n回复 1=本次允许 / 2=长期允许 / 3=拒绝`,
            { success: true, requests: list }
          );
        } catch (error) {
          return toolTextResult(`获取协作请求失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'respond_collaboration_request',
      label: 'Respond Collaboration Request',
      description: 'Approve once, approve persistently, or reject a pending collaboration request.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['request_id', 'decision'],
        properties: {
          request_id: { type: 'string' },
          decision: { type: 'string', enum: ['approved_once', 'approved_persistent', 'rejected'] }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['respond-collaboration', params.request_id, params.decision]);
          const decisionLabel = {
            approved_once: '本次允许',
            approved_persistent: '长期允许',
            rejected: '已拒绝',
          }[params.decision] ?? params.decision;
          return toolTextResult(
            `协作请求 ${params.request_id} 处理完成：${decisionLabel}。`,
            { success: true, result }
          );
        } catch (error) {
          return toolTextResult(`处理协作请求失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'handle_collaboration_approval',
      label: 'Handle Collaboration Approval',
      description: 'Use numeric choices 1/2/3 to approve once, approve persistently, or reject the latest pending collaboration request.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['choice'],
        properties: {
          choice: { type: 'string', enum: ['1', '2', '3'] },
          request_id: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          let requestId = params.request_id;
          if (!requestId) {
            const pending = await runClient(api, ['list-collaboration-requests', '--direction', 'incoming']);
            if (!Array.isArray(pending) || pending.length === 0) {
              return toolTextResult('当前没有待处理的协作审批请求。', { success: false });
            }
            if (pending.length > 1) {
              const lines = pending.map((r, i) => {
                const from = r.from_name ?? r.from_claw_id ?? '';
                return `${i + 1}. ID:${r.id}  来自：${from}`;
              });
              return toolTextResult(
                `当前有 ${pending.length} 条待处理协作请求，请先确认要处理哪一条：\n${lines.join('\n')}`,
                { success: false, pending_requests: pending }
              );
            }
            requestId = latestPendingRequest(pending)?.id;
          }

          const decision = decisionFromNumericChoice(params.choice);
          const result = await runClient(api, ['respond-collaboration', requestId, decision]);
          const decisionLabel = { '1': '本次允许', '2': '长期允许', '3': '已拒绝' }[params.choice];
          return toolTextResult(
            `审批完成：${decisionLabel}。`,
            { success: true, choice: params.choice, decision, request_id: requestId, result }
          );
        } catch (error) {
          return toolTextResult(`审批失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'send_lobster_message',
      label: 'Send Lobster Message',
      description: 'Send a direct message to a friend lobster by CLAW-XXXXXX.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['to', 'message'],
        properties: {
          to: { type: 'string' },
          message: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['send-message', params.to, params.message]);
          const statusLabel = result?.event?.status_label ?? result?.status_label ?? '已发送';
          return toolTextResult(
            `消息${statusLabel}给「${params.to}」。`,
            { success: true, result }
          );
        } catch (error) {
          return toolTextResult(`发送失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'official_broadcast',
      label: 'Official Broadcast',
      description: 'Send an official broadcast from the official lobster to all joined lobsters, or only currently online lobsters.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['message'],
        properties: {
          message: { type: 'string' },
          online_only: { type: 'boolean' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const extraArgs = ['broadcast-official', params.message];
          if (params.online_only) {
            extraArgs.push('--online-only');
          }
          const result = await runClient(api, extraArgs);
          const sentCount = result?.sent_count ?? 0;
          const deliveredCount = result?.delivered_count ?? 0;
          const queuedCount = result?.queued_count ?? 0;
          return toolTextResult(
            `官方广播已发送：共 ${sentCount} 个目标，已送达 ${deliveredCount}，排队中 ${queuedCount}。`,
            { success: true, result }
          );
        } catch (error) {
          return toolTextResult(`广播失败：${String(error)}`, { success: false });
        }
      }
    });

    api.registerTool({
      name: 'ask_lobster',
      label: 'Ask Lobster',
      description: 'Ask a lobster by name or CLAW-XXXXXX and wait for the first reply in the current command.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['target', 'message'],
        properties: {
          target: { type: 'string' },
          message: { type: 'string' },
          timeout: { type: 'number' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const extraArgs = ['ask-lobster', params.target, params.message];
          extraArgs.push('--timeout', String(params.timeout ?? 45));
          const result = await runClient(api, extraArgs);
          if (result?.awaiting_approval) {
            return toolTextResult(
              `已向「${params.target}」发起协作请求，当前正在等待对方审批。`,
              { success: true, result }
            );
          }
          if (result?.reply_received && result?.reply?.content) {
            return toolTextResult(String(result.reply.content), { success: true, result });
          }
          if (result?.timed_out) {
            const delivered = result?.sent?.event?.status_label ?? '已发送';
            return toolTextResult(
              `消息${delivered}，但在等待时间内没有收到「${params.target}」的回复。`,
              { success: true, result }
            );
          }
          return toolTextResult(
            `已向「${params.target}」发送消息。`,
            { success: true, result }
          );
        } catch (error) {
          return toolTextResult(`操作失败：${String(error)}`, { success: false });
        }
      }
    });

    // -----------------------------------------------------------------------
    // setup_lobster：引导用户完成四项配置，写入 openclaw.json 并重新注册
    // AI 应逐步问用户每个选项，收集完毕后调用此工具一次提交
    // -----------------------------------------------------------------------
    api.registerTool({
      name: 'setup_lobster',
      label: 'Setup Lobster',
      description: [
        'Apply lobster identity and onboarding policy configuration.',
        'Collect name, owner_name, and all four policy choices from the user first,',
        'then call this tool once with all fields filled in.',
        'Do NOT call this tool mid-collection — only call after all four policies are confirmed.',
      ].join(' '),
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['name', 'owner_name', 'connection_request_policy', 'collaboration_policy', 'official_lobster_policy', 'session_limit_policy'],
        properties: {
          name: {
            type: 'string',
            description: '龙虾的显示名称',
          },
          owner_name: {
            type: 'string',
            description: '主人的名字或昵称',
          },
          connection_request_policy: {
            type: 'string',
            enum: ['open', 'known_name_or_id_only', 'invite_only', 'closed'],
            description: '谁可以向我发起连接申请',
          },
          collaboration_policy: {
            type: 'string',
            enum: ['confirm_every_time', 'friends_low_risk_auto_allow', 'official_auto_allow_others_confirm'],
            description: '其他龙虾请求调用我时的默认策略',
          },
          official_lobster_policy: {
            type: 'string',
            enum: ['confirm_every_time', 'low_risk_auto_allow', 'low_risk_auto_allow_persistent'],
            description: '对官方龙虾的协作策略',
          },
          session_limit_policy: {
            type: 'string',
            enum: ['10_turns_3_minutes', '5_turns_2_minutes', '20_turns_5_minutes'],
            description: '单次协作的轮次和时间限制',
          },
        },
      },
      async execute(_toolCallId, params) {
        try {
          // 1. 读取并更新 openclaw.json
          const configPath = getOpenclaConfigPath(api);
          const ocConfig = loadOpenclaConfig(configPath);

          ocConfig.plugins = ocConfig.plugins ?? {};
          ocConfig.plugins.entries = ocConfig.plugins.entries ?? {};
          const entry = ocConfig.plugins.entries['claw-network'] ?? {};
          entry.config = entry.config ?? {};

          entry.config.name = params.name;
          entry.config.ownerName = params.owner_name;
          entry.config.onboarding = {
            connectionRequestPolicy: params.connection_request_policy,
            collaborationPolicy: params.collaboration_policy,
            officialLobsterPolicy: params.official_lobster_policy,
            sessionLimitPolicy: params.session_limit_policy,
          };
          ocConfig.plugins.entries['claw-network'] = entry;
          saveOpenclaConfig(configPath, ocConfig);

          // 2. 用新配置重新注册，让服务端同步
          const register = await runClient(api, ['register']);
          const clawId = register?.lobster?.claw_id ?? '（注册后生成）';

          const summary = [
            `龙虾配置已完成并保存：`,
            ``,
            `• 龙虾名称：${params.name}`,
            `• 主人名称：${params.owner_name}`,
            `• 你的 CLAW-ID：${clawId}`,
            ``,
            `策略设置：`,
            `• 谁可以加你：${CONNECTION_LABELS[params.connection_request_policy] ?? params.connection_request_policy}`,
            `• 协作授权：${COLLAB_LABELS[params.collaboration_policy] ?? params.collaboration_policy}`,
            `• 官方龙虾权限：${OFFICIAL_LABELS[params.official_lobster_policy] ?? params.official_lobster_policy}`,
            `• 单次协作限制：${SESSION_LABELS[params.session_limit_policy] ?? params.session_limit_policy}`,
            ``,
            `推荐触发词：我的龙虾ID / 加龙虾 XXX / 问龙虾 XXX：YYY`,
            `审批时直接回复 1（本次允许）/ 2（长期允许）/ 3（拒绝）`,
          ].join('\n');

          return toolTextResult(summary, {
            success: true,
            claw_id: clawId,
            config_path: configPath,
            registration: register,
          });
        } catch (error) {
          return toolTextResult(`配置保存失败：${String(error)}`, { success: false });
        }
      }
    });
  }
};

export default plugin;
