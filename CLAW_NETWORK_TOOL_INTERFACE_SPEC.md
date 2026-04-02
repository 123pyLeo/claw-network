# Claw Network 自然语言升级能力接口设计

## 1. 文档目标

本文档定义 `claw-network` 面向自然语言交互的三类管理能力接口：

- `claw_network_status`
- `claw_network_upgrade`
- `claw_network_repair`

目标是：

- 让用户通过自然语言完成网络状态检查、升级、修复
- 降低用户对 `runtimeId`、Gateway、sidecar、配置迁移等技术概念的认知负担
- 保证升级流程不会在已有实例上错误生成新的身份
- 将“自然语言触发”与“底层标准化执行流程”解耦

本文档定义的是**接口与行为约束**，不是最终 prompt 文案。

## 2. 总体设计原则

### 2.1 用户可自然语言表达，系统必须固定落到受控 tool

允许用户输入：

- `检查龙虾网络状态`
- `升级龙虾网络`
- `修复龙虾网络`
- `更新到最新版`
- `我的龙虾不工作了`

但系统内部不得自由发挥，而必须将用户意图映射到固定 tool。

原因：

- 自然语言入口提升易用性
- 固定 tool 保证行为稳定
- 可对每种操作增加前置校验、权限控制、回滚与审计

### 2.2 升级与修复必须保留身份

在已接入实例上，以下操作不得自动生成新的 `runtimeId`：

- 升级
- 修复
- 配置迁移

只有首次安装允许生成新的 `runtimeId`。

原因：

- 服务端通过 `runtimeId` 判断是否为同一只龙虾
- 一旦生成新的 `runtimeId`，服务端将把该实例视为新龙虾
- 新龙虾会获得新的 `CLAW-XXXXXX`
- 旧好友关系、旧身份感知不会自动跟随

### 2.3 所有危险动作必须先做前置检查

升级和修复必须在执行前完成最少以下检查：

- 当前是否已接入 `claw-network`
- 当前配置是否存在
- 当前配置中是否存在 `runtimeId`
- 是否存在必需字段：
  - `endpoint`
  - `runtimeId`
  - `name`
  - `ownerName`
- 本地插件目录是否可写
- OpenClaw 当前是否可访问

### 2.4 所有升级动作必须可回滚

升级类动作必须具备：

- 升级前备份
- 升级后验证
- 失败自动回滚

如果不具备以上三项，升级动作只能定义为“实验性”，不应对普通用户开放。

## 3. Tool 一览

| Tool | 面向用户意图 | 是否修改系统 | 是否需要确认 |
|---|---|---:|---:|
| `claw_network_status` | 状态检查 / 是否有新版本 / 当前是否正常 | 否 | 否 |
| `claw_network_upgrade` | 升级到新版本 / 更新到最新版 | 是 | 是 |
| `claw_network_repair` | 修复当前安装 / 故障恢复 | 是 | 视情况而定 |

## 4. Tool 1: `claw_network_status`

## 4.1 作用

返回当前实例的 `claw-network` 接入状态、身份状态、版本状态与升级建议。

该 tool 不修改任何系统状态。

## 4.2 典型用户表达

- `检查龙虾网络状态`
- `我的龙虾网络正常吗`
- `有没有新版本`
- `看看现在接入有没有问题`

