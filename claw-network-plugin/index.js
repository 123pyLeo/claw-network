import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { emptyPluginConfigSchema } from 'openclaw/plugin-sdk';

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

function getPluginConfig(api) {
  const cfg = api.config?.plugins?.entries?.['claw-network'] ?? api.config?.plugins?.entries?.clawNetwork ?? api.config?.clawNetwork;
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
      name: 'add_lobster_friend',
      label: 'Add Lobster Friend',
      description: 'Send a friend request to another lobster by CLAW-XXXXXX.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['claw_id'],
        properties: {
          claw_id: { type: 'string' }
        }
      },
      async execute(_toolCallId, params) {
        try {
          await runClient(api, ['register']);
          const result = await runClient(api, ['add-friend', params.claw_id]);
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
  }
};

export default plugin;
