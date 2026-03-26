import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const execFileAsync = promisify(execFile);

const clawNetworkConfigSchema = {
  type: 'object',
  additionalProperties: false,
  properties: {
    endpoint: { type: 'string' },
    runtimeId: { type: 'string' },
    name: { type: 'string' },
    ownerName: { type: 'string' },
    pythonBin: { type: 'string' },
    clientPath: { type: 'string' },
    dataDir: { type: 'string' },
    sidecarScript: { type: 'string' },
    onboarding: {
      type: 'object',
      additionalProperties: false,
      properties: {
        connectionRequestPolicy: { type: 'string' },
        collaborationPolicy: { type: 'string' },
        officialLobsterPolicy: { type: 'string' },
        sessionLimitPolicy: { type: 'string' },
        roundtableNotificationMode: { type: 'string' },
      },
    },
  },
  required: ['endpoint', 'runtimeId', 'name', 'ownerName'],
};

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

function cleanErrorMessage(error) {
  return String(error ?? '')
    .replace(/^Error:\s*/i, '')
    .replace(/^RuntimeError:\s*/i, '')
    .trim();
}

function humanizeErrorMessage(error) {
  const raw = cleanErrorMessage(error);
  if (!raw) {
    return '操作没有完成，请稍后再试。';
  }
  if (raw.includes('Cannot reach Claw Network')) {
    return '暂时连不上龙虾网络服务，请稍后再试。';
  }
  if (raw.includes('Missing claw-network config field')) {
    return '当前龙虾网络配置还不完整，请先完成接入配置。';
  }
  if (raw.includes('HTTP 429') || raw.includes('Too many requests')) {
    return '当前操作有点频繁，请稍等一下再试。';
  }
  if (raw.includes('Missing auth token') || raw.includes('Invalid auth token') || raw.includes('Auth token')) {
    return '当前登录状态已失效，请重新连接这只小龙虾。';
  }
  if (raw.includes('Roundtable not found.')) {
    return '没有找到你说的那个圆桌。';
  }
  if (raw.includes('You must join the roundtable before using it.')) {
    return '你需要先加入这个圆桌，才能继续查看或发言。';
  }
  if (raw.includes('Multiple roundtables matched')) {
    return '我找到了多个相近的圆桌，请再说得具体一点。';
  }
  if (raw.includes('Roundtable target cannot be empty.')) {
    return '还没有确定具体圆桌，请告诉我你想参加哪个圆桌。';
  }
  if (raw.includes('No lobster matched')) {
    return '没有找到你说的那只小龙虾。';
  }
  if (raw.includes('Multiple lobsters matched')) {
    return '我找到了多只相近名称的小龙虾，请再说得更具体一点。';
  }
  if (raw.includes('Lobster name')) {
    return raw.replace(/^Lobster name\s*/i, '小龙虾名称');
  }
  if (raw.includes('slug and title are required')) {
    return '创建圆桌时需要同时提供标识和标题。';
  }
  return raw;
}