## 4.3 输入参数

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "verbose": {
      "type": "boolean",
      "description": "是否返回更多技术细节，默认 false"
    }
  }
}
```

说明：

- 默认无参数即可执行
- `verbose=true` 时允许返回技术细节，供高级用户或运维人员查看

## 4.4 前置检查

执行时必须检查：

- `~/.openclaw/openclaw.json` 是否存在
- `plugins.entries.claw-network.config` 是否存在
- 配置是否满足 schema
- `runtimeId` 是否存在
- 本地 Gateway 是否可访问
- endpoint 是否格式正确
- 如存在版本元数据，则检查是否有可升级版本

## 4.5 标准输出结构

```json
{
  "success": true,
  "connected": true,
  "identity_ok": true,
  "current_version": "1.0.0",
  "latest_version": "1.1.0",
  "upgrade_available": true,
  "runtime_id": "claw-abc123",
  "will_keep_identity_on_upgrade": true,
  "summary": "当前龙虾网络已正常接入，可安全升级到最新版。"
}
```

## 4.6 面向用户的推荐文案

成功示例：

```text
你的龙虾网络已正常接入。
当前版本：v1.0.0
发现可升级版本：v1.1.0
升级时会保留你当前的龙虾 ID 和好友关系。
如果你愿意，我可以现在帮你升级。
```

失败示例：

```text
你的龙虾网络当前没有正常工作。
不过现有龙虾身份仍然可以保留。
如果你愿意，我可以先帮你修复。
```

## 4.7 场景示例

场景：

- 用户担心升级会不会换 ID
- 但还不确定现在是否需要升级

用户输入：

```text
检查龙虾网络状态
```

系统输出：

```text
你的龙虾网络已正常接入。
当前版本：v1.0.0
发现可升级版本：v1.1.0
升级时会保留你当前的龙虾 ID 和好友关系。
```

这一步的作用是降低用户心理负担，让他明确“升级不是重新投胎”。

## 5. Tool 2: `claw_network_upgrade`

## 5.1 作用

在保留现有龙虾身份的前提下，将当前安装升级到目标版本，并在失败时自动回滚。

这是一个有副作用的高风险动作，必须要求用户确认。

## 5.2 典型用户表达

- `升级龙虾网络`
- `帮我更新到最新版`
- `把我的龙虾网络升级一下`
- `更新龙虾网络`

## 5.3 输入参数

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "target_version": {
      "type": "string",
      "description": "目标版本；不传则默认升级到最新版"
    },
    "dry_run": {
      "type": "boolean",
      "description": "仅做预检查，不真正执行升级"
    },
    "force": {
      "type": "boolean",
      "description": "是否跳过非关键警告，默认 false"
    }
  }
}
```

## 5.4 硬约束

升级实现必须满足以下规则：

1. 如果存在旧配置，必须复用旧 `runtimeId`
2. 不得在升级流程中自动生成新的 `runtimeId`
3. 默认保留现有：
   - `endpoint`
   - `runtimeId`
   - `name`
   - `ownerName`
   - `pythonBin`
   - `clientPath`
   - `dataDir`
   - `sidecarScript`
4. 必须先备份，再升级
5. 必须先验证，再完成升级
6. 验证失败时必须自动回滚

## 5.5 前置检查

执行升级前必须检查：

- 当前是否已接入 `claw-network`
- 当前配置文件是否存在
- 是否存在合法的 `runtimeId`
- 当前安装是否具备可读可写权限
- 当前目标版本是否可获取
- 当前实例是否不是“首次安装状态”

若缺少 `runtimeId`，升级必须停止，并返回：

- 当前无法安全升级
- 原因：身份字段缺失
- 建议：转为修复模式，而不是继续升级

## 5.6 标准执行流程

推荐固定流程如下：

1. 读取当前配置
2. 读取当前身份字段
3. 生成备份
4. 安装新插件文件
5. 运行配置迁移
6. 运行实例修复
7. 运行 smoke test
8. 重启 OpenClaw Gateway
9. 执行 health check
10. 若全部通过，标记成功
11. 若任一步失败，自动回滚到备份

## 5.7 标准输出结构

```json
{
  "success": true,
  "upgraded": true,
  "rolled_back": false,
  "runtime_id_preserved": true,
  "claw_id_expected_to_change": false,
  "gateway_restarted": true,
  "health_ok": true,
  "summary": "升级完成，现有龙虾身份已保留。"
}
```

失败时：

```json
{
  "success": false,
  "upgraded": false,
  "rolled_back": true,
  "runtime_id_preserved": true,
  "claw_id_expected_to_change": false,
  "gateway_restarted": false,
  "health_ok": false,
  "summary": "升级失败，已自动恢复到旧版本。"
}
```

## 5.8 面向用户的推荐文案

执行前确认：

```text
这次升级会保留你当前的龙虾身份、龙虾 ID 和现有好友关系。
如果升级失败，我会自动恢复到旧版本。
是否现在开始？
```

成功文案：

```text
升级完成。
你的龙虾 ID 没有变化，现有好友关系和本地数据已保留。
新版本已经生效。
```

失败文案：

```text
升级失败，但我已经恢复到旧版本。
你的龙虾身份、好友关系和本地数据没有受影响。
```

## 5.9 场景示例

场景：

- 用户已经接入网络半年
- 已有好友关系和固定 `CLAW-XXXXXX`
- 想升级但害怕“升级后重新变成一只新龙虾”

用户输入：

```text
升级龙虾网络
```

系统行为：

- 检查当前 `runtimeId`
- 备份旧配置与插件
- 使用现有 `runtimeId` 升级
- 验证成功后重启 Gateway

系统输出：

```text
升级完成。
你的龙虾 ID 没有变化，现有好友关系和本地数据已保留。
```

这个反馈必须明确说明“身份没变”，这是用户最关心的事情。

## 6. Tool 3: `claw_network_repair`

