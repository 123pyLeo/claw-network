import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
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
          return jsonResult({
            success: true,
            claw_id: clawId,
            registration: register,
          });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: true, result });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: true, result });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: true, friends: result });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: true, result });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: true, requests: result });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: true, result });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          let pending = [];
          if (!requestId) {
            pending = await runClient(api, ['list-collaboration-requests', '--direction', 'incoming']);
            if (!Array.isArray(pending) || pending.length === 0) {
              return jsonResult({
                success: false,
                error: '当前没有待处理的协作审批请求。',
              });
            }
            if (pending.length > 1) {
              return jsonResult({
                success: false,
                error: '当前有多条待处理协作请求，请先确认具体请求。',
                pending_requests: pending,
              });
            }
            requestId = latestPendingRequest(pending)?.id;
          }

          const decision = decisionFromNumericChoice(params.choice);
          const result = await runClient(api, ['respond-collaboration', requestId, decision]);
          return jsonResult({
            success: true,
            choice: params.choice,
            decision,
            request_id: requestId,
            result,
          });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: true, result });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: false, error: String(error) });
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
          return jsonResult({ success: true, result });
        } catch (error) {
          return jsonResult({ success: false, error: String(error) });
        }
      }
    });
  }
};

export default plugin;
