# 最近一轮对话删除（astrbot_plugin_turn_eraser）

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.0.0-5B8FF9)](https://astrbot.app)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-blue)](https://github.com/bugusun/astrbot-del-message/releases)

一个给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 用的对话管理插件：在聊天里用 `/del` 删掉**最近一轮对话**，被删的内容不会再进入 LLM 的上下文；这条指令本身也不会被写进聊天记录。删除后还可以用 `/undel` 反悔。

> 注意：本插件删除的是 **AstrBot 保存的会话上下文**，不是撤回已经在 QQ / Telegram 等平台上发出的消息。已经发出去的消息仍需用平台自带的撤回功能处理。

## 功能

- `/del`：删除当前会话最近一轮对话（用户提问 + 模型回复 + 中间的思考/工具调用/工具结果）
- `/undel`：恢复上一次被 `/del` 删除的内容（保存在内存中，重启后失效，最多缓存 200 个会话）
- 指令本身不进入上下文：通过 `event.should_call_llm(False)` 阻止框架发起 LLM 请求，因此不会污染聊天记录
- 等会话空闲再删：会和框架的会话锁串行，避免「刚删完又被正在生成的回复写回去」
- 支持按轮删除 / 只删最后一条用户消息两种模式
- 删除回执可预览被删内容开头，可配置长度
- 可选仅管理员可用

## 兼容性

| 项目 | 要求 |
| --- | --- |
| AstrBot | `>= 4.0.0` |
| Python | 跟随 AstrBot 自带环境（使用 `asyncio.timeout`，建议 Python 3.11+） |
| 平台 | `aiocqhttp`、`qq_official`、`telegram`、`webchat`（其他适配器只要走 AstrBot 会话历史即可） |

## 安装

仓库地址：<https://github.com/bugusun/astrbot-del-message>

### 方式一：WebUI 从链接安装（推荐）

打开 AstrBot WebUI → **插件管理** → **安装插件** → 选择 **从链接安装**，填入：

```text
https://github.com/bugusun/astrbot-del-message
```

安装完成后在插件列表里启用「最近一轮对话删除」即可。

### 方式二：手动 clone

```bash
cd AstrBot/data/plugins
git clone https://github.com/bugusun/astrbot-del-message.git astrbot_plugin_turn_eraser
```

重启 AstrBot，或在 WebUI 插件页点击重载。

### 方式三：下载 ZIP

从 [Releases](https://github.com/bugusun/astrbot-del-message/releases) 下载最新源码包，解压后把里面的插件文件放到：

```text
AstrBot/data/plugins/astrbot_plugin_turn_eraser/
├── main.py
├── metadata.yaml
└── _conf_schema.json
```

> 目录名需要是 `astrbot_plugin_turn_eraser`（AstrBot 也可以自动识别 `metadata.yaml` 里的 `name`）。

本插件只用 AstrBot 自带依赖和 Python 标准库，无需额外 `pip install`。

## 使用

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/del` | `/undo`、`/撤销` | 删除最近一轮对话，并把该指令从上下文中排除 |
| `/undel` | `/恢复对话` | 恢复上一次被删除的一轮对话 |

示例：

```text
用户：/del
机器人：已删除最近一轮对话：今天天气怎么样
       （剩余 6 条上下文，这条指令不会进入对话）

用户：/undel
机器人：已恢复 2 条消息到当前对话末尾。
```

`/del` 的删除范围：

1. 默认 `whole_turn: true`：删除最近一条模型回复，以及它前面属于同一轮的用户提问、工具调用和工具结果；
2. 如果上一轮其实是没被回复的孤句（例如上次 LLM 报错中断），只删那一条；
3. 如果上一条就是一条指令（如 `/del`），会再往前多删一整轮，避免留下没有回复的残句；
4. 如果会话正被 LLM 占用超过 3 秒，会提示「机器人还在回复上一条消息」，不会强行删除。

## 配置

在 WebUI 插件设置中修改：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `admin_only` | bool | `false` | 仅管理员可使用 `/del`、`/undel` |
| `whole_turn` | bool | `true` | 整轮删除（连同用户提问）；关闭时只删最后一轮里的用户消息及其之后内容 |
| `allow_undo` | bool | `true` | 允许用 `/undel` 恢复。恢复记录只在内存中，重启即失效 |
| `show_preview` | bool | `true` | 删除后在回执里显示被删对话的开头片段 |
| `preview_length` | int | `40` | 回执预览的最大字数 |

## 原理简述

- 指令由插件直接处理，并调用 `event.should_call_llm(False)`，框架不会为这条消息发起 LLM 请求；AstrBot 的历史写入发生在 LLM 流程里，因此该指令不会进入上下文。
- 插件通过 `conversation_manager` 读取当前会话的 `conversation.history`，计算要删除的区间后写回。
- 删除前会等待该会话的 LLM 锁（`session_lock_manager`）释放，避免并发写入互相覆盖。
- `_checkpoint` 是 AstrBot 的内部检查点角色，计算删除区间时会跳过，但边界上的检查点会被保留。

## 常见问题

**Q：删了之后 AI 还记得刚才的内容？**
A：请确认 `/del` 的回复是插件返回的「已删除…」，而不是模型回复。若模型仍在回答，说明指令没有命中插件（检查唤醒前缀、插件是否启用、配置是否开启了仅管理员）。

**Q：能撤回 QQ 里已经发出的消息吗？**
A：不能。本插件只清理 AstrBot 的对话上下文。撤回平台消息需要平台适配器提供的能力，不在本插件范围内。

**Q：`/undel` 重启后还能用吗？**
A：不能。恢复内容只存在内存里，AstrBot 重启后清空。

**Q：删除后历史里的 `_checkpoint` 会出问题吗？**
A：不会。插件保留边界上的检查点，且不会产生悬空引用。

## 目录结构

```text
astrbot_plugin_turn_eraser/
├── main.py            # 插件实现
├── metadata.yaml      # 插件元数据
├── _conf_schema.json  # WebUI 配置 schema
├── logo.png           # 插件图标（256×256）
├── README.md
├── CHANGELOG.md
└── LICENSE
```

## 开发与更新

```bash
git clone https://github.com/bugusun/astrbot-del-message.git
cd astrbot-del-message
# 修改后
git add .
git commit -m "feat: xxx"
git push
```

发布新版本时同步更新 `metadata.yaml`、`CHANGELOG.md` 中的 `version`，然后打 tag：

```bash
git tag v1.0.1
git push origin v1.0.1
```

## License

[MIT](LICENSE) © 2026 玉米妙妙屋
