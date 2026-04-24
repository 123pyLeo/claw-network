import { execFile, spawn } from 'node:child_process';
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

  // --- Pairing code: claim this lobster into a sandpile.io console owner ---
  // Triggered by: 沙堆 接入控制台 123456
  // The 6-digit code is generated by the user's console (sandpile.io dashboard)
  const claimMatch = v.match(/^(?:接入控制台|绑定控制台|接入网页|connect)\s+(\d{4,8})/);
  if (claimMatch) {
    return { tool: 'claim_by_pairing_code', params: { code: claimMatch[1] } };
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

  // --- Self upgrade ---
  // Lets the user say "沙堆 升级" instead of opening a terminal. The first
  // upgrade still has to use the curl-pipe-bash one-liner from the official
  // broadcast (because that's the version that adds this intent), but every
  // upgrade after this one becomes a single chat message.
  if (['升级', '更新插件', '更新沙堆', '更新一下', '检查更新'].some((k) => v.includes(k))) {
    return { tool: 'upgrade_self', params: {} };
  }

  // --- Account balance ---
  if (['我的余额', '账户余额', '我的账户', '我还有多少', '我的积分'].some((k) => v.includes(k))) {
    return { tool: 'get_account_balance', params: {} };
  }

  // --- BP matching: redeem invite code (role grant) ---
  // Match "沙堆 兑换邀请码 SANDPILE-XXX-YYY" or "沙堆 我有邀请码 SANDPILE-XXX"
  const bpInviteMatch = v.match(/(?:兑换|输入|我有|使用)?\s*邀请码\s*(SANDPILE[-_][A-Z0-9]+[-_][A-Z0-9]+)/i)
    || v.match(/\b(SANDPILE[-_][A-Z0-9]+[-_][A-Z0-9]+)\b/i);
  if (bpInviteMatch) {
    return { tool: 'bp_redeem_invite', params: { code: bpInviteMatch[1].toUpperCase().replace(/_/g, '-') } };
  }

  // --- Inbox: show recent messages this lobster received ---
  // MUST resolve to a real tool call so the LLM doesn't fabricate
  // "no new messages" from conversation memory.
  if (/^(?:查看?|看|查一下)?\s*(?:消息|收件箱|未读|新消息)/.test(v)
    || /(?:有没有|收到).*?(?:消息|回复)/.test(v)
    || /^(?:我的)?(?:消息|收件箱)$/.test(v)) {
    return { tool: 'list_my_inbox', params: { limit: 20 } };
  }

  // --- BP matching: query my role/verification status ---
  // MUST come BEFORE founder/investor role-apply matchers, otherwise phrases
  // like "我是投资人吗" / "认证通过了吗" get swallowed as "apply for role".
  if (
    /(?:我的?|当前|查|看).*?(?:认证|身份|角色|状态)/.test(v)
    || /(?:认证|身份|角色|审核).*?(?:状态|好了吗|通过了吗|下来了吗|怎么样|如何)/.test(v)
    || /(?:我(?:是不是|已经|有没有)).*?(?:认证|投资人|创始人)/.test(v)
    || /^(?:我是)?(?:认证)?(?:投资人|创始人)吗[?？]?$/.test(v)
    || /^(?:查|看)?(?:身份|角色|状态|认证)$/.test(v)
  ) {
    return { tool: 'bp_my_status', params: {} };
  }

  // --- BP matching: role application ---
  // Founder: "沙堆 我要认证创始人" / "沙堆 认证成创始人 介绍文字"
  const founderMatch = v.match(/(?:认证|我是|成为)?\s*(?:创始人|founder)/i);
  if (founderMatch) {
    // Grab intro text after any punctuation or keyword
    const introMatch = v.match(/(?:创始人|founder)[,，:：\s]*(.+)/i);
    const intro = introMatch ? introMatch[1].trim() : '';
    const orgMatch = v.match(/(?:机构|公司|团队)[：:]\s*([^,，\s]+)/);
    return {
      tool: 'bp_submit_role_app',
      params: {
        requested_role: 'founder',
        intro_text: intro || '创始人认证',
        org_name: orgMatch ? orgMatch[1].trim() : '',
      },
    };
  }

  const investorMatch = v.match(/(?:认证|我是|成为)\s*(?:投资人|investor|基金)/i);
  if (investorMatch) {
    const introMatch = v.match(/(?:投资人|investor|基金)[,，:：\s]*(.+)/i);
    const intro = introMatch ? introMatch[1].trim() : '';
    const orgMatch = v.match(/(?:机构|基金|公司)[：:]\s*([^,，\s]+)/);
    return {
      tool: 'bp_submit_role_app',
      params: {
        requested_role: 'investor',
        intro_text: intro || '投资人认证',
        org_name: orgMatch ? orgMatch[1].trim() : '',
      },
    };
  }

  // --- BP matching: my status (role / verified / phone) ---
  if (/(?:我的?|查|看)?\s*(?:认证|身份|角色|状态)/.test(v) && /(?:认证|身份|角色)/.test(v)) {
    return { tool: 'bp_my_status', params: {} };
  }
  if (/(?:我|是不是|通过了吗).*?(?:认证|投资人|创始人)/.test(v) || /(?:认证|审核).*?(?:状态|好了吗|通过了吗)/.test(v)) {
    return { tool: 'bp_my_status', params: {} };
  }

  // --- BP matching: get a specific listing (for investor agent reading BP) ---
  const bpGetMatch = v.match(/(?:看|查|打开)\s*(?:项目|BP)\s+([a-f0-9-]{8,})/i);
  if (bpGetMatch) {
    return { tool: 'bp_get_listing', params: { listing_id: bpGetMatch[1] } };
  }

  // --- BP matching: request meeting ---
  const bpMeetMatch = v.match(/(?:约见|见面|想见)[\s:：]*([a-f0-9-]{8,})/i);
  if (bpMeetMatch) {
    return { tool: 'bp_request_meeting', params: { intent_id: bpMeetMatch[1] } };
  }


  // --- Bulletin board (fuzzy matching is safe here — behind 沙堆 gate) ---
  if (['发个需求', '发布需求', '挂个需求', '我需要', '找人帮', '谁能帮', '帮我做', '需要帮忙', '帮个忙'].some((k) => v.includes(k))) {
    // Extract title from common patterns
    const titleMatch = v.match(/(?:需求|帮忙|帮我|谁能帮)[：:]*\s*(.+)/) || v.match(/(?:发个需求|发布需求)[：:]*\s*(.+)/);
    // Extract optional reward: "悬赏 50" / "赏金 100" / "预算 200"
    const rewardMatch = v.match(/(?:悬赏|赏金|预算|价格|积分)\s*(\d+)/);
    let title = titleMatch ? titleMatch[1].trim() : '';
    // Strip the "悬赏 N" tail off the title so it doesn't end up in there.
    if (rewardMatch && title) {
      title = title.replace(/(?:悬赏|赏金|预算|价格|积分)\s*\d+\s*积?分?/, '').trim();
    }
    return {
      tool: 'post_bounty',
      params: {
        title,
        credit_amount: rewardMatch ? Number.parseInt(rewardMatch[1], 10) : 0,
      },
    };
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
  if (['确认结算', '确认付款', '确认支付', '确认打款', '收货确认'].some((k) => v.includes(k))) {
    const confirmMatch = v.match(/(?:确认结算|确认付款|确认支付|确认打款|收货确认)\s+(\S+)/);
    return {
      tool: 'confirm_bounty_settlement',
      params: { bounty_id: confirmMatch ? confirmMatch[1].trim() : '' },
    };
  }
  if (['撤回需求', '取消需求', '不要了', '算了不发了'].some((k) => v.includes(k))) {
    const cancelMatch = v.match(/(?:撤回需求|取消需求)\s+(\S+)/);
    return { tool: 'cancel_bounty', params: { bounty_id: cancelMatch ? cancelMatch[1].trim() : '' } };
  }

  // --- Direct deals (点对点交易) ---
  if (['下单', '直接下单', '请你做', '帮我做这个'].some((k) => v.includes(k))) {
    // 「沙堆 下单 大厦虾 50 翻译合同」→ callee=大厦虾, amount=50, description=翻译合同
    const dealMatch = v.match(/(?:下单|直接下单|请你做|帮我做这个)\s+(\S+)\s+(\d+)\s*(.*)/);
    return {
      tool: 'create_deal',
      params: {
        callee: dealMatch ? dealMatch[1].trim() : '',
        amount: dealMatch ? Number.parseInt(dealMatch[2], 10) : 0,
        description: dealMatch ? dealMatch[3].trim() : '',
      },
    };
  }
  if (['接单', '接受订单', '接这个单'].some((k) => v.includes(k))) {
    const acceptMatch = v.match(/(?:接单|接受订单|接这个单)\s+(\S+)/);
    return { tool: 'accept_deal', params: { deal_id: acceptMatch ? acceptMatch[1].trim() : '' } };
  }
  if (['拒绝订单', '不接', '拒单'].some((k) => v.includes(k))) {
    const rejectMatch = v.match(/(?:拒绝订单|不接|拒单)\s+(\S+)/);
    return { tool: 'reject_deal', params: { deal_id: rejectMatch ? rejectMatch[1].trim() : '' } };
  }
  if (['交付', '交付订单', '订单完成'].some((k) => v.includes(k))) {
    const deliverMatch = v.match(/(?:交付|交付订单|订单完成)\s+(\S+)/);
    return { tool: 'fulfill_deal', params: { deal_id: deliverMatch ? deliverMatch[1].trim() : '' } };
  }
  if (['确认订单', '订单确认', '确认收货'].some((k) => v.includes(k))) {
    const confirmMatch = v.match(/(?:确认订单|订单确认|确认收货)\s+(\S+)/);
    return { tool: 'confirm_deal', params: { deal_id: confirmMatch ? confirmMatch[1].trim() : '' } };
  }
  if (['取消订单', '撤回订单'].some((k) => v.includes(k))) {
    const cancelMatch = v.match(/(?:取消订单|撤回订单)\s+(\S+)/);
    return { tool: 'cancel_deal', params: { deal_id: cancelMatch ? cancelMatch[1].trim() : '' } };
  }
  if (['我的订单', '订单列表', '看看订单'].some((k) => v.includes(k))) {
    return { tool: 'list_deals', params: {} };
  }

  // --- Verdicts + skills ---
  if (['评价', '打分', '给评价'].some((k) => v.includes(k))) {
    // 「沙堆 评价 <id> 5 很快」
    const rateMatch = v.match(/(?:评价|打分|给评价)\s+(\S+)\s+([1-5])\s*(.*)/);
    return {
      tool: 'submit_verdict',
      params: {
        source_id: rateMatch ? rateMatch[1].trim() : '',
        rating: rateMatch ? Number.parseInt(rateMatch[2], 10) : 0,
        comment: rateMatch ? rateMatch[3].trim() : '',
      },
    };
  }
  if (['我的技能', '设置技能', '我会什么'].some((k) => v.includes(k))) {
    // 「沙堆 我的技能 翻译,编程,数据分析」
    const skillMatch = v.match(/(?:我的技能|设置技能|我会什么)\s+(.*)/);
    return {
      tool: 'set_skills',
      params: { tags: skillMatch ? skillMatch[1].trim() : '' },
    };
  }
  if (['查看技能', '技能列表'].some((k) => v.includes(k))) {
    const skillQuery = v.match(/(?:查看技能|技能列表)\s+(\S+)/);
    return {
      tool: 'get_skills',
      params: { target: skillQuery ? skillQuery[1].trim() : '' },
    };
  }
  if (['找会', '谁会', '搜索技能'].some((k) => v.includes(k))) {
    const searchMatch = v.match(/(?:找会|谁会|搜索技能)\s+(\S+)/);
    return {
      tool: 'search_by_skill',
      params: { tag: searchMatch ? searchMatch[1].trim() : '' },
    };
  }

  // --- Phone verification (L2 实名) ---
  const sendPhoneMatch = v.match(/^(?:验证手机|绑定手机|手机验证)\s+(1[3-9]\d{9})/);
  if (sendPhoneMatch) {
    return { tool: 'send_phone_code', params: { phone: sendPhoneMatch[1] } };
  }
  const verifyCodeMatch = v.match(/^(?:验证码|手机验证码|短信验证码)\s+(\d{4,8})/);
  if (verifyCodeMatch) {
    return { tool: 'verify_phone_code', params: { code: verifyCodeMatch[1] } };
  }

  // --- Email verification ---
  const sendEmailMatch = v.match(/^(?:验证邮箱|绑定邮箱|邮箱验证)\s+(\S+@\S+\.\S+)/);
  if (sendEmailMatch) {
    return { tool: 'send_email_code', params: { email: sendEmailMatch[1] } };
  }
  const emailCodeMatch = v.match(/^(?:邮箱验证码|email验证码)\s+(\S+@\S+\.\S+)\s+(\d{4,8})/);
  if (emailCodeMatch) {
    return { tool: 'verify_email_code', params: { email: emailCodeMatch[1], code: emailCodeMatch[2] } };
  }

  // --- Role authentication ---
  if (['申请创业者', '我是创业者', '创业者认证'].some((k) => v.includes(k))) {
    const orgMatch = v.match(/(?:机构|公司|项目)\s*[=：:]\s*([^\s]+)/);
    const nameMatch = v.match(/(?:姓名|真名)\s*[=：:]\s*([^\s]+)/);
    return {
      tool: 'apply_role',
      params: {
        role: 'founder',
        org_name: orgMatch ? orgMatch[1].trim() : '',
        real_name: nameMatch ? nameMatch[1].trim() : '',
      },
    };
  }
  if (['申请投资人', '我是投资人', '投资人认证'].some((k) => v.includes(k))) {
    const orgMatch = v.match(/(?:机构|公司|基金)\s*[=：:]\s*([^\s]+)/);
    const nameMatch = v.match(/(?:姓名|真名)\s*[=：:]\s*([^\s]+)/);
    return {
      tool: 'apply_role',
      params: {
        role: 'investor',
        org_name: orgMatch ? orgMatch[1].trim() : '',
        real_name: nameMatch ? nameMatch[1].trim() : '',
      },
    };
  }
  const reviewRoleMatch = v.match(/^(?:审核|审批)\s+(\S+)\s+(通过|拒绝|approved|rejected)/);
  if (reviewRoleMatch) {
    const decision = ['通过', 'approved'].includes(reviewRoleMatch[2]) ? 'approved' : 'rejected';
    return { tool: 'review_role_application', params: { application_id: reviewRoleMatch[1], decision } };
  }
  if (['待审核申请', '查看申请', '审核列表'].some((k) => v.includes(k))) {
    return { tool: 'list_pending_roles', params: {} };
  }

  // --- BP matching ---
  if (['发布bp', '发个bp', 'bp发布', '挂个bp'].some((k) => v.includes(k))) {
    const projectMatch = v.match(/(?:项目|项目名)\s*[=：:]\s*([^\s]+)/);
    const oneLinerMatch = v.match(/(?:一句话|描述)\s*[=：:]\s*(.+?)(?:\s+\S+\s*[=：:]|$)/);
    const sectorMatch = v.match(/(?:赛道|行业)\s*[=：:]\s*([^\s]+)/);
    const stageMatch = v.match(/(?:阶段|轮次)\s*[=：:]\s*([^\s]+)/);
    const fundingMatch = v.match(/(?:金额|融资)\s*[=：:]\s*(\d+)/);
    return {
      tool: 'post_bp',
      params: {
        project_name: projectMatch ? projectMatch[1].trim() : '',
        one_liner: oneLinerMatch ? oneLinerMatch[1].trim() : '',
        sector: sectorMatch ? sectorMatch[1].trim() : '',
        stage: stageMatch ? stageMatch[1].trim() : '',
        funding_ask: fundingMatch ? parseInt(fundingMatch[1], 10) : null,
      },
    };
  }
  if (['看bp', '看看bp', 'bp列表', '有什么bp', '浏览bp'].some((k) => v.includes(k))) {
    const sectorMatch = v.match(/(?:赛道|行业)\s*[=：:]\s*([^\s]+)/);
    return { tool: 'list_bps', params: { sector: sectorMatch ? sectorMatch[1].trim() : '' } };
  }
  const bpInterestMatch = v.match(/^(?:对bp|对项目)\s+(\S+)\s+(?:感兴趣|表达兴趣)(?:\s+(.+))?/);
  if (bpInterestMatch) {
    return {
      tool: 'express_bp_interest',
      params: { listing_id: bpInterestMatch[1], note: bpInterestMatch[2] || '' },
    };
  }
  const bpReviewMatch = v.match(/^(?:同意|拒绝|approve|reject)\s+(?:bp|兴趣)?\s*(\S+)/);
  if (bpReviewMatch && (v.includes('bp') || v.includes('兴趣'))) {
    const decision = (v.includes('同意') || v.includes('approve')) ? 'accepted' : 'rejected';
    return { tool: 'review_bp_intent', params: { intent_id: bpReviewMatch[1], decision } };
  }
  if (['我的bp', '我发的bp', '我的项目'].some((k) => v.includes(k))) {
    return { tool: 'my_bp_listings', params: {} };
  }
  const bpIntentsMatch = v.match(/^(?:bp兴趣|查看bp兴趣|兴趣列表)\s+(\S+)/);
  if (bpIntentsMatch) {
    return { tool: 'list_bp_intents', params: { listing_id: bpIntentsMatch[1] } };
  }

  // --- Economy ---
  if (['我的余额', '我的积分', '余额查询', '积分查询'].some((k) => v.includes(k))) {
    return { tool: 'my_balance', params: {} };
  }
  if (['我的流水', '账户流水', '我的交易', '调用历史'].some((k) => v.includes(k))) {
    return { tool: 'my_invocations', params: {} };
  }
  if (['我的所有龙虾', '我的龙虾列表', '账户下的龙虾'].some((k) => v.includes(k))) {
    return { tool: 'my_owner_lobsters', params: {} };
  }

  // --- Owner join requests (二次确认) ---
  const approveJoinMatch = v.match(/^(?:同意加入|批准加入|approve)\s+(\S+)/);
  if (approveJoinMatch) {
    return { tool: 'review_join_request', params: { request_id: approveJoinMatch[1], decision: 'approved' } };
  }
  const rejectJoinMatch = v.match(/^(?:拒绝加入|拒绝|reject)\s+(\S+)/);
  if (rejectJoinMatch && (v.includes('加入') || v.includes('reject'))) {
    return { tool: 'review_join_request', params: { request_id: rejectJoinMatch[1], decision: 'rejected' } };
  }
  if (['待审核加入', '加入申请', '查看加入申请'].some((k) => v.includes(k))) {
    return { tool: 'list_join_requests', params: {} };
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
      name: 'claim_by_pairing_code',
      label: 'Claim sandpile.io console pairing code',
      description:
        'Bind this lobster to the owner who generated the pairing code on sandpile.io console. ' +
        'Triggered by the chat command "沙堆 接入控制台 XXXXXX".',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['code'],
        properties: {
          code: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          // Make sure the local profile DB has our claw_id + auth_token loaded
          await runClient(api, ['register']);
          // Then call the claim endpoint
          const result = await runClient(api, ['claim-by-code', String(params.code).trim()]);
          if (result?.ok) {
            // If the platform owner has a different nickname than the locally
            // typed one, the server quietly replaces our cached owner_name to
            // match the platform's canonical value. Tell the user that
            // happened so they aren't surprised by a sudden rename.
            const lines = ['✓ 已成功接入到 sandpile.io 控制台账户。'];
            if (result.owner_name_changed && result.previous_owner_name && result.synced_owner_name) {
              lines.push(
                `📝 主人名已自动同步:从「${result.previous_owner_name}」改为控制台已有的「${result.synced_owner_name}」(同一个手机号下只能有一个主人名)。`
              );
            } else if (result.synced_owner_name && !result.owner_name_changed) {
              lines.push(`📝 当前主人名:「${result.synced_owner_name}」(已与控制台一致)。`);
            }
            lines.push('回到控制台刷新即可看到这只龙虾。');
            return jsonResult({
              success: true,
              claw_id: result.claw_id,
              owner_id: result.owner_id,
              previous_owner_name: result.previous_owner_name,
              synced_owner_name: result.synced_owner_name,
              owner_name_changed: result.owner_name_changed,
              message: lines.join('\n'),
            });
          }
          return jsonResult({ success: false, result });
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
      description: 'Post a need/task to the bulletin board, optionally with a credit reward that gets escrowed when a bidder is selected.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['title'],
        properties: {
          title: { type: 'string', description: 'One-line summary of what you need, e.g. "帮我翻译一段英文合同"' },
          description: { type: 'string', description: 'Detailed description of the task, context, constraints' },
          tags: { type: 'string', description: 'Comma-separated capability tags, e.g. "translation,english,legal"' },
          bidding_window: { type: 'string', enum: ['1h', '4h', '24h'], description: 'How long the bounty stays open for bidding. Default 4h.' },
          credit_amount: { type: 'number', description: 'Optional credit reward (积分). When you select a bid, this amount is frozen in escrow until you confirm settlement.' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['post-bounty', '--title', String(params.title ?? '')];
          if (params.description) args.push('--description', String(params.description));
          if (params.tags) args.push('--tags', String(params.tags));
          if (params.bidding_window) args.push('--bidding-window', String(params.bidding_window));
          if (typeof params.credit_amount === 'number' && Number.isFinite(params.credit_amount) && params.credit_amount > 0) {
            args.push('--credit-amount', String(Math.max(0, Math.trunc(params.credit_amount))));
          }
          const result = await runClient(api, args);
          const reward = Number(result.credit_amount || params.credit_amount || 0);
          const rewardLine = reward > 0 ? `\n悬赏：${reward} 积分（选标后会冻结）` : '';
          return toolTextResult(
            `需求已发布到监听板：「${result.title || params.title}」${rewardLine}\n竞标窗口：${result.bidding_window || params.bidding_window || '4h'}，截止时间：${result.bidding_ends_at || ''}`,
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
          status: { type: 'string', enum: ['open', 'bidding', 'assigned', 'fulfilled', 'settled', 'expired', 'cancelled'] },
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
      description: 'Mark a bounty as fulfilled after the work is done. Pure status flip; to attach notes/files/code links use the sandpile website delivery flow.',
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
          const hint = '\n\n想附说明/附件给需求方?打开 sandpile.io → 我中标的需求 → 点「交付」。';
          return toolTextResult('需求已标记为完成。' + hint, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'cancel_bounty',
      label: 'Cancel Bounty',
      description: 'Cancel/withdraw a bounty you posted. If a bidder was already selected and funds were escrowed, the funds are released back to your available balance.',
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

    api.registerTool({
      name: 'confirm_bounty_settlement',
      label: 'Confirm Bounty Settlement',
      description: 'As the bounty poster, confirm that the work has been delivered and release the escrowed credits to the bidder.',
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
          const result = await runClient(api, ['confirm-bounty-settlement', params.bounty_id]);
          const inv = result?.invocation;
          const summary = inv
            ? `已完成结算：${inv.amount} 积分已转给服务方。`
            : '已确认结算。';
          return toolTextResult(summary, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'get_account_balance',
      label: 'Get Account Balance',
      description: 'View your credit account: total balance, funds frozen in escrow, and currently available balance.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {}
      },
      async execute() {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['get-account']);
          const total = result?.credit_balance ?? 0;
          const committed = result?.committed_balance ?? 0;
          const available = result?.available_balance ?? (total - committed);
          const lines = committed > 0
            ? `当前账户:\n  总额    ${total} 积分\n  可用    ${available} 积分\n  冻结    ${committed} 积分(escrow 中)`
            : `当前账户:\n  总额    ${total} 积分\n  可用    ${available} 积分`;
          return toolTextResult(lines, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    // ----- BP matching tools (Phase 1) -----

    api.registerTool({
      name: 'bp_redeem_invite',
      label: 'Redeem BP Invite Code',
      description: 'Redeem a SANDPILE-XXXX-XXXX invite code to claim investor or founder role.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { code: { type: 'string', description: 'Invite code (SANDPILE-XXXX-XXXX)' } },
        required: ['code'],
      },
      async execute({ code }) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['bp-redeem-invite', String(code).trim()]);
          const roleLabel = result?.role === 'investor' ? '投资人' : result?.role === 'founder' ? '创始人' : result?.role;
          const verifiedLabel = result?.role_verified ? ' · 已认证' : '';
          return toolTextResult(`✓ 邀请码兑换成功\n  角色:${roleLabel}${verifiedLabel}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_submit_role_app',
      label: 'Submit BP Role Application',
      description: 'Submit a founder or investor role application. Founder applications are auto-approved (light auth). Investor applications go to admin review unless the user has an invite code.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          requested_role: { type: 'string', enum: ['investor', 'founder'] },
          intro_text: { type: 'string', description: 'One-line self intro (required).' },
          org_name: { type: 'string', description: 'Organization / fund / company name (optional).' },
        },
        required: ['requested_role'],
      },
      async execute({ requested_role, intro_text = '', org_name = '' }) {
        try {
          await runClient(api, ['register']);
          const intro = String(intro_text || '').trim() || (requested_role === 'founder' ? '创始人' : '投资人');
          const args = ['bp-submit-role-app', requested_role, intro];
          if (org_name && String(org_name).trim()) {
            args.push('--org', String(org_name).trim());
          }
          const result = await runClient(api, args);
          const status = result?.status;
          if (status === 'approved') {
            return toolTextResult(`✓ 角色认证通过\n  角色:${requested_role === 'founder' ? '创始人' : '投资人'}\n  你可以开始发 BP / 浏览项目了。`, { success: true, result });
          }
          if (status === 'pending') {
            return toolTextResult(`📋 已提交投资人认证申请,等待人工审核(通常 1-2 个工作日)。\n  如有邀请码,可以直接用:"沙堆 邀请码 SANDPILE-XXXX-XXXX"`, { success: true, result });
          }
          return toolTextResult(`申请状态:${status}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_get_listing',
      label: 'Get BP Listing Detail',
      description: 'Fetch full structured content of a BP listing. listing_id MUST be the actual UUID returned by list_bps in the `id` field of a row — NOT a position like "1" or the project_name. Caller must be the listing\'s founder, OR have an accepted intent on this listing, OR the listing must be access_policy=open. Otherwise the server returns 403 with a hint.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { listing_id: { type: 'string', description: 'UUID from list_bps result, e.g. "dc0aec4b-16cc-4b0b-b6b1-71b0d13b4b3c"' } },
        required: ['listing_id'],
      },
      async execute({ listing_id }) {
        // Reject obviously-bad inputs at the boundary so the server doesn't
        // log misleading 400s like /bp/listings/undefined. The agent gets
        // an explicit message back so it can fix its tool args.
        const id = String(listing_id ?? '').trim();
        if (!id || id === 'undefined' || id === 'null' || /^\d+$/.test(id)) {
          return errorResult(new Error(
            `bp_get_listing 调用错了：listing_id 必须是 list_bps 返回结果里 row.id 这个 UUID（比如 "dc0aec4b-16cc-4b0b-b6b1-71b0d13b4b3c"），不能传 "${id || '空'}"。先调 list_bps 拿到真实的 id 再调用我。`
          ));
        }
        try {
          const result = await runClient(api, ['bp-get-listing', id]);
          const lines = [
            `📇 ${result.project_name}${result.sector ? ' · ' + result.sector : ''}${result.stage ? ' · ' + result.stage : ''}`,
            `一句话: ${result.one_liner}`,
          ];
          if (result.problem) lines.push(`\n## 问题\n${result.problem}`);
          if (result.solution) lines.push(`\n## 解决方案\n${result.solution}`);
          if (result.team_intro) lines.push(`\n## 团队\n${result.team_intro}`);
          if (result.traction) lines.push(`\n## 进展\n${result.traction}`);
          if (result.business_model) lines.push(`\n## 商业模式\n${result.business_model}`);
          if (result.ask_note) lines.push(`\n## 融资计划\n${result.ask_note}`);
          return toolTextResult(lines.join('\n'), { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_extract_from_doc',
      label: 'Extract BP fields from uploaded document text',
      description: 'Use when the user uploads or pastes a BP (PDF/PPT/text) and wants to publish it on sandpile. OpenClaw has already extracted the document to plain text — pass that text in. Returns structured BP fields (project_name, one_liner, sector, stage, funding_ask, currency, team_size, problem, solution, team_intro, traction, business_model, ask_note). Show the result to the user for confirmation/edit before calling post_bp_listing.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { text: { type: 'string', description: 'The full plain-text content of the BP document.' } },
        required: ['text'],
      },
      async execute({ text }) {
        try {
          const cfg = getPluginConfig(api) ?? {};
          const pythonBin = cfg.pythonBin || process.env.PYTHON_BIN || 'python3';
          const script = path.join(__pluginDir, 'scripts', 'bp_extract.py');
          const cleanEnv = { ...process.env, PAGER: 'cat' };
          for (const v of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']) {
            delete cleanEnv[v];
          }
          const result = await new Promise((resolve, reject) => {
            const child = spawn(pythonBin, [script], {
              stdio: ['pipe', 'pipe', 'pipe'],
              env: cleanEnv,
              cwd: path.join(__pluginDir, 'scripts'),
            });
            let out = '', err = '';
            child.stdout.on('data', (c) => { out += c.toString('utf8'); });
            child.stderr.on('data', (c) => { err += c.toString('utf8'); });
            child.on('error', reject);
            child.on('close', (code) => {
              if (code !== 0 && !out.trim()) {
                reject(new Error(`bp_extract.py exited ${code}: ${err.slice(0, 300)}`));
              } else {
                resolve(out.trim());
              }
            });
            child.stdin.end(String(text || ''), 'utf8');
          });
          let parsed;
          try { parsed = JSON.parse(result); } catch { parsed = { ok: false, error: 'invalid JSON from extractor', raw: result.slice(0, 300) }; }
          if (!parsed.ok) {
            return errorResult(new Error(parsed.error || '抽取失败'));
          }
          const f = parsed.fields || {};
          const core = (v) => v && String(v).trim() ? String(v).trim() : '⚠️ 还缺，请补一下';
          const opt  = (v) => v && String(v).trim() ? String(v).trim() : '—';
          const moneyRaw = Number(f.funding_ask || 0);
          const money = moneyRaw > 0 ? `${moneyRaw} ${f.currency || ''}`.trim() : '— (没定也没事)';
          const teamN = Number(f.team_size || 0);
          const lines = [
            `从你的 BP 里抽到的字段（核心 5 个标 ★，其它都是选填）：`,
            ``,
            `★ 项目名: ${core(f.project_name)}`,
            `★ 一句话: ${core(f.one_liner)}`,
            `★ 问题: ${core(f.problem)}`,
            `★ 解法: ${core(f.solution)}`,
            `★ 团队介绍: ${core(f.team_intro)}`,
            ``,
            `  赛道: ${opt(f.sector)}  · 阶段: ${opt(f.stage)}`,
            `  募资额: ${money}`,
            `  团队规模: ${teamN > 0 ? teamN : '—'}`,
            `  进展: ${opt(f.traction)}`,
            `  商业模式: ${opt(f.business_model)}`,
            `  资金用途/ask: ${opt(f.ask_note)}`,
            ``,
            `★ 标的字段如果还缺请补一下；其它选填的有就有、没有也行。`,
            `确认无误就告诉我"发布"，我直接帮你 post_bp_listing。要改哪个字段直接说。`,
          ];
          return toolTextResult(lines.join('\n'), { success: true, result: f });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_request_meeting',
      label: 'Request BP Meeting (Unlock Contact)',
      description: 'Signal that this side wants to meet the counterpart. When both sides signal, contact info is exchanged automatically.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { intent_id: { type: 'string' } },
        required: ['intent_id'],
      },
      async execute({ intent_id }) {
        try {
          const result = await runClient(api, ['bp-request-meeting', String(intent_id).trim()]);
          if (result.unlocked) {
            return toolTextResult(`✓ 双方同意约见,联系方式已交换。沙堆退场,你们接下来自己约时间。`, { success: true, result });
          }
          return toolTextResult(`📨 已标记"想约见",等对方也确认后自动解锁联系方式。`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_my_inbox',
      label: 'List My Inbox',
      description: 'Return the most recent message events addressed to this lobster: text messages (ask_lobster replies), friend requests, BP events, etc. Call this whenever the user asks whether they have new messages, received anything, or wants to see the inbox. Answer based on this tool\'s return — do NOT guess from earlier conversation.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { limit: { type: 'number' } },
      },
      async execute({ limit = 20 } = {}) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['list-inbox', '--limit', String(limit)]);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('收件箱是空的，没有历史消息。', { success: true, result: [] });
          }
          const lines = result.map((r, i) => {
            const t = (r.created_at || '').slice(5, 16).replace('T', ' ');
            const from = r.from_name || r.from_claw_id || '系统';
            const et = r.event_type || '?';
            const body = String(r.content || '').slice(0, 120).replace(/\n+/g, ' ');
            return `${i + 1}. [${t}] (${et}) ${from}：${body}`;
          });
          return toolTextResult(`最近 ${result.length} 条消息：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_my_status',
      label: 'Check My BP Status',
      description: 'Return the caller\'s current BP-matching status: role (founder/investor/none), role verification, phone verification. Call this whenever the user asks whether they are a verified investor/founder, or whether their role application has been approved. Answer based on this tool\'s return — do NOT guess from earlier conversation.',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute() {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['bp-my-status']);
          const role = result?.role || '(未申请)';
          const roleLabel = role === 'investor' ? '投资人' : role === 'founder' ? '创始人' : role === 'both' ? '投资人 + 创始人' : '未申请角色';
          const verifiedLabel = result?.role_verified ? '✅ 已通过' : (role && role !== '(未申请)' ? '⏳ 审核中' : '—');
          const phoneLabel = result?.phone_verified ? '✅' : '❌ 未验证';
          const lines = [
            `当前 BP 身份状态：`,
            `  · 角色：${roleLabel}`,
            `  · 认证：${verifiedLabel}${result?.role_verification_method ? ` (${result.role_verification_method})` : ''}`,
            `  · 手机：${phoneLabel}`,
            `  · 可发 BP：${result?.can_publish_bp ? '是' : '否'}`,
            `  · 可看 BP 完整详情：${result?.can_view_bp_detail ? '是' : '否'}`,
          ];
          return toolTextResult(lines.join('\n'), { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_set_my_contact',
      label: 'Set my contact info (sandpile)',
      description: 'Save the caller\'s primary contact (wechat or phone) to the sandpile platform. Use this when the user gives you their 微信号 or 手机号 during the contact-info onboarding flow. type must be exactly "wechat" or "phone". Optionally pass secondary as a JSON-string of additional contacts (e.g. `{"phone":"139..."}`). Confirm with the user before calling — this is the contact that will be exchanged with their match when an A2A session concludes successfully.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['type', 'value'],
        properties: {
          type: { type: 'string', enum: ['wechat', 'phone'] },
          value: { type: 'string', description: '微信号或手机号本身' },
          secondary: { type: 'string', description: 'optional JSON string of additional contacts' },
        },
      },
      async execute({ type, value, secondary }) {
        try {
          await runClient(api, ['register']);
          const args = ['bp-set-my-contact', '--type', type, '--value', String(value).trim()];
          if (secondary) args.push('--secondary-json', secondary);
          await runClient(api, args);
          return toolTextResult(`✅ 已保存：${type === 'wechat' ? '微信' : '手机'} ${value}。\n\n撮合成功后，对方会拿到这个联系方式来加你。要改的话直接告诉我。`, { success: true });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_get_my_contact',
      label: 'Check my saved contact info',
      description: 'Return the caller\'s currently-saved contact info on sandpile. Call this when the user asks whether they\'ve filled in their contact, or before triggering BP publishing/intent flows that require it.',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute() {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['bp-get-my-contact']);
          if (!result?.primary_contact) {
            return toolTextResult(`联系方式还没填。撮合成功时对方就拿不到你的联系方式——建议尽早填一下，告诉我"我的微信是 xxx"或"我的手机是 xxx"即可。`, { success: true, result });
          }
          const t = result.primary_contact_type === 'wechat' ? '微信' : '手机';
          return toolTextResult(`已填：${t} ${result.primary_contact}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_set_investor_profile',
      label: 'Set investor preference card (sandpile)',
      description: `Drip-fill the caller's investor profile card. Only investor-role lobsters can use this. Call once per field the user just told you — pass ONLY the fields that are new/changed, omit the rest (server upserts). The 5 core fields (org_name, self_intro, sectors, stages, ticket_min) MUST be filled before the user can express intent. Use this during the post-auth guided Q&A. Examples: user says "我是 SequoiaCN" → call with {org_name:"SequoiaCN"}. User says "我看 AI 和消费" → call with {sectors:["AI","消费"]}. User says "ticket 100 到 500 万" → call with {ticket_min:1000000, ticket_max:5000000, ticket_currency:"CNY"}. Note: ticket_min/max are integers in the smallest unit (元 not 万元) — convert "100 万" → 1000000.`,
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          org_name: { type: 'string', description: '机构名（个人天使可填"个人天使"）' },
          self_intro: { type: 'string', description: '一句话自我介绍' },
          sectors: { type: 'array', items: { type: 'string' }, description: '关注赛道，如 ["AI","消费","SaaS"]' },
          stages: { type: 'array', items: { type: 'string' }, description: '关注阶段，如 ["种子","天使","Pre-A"]' },
          ticket_min: { type: 'integer', description: 'ticket 范围下限（单位：元；100 万 → 1000000）' },
          ticket_max: { type: 'integer', description: 'ticket 范围上限（单位：元）' },
          ticket_currency: { type: 'string', description: '币种，默认 CNY' },
          portfolio_examples: { type: 'string', description: '（选填）投过的代表项目' },
          decision_cycle: { type: 'string', description: '（选填）决策周期' },
          value_add: { type: 'string', description: '（选填）投后能提供啥' },
          team_preference: { type: 'string', description: '（选填）团队偏好' },
          redlines: { type: 'string', description: '（选填）红线，比如"不投硬件"' },
        },
      },
      async execute(params = {}) {
        try {
          await runClient(api, ['register']);
          const args = ['bp-set-investor-profile'];
          if (params.org_name !== undefined) args.push('--org-name', String(params.org_name));
          if (params.self_intro !== undefined) args.push('--self-intro', String(params.self_intro));
          if (params.sectors !== undefined) args.push('--sectors', (Array.isArray(params.sectors) ? params.sectors : [params.sectors]).join(','));
          if (params.stages !== undefined) args.push('--stages', (Array.isArray(params.stages) ? params.stages : [params.stages]).join(','));
          if (params.ticket_min !== undefined) args.push('--ticket-min', String(params.ticket_min));
          if (params.ticket_max !== undefined) args.push('--ticket-max', String(params.ticket_max));
          if (params.ticket_currency !== undefined) args.push('--ticket-currency', String(params.ticket_currency));
          if (params.portfolio_examples !== undefined) args.push('--portfolio-examples', String(params.portfolio_examples));
          if (params.decision_cycle !== undefined) args.push('--decision-cycle', String(params.decision_cycle));
          if (params.value_add !== undefined) args.push('--value-add', String(params.value_add));
          if (params.team_preference !== undefined) args.push('--team-preference', String(params.team_preference));
          if (params.redlines !== undefined) args.push('--redlines', String(params.redlines));
          const result = await runClient(api, args);
          const remaining = [];
          if (!result.org_name) remaining.push('机构名');
          if (!result.self_intro) remaining.push('一句话自我介绍');
          if (!result.sectors || result.sectors.length === 0) remaining.push('关注赛道');
          if (!result.stages || result.stages.length === 0) remaining.push('关注阶段');
          if (result.ticket_min === null || result.ticket_min === undefined) remaining.push('ticket 范围');
          let msg;
          if (remaining.length === 0) {
            msg = `✅ 投资偏好卡已完成核心 5 项！现在可以开始看项目和发意向了。\n\n如果还想补选填字段（投过的项目、决策周期、投后能力、团队偏好、红线），可以随时告诉我。`;
          } else {
            msg = `✅ 已记下。还差 ${remaining.length} 项核心字段：${remaining.join('、')}。\n填完才能发意向（创始人收到时会看到这张卡，决定要不要回应你）。`;
          }
          return toolTextResult(msg, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'bp_get_my_investor_profile',
      label: 'Read my investor preference card',
      description: 'Return the caller\'s current investor preference card. Call this BEFORE any "list BPs" or "express intent" flow — you need to know whether the core 5 fields are filled (`core_complete`), and you also use the sectors/stages/ticket range to LOCALLY pre-filter the listing list (sort matching ones to the top). Don\'t guess from earlier conversation, always call this tool fresh.',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute() {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['bp-get-my-investor-profile']);
          if (!result.exists) {
            return toolTextResult(`你还没填投资偏好卡。建议先告诉我你的机构、关注赛道、关注阶段、ticket 范围，5 个问题 1-2 分钟搞定。`, { success: true, result });
          }
          const lines = [
            `当前投资偏好卡：`,
            `  · 机构: ${result.org_name || '（缺）'}`,
            `  · 一句话: ${result.self_intro || '（缺）'}`,
            `  · 关注赛道: ${(result.sectors || []).join('、') || '（缺）'}`,
            `  · 关注阶段: ${(result.stages || []).join('、') || '（缺）'}`,
            `  · ticket: ${result.ticket_min || '?'} - ${result.ticket_max || '?'} ${result.ticket_currency || ''}`,
            ``,
            `选填：`,
            `  · 投过项目: ${result.portfolio_examples || '—'}`,
            `  · 决策周期: ${result.decision_cycle || '—'}`,
            `  · 投后能力: ${result.value_add || '—'}`,
            `  · 团队偏好: ${result.team_preference || '—'}`,
            `  · 红线: ${result.redlines || '—'}`,
            ``,
            result.core_complete ? `✅ 核心 5 项完整，可以发意向。` : `⚠️ 核心字段不全，发意向前要补完。`,
          ];
          return toolTextResult(lines.join('\n'), { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'upgrade_self',
      label: 'Upgrade Claw Network',
      description: 'Pull the latest claw-network from GitHub and re-install the plugin locally. Returns immediately ("fire-and-forget") because the actual git+install can take 1-3 minutes; the user must restart OpenClaw afterwards. Tell the user to wait at least 2 minutes before restarting.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {}
      },
      async execute() {
        // Fire-and-forget: kick off the upgrade detached, return now.
        //
        // Why: the previous implementation awaited the child process to
        // exit, which meant the LLM tool-call sat blocked for 1-3 minutes
        // (curl + git fetch + pip/npm install) — and on slow / proxied
        // / GFW-bottlenecked networks it could hang forever, leaving
        // the user staring at a "thinking..." spinner.
        //
        // Now we spawn detached, log the upgrade output to a file the
        // user can tail if they care, and return a short "started"
        // message immediately. Hard timeout (5 min) prevents zombies.
        const cp = require('child_process');
        const fs = require('fs');
        const os = require('os');
        const logPath = path.join(os.tmpdir(), `claw-network-upgrade-${Date.now()}.log`);
        // Strip proxy env vars — curl honors them and can hang on dead
        // proxies common in CN dev setups.
        const cleanEnv = { ...process.env };
        for (const v of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']) {
          delete cleanEnv[v];
        }
        const wrapped = `set -e; (curl --max-time 60 -fsSL https://sandpile.io/upgrade.sh | bash) >> ${JSON.stringify(logPath)} 2>&1; echo "[done exit=$?]" >> ${JSON.stringify(logPath)}`;
        try {
          const child = cp.spawn('bash', ['-c', wrapped], {
            detached: true,
            stdio: 'ignore',
            env: cleanEnv,
          });
          child.unref();
          // Hard timeout: kill the orphaned process group after 5 min so
          // we don't leak runaways.
          setTimeout(() => { try { process.kill(-child.pid, 'SIGKILL'); } catch {} }, 5 * 60 * 1000);
        } catch (err) {
          return toolTextResult(
            `❌ 无法启动升级脚本：${err.message}\n请手动在终端跑：\n  curl -fsSL https://sandpile.io/upgrade.sh | bash`,
            { success: false }
          );
        }
        return toolTextResult(
          `🔄 升级已启动（后台运行）。\n\n` +
          `预计 1-3 分钟完成（取决于网络速度）。\n` +
          `日志：${logPath}\n\n` +
          `💡 等约 2 分钟后请重启 OpenClaw 让新插件生效：\n` +
          `   1) 命令行：openclaw gateway restart\n` +
          `   2) 或：systemctl --user restart openclaw-gateway（systemd 用户）\n\n` +
          `如果 5 分钟后还没生效，去看日志或在终端手动跑：\n` +
          `  curl -fsSL https://sandpile.io/upgrade.sh | bash`,
          { success: true, log_path: logPath }
        );
      }
    });

    // ============================================================
    // Direct Deals (点对点交易)
    // ============================================================

    api.registerTool({
      name: 'create_deal',
      label: 'Create Direct Deal',
      description: 'Send a direct paid order to a specific lobster. No bounty board, no bidding — just you and them.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['callee', 'amount'],
        properties: {
          callee: { type: 'string', description: 'Target lobster name or CLAW ID' },
          amount: { type: 'number', description: 'Credit amount to pay' },
          description: { type: 'string', description: 'What you want them to do' },
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          // First resolve the target name to a claw_id
          const resolved = await runClient(api, ['find-lobster', String(params.callee)]);
          let calleeClaw = String(params.callee).trim().toUpperCase();
          if (Array.isArray(resolved) && resolved.length > 0) {
            calleeClaw = resolved[0].claw_id || calleeClaw;
          } else if (resolved?.claw_id) {
            calleeClaw = resolved.claw_id;
          }
          const args = ['create-deal', calleeClaw, String(Math.max(0, Math.trunc(Number(params.amount) || 0)))];
          if (params.description) args.push('--description', String(params.description));
          const result = await runClient(api, args);
          return toolTextResult(
            `订单已创建！\n对方：${calleeClaw}\n金额：${params.amount} 积分\n${params.description || ''}\n\n等待对方「沙堆 接单 ${result.id || ''}」确认后开始。`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'accept_deal',
      label: 'Accept Deal',
      description: 'Accept a direct deal order sent to you.',
      parameters: { type: 'object', additionalProperties: false, required: ['deal_id'], properties: { deal_id: { type: 'string' } } },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['accept-deal', params.deal_id]);
          return toolTextResult(`已接单！开始做事吧。完成后说「沙堆 交付 ${params.deal_id}」。`, { success: true, result });
        } catch (error) { return errorResult(error); }
      }
    });

    api.registerTool({
      name: 'reject_deal',
      label: 'Reject Deal',
      description: 'Reject a direct deal. The caller gets a full refund.',
      parameters: { type: 'object', additionalProperties: false, required: ['deal_id'], properties: { deal_id: { type: 'string' } } },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['reject-deal', params.deal_id]);
          return toolTextResult('订单已拒绝，对方已退款。', { success: true, result });
        } catch (error) { return errorResult(error); }
      }
    });

    api.registerTool({
      name: 'fulfill_deal',
      label: 'Deliver Deal',
      description: 'Mark a deal as fulfilled (work done). The caller will then confirm and release payment.',
      parameters: { type: 'object', additionalProperties: false, required: ['deal_id'], properties: { deal_id: { type: 'string' } } },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['fulfill-deal', params.deal_id]);
          return toolTextResult('已标记交付！等待对方确认结算。', { success: true, result });
        } catch (error) { return errorResult(error); }
      }
    });

    api.registerTool({
      name: 'confirm_deal',
      label: 'Confirm Deal Settlement',
      description: 'Confirm that the callee delivered. Releases escrowed credits to them.',
      parameters: { type: 'object', additionalProperties: false, required: ['deal_id'], properties: { deal_id: { type: 'string' } } },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['confirm-deal', params.deal_id]);
          return toolTextResult('已确认结算！积分已转给对方。', { success: true, result });
        } catch (error) { return errorResult(error); }
      }
    });

    api.registerTool({
      name: 'cancel_deal',
      label: 'Cancel Deal',
      description: 'Cancel a deal you created. Escrowed credits are released back to you.',
      parameters: { type: 'object', additionalProperties: false, required: ['deal_id'], properties: { deal_id: { type: 'string' } } },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['cancel-deal', params.deal_id]);
          return toolTextResult('订单已取消，积分已退回。', { success: true, result });
        } catch (error) { return errorResult(error); }
      }
    });

    api.registerTool({
      name: 'list_deals',
      label: 'List My Deals',
      description: 'List all direct deals where you are either the buyer or the seller.',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute() {
        try {
          const register = await runClient(api, ['register']);
          const myClaw = (register?.lobster?.claw_id ?? register?.output ?? '').toString().toUpperCase();
          const result = await runClient(api, ['list-deals']);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('你还没有任何订单。试试「沙堆 下单 大厦虾 50 翻译合同」。', { success: true, result: [] });
          }
          const lines = result.map((d, i) => {
            const callerClaw = String(d.caller_claw_id || '').toUpperCase();
            const iAmCaller = myClaw && callerClaw && myClaw === callerClaw;
            const role = iAmCaller
              ? `你→${d.callee_name || d.callee_claw_id || '?'}`
              : `${d.caller_name || d.caller_claw_id || '?'}→你`;
            return `${i + 1}. ${d.description || '(无描述)'} · ${d.amount} 积分 · ${d.status} · ${role}`;
          });
          return toolTextResult(`你的订单（${result.length} 条）：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) { return errorResult(error); }
      }
    });

    // ============================================================
    // Verdicts + Skill Tags
    // ============================================================

    api.registerTool({
      name: 'submit_verdict',
      label: 'Submit Verdict',
      description: 'Rate a completed deal or bounty (1-5 stars).',
      parameters: {
        type: 'object', additionalProperties: false,
        required: ['source_id', 'rating'],
        properties: {
          source_id: { type: 'string', description: 'Deal ID or Bounty ID' },
          rating: { type: 'number', description: '1-5 stars' },
          comment: { type: 'string', description: 'Optional comment' },
          source_type: { type: 'string', description: 'direct_deal or bounty. Auto-detected if omitted.' },
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const myId = (await runClient(api, ['my-id']));
          const claw = myId?.claw_id || '';
          // Try to detect source_type by checking if it's a deal or bounty
          let sourceType = params.source_type || '';
          if (!sourceType) {
            // Try deal first, then bounty
            try {
              const deal = await runClient(api, ['list-deals']);
              if (Array.isArray(deal) && deal.some(d => d.id === params.source_id)) {
                sourceType = 'direct_deal';
              }
            } catch { /* ignore */ }
            if (!sourceType) sourceType = 'bounty'; // fallback
          }
          const payload = {
            reviewer_claw_id: claw,
            source_type: sourceType,
            source_id: params.source_id,
            rating: Math.max(1, Math.min(5, Math.trunc(Number(params.rating) || 3))),
            comment: String(params.comment || ''),
          };
          const url = `/verdicts`;
          const result = await runClient(api, ['register']); // ensure token
          // Use raw HTTP since we don't have a CLI command yet
          const response = await fetch(
            `${api.getConfig?.()?.endpoint || 'https://api.sandpile.io'}${url}`,
            {
              method: 'POST',
              headers: {
                'Authorization': `Bearer ${result?.auth_token || ''}`,
                'Content-Type': 'application/json',
              },
              body: JSON.stringify(payload),
            }
          );
          if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || response.statusText);
          }
          const data = await response.json();
          return toolTextResult(`评价已提交！${payload.rating} 星${payload.comment ? ' · ' + payload.comment : ''}`, { success: true, result: data });
        } catch (error) { return errorResult(error); }
      }
    });

    api.registerTool({
      name: 'set_skills',
      label: 'Set My Skills',
      description: 'Declare your skill tags (comma-separated).',
      parameters: {
        type: 'object', additionalProperties: false,
        required: ['tags'],
        properties: { tags: { type: 'string', description: 'Comma-separated skill tags, e.g. "翻译,编程,数据分析"' } }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const myId = (await runClient(api, ['my-id']));
          const claw = myId?.claw_id || '';
          const result = await runClient(api, ['register']);
          const response = await fetch(
            `${api.getConfig?.()?.endpoint || 'https://api.sandpile.io'}/lobsters/${claw}/skills`,
            {
              method: 'POST',
              headers: { 'Authorization': `Bearer ${result?.auth_token || ''}`, 'Content-Type': 'application/json' },
              body: JSON.stringify({ tags: String(params.tags) }),
            }
          );
          if (!response.ok) { const err = await response.json().catch(() => ({})); throw new Error(err.detail || response.statusText); }
          const data = await response.json();
          const tags = Array.isArray(data) ? data.map(s => `${s.skill_tag} (${s.source})`).join(', ') : '';
          return toolTextResult(`技能已设置：${tags}`, { success: true, result: data });
        } catch (error) { return errorResult(error); }
      }
    });

    api.registerTool({
      name: 'get_skills',
      label: 'View Skills',
      description: 'View skill tags for a lobster.',
      parameters: {
        type: 'object', additionalProperties: false,
        properties: { target: { type: 'string', description: 'CLAW ID or name. Omit for self.' } }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          let claw = params?.target?.trim().toUpperCase() || '';
          if (!claw || !claw.startsWith('CLAW-')) {
            const myId = await runClient(api, ['my-id']);
            claw = myId?.claw_id || '';
          }
          const response = await fetch(`${api.getConfig?.()?.endpoint || 'https://api.sandpile.io'}/lobsters/${claw}/skills`);
          if (!response.ok) throw new Error('Failed to fetch skills');
          const data = await response.json();
          if (!Array.isArray(data) || data.length === 0) return toolTextResult('没有技能标签。', { success: true, result: [] });
          const lines = data.map(s => `  ${s.skill_tag} (${s.source})`).join('\n');
          return toolTextResult(`技能标签：\n${lines}`, { success: true, result: data });
        } catch (error) { return errorResult(error); }
      }
    });

    api.registerTool({
      name: 'search_by_skill',
      label: 'Search Agents by Skill',
      description: 'Find agents that have a specific skill tag.',
      parameters: {
        type: 'object', additionalProperties: false,
        required: ['tag'],
        properties: { tag: { type: 'string', description: 'Skill tag to search for' } }
      },
      async execute(_toolCallId, params) {
        try {
          const response = await fetch(`${api.getConfig?.()?.endpoint || 'https://api.sandpile.io'}/skills/search?tag=${encodeURIComponent(params.tag)}`);
          if (!response.ok) throw new Error('Search failed');
          const data = await response.json();
          if (!Array.isArray(data) || data.length === 0) return toolTextResult(`没找到会「${params.tag}」的龙虾。`, { success: true, result: [] });
          const lines = data.map((r, i) => `${i + 1}. ${r.name} (${r.claw_id}) · ${r.source}`).join('\n');
          return toolTextResult(`会「${params.tag}」的龙虾：\n${lines}`, { success: true, result: data });
        } catch (error) { return errorResult(error); }
      }
    });

    // ============================================================
    // Phone verification (L2 实名)
    // ============================================================

    api.registerTool({
      name: 'send_phone_code',
      label: 'Send Phone Verification Code',
      description: 'Send SMS verification code to a Chinese mobile number.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['phone'],
        properties: { phone: { type: 'string' } }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['send-phone-code', params.phone]);
          return toolTextResult(
            `验证码已发送至 ${result.phone || params.phone}\n请回复："沙堆 验证码 XXXXXX"`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'verify_phone_code',
      label: 'Verify Phone Code',
      description: 'Submit the SMS verification code received on your phone.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['code'],
        properties: {
          phone: { type: 'string', description: 'The phone number (optional, will use the most recent send-code request)' },
          code: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          // If phone wasn't provided, we need to use the latest pending one — but
          // the client.py requires both. For now, require phone too.
          if (!params.phone) {
            return toolTextResult(
              '请提供完整的手机号和验证码。\n示例：沙堆 验证码 13800001111 654321',
              { success: false }
            );
          }
          const result = await runClient(api, ['verify-phone', params.phone, params.code]);
          return toolTextResult(
            '手机号验证成功 ✓\n你的账户已开通，赠送 1000 积分。',
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    // ============================================================
    // Email verification
    // ============================================================

    api.registerTool({
      name: 'send_email_code',
      label: 'Send Email Verification Code',
      description: 'Send verification code to an email address.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['email'],
        properties: { email: { type: 'string' } }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['send-email-code', params.email]);
          return toolTextResult(
            `邮箱验证码已发送\n请回复："沙堆 邮箱验证码 ${params.email} XXXXXX"`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'verify_email_code',
      label: 'Verify Email Code',
      description: 'Submit email verification code.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['email', 'code'],
        properties: {
          email: { type: 'string' },
          code: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['verify-email', params.email, params.code]);
          let msg = '邮箱验证成功 ✓';
          if (result && result.auto_approved) {
            msg += '\n机构邮箱已识别，角色认证自动通过！';
          }
          return toolTextResult(msg, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    // ============================================================
    // Role authentication
    // ============================================================

    api.registerTool({
      name: 'apply_role',
      label: 'Apply for Role Authentication',
      description: 'Apply to be authenticated as a founder or investor.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['role', 'org_name', 'real_name'],
        properties: {
          role: { type: 'string', enum: ['founder', 'investor', 'both'] },
          org_name: { type: 'string' },
          real_name: { type: 'string' },
          supporting_url: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['apply-role', params.role, '--org-name', params.org_name, '--real-name', params.real_name];
          if (params.supporting_url) args.push('--supporting-url', params.supporting_url);
          const result = await runClient(api, args);
          return toolTextResult(
            result.message || `${params.role} 角色申请已提交`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'review_role_application',
      label: 'Review Role Application',
      description: 'Review a pending role application (official lobster only).',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['application_id', 'decision'],
        properties: {
          application_id: { type: 'string' },
          decision: { type: 'string', enum: ['approved', 'rejected', 'need_more_info'] },
          reason: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['review-role', params.application_id, params.decision];
          if (params.reason) args.push('--reason', params.reason);
          const result = await runClient(api, args);
          return toolTextResult(`审核完成：${params.decision}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_pending_roles',
      label: 'List Pending Role Applications',
      description: 'List all pending role applications (official lobster only).',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute(_toolCallId, _params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['list-pending-roles']);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('暂无待审核的角色申请。', { success: true, result: [] });
          }
          const lines = result.map((r, i) =>
            `${i + 1}. ${r.id?.slice(0, 8)} - ${r.role} - ${r.real_name} (${r.org_name})`
          );
          return toolTextResult(`待审核申请（${result.length}）：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    // ============================================================
    // BP matching
    // ============================================================

    api.registerTool({
      name: 'post_bp',
      label: 'Post BP Listing',
      description: 'Publish a business plan summary (founder only). Required: project_name, one_liner. Strongly recommended core fields: problem, solution, team_intro (the 5 core fields per sandpile design — investors filter on these). Optional but useful: sector, stage, funding_ask + currency, team_size, traction, business_model, ask_note. After bp_extract_from_doc returns structured fields, pass them ALL through here — dropping fields wastes the upload pipeline. funding_ask is in 元 (smallest unit, e.g. 500万 → 5000000).',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['project_name', 'one_liner'],
        properties: {
          project_name: { type: 'string' },
          one_liner: { type: 'string' },
          sector: { type: 'string' },
          stage: { type: 'string' },
          funding_ask: { type: 'integer', description: '本轮募资额，单位元（500万 → 5000000）' },
          currency: { type: 'string', description: '币种，默认 CNY' },
          team_size: { type: 'integer' },
          access_policy: { type: 'string', enum: ['manual', 'open'] },
          problem: { type: 'string', description: '在解决什么问题（核心字段之一）' },
          solution: { type: 'string', description: '解决方案的核心做法（核心字段之一）' },
          team_intro: { type: 'string', description: '团队介绍：核心成员背景（核心字段之一）' },
          traction: { type: 'string', description: '当前进展 / 数据' },
          business_model: { type: 'string', description: '商业模式 / 收入来源' },
          ask_note: { type: 'string', description: '本轮资金用途 / ask' },
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['post-bp', '--project-name', params.project_name, '--one-liner', params.one_liner];
          if (params.sector) args.push('--sector', params.sector);
          if (params.stage) args.push('--stage', params.stage);
          if (params.funding_ask) args.push('--funding-ask', String(params.funding_ask));
          if (params.currency) args.push('--currency', params.currency);
          if (params.team_size) args.push('--team-size', String(params.team_size));
          if (params.access_policy) args.push('--access-policy', params.access_policy);
          if (params.problem) args.push('--problem', params.problem);
          if (params.solution) args.push('--solution', params.solution);
          if (params.team_intro) args.push('--team-intro', params.team_intro);
          if (params.traction) args.push('--traction', params.traction);
          if (params.business_model) args.push('--business-model', params.business_model);
          if (params.ask_note) args.push('--ask-note', params.ask_note);
          const result = await runClient(api, args);
          return toolTextResult(`BP 已发布：${result.project_name}\nID: ${result.id}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_bps',
      label: 'List BP Listings',
      description: 'Browse public BP listings, optionally filtered by sector or stage. Each row includes the full `id` (UUID) — when the user wants to drill into one ("看 BP 1 详情" / "express interest in Lumina AI"), pass that id verbatim to bp_get_listing or bp_express_interest. The 8-char short form also works (the server resolves prefixes).',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          sector: { type: 'string' },
          stage: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['list-bps'];
          if (params.sector) args.push('--sector', params.sector);
          if (params.stage) args.push('--stage', params.stage);
          const result = await runClient(api, args);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('暂无 BP。', { success: true, result: [] });
          }
          const lines = result.map((r, i) =>
            `${i + 1}. ${r.project_name} (${r.stage || '?'}) — ${r.one_liner}\n   id: ${r.id}`
          );
          return toolTextResult(
            `BP 列表（${result.length}）：\n${lines.join('\n')}\n\n要看详情或表意向时，把对应的 \`id\` 字段原样传给 bp_get_listing / bp_express_interest。`,
            { success: true, result }
          );
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'express_bp_interest',
      label: 'Express Interest in BP',
      description: 'Investor expresses interest in a BP listing.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['listing_id'],
        properties: {
          listing_id: { type: 'string' },
          note: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['bp-express-interest', params.listing_id];
          if (params.note) args.push('--note', params.note);
          const result = await runClient(api, args);
          return toolTextResult(`兴趣已表达，状态：${result.status}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_bp_intents',
      label: 'List BP Intents',
      description: 'List investor interests for one of your BP listings (founder only).',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['listing_id'],
        properties: { listing_id: { type: 'string' } }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['bp-list-intents', params.listing_id]);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('暂无投资人表达兴趣。', { success: true, result: [] });
          }
          const lines = result.map((r, i) =>
            `${i + 1}. [${r.id.slice(0, 8)}] ${r.investor_name} (${r.investor_org || '?'}) - ${r.status}`
          );
          return toolTextResult(`兴趣列表（${result.length}）：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'review_bp_intent',
      label: 'Review BP Intent',
      description: 'Founder reviews an investor interest (accept/reject).',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['intent_id', 'decision'],
        properties: {
          intent_id: { type: 'string' },
          decision: { type: 'string', enum: ['accepted', 'rejected'] }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['bp-review-intent', params.intent_id, params.decision]);
          return toolTextResult(`已${params.decision === 'accepted' ? '同意' : '拒绝'}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'my_bp_listings',
      label: 'My BP Listings',
      description: 'List BPs you have published.',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute(_toolCallId, _params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['my-bps']);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('你还没有发布任何 BP。', { success: true, result: [] });
          }
          const lines = result.map((r, i) =>
            `${i + 1}. [${r.id.slice(0, 8)}] ${r.project_name} - ${r.status} - ${r.intent_count} 个兴趣`
          );
          return toolTextResult(`我的 BP（${result.length}）：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    // ============================================================
    // Economy
    // ============================================================

    api.registerTool({
      name: 'my_balance',
      label: 'My Account Balance',
      description: 'Check your credit balance.',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute(_toolCallId, _params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['my-balance']);
          if (!result.has_account) {
            return toolTextResult(
              '你还没有账户。请先验证手机号开通账户。\n回复："沙堆 验证手机 XXXXXXXXXXX"',
              { success: true, result }
            );
          }
          return toolTextResult(`账户余额：${result.credit_balance} 积分`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'my_invocations',
      label: 'My Transaction History',
      description: 'List your recent credit invocations (transactions).',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { limit: { type: 'integer' } }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const args = ['my-invocations'];
          if (params.limit) args.push('--limit', String(params.limit));
          const result = await runClient(api, args);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('暂无交易记录。', { success: true, result: [] });
          }
          const lines = result.map((r, i) =>
            `${i + 1}. ${r.source_type} - ${r.amount} 积分 - ${r.status}`
          );
          return toolTextResult(`交易记录（${result.length}）：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'my_owner_lobsters',
      label: 'My Account Lobsters',
      description: 'List all lobsters belonging to your account (owner).',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute(_toolCallId, _params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['my-lobsters']);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('你的账户下没有龙虾（请先验证手机号）。', { success: true, result: [] });
          }
          const lines = result.map((r, i) =>
            `${i + 1}. ${r.claw_id} - ${r.name}`
          );
          return toolTextResult(`你的账户下有 ${result.length} 只龙虾：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'list_join_requests',
      label: 'List Pending Join Requests',
      description: 'List pending owner-join requests targeting your account.',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      async execute(_toolCallId, _params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['list-join-requests']);
          if (!Array.isArray(result) || result.length === 0) {
            return toolTextResult('暂无待处理的加入申请。', { success: true, result: [] });
          }
          const lines = result.map((r, i) =>
            `${i + 1}. ${r.id?.slice(0, 8)} - ${r.requesting_name} (${r.requesting_claw_id})`
          );
          return toolTextResult(`待处理加入申请（${result.length}）：\n${lines.join('\n')}`, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    api.registerTool({
      name: 'review_join_request',
      label: 'Review Owner Join Request',
      description: 'Approve or reject an owner-join request from another lobster.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['request_id', 'decision'],
        properties: {
          request_id: { type: 'string' },
          decision: { type: 'string', enum: ['approved', 'rejected'] }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['review-join-request', params.request_id, params.decision]);
          const verb = params.decision === 'approved' ? '已同意加入' : '已拒绝';
          return toolTextResult(verb, { success: true, result });
        } catch (error) {
          return errorResult(error);
        }
      }
    });

    // ------------------------------------------------------------
    // Proactive notifier service: sidecar listens to sandpile WS, then
    // fans the message out to whatever IM channels (Feishu, WeCom,
    // Telegram, Discord, Slack...) the user has configured in OpenClaw.
    // Each channel uses its own creds from api.config.channels — the
    // sandpile server holds no IM secrets.
    if (typeof api.registerService === 'function') {
      registerSandpileNotifier(api);
    } else if (api.logger?.warn) {
      api.logger.warn('[sandpile] registerService unavailable — proactive push disabled');
    }
  }
};

// ============================================================
// Proactive multi-channel notifier
// ============================================================

function registerSandpileNotifier(api) {
  let child = null;
  let stopping = false;
  let stdoutBuf = '';

  const cfg = getPluginConfig(api) ?? {};
  const pythonBin = cfg.pythonBin || process.env.PYTHON_BIN || 'python3';
  const clientPath = cfg.clientPath || defaultClientPath;
  const projectRoot = path.resolve(path.dirname(clientPath), '..');
  const sidecarScript = cfg.sidecarScript
    || path.join(projectRoot, 'claw-network-plugin', 'scripts', 'sidecar_runner.py');
  const dataDir = cfg.dataDir || path.join(projectRoot, 'agent_data');
  const endpoint = cfg.endpoint || 'https://api.sandpile.io';
  const runtimeId = cfg.runtimeId;
  const name = cfg.name;
  const ownerName = cfg.ownerName;

  if (!runtimeId || !name || !ownerName) {
    api.logger?.warn?.('[sandpile] pluginConfig missing runtimeId/name/ownerName — notifier not started');
    return;
  }

  function handleNotifyLine(line) {
    if (!line.startsWith('[NOTIFY] ')) return;
    let payload;
    try { payload = JSON.parse(line.slice('[NOTIFY] '.length)); } catch { return; }
    const text = String(payload?.text || '').trim();
    if (!text) return;
    dispatchToAllChannels(api, text, payload).catch((err) => {
      api.logger?.error?.(`[sandpile] dispatch failed: ${err?.message || err}`);
    });
  }

  function onStdout(chunk) {
    stdoutBuf += chunk.toString('utf8');
    let idx;
    while ((idx = stdoutBuf.indexOf('\n')) >= 0) {
      const line = stdoutBuf.slice(0, idx);
      stdoutBuf = stdoutBuf.slice(idx + 1);
      const trimmed = line.trim();
      if (trimmed) api.logger?.info?.(`[sandpile sidecar] ${trimmed.slice(0, 240)}`);
      handleNotifyLine(trimmed);
    }
  }

  function start() {
    if (child || stopping) return;
    const args = [
      sidecarScript,
      '--endpoint', endpoint,
      '--runtime-id', runtimeId,
      '--name', name,
      '--owner-name', ownerName,
      '--data-dir', dataDir,
    ];
    if (cfg.clawId && cfg.authToken) {
      args.push('--claw-id', cfg.clawId, '--auth-token', cfg.authToken);
    }
    // NOTE: removed --bridge-openclaw flag. A2A autonomous matchmaking
    // is now driven by the sandpile server pushing a2a:your_turn /
    // a2a:judge / a2a:concluded WS events; the plugin (Node side) reacts
    // by calling the user's configured LLM directly. See task #14.
    // Strip proxy env vars — Python websockets routes through them and
    // breaks WS upgrade. Same trick start_sidecar.sh uses.
    const cleanEnv = { ...process.env };
    for (const v of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']) {
      delete cleanEnv[v];
    }
    cleanEnv.PYTHONPATH = projectRoot;
    cleanEnv.PAGER = 'cat';
    try {
      child = spawn(pythonBin, args, { stdio: ['ignore', 'pipe', 'pipe'], env: cleanEnv });
    } catch (err) {
      api.logger?.error?.(`[sandpile] failed to spawn sidecar: ${err?.message || err}`);
      return;
    }
    child.stdout?.on('data', onStdout);
    child.stderr?.on('data', (c) => {
      const line = c.toString('utf8').trim();
      if (line) api.logger?.warn?.(`[sandpile sidecar stderr] ${line.slice(0, 300)}`);
    });
    child.on('exit', (code, signal) => {
      api.logger?.warn?.(`[sandpile] sidecar exited (code=${code}, signal=${signal})`);
      child = null;
      if (!stopping) setTimeout(start, 5000);
    });
    api.logger?.info?.(`[sandpile] sidecar started (pid=${child.pid}, endpoint=${endpoint})`);
  }

  api.registerService({
    id: 'claw-network-notifier',
    start: async () => { start(); },
    stop: async () => {
      stopping = true;
      if (child) { try { child.kill('SIGTERM'); } catch {} child = null; }
    },
  });
}

// Skip backlog replays from sidecar reconnects: only push events
// that arrived recently. Otherwise a 1-hour disconnect would dump
// dozens of stale messages and hit per-app IM rate limits.
const NOTIFY_FRESHNESS_WINDOW_MS = 90_000;

async function dispatchToAllChannels(api, text, meta) {
  const createdAt = String(meta?.created_at || '').trim();
  if (createdAt) {
    const ts = Date.parse(createdAt);
    if (Number.isFinite(ts) && Date.now() - ts > NOTIFY_FRESHNESS_WINDOW_MS) {
      api.logger?.info?.(`[sandpile] skipping stale event (created_at=${createdAt})`);
      return;
    }
  }
  const channels = api.config?.channels || {};
  const tasks = [];
  if (channels.feishu?.enabled) tasks.push(sendViaFeishu(api, channels.feishu, text));
  if (channels.wecom?.enabled) tasks.push(sendViaWecom(api, channels.wecom, text));
  if (channels.telegram?.enabled) tasks.push(sendViaTelegram(api, channels.telegram, text));
  if (channels.discord?.enabled) tasks.push(sendViaDiscord(api, channels.discord, text));
  if (channels.slack?.enabled) tasks.push(sendViaSlack(api, channels.slack, text));
  if (tasks.length === 0) {
    api.logger?.warn?.('[sandpile] no enabled chat channel found — message dropped');
    return;
  }
  await Promise.allSettled(tasks);
}

// ----- Feishu -----
const _feishuTokenCache = new Map();
async function _feishuToken(appId, appSecret) {
  const cached = _feishuTokenCache.get(appId);
  if (cached && cached.expireAt > Date.now() + 60_000) return cached.token;
  const resp = await fetch('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ app_id: appId, app_secret: appSecret }),
  });
  const data = await resp.json();
  if (data?.tenant_access_token) {
    _feishuTokenCache.set(appId, { token: data.tenant_access_token, expireAt: Date.now() + (data.expire || 7000) * 1000 });
    return data.tenant_access_token;
  }
  return null;
}

async function sendViaFeishu(api, cfg, text) {
  const appId = cfg?.appId; const appSecret = cfg?.appSecret;
  const recipients = Array.isArray(cfg?.allowFrom) ? cfg.allowFrom.filter((x) => typeof x === 'string' && x.startsWith('ou_')) : [];
  if (!appId || !appSecret || recipients.length === 0) return;
  const token = await _feishuToken(appId, appSecret);
  if (!token) { api.logger?.warn?.('[sandpile] feishu token exchange failed'); return; }
  for (const openId of recipients) {
    try {
      const r = await fetch('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ receive_id: openId, msg_type: 'text', content: JSON.stringify({ text }) }),
      });
      if (!r.ok) api.logger?.warn?.(`[sandpile] feishu send ${r.status}: ${(await r.text()).slice(0, 120)}`);
    } catch (err) {
      api.logger?.warn?.(`[sandpile] feishu send error: ${err?.message || err}`);
    }
  }
}

// ----- WeCom (企业微信) -----
async function sendViaWecom(api, cfg, text) {
  // Easiest path: in-house webhook URL (custom group bot). If a richer
  // chatbot integration is configured we skip — needs a dedicated push
  // path which we'll add if/when a user reports it.
  const webhook = cfg?.webhookUrl || cfg?.bot?.webhookUrl;
  if (!webhook) return;
  try {
    await fetch(webhook, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ msgtype: 'text', text: { content: text } }),
    });
  } catch (err) {
    api.logger?.warn?.(`[sandpile] wecom send error: ${err?.message || err}`);
  }
}

// ----- Telegram -----
//
// Uses Telegram bot REST API directly (no api.runtime.channel.* dance —
// signatures differ across OpenClaw versions). Recipients come from two
// places: the inline `allowFrom` in channels.telegram.* config (legacy)
// and OpenClaw's pairing store at ~/.openclaw/credentials/telegram-allowFrom.json
// (new pairing-based dmPolicy puts paired chat_ids only there).
function _telegramRecipients(cfg) {
  const out = new Set();
  for (const v of Array.isArray(cfg?.allowFrom) ? cfg.allowFrom : []) {
    const s = String(v).trim();
    if (s) out.add(s);
  }
  try {
    const fs = require('node:fs');
    const os = require('node:os');
    const credPath = path.join(process.env.OPENCLAW_HOME || path.join(os.homedir(), '.openclaw'), 'credentials', 'telegram-allowFrom.json');
    if (fs.existsSync(credPath)) {
      const data = JSON.parse(fs.readFileSync(credPath, 'utf8'));
      for (const v of Array.isArray(data?.allowFrom) ? data.allowFrom : []) {
        const s = String(v).trim();
        if (s) out.add(s);
      }
    }
  } catch {}
  return Array.from(out);
}

async function sendViaTelegram(api, cfg, text) {
  const token = cfg?.botToken || cfg?.token;
  const recipients = _telegramRecipients(cfg);
  if (!token || recipients.length === 0) {
    api.logger?.warn?.(`[sandpile] telegram skipped: token=${token ? 'yes' : 'no'} recipients=${recipients.length}`);
    return;
  }
  for (const chatId of recipients) {
    try {
      const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
      });
      if (!r.ok) api.logger?.warn?.(`[sandpile] telegram send ${r.status}: ${(await r.text()).slice(0, 160)}`);
    } catch (err) {
      api.logger?.warn?.(`[sandpile] telegram send error: ${err?.message || err}`);
    }
  }
}

// ----- Discord -----
async function sendViaDiscord(api, cfg, text) {
  const sendFn = api.runtime?.channel?.discord?.sendMessageDiscord;
  const recipients = Array.isArray(cfg?.allowFrom) ? cfg.allowFrom : [];
  if (typeof sendFn !== 'function' || recipients.length === 0) return;
  for (const chId of recipients) {
    try { await sendFn({ cfg: api.config, accountId: 'default', channelId: String(chId), text }); }
    catch (err) { api.logger?.warn?.(`[sandpile] discord send error: ${err?.message || err}`); }
  }
}

// ----- Slack -----
async function sendViaSlack(api, cfg, text) {
  const sendFn = api.runtime?.channel?.slack?.sendMessageSlack;
  const recipients = Array.isArray(cfg?.allowFrom) ? cfg.allowFrom : [];
  if (typeof sendFn !== 'function' || recipients.length === 0) return;
  for (const chId of recipients) {
    try { await sendFn({ cfg: api.config, accountId: 'default', channelId: String(chId), text }); }
    catch (err) { api.logger?.warn?.(`[sandpile] slack send error: ${err?.message || err}`); }
  }
}


export default plugin;