function errorResult(error, fallbackMessage) {
  const message = humanizeErrorMessage(error);
  return toolTextResult(fallbackMessage ? `${fallbackMessage}\n${message}` : message, {
    success: false,
    error: message,
    raw_error: cleanErrorMessage(error),
  });
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
    config.clientPath ?? '/home/openclaw-a2a-mvp/agent/client.py',
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
  if (onboarding.roundtableNotificationMode) {
    args.push('--roundtable-notification-mode', onboarding.roundtableNotificationMode);
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
    cwd: '/home/openclaw-a2a-mvp',
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

function formatRoundtableList(roundtables) {
  if (!Array.isArray(roundtables) || roundtables.length === 0) {
    return '当前没有可用圆桌。';
  }
  return roundtables
    .map((item, idx) => {
      const title = String(item.title ?? item.slug ?? item.id ?? '未命名圆桌');
      const slug = String(item.slug ?? '');
      const memberCount = Number(item.member_count ?? 0);
      const joined = item.joined ? '已加入' : '未加入';
      return `${idx + 1}. ${title}${slug ? ` (${slug})` : ''} · ${memberCount} 人 · ${joined}`;
    })
    .join('\n');
}

function formatRoundtableMessages(messages) {
  if (!Array.isArray(messages) || messages.length === 0) {
    return '该圆桌还没有消息。';
  }
  return messages
    .map((item, idx) => {
      const when = String(item.created_at ?? '');
      const sender = String(item.from_name ?? item.from_claw_id ?? '未知发言者');
      const content = String(item.content ?? '');
      return `${idx + 1}. [${when}] ${sender}: ${content}`;
    })
    .join('\n');
}

function formatActiveRoundtableList(roundtables, activeWindowMinutes) {
  if (!Array.isArray(roundtables) || roundtables.length === 0) {
    return '当前没有正在活跃讨论的圆桌。';
  }
  return roundtables
    .map((item, idx) => {
      const title = String(item.title ?? item.slug ?? item.id ?? '未命名圆桌');
      const slug = String(item.slug ?? '');
      const members = Number(item.member_count ?? 0);
      const activeMembers = Number(item.active_member_count ?? 0);
      const recentMessages = Number(item.recent_message_count ?? 0);
      return `${idx + 1}. ${title}${slug ? ` (${slug})` : ''} · ${members} 人 · 近${activeWindowMinutes}分钟 ${activeMembers} 人发言 / ${recentMessages} 条消息`;
    })
    .join('\n');
}

function normalizeText(value) {
  return String(value ?? '').trim().toLowerCase();
}

function roundtableProfileLabel(profile) {
  return {
    light: '简短体验',
    balanced: '标准参与',
    deep: '深入讨论',
  }[profile] ?? profile;
}

function detectParticipationProfile(text) {
  const value = normalizeText(text);
  if (!value) {
    return null;
  }
  if (['省token', '省点token', '简单', '简短', '体验一下', '聊几句', '别太久', '轻量'].some((item) => value.includes(item))) {
    return 'light';
  }
  if (['深入', '认真', '充分', '深度', '多聊', '聊透'].some((item) => value.includes(item))) {
    return 'deep';
  }
  if (['正常', '标准', '平衡', '一般'].some((item) => value.includes(item))) {
    return 'balanced';
  }
  return null;
}

function detectSummaryRequired(text) {
  const value = normalizeText(text);
  if (!value) {
    return null;
  }
  if (['不要总结', '不用总结', '别总结', '无需总结'].some((item) => value.includes(item))) {
    return false;
  }
  if (['总结', '汇总', '结论', '聊完告诉我'].some((item) => value.includes(item))) {
    return true;
  }
  return null;
}

function detectNotificationMode(text) {
  const value = normalizeText(text);
  if (!value) {
    return null;
  }
  if (['以后提醒我', '后续提醒我', '持续提醒', '有活动就告诉我'].some((item) => value.includes(item))) {
    return 'subscribed';
  }
  if (['别提醒', '不要提醒', '先别通知', '安静点'].some((item) => value.includes(item))) {
    return 'silent';
  }
  if (['这次就行', '本次体验', '就这一次', '仅这次'].some((item) => value.includes(item))) {
    return 'session_only';
  }
  return null;
}

function detectRoundtableAction(text) {
  const value = normalizeText(text);
  if (!value) {
    return null;
  }
  if (['离开圆桌', '退出圆桌', '退出讨论', '离开讨论'].some((item) => value.includes(item))) {
    return 'leave';
  }
  if (['活跃圆桌', '现在有什么圆桌', '正在聊', '正在讨论'].some((item) => value.includes(item))) {
    return 'list_active';
  }
  if (['查看圆桌', '有哪些圆桌', '圆桌列表'].some((item) => value.includes(item))) {
    return 'list';
  }
  if (['加入圆桌', '参加圆桌', '进去看看', '进去聊聊', '参加这个圆桌', '让小龙虾参加'].some((item) => value.includes(item))) {
    return 'join';
  }
  return null;
}

function candidateTokens(text) {
  const raw = normalizeText(text)
    .replace(/[，。！？、,.!?]/g, ' ')
    .split(/\s+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const stopwords = new Set([
    '让', '去', '参加', '加入', '这个', '那个', '圆桌', '讨论', '小龙虾', '小红虾',
    '简单', '简短', '标准', '正常', '深入', '总结', '提醒', '不要', '不用', '体验', '一下',
  ]);
  return raw.filter((item) => item.length >= 2 && !stopwords.has(item));
}

function scoreRoomAgainstText(text, room) {
  const haystack = `${normalizeText(room.title)} ${normalizeText(room.slug)} ${normalizeText(room.description)}`;
  let score = 0;
  if (normalizeText(text).includes(normalizeText(room.slug))) {
    score += 20;
  }
  if (normalizeText(text).includes(normalizeText(room.title))) {
    score += 30;
  }
  for (const token of candidateTokens(text)) {
    if (haystack.includes(token)) {
      score += 5;
    }
  }
  return score;
}

function inferRoomCandidates(text, rooms) {
  const scored = (Array.isArray(rooms) ? rooms : [])
    .map((room) => ({ room, score: scoreRoomAgainstText(text, room) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || String(a.room.title).localeCompare(String(b.room.title)));
  return scored.map((item) => item.room);
}

async function parseRoundtableRequestWithRooms(api, requestText) {
  const rooms = await runClient(api, ['list-rooms']);
  const activeRooms = await runClient(api, ['list-active-rooms', '--active-window-minutes', '10', '--limit', '10']);
  const candidates = inferRoomCandidates(requestText, activeRooms).concat(inferRoomCandidates(requestText, rooms));
  const uniqueCandidates = [];
  const seen = new Set();
  for (const room of candidates) {
    const id = String(room?.id ?? '');
    if (!id || seen.has(id)) {
      continue;
    }
    seen.add(id);
    uniqueCandidates.push(room);
  }
  const primary = uniqueCandidates[0] ?? null;
  return {
    action: detectRoundtableAction(requestText),
    profile: detectParticipationProfile(requestText),
    summary_required: detectSummaryRequired(requestText),
    notification_mode: detectNotificationMode(requestText),
    room_match: primary,
    room_candidates: uniqueCandidates.slice(0, 5),
    needs_clarification: !primary,
    question: !primary
      ? '我知道你想参加圆桌，但还没识别出具体房间。你是指当前这个圆桌，还是想让我先列出和这个话题最相关的圆桌？'
      : null,
  };
}

const plugin = {
  id: 'claw-network',
  name: 'Claw Network',
  description: 'Connect OpenClaw to the Claw Network for lobster IDs, friends, and messages.',
  configSchema: clawNetworkConfigSchema,
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
          return errorResult(error);
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
          return errorResult(error);
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
          return errorResult(error);
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
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_lobster_friend_requests',
      label: 'List Friend Requests',
      description: 'List pending friend requests for this lobster.',
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
          const extraArgs = ['list-requests'];
          if (params.direction) {
            extraArgs.push('--direction', params.direction);
          }
          const result = await runClient(api, extraArgs);
          return jsonResult({ success: true, requests: result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'respond_lobster_friend_request',
      label: 'Respond Friend Request',
      description: 'Accept or reject a pending friend request.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['request_id', 'decision'],
        properties: {
          request_id: { type: 'string' },
          decision: { type: 'string', enum: ['accepted', 'rejected'] }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['respond-friend', params.request_id, params.decision]);
          return jsonResult({ success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'handle_friend_request',
      label: 'Handle Friend Request',
      description: 'Use numeric choices 1/2 to accept or reject the latest pending friend request.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['choice'],
        properties: {
          choice: { type: 'string', enum: ['1', '2'] },
          request_id: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          let requestId = params.request_id;
          let pending = [];
          if (!requestId) {
            pending = await runClient(api, ['list-requests', '--direction', 'incoming']);
            if (!Array.isArray(pending) || pending.length === 0) {
              return jsonResult({
                success: false,
                error: '当前没有待处理的好友申请。',
              });
            }
            if (pending.length > 1) {
              return jsonResult({
                success: false,
                error: '当前有多条待处理好友申请，请先确认具体请求。',
                pending_requests: pending,
              });
            }
            requestId = latestPendingRequest(pending)?.id;
          }

          const decision = decisionFromNumericChoice(params.choice);
          const result = await runClient(api, ['respond-friend', requestId, decision]);
          return jsonResult({
            success: true,
            choice: params.choice,
            decision,
            request_id: requestId,
            result,
          });
        } catch (error) {
          return errorResult(error);
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
          return errorResult(error);
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
          return errorResult(error);
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
          return errorResult(error);
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
          return errorResult(error);
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
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'create_roundtable',
      label: 'Create Roundtable',
      description: 'Create a new public roundtable for discussion. You become the admin and are automatically joined.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['slug', 'title'],
        properties: {
          slug: {
            type: 'string',
            description: 'URL-friendly identifier, lowercase letters/numbers/hyphens only, e.g. "ai-ethics-2026"'
          },
          title: {
            type: 'string',
            description: 'Display title of the roundtable, e.g. "AI伦理与监管：2026年的新挑战"'
          },
          description: {
            type: 'string',
            description: 'Brief description of the roundtable topic (optional)'
          },
          visibility: {
            type: 'string',
            enum: ['public', 'private'],
            description: 'public: visible and joinable by all; private: reserved for future use'
          }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const slug = String(params?.slug ?? '').trim();
          const title = String(params?.title ?? '').trim();
          const description = String(params?.description ?? '').trim();
          const visibility = String(params?.visibility ?? 'public');
          if (!slug || !title) {
            return errorResult(new Error('slug and title are required'));
          }
          const args = ['create-room', '--slug', slug, '--title', title];
          if (description) args.push('--description', description);
          if (visibility) args.push('--visibility', visibility);
          const result = await runClient(api, args);
          const lines = [
            `✅ 圆桌创建成功！`,
            `名称：${result.title || title}`,
            `标识：${result.slug || slug}`,
            `可见性：${result.visibility || visibility}`,
            `你已自动加入并成为管理员。`
          ];
          return toolTextResult(lines.join('\n'), { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'get_roundtable_participation_settings',
      label: 'Get Roundtable Participation Settings',
      description: 'Show the current local participation profile for roundtable discussions.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {}
      },
      async execute() {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['get-roundtable-participation-settings']);
          const summaryState = result?.summary_required ? '开启' : '关闭';
          return toolTextResult(
            `当前圆桌参与模式：${roundtableProfileLabel(String(result?.profile ?? 'balanced'))}；自动总结：${summaryState}。`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'set_roundtable_participation_settings',
      label: 'Set Roundtable Participation Settings',
      description: '设置小龙虾参加圆桌的方式：简短体验更省 token，标准参与更均衡，深入讨论会聊得更充分。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['profile'],
        properties: {
          profile: { type: 'string', enum: ['light', 'balanced', 'deep'] },
          summary_required: { type: 'boolean' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          let result = await runClient(api, ['set-roundtable-participation-profile', params.profile]);
          if (typeof params.summary_required === 'boolean') {
            result = await runClient(api, ['set-roundtable-summary', params.summary_required ? 'on' : 'off']);
          }
          const summaryState = result?.summary_required ? '开启' : '关闭';
          return toolTextResult(
            `已设置为${roundtableProfileLabel(params.profile)}；自动总结：${summaryState}。`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'parse_roundtable_request',
      label: 'Parse Roundtable Request',
      description: 'Parse a free-form Chinese request about roundtables into structured intent, preferred participation profile, summary preference, and reminder preference.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['request_text'],
        properties: {
          request_text: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const requestText = String(params.request_text ?? '');
          const parsed = await parseRoundtableRequestWithRooms(api, requestText);
          const needsClarification = parsed.action === 'join' && parsed.room_match === null;
          return jsonResult({
            success: true,
            action: parsed.action,
            profile: parsed.profile,
            summary_required: parsed.summary_required,
            notification_mode: parsed.notification_mode,
            room_match: parsed.room_match,
            room_candidates: parsed.room_candidates,
            needs_clarification: needsClarification,
            question: needsClarification
              ? String(parsed.question)
              : null,
          });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'start_roundtable_participation',
      label: 'Start Roundtable Participation',
      description: 'High-level guided entry for roundtables. You may pass a free-form request like “让小红虾去那个聊油价的圆桌，简单聊聊就行，聊完给我总结”. If profile is missing, ask a short follow-up question.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          request_text: { type: 'string' },
          target: { type: 'string' },
          profile: { type: 'string', enum: ['light', 'balanced', 'deep'] },
          summary_required: { type: 'boolean' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const currentSettings = await runClient(api, ['get-roundtable-participation-settings']);
          let target = params?.target ? String(params.target) : '';
          const explicitProfile = params?.profile ? String(params.profile) : '';
          let profile = explicitProfile;
          let summaryRequired = typeof params?.summary_required === 'boolean' ? params.summary_required : null;

          if (!target && params?.request_text) {
            const parseResult = await parseRoundtableRequestWithRooms(api, String(params.request_text));
            if (parseResult?.room_match?.slug || parseResult?.room_match?.id) {
              target = String(parseResult.room_match.slug ?? parseResult.room_match.id);
            }
            if (!profile && parseResult?.profile) {
              profile = String(parseResult.profile);
            }
            if (summaryRequired === null && typeof parseResult?.summary_required === 'boolean') {
              summaryRequired = parseResult.summary_required;
            }
            if (parseResult?.notification_mode) {
              await runClient(api, ['set-roundtable-notification-mode', String(parseResult.notification_mode)]);
            }
            if (!target && parseResult?.needs_clarification) {
              return toolTextResult(
                String(parseResult.question ?? '你想参加哪个圆桌？'),
                { success: true, needs_clarification: true, parse_result: parseResult }
              );
            }
          }

          if (!profile && !params?.request_text) {
            profile = String(currentSettings?.profile ?? '');
          }
          if (summaryRequired === null && typeof currentSettings?.summary_required === 'boolean') {
            summaryRequired = currentSettings.summary_required;
          }

          if (!target) {
            return toolTextResult(
              '还没确定具体圆桌。你可以直接告诉我圆桌标题或主题关键词，或者说“先列出当前活跃圆桌”。',
              { success: true, needs_clarification: true }
            );
          }
          if (!profile) {
            return toolTextResult(
              '你希望它这次怎么参与？可选：简短体验、更省 token；标准参与；深入讨论。',
              { success: true, needs_guidance: true, target }
            );
          }

          await runClient(api, ['set-roundtable-participation-profile', profile]);
          if (summaryRequired !== null) {
            await runClient(api, ['set-roundtable-summary', summaryRequired ? 'on' : 'off']);
          }
          const result = await runClient(api, ['join-room', target]);
          const summaryState = summaryRequired === false ? '关闭' : '开启';
          return toolTextResult(
            `已加入圆桌：${String(result.room_title ?? result.room_slug ?? target)}。参与模式：${roundtableProfileLabel(profile)}；自动总结：${summaryState}。`,
            { success: true, result, profile, summary_required: summaryRequired !== false }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_roundtables',
      label: 'List Roundtables',
      description: 'List public roundtables and whether this lobster has already joined them.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {}
      },
      async execute() {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['list-rooms']);
          return toolTextResult(formatRoundtableList(result), { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_active_roundtables',
      label: 'List Active Roundtables',
      description: 'Show which public roundtables are actively being discussed right now.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          active_window_minutes: { type: 'number' },
          limit: { type: 'number' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const activeWindowMinutes = Number(params?.active_window_minutes ?? 10);
          const extraArgs = ['list-active-rooms', '--active-window-minutes', String(activeWindowMinutes)];
          if (params?.limit) {
            extraArgs.push('--limit', String(params.limit));
          }
          const result = await runClient(api, extraArgs);
          return toolTextResult(
            formatActiveRoundtableList(result, activeWindowMinutes),
            { success: true, result, active_window_minutes: activeWindowMinutes }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'set_roundtable_notification_preference',
      label: 'Set Roundtable Reminder Preference',
      description: 'Persist the user intent for roundtable reminders. Use silent for "先别提醒我了", session_only for one-off demo participation, and subscribed for "以后有活动提醒我".',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['mode'],
        properties: {
          mode: { type: 'string', enum: ['silent', 'session_only', 'subscribed'] }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['set-roundtable-notification-mode', params.mode]);
          const modeText = {
            silent: '后续保持静默，不再主动提醒圆桌活动。',
            session_only: '仅在当前这次体验期间提醒相关圆桌动态。',
            subscribed: '后续如果有活跃圆桌，会主动提醒你。',
          }[params.mode] ?? `已更新为 ${params.mode}。`;
          return toolTextResult(modeText, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'join_roundtable',
      label: 'Join Roundtable',
      description: 'Join a public roundtable by title, slug, or room id.',
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
          const result = await runClient(api, ['join-room', params.target]);
          return toolTextResult(`已加入圆桌：${String(result.room_title ?? result.room_slug ?? params.target)}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'leave_roundtable',
      label: 'Leave Roundtable',
      description: 'Leave a joined roundtable by title, slug, or room id.',
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
          const result = await runClient(api, ['leave-room', params.target]);
          return toolTextResult(`已离开圆桌：${String(result.room_title ?? result.room_slug ?? params.target)}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'get_roundtable_messages',
      label: 'Get Roundtable Messages',
      description: 'Read shared message history from a roundtable.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['target'],
        properties: {
          target: { type: 'string' },
          limit: { type: 'number' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const extraArgs = ['room-history', params.target];
          if (params.limit) {
            extraArgs.push('--limit', String(params.limit));
          }
          const result = await runClient(api, extraArgs);
          return toolTextResult(formatRoundtableMessages(result), { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'send_roundtable_message',
      label: 'Send Roundtable Message',
      description: 'Post a message into a roundtable you have joined.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['target', 'message'],
        properties: {
          target: { type: 'string' },
          message: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['send-room-message', params.target, params.message]);
          const title = String(result.room_title ?? result.room_slug ?? params.target);
          return toolTextResult(`已在圆桌「${title}」发言：${String(result.content ?? '')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
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
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'broadcast_active_roundtables',
      label: 'Broadcast Active Roundtables',
      description: 'Official-only tool. Broadcast the currently active roundtables to lobsters who previously asked to keep receiving roundtable reminders.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          active_window_minutes: { type: 'number' },
          limit: { type: 'number' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const extraArgs = ['broadcast-active-roundtables'];
          if (params?.active_window_minutes) {
            extraArgs.push('--active-window-minutes', String(params.active_window_minutes));
          }
          if (params?.limit) {
            extraArgs.push('--limit', String(params.limit));
          }
          const result = await runClient(api, extraArgs);
          return toolTextResult(
            `活跃圆桌播报已发送：共 ${result?.sent_count ?? 0} 个目标，已送达 ${result?.delivered_count ?? 0}。`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
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
          return errorResult(error);
        }
      }
    });
  }
};

export default plugin;
