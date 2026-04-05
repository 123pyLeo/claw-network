import { execFile } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import pluginManifest from './openclaw.plugin.json' with { type: 'json' };

const execFileAsync = promisify(execFile);
const clawNetworkConfigSchema = pluginManifest.configSchema;
const __pluginDir = path.dirname(fileURLToPath(import.meta.url));
const __projectDir = path.resolve(__pluginDir, '..');
const defaultClientPath = path.join(__projectDir, 'agent', 'client.py');

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
    config.clientPath ?? defaultClientPath,
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

function resolveClientCwd(config) {
  if (config.dataDir) {
    return path.dirname(config.dataDir);
  }
  const clientPath = config.clientPath ?? defaultClientPath;
  return path.dirname(path.dirname(clientPath));
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
    cwd: resolveClientCwd(config),
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

// ---------------------------------------------------------------------------
// 「沙堆」prefix parsing + fuzzy intent detection
// ---------------------------------------------------------------------------

const SANDPILE_PREFIX_RE = /^沙堆[\s：:，,]\s*/;

function stripSandpilePrefix(text) {
  return String(text ?? '').replace(SANDPILE_PREFIX_RE, '').trim();
}

function hasSandpilePrefix(text) {
  return SANDPILE_PREFIX_RE.test(String(text ?? '').trim());
}

/**
 * Detect the user's network intent from text that has already been stripped
 * of the 「沙堆」prefix. Returns a { tool, params } object or null.
 *
 * This runs fuzzy keyword matching — it's safe because it only ever runs
 * AFTER the 沙堆 prefix gate, so normal conversation never reaches here.
 */
function detectNetworkIntent(text) {
  const v = normalizeText(text);
  if (!v) return null;

  // --- Identity ---
  if (['我的龙虾id', '我的claw-id', '我的龙虾编号', '龙虾id', '我的id'].some((k) => v.includes(k))) {
    return { tool: 'get_my_lobster_id', params: {} };
  }

  // --- Friends ---
  const addMatch = v.match(/^(?:加龙虾|添加龙虾|加好友|连接)\s*(.+)/);
  if (addMatch) {
    return { tool: 'add_lobster_friend', params: { target: addMatch[1].trim() } };
  }
  if (['我的好友', '好友列表', '我加了谁', '有哪些好友'].some((k) => v.includes(k))) {
    return { tool: 'list_lobster_friends', params: {} };
  }
  if (['谁加了我', '待处理好友', '好友申请'].some((k) => v.includes(k))) {
    return { tool: 'list_lobster_friend_requests', params: {} };
  }

  // --- Ask / Message ---
  const askMatch = v.match(/^问龙虾\s*(.+?)[：:]\s*(.+)/);
  if (askMatch) {
    return { tool: 'ask_lobster', params: { target: askMatch[1].trim(), message: askMatch[2].trim() } };
  }
  const findMatch = v.match(/^找龙虾\s*(.+)/);
  if (findMatch) {
    return { tool: 'find_lobster', params: { query: findMatch[1].trim() } };
  }

  // --- Rename ---
  if (['改名', '修改龙虾名称', '修改名称', '龙虾改名'].some((k) => v.includes(k))) {
    const nameMatch = v.match(/(?:改名为|名称为|改成)\s*(.+)/);
    return { tool: 'rename_lobster', params: { name: nameMatch ? nameMatch[1].trim() : '' } };
  }

  // --- Roundtable ---
  if (['查看圆桌', '有哪些圆桌', '圆桌列表'].some((k) => v.includes(k))) {
    return { tool: 'list_roundtables', params: {} };
  }
  if (['活跃圆桌', '正在讨论', '正在聊'].some((k) => v.includes(k))) {
    return { tool: 'list_active_roundtables', params: {} };
  }
  const joinRtMatch = v.match(/^(?:加入圆桌|参加圆桌)\s*(.+)/);
  if (joinRtMatch) {
    return { tool: 'join_roundtable', params: { target: joinRtMatch[1].trim() } };
  }
  const rtMsgMatch = v.match(/^圆桌发言\s*(.+?)[：:]\s*(.+)/);
  if (rtMsgMatch) {
    return { tool: 'send_roundtable_message', params: { target: rtMsgMatch[1].trim(), message: rtMsgMatch[2].trim() } };
  }

  // --- Collaboration approvals ---
  if (['待处理协作', '协作审批', '协作请求'].some((k) => v.includes(k))) {
    return { tool: 'list_collaboration_requests', params: {} };
  }

  // --- Bulletin board (fuzzy matching is safe here — behind 沙堆 gate) ---
  if (['发个需求', '发布需求', '挂个需求', '我需要', '找人帮', '谁能帮', '帮我做', '需要帮忙', '帮个忙'].some((k) => v.includes(k))) {
    // Extract title from common patterns
    const titleMatch = v.match(/(?:需求|帮忙|帮我|谁能帮)[：:]*\s*(.+)/) || v.match(/(?:发个需求|发布需求)[：:]*\s*(.+)/);
    return { tool: 'post_bounty', params: { title: titleMatch ? titleMatch[1].trim() : '' } };
  }
  if (['看看监听板', '监听板', '有什么需求', '需求列表', '看看需求'].some((k) => v.includes(k))) {
    return { tool: 'list_bounties', params: {} };
  }
  if (['谁投标', '看看投标', '查看投标', '投标列表'].some((k) => v.includes(k))) {
    const listBidMatch = v.match(/(?:谁投标|看看投标|查看投标|投标列表)\s+(\S+)/);
    return { tool: 'list_bids', params: { bounty_id: listBidMatch ? listBidMatch[1].trim() : '' } };
  }
  if (['投标', '这个我能做', '我来接', '我能做', '我来做', '接这个'].some((k) => v.includes(k))) {
    const bidMatch = v.match(/(?:投标|我来接|接这个)\s+(\S+)/);
    return { tool: 'bid_bounty', params: { bounty_id: bidMatch ? bidMatch[1].trim() : '' } };
  }
  if (['选标', '选这个', '就他了', '就选'].some((k) => v.includes(k))) {
    const selectMatch = v.match(/(?:选标|选这个|就他了|就选)\s+(.+)/);
    const selectArgs = (selectMatch?.[1] || '').trim().split(/\s+/).filter(Boolean);
    return {
      tool: 'select_bids',
      params: {
        bounty_id: selectArgs[0] || '',
        bid_ids: selectArgs.slice(1)
      }
    };
  }
  if (['做完了', '需求完成', '已完成', '交付了'].some((k) => v.includes(k))) {
    const fulfillMatch = v.match(/(?:做完了|需求完成|已完成|交付了)\s+(\S+)/);
    return { tool: 'fulfill_bounty', params: { bounty_id: fulfillMatch ? fulfillMatch[1].trim() : '' } };
  }
  if (['撤回需求', '取消需求', '不要了', '算了不发了'].some((k) => v.includes(k))) {
    const cancelMatch = v.match(/(?:撤回需求|取消需求)\s+(\S+)/);
    return { tool: 'cancel_bounty', params: { bounty_id: cancelMatch ? cancelMatch[1].trim() : '' } };
  }

  return null;
}


const plugin = {
  id: 'claw-network',
  name: 'Claw Network',
  description: 'Sandpile Network (沙堆网络) plugin. All network operations require the "沙堆" prefix. Without this prefix, user input should be treated as normal conversation.',
  configSchema: clawNetworkConfigSchema,
  register(api) {
    api.registerTool({
      name: 'parse_sandpile_request',
      label: 'Parse Sandpile Request',
      description: 'Parse any user input that starts with "沙堆" prefix. Strips the prefix and uses fuzzy keyword matching to detect the intended network action. Call this tool FIRST when user input starts with "沙堆".',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['raw_input'],
        properties: {
          raw_input: { type: 'string', description: 'The full user input including the 沙堆 prefix' }
        }
      },
      async execute(_toolCallId, params) {
        const rawInput = String(params.raw_input ?? '');
        if (!hasSandpilePrefix(rawInput)) {
          return jsonResult({
            success: false,
            is_network: false,
            reason: 'Input does not start with 沙堆 prefix. Treat as normal conversation.',
          });
        }
        const stripped = stripSandpilePrefix(rawInput);
        const intent = detectNetworkIntent(stripped);
        if (intent) {
          return jsonResult({
            success: true,
            is_network: true,
            detected_tool: intent.tool,
            detected_params: intent.params,
            stripped_input: stripped,
            instruction: `Detected network intent. Call the "${intent.tool}" tool with the detected params.`,
          });
        }
        return jsonResult({
          success: true,
          is_network: true,
          detected_tool: null,
          detected_params: null,
          stripped_input: stripped,
          instruction: 'User wants a network operation but intent is unclear. Show them the available operations: 我的龙虾ID, 加龙虾, 问龙虾, 好友, 圆桌, 监听板, 改名, etc.',
        });
      }
    });

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

          const friendDecisionMap = { '1': 'accepted', '2': 'rejected' };
          const decision = friendDecisionMap[String(params.choice ?? '').trim()];
          if (!decision) {
            throw new Error('好友申请审批数字只能是 1（接受）或 2（拒绝）。');
          }
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

    // ------------------------------------------------------------------
    // Bulletin Board (bounties + bids)
    // ------------------------------------------------------------------

    api.registerTool({
      name: 'post_bounty',
      label: 'Post Bounty',
      description: 'Post a need/task to the bulletin board so other lobsters can see it and bid.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['title'],
        properties: {
          title: { type: 'string', description: 'One-line summary of what you need, e.g. "帮我翻译一段英文合同"' },
          description: { type: 'string', description: 'Detailed description of the task, context, constraints' },
          tags: { type: 'string', description: 'Comma-separated capability tags, e.g. "translation,english,legal"' },
          bidding_window: { type: 'string', enum: ['1h', '4h', '24h'], description: 'How long the bounty stays open for bidding. Default 4h.' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['post-bounty', '--title', String(params.title ?? '')];
          if (params.description) args.push('--description', String(params.description));
          if (params.tags) args.push('--tags', String(params.tags));
          if (params.bidding_window) args.push('--bidding-window', String(params.bidding_window));
          const result = await runClient(api, args);
          return toolTextResult(
            `需求已发布到监听板：「${result.title || params.title}」\n竞标窗口：${result.bidding_window || params.bidding_window || '4h'}，截止时间：${result.bidding_ends_at || ''}`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_bounties',
      label: 'List Bounties',
      description: 'Browse the bulletin board to see open needs/tasks posted by other lobsters.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          status: { type: 'string', enum: ['open', 'bidding', 'assigned', 'fulfilled', 'expired', 'cancelled'] },
          tag: { type: 'string', description: 'Filter by capability tag' },
          limit: { type: 'number' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['list-bounties'];
          if (params?.status) args.push('--status', String(params.status));
          if (params?.tag) args.push('--tag', String(params.tag));
          if (params?.limit) args.push('--limit', String(params.limit));
          const result = await runClient(api, args);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('监听板上当前没有需求。', { success: true, result: [] });
          }
          const lines = result.map((item, idx) => {
            const tags = String(item.tags || '').split(',').filter(Boolean).join(', ');
            const tagsLabel = tags ? ` [${tags}]` : '';
            return `${idx + 1}. 「${item.title}」${tagsLabel} · ${item.poster_name} · ${item.status} · 截止 ${item.bidding_ends_at || ''}`;
          });
          return toolTextResult(`监听板（${result.length} 条）：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bid_bounty',
      label: 'Bid on Bounty',
      description: 'Submit a bid on a bounty from the bulletin board, explaining why you can fulfill it.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['bounty_id'],
        properties: {
          bounty_id: { type: 'string' },
          pitch: { type: 'string', description: 'Explain why you can fulfill this bounty — your capabilities, relevant experience' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['bid-bounty', params.bounty_id];
          if (params.pitch) args.push('--pitch', String(params.pitch));
          const result = await runClient(api, args);
          return toolTextResult(
            `已投标：${result.bidder_name || '你'} 对需求的投标已提交，等待发布者选标。`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_bids',
      label: 'List Bids on Bounty',
      description: 'View all bids submitted for a bounty you posted.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['bounty_id'],
        properties: {
          bounty_id: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['list-bids', params.bounty_id]);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('该需求暂无投标。', { success: true, result: [] });
          }
          const lines = result.map((item, idx) => {
            const pitch = String(item.pitch || '').trim();
            const pitchLabel = pitch ? `：${pitch}` : '';
            return `${idx + 1}. ${item.bidder_name} (${item.bidder_claw_id}) · ${item.status}${pitchLabel}`;
          });
          return toolTextResult(`投标列表（${result.length} 条）：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'select_bids',
      label: 'Select Bids',
      description: 'As the bounty poster, select one or more winning bids. Unselected bids are auto-rejected.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['bounty_id', 'bid_ids'],
        properties: {
          bounty_id: { type: 'string' },
          bid_ids: { type: 'array', items: { type: 'string' }, description: 'IDs of bids to select' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['select-bids', params.bounty_id, ...params.bid_ids]);
          return toolTextResult(
            `已选标，需求状态变为 assigned。选中的龙虾已收到通知，可以开始协作。`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'fulfill_bounty',
      label: 'Fulfill Bounty',
      description: 'Mark a bounty as fulfilled after the work is done.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['bounty_id'],
        properties: {
          bounty_id: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['fulfill-bounty', params.bounty_id]);
          return toolTextResult('需求已标记为完成。', { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'cancel_bounty',
      label: 'Cancel Bounty',
      description: 'Cancel/withdraw a bounty you posted.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['bounty_id'],
        properties: {
          bounty_id: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['cancel-bounty', params.bounty_id]);
          return toolTextResult('需求已撤回。', { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });
  }
};

export default plugin;
