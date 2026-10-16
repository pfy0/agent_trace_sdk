# agent-trace-opencode

把 **OpenCode** 的会话轨迹接进 Agent Trace 的采集插件（JS SDK）。

它订阅 OpenCode 的事件流（`ctx.event.subscribe()`），在 OpenCode 进程内把一轮交互
实时归一成 Agent Trace 契约 `Trace` / `Step`，像 DeerFlow 的 `output:` 一样
**直接写本地 JSON 文件**——不需要接收端，也不依赖 SQLite 库表。

本分支（`js-sdk`）把本插件作为**仓库根**的独立 npm 包发布，专门用于安装：
`github:pfy0/agent_trace_sdk#js-sdk`。

## 安装（三选一）

### 1. Git 源（一条命令，推荐）

```bash
opencode plugin add github:pfy0/agent_trace_sdk#js-sdk
```

它会安装到 `~/.cache/opencode/node_modules/` 并把包名写入全局 `opencode.json`。

### 2. 手动写进配置

```jsonc
// ~/.config/opencode/opencode.json
{
  "plugins": ["github:pfy0/agent_trace_sdk#js-sdk"]
}
```

想连输出路径一并写进配置（就不用设环境变量了），**用对象形态**（不是两层数组）：

```jsonc
{
  "plugins": [
    {
      "package": "github:pfy0/agent_trace_sdk#js-sdk",
      "options": { "output": "C:\\Users\\you\\opencode_traces.json" }
    }
  ]
}
```

> 配置键是 **`plugins`（复数）**；它支持 npm 包 / git 源 / 本地路径。
> 旧的 **`plugin`（单数）** 键只接受 npm 包或 git 源。
> ⚠️ 常见错误：写成 `[["包名", { ... }]]`（数组套数组）会被 OpenCode
> 判为格式非法、**直接跳过不加载**（日志里会出现
> `configuration normalization diagnostic ... kind=invalid`）。

### 3. 本地目录（离线 / 内网）

把本目录整个拷成 `~/.config/opencode/plugins/oc-plugin/` 即可自动加载
（OpenCode 会自动加载 `plugins/<名字>/index.js`，无需改配置）。

> 注意：`~/.config/opencode/plugins/` 下的**单文件** `*.js` 也会被自动加载，
> 但**只有 `index.js`** 是子目录的入口名。

## 配置（环境变量）

唯一必须设置的是输出路径，不设则只留在内存、不落盘：

| 变量 | 必填 | 说明 |
| :--- | :--- | :--- |
| `AGENT_TRACE_OUTPUT` | 是 | 输出 JSON 路径；`.jsonl` 后缀则一行一个 step |
| `AGENT_TRACE_AGENT_ID` | 否 | 默认 `agent-opencode` |
| `AGENT_TRACE_APP_NAME` | 否 | 默认 `opencode` |
| `AGENT_TRACE_APP_ID` / `AGENT_TRACE_SYSTEM_CODE` / `AGENT_TRACE_USER_ID` | 否 | 身份属性，缺省不写 |
| `AGENT_TRACE_DEBUG` | 否 | `true` 打印调试日志 |
| `AGENT_TRACE_DEBUG_LOG` | 否 | 调试日志同时追加到该文件 |

Windows 永久设置：`setx AGENT_TRACE_OUTPUT "D:\traces\opencode.json"`（重开终端生效）。

## 输出

顶层是 trace 数组，元素是契约信封；`conversations.messages` 里是每一步：

```json
[
  {
    "turn_id": "turn_01eccf476c68437ea494a7bfc6a7c722",
    "turn_no": 1,
    "session_id": "ses_xxx",
    "app_name": "opencode",
    "agent_id": "agent-opencode",
    "conversations_type": "openai_messages",
    "conversations": {
      "messages": [
        { "message_id": "msg_usr_1", "message_type": "user_query", "role": "user" },
        { "message_id": "msg_rsn_1", "message_type": "reasoning", "role": "assistant" },
        { "message_id": "msg_ans_1", "message_type": "final_answer", "role": "assistant" }
      ]
    },
    "completed": true,
    "tool_stats": { "shell": { "total": 1, "success": 1, "failure": 0 } },
    "token_usage": { "prompt_tokens": 9574, "completion_tokens": 61, "total_tokens": 9635 },
    "metadata": { "framework": "opencode", "opencode_session_id": "ses_xxx" }
  }
]
```

## 事件映射

| OpenCode 事件 | → 契约 |
| :--- | :--- |
| `session.inbox.enqueued`（user） | `user_query`（同轮第 2 条起 `is_follow_up=true`） |
| `session.reasoning.started/ended` | `reasoning` |
| `session.text.started/delta/ended` | 先缓冲；收尾时最后一段升为 `final_answer` |
| `session.tool.input.*` / `session.tool.called` | `tool_call`（按 toolCallID 去重） |
| `session.tool.success` / `session.tool.failed` | `tool_response`（回填 `tool_success`/`tool_name`/`parent_span_id`） |
| `session.step.ended`（tokens/finish） | 挂到该 assistant 消息首个 step 的 token 增量 |
| `session.execution.succeeded/failed` | 收尾该轮并提交 |

## 组成

| 文件 | 作用 |
| :--- | :--- |
| `index.js` | 包入口 / 目录入口：re-export 插件（`exports["."]` 与 `exports["./server"]`） |
| `agent-trace.js` | 插件本体：`ctx.event.subscribe()` 泵取事件流，退出时兜底收尾 |
| `session-collector.js` | 事件 → Trace：轮次边界、文本/推理/工具、token、收尾 |
| `trace-builder.js` | 契约层：Step/Trace 构造、id/span 生成、`tool_success` 判定 |
| `exporters.js` | 落盘：JSON（顶层数组）/ JSONL（一行一 step）/ Memory |
| `schema/` | 语言无关的契约（`trace.schema.json`），随包分发以便校验 |

## 契约一致

字段严格对齐 `schema/trace.schema.json`；序列化丢弃空值（对齐 Python
`serializable()`）；工具成败判定与 Python `agent_trace.tool_outcome` 逐条等价。
JS 输出可直接过 Python 的 `Trace.model_validate`（`extra="forbid"`），
离线一致性测试在 `main` 分支的 `tests/test_opencode_js_sdk.py`。

## 设计约束

- **非阻塞**：事件流旁路泵取，逐事件处理不拖慢 agent loop。
- **fail-open**：单条脏事件不打断整条流；未配置 output 也能运行。
- **进程内归一**：契约只有一份（JS 侧），无需服务端二次翻译。
