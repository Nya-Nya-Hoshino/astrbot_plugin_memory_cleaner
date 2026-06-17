# astrbot-plugin-memory_cleaner

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-6c5ce7.svg)](https://github.com/AstrBotDevs/AstrBot)

记忆清洗 —— 管理员一键清洗上下文记忆，强制重载提示词人格设定并进行理解度自检评分。

## 为什么需要这个插件？

当你在 AstrBot 中更新了 System Prompt 或切换了人格设定后，机器人往往仍受旧会话记忆的影响：

- 😤 继续沿用旧人格、旧语气
- 📜 优先参考历史对话而非当前 Prompt
- 🐌 群聊中人格切换严重滞后
- ❓ 更新 Prompt 后效果不明显
- 🚫 新规则无法被严格执行

**本插件提供一键重置 + 重建 + 检测的完整流程**，确保机器人严格以最新 Prompt 为准。

## 功能

| 功能 | 说明 |
|---|---|
| 🧹 **记忆清洗** | 一键清除当前会话所有对话历史 |
| 🔍 **记忆查询** | 任何人可查看当前会话的对话历史（不含 Prompt）|
| 🔄 **Prompt 重载** | 自动读取 AstrBot 当前配置的人格 System Prompt（非硬编码） |
| 🧠 **人格重建** | 让 LLM 深度内化新 Prompt，而非简单重复 |
| 📊 **理解度检测** | 5 维度自动评分：人设一致性、风格匹配度、规则遵循度、记忆污染度、综合评分 |
| 🐛 **调试模式** | 可开启详细分析日志，排查人格切换失败原因 |
| 🛡️ **权限控制** | 双重鉴权：AstrBot 内置管理员 + 插件自定义管理员列表 |

## 安装

```bash
# 进入 AstrBot 插件目录
cd ~/.astrbot/data/plugins

# 克隆插件
git clone https://github.com/Nya-Nya-Hoshino/astrbot_plugin_memory_cleaner.git memory_cleaner

# 重启 AstrBot
```

## 配置

在 WebUI **插件管理** → memory_cleaner 中配置：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `admin_users` | string | (空) | 管理员 QQ 号列表，英文逗号分隔。例如: `123456789,987654321` |
| `debug_mode` | bool | false | 调试模式，开启后输出详细的 Prompt 分析、评分依据和清洗日志 |

## 使用

### /清洗记忆（管理员）

在 QQ 群聊或私聊中发送（需管理员权限）：

```
/清洗记忆
```

### /记忆查询（所有人）

任何人可查询当前会话的对话记忆：

```
/记忆查询
```

输出示例：
```
🧠 记忆查询 | 请求者: 用户

📊 对话总数: 1
💬 总对话轮次: 5
📍 当前对话: a1b2c3d4...

📜 最近 3 轮对话:
  👤 用户: 你好
  🤖 助手: 你好！有什么可以帮你的？
  👤 用户: 今天天气怎么样
  🤖 助手: 抱歉，我无法获取实时天气数据...
  👤 用户: 讲个笑话
  🤖 助手: 为什么程序员总是分不清万圣节和圣诞节...

💡 仅展示最近对话历史，不含系统 Prompt 设定。
```

### /清洗记忆 执行流程

```
/清洗记忆
  │
  ├─ [1/7] 停止活跃 Agent
  ├─ [2/7] 读取当前人格 System Prompt
  ├─ [3/7] 清除当前会话所有对话历史
  ├─ [4/7] 刷新 Persona 缓存
  ├─ [5/7] 人格重建（LLM 内化新 Prompt）
  ├─ [6/7] Prompt 理解度检测（5维度评分）
  └─ [7/7] 生成并发送评分报告
```

### 输出示例

```
✅ 记忆清洗报告 | 操作者: 管理员

📊 综合评分: 优秀 —— 人格已成功切换！

【评分详情】
  人设一致性:    █████████░ 92
  风格匹配度:    ████████░░ 85
  规则遵循度:    █████████░ 90
  记忆污染度:    █░░░░░░░░░  5  (越低越好)
  综合评分:      █████████░ 89

💬 评语: 助手准确理解了设定角色，风格基本一致，
        无明显旧记忆残留，规则遵循良好。
```

## 权限控制

本插件使用 **双重鉴权**：

1. **AstrBot 内置管理员**：平台适配器识别为 `admin` 角色的用户
2. **插件配置管理员**：在 `admin_users` 中指定的 QQ 号列表

任一满足即可使用 `/清洗记忆` 命令。

## 兼容性

- **AstrBot**: v4.5.7+（使用 `delete_conversations_by_user_id` API）
- **平台**: 全平台兼容（通过 `unified_msg_origin` 标识会话）
- **LLM**: 所有 AstrBot 支持的模型服务

## 安全设计

- ✅ 所有关键步骤均有日志记录
- ✅ 发生异常时不中断 AstrBot 运行
- ✅ 失败时给出明确错误反馈
- ✅ 不会删除其他会话/其他用户的数据（仅操作当前会话）
- ✅ 不可逆操作需管理员权限

## 测试方案

| 测试项 | 方法 | 预期结果 |
|---|---|---|
| 权限校验 | 非管理员发送 `/清洗记忆` | 返回"权限不足" |
| 记忆清除 | 对话多轮后执行，再对话 | 机器人不再引用之前的对话内容 |
| 人格切换 | 修改 AstrBot Persona 后执行 | 机器人立即按新人格回复 |
| 评分有效性 | 使用正确/错误 Prompt 各测一次 | 正确 Prompt 高分，错误的低分 |
| Debug 模式 | 开启 debug_mode 后执行 | 输出详细 Prompt 和日志 |

## 异常处理说明

| 异常场景 | 处理方式 |
|---|---|
| LLM provider 不可用 | 跳过人格重建和检测，仅清除记忆 |
| Persona 配置为空 | 提示用户检查配置，终止操作 |
| Agent 停止失败 | 记录警告日志，继续执行后续步骤 |
| 对话删除失败 | 尝试降级为清空当前对话 |
| 评分解析失败 | 显示原始 LLM 返回，标记评分为 "?" |

## About

### 简介

**记忆清洗 (Memory Cleaner)** 是一个 AstrBot 管理类插件，解决 LLM 更新 System Prompt / Persona 后旧人格残留的问题。

插件并不是简单地"删除记忆"——因为 LLM 本身是无状态的。所谓"记忆"实际上是 AstrBot 在每次推理时从多个来源组装注入的上下文。本插件通过 **删除旧对话 + 创建全新会话 + 清除 SP 偏好 + 清除 Workspace + 重载 Persona** 的组合操作，确保下一次推理从零开始，只基于最新的 Prompt。

### 指令一览

| 指令 | 权限 | 功能 |
|---|---|---|
| `/清洗记忆` | 管理员 | 8步状态重建 + 6维评分 |
| `/记忆查询` | 所有人 | 查看当前会话对话记忆 |
| `/诊断会话` | 所有人 | 查看 umo 结构和会话模式 |

### 版本历史

| 版本 | 日期 | 说明 |
|---|---|---|
| v2.0.0 | 2025-06 | 重构为"会话状态重建"：新增新对话创建、SP清除、Workspace清除、6维评分 |
| v1.0.2 | 2025-06 | 修复 /记忆查询 list+str 崩溃 + LLM抢答 |
| v1.0.1 | 2025-06 | 修复 LLM API 调用错误 + _bar() 参数缺失 |
| v1.0.0 | 2025-06 | 初始发布 |

### 作者

- **GitHub**: [Nya-Nya-Hoshino](https://github.com/Nya-Nya-Hoshino)
- **仓库**: [astrbot_plugin_memory_cleaner](https://github.com/Nya-Nya-Hoshino/astrbot_plugin_memory_cleaner)

### 技术栈

- Python 3.10+
- AstrBot Plugin SDK (Star 基类)
- AstrBot ConversationManager / PersonaManager API
- SharedPreferences (sp) 会话偏好管理

## 许可证

MIT License