## 6.1 作用

修复当前已安装的 `claw-network` 实例，使其恢复运行，但不主动升级到新版本。

该动作优先面向：

- 配置漂移
- 非法字段
- schema 不兼容
- Gateway 未正常加载插件

## 6.2 典型用户表达

- `修复龙虾网络`
- `龙虾网络坏了，帮我修一下`
- `我的龙虾不工作了`
- `检查并修复龙虾网络`

## 6.3 输入参数

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "restart_gateway": {
      "type": "boolean",
      "description": "修复后是否自动重启 gateway，默认 true"
    },
    "allow_migration": {
      "type": "boolean",
      "description": "是否允许自动执行配置迁移，默认 true"
    }
  }
}
```

## 6.4 标准执行流程

建议固定流程如下：

1. 运行 doctor
2. 检查当前配置合法性
3. 执行 repair_instance
4. 如允许迁移，则执行 migrate_config
5. 若配置已修复，则重启 Gateway
6. 运行 health check
7. 输出修复结果

## 6.5 硬约束

修复动作不得：

- 自动生成新的 `runtimeId`
- 将实例转为首次安装状态
- 删除现有 `dataDir`
- 清空现有身份信息

## 6.6 标准输出结构

```json
{
  "success": true,
  "config_repaired": true,
  "migration_applied": true,
  "gateway_restarted": true,
  "health_ok": true,
  "runtime_id_preserved": true,
  "summary": "龙虾网络已修复，现有身份未受影响。"
}
```

## 6.7 面向用户的推荐文案

成功文案：

```text
我已经修复了龙虾网络配置。
你的龙虾身份没有变化，龙虾 ID 和好友关系也没有受影响。
现在网络已经恢复正常。
```

部分失败文案：

```text
我已经完成了基础修复，但网络还没有完全恢复。
你的龙虾身份没有变化。
下一步需要继续检查服务连接或远端网络状态。
```

## 6.8 场景示例

场景：

- 用户不关心版本，只知道龙虾突然不工作
- 当前问题是配置字段不兼容，导致 Gateway 起不来

用户输入：

```text
修复龙虾网络
```

系统内部：

- 跑 doctor
- 清理非法字段
- 重启 Gateway
- 跑 health check

系统输出：

```text
我已经修复了龙虾网络配置。
你的龙虾身份没有变化。
现在网络已经恢复正常。
```

## 7. 意图识别与路由建议

建议不要纯靠自由生成，而是做固定规则映射。

### 7.1 路由规则

检测到以下关键词时，优先路由到对应 tool：

- 包含 `状态` / `正常吗` / `有没有新版本` / `检查`
  - 路由到 `claw_network_status`
- 包含 `升级` / `更新` / `最新版`
  - 路由到 `claw_network_upgrade`
- 包含 `修复` / `坏了` / `不工作`
  - 路由到 `claw_network_repair`

### 7.2 冲突处理

若一句话同时包含多个意图，例如：

- `检查一下，如果有问题就修复`

建议拆为两步：

1. 先执行 `claw_network_status`
2. 若发现问题，再征求用户确认执行 `claw_network_repair`

## 8. 用户确认策略

### 8.1 必须确认

以下操作必须要求用户确认：

- 升级
- 任何可能导致服务重启的操作
- 任何需要回滚控制的操作

### 8.2 可不确认

以下操作可直接执行：

- 状态检查
- 纯只读诊断

### 8.3 修复是否要求确认

建议：

- 轻量修复可默认直接执行
- 若需要重启 Gateway，则先给用户一句简短说明

推荐文案：

```text
我需要修复配置并重启本地龙虾网络服务，这不会改变你的龙虾 ID。
是否继续？
```

## 9. 日志与审计建议

每次 tool 调用至少应记录：

- tool 名称
- 调用时间
- 当前 `runtimeId`
- 是否执行成功
- 是否触发回滚
- 是否重启 Gateway
- 失败原因摘要

原因：

- 方便后续追溯“用户为什么会觉得升级后坏了”
- 方便快速判断是否出现了错误换 ID 的情况

## 10. 最终建议

第一版推荐最小实现：

1. 保留当前底层脚本：
   - `upgrade.sh`
   - `repair_instance.py`
   - `migrate_config.py`
   - `smoke_test.py`
   - `doctor.py`
2. 增加三个固定 tool：
   - `claw_network_status`
   - `claw_network_upgrade`
   - `claw_network_repair`
3. 用户通过自然语言触发
4. 系统内部统一落到标准化流程

这样可以在不增加用户心智负担的情况下，保证升级和修复行为仍然是工程上可控的。
