# Agent Trace SDK

Agent 运行轨迹采集 SDK。业务方以**最小改动**接入后，SDK 自动捕获一轮交互
（turn）的完整轨迹：系统提示词、用户输入、推理过程、工具调用、工具返回、
最终回答，并结构化导出。

## 采集单位

- **Trace**：一次 agent 运行 = 一个 turn = 一轮交互，是采集/校验/上报的基本单位。
- **Step**：trace 中的一步，即 `conversations` 数组里的一条消息。
- 轮次边界由 `turn_id` 决定：**同一 `turn_id` 下的消息属于同一轮**，一轮内可以出现
  多条 `user_query`（第一条是初始输入，其余是追问/补充，会标记 `is_follow_up=True`），
  不做切分；跨轮切分只按 `turn_id`。

`turn_id` 由业务方传入，作为 trace 的唯一标识；`session_id` 同样由业务方传入，
用于关联同一会话的多个 turn。

## 安装

**业务方（从 GitHub 私有仓库安装）**：

```bash
pip install "git+https://github.com/pfy0/agent_trace_sdk.git@v0.7.0"
```

- 需要：机器能访问 GitHub、装了 `git`、对仓库有读权限。
- 私有仓库还需带上凭证（SSH key 或 Token）：
  `pip install "git+ssh://git@github.com/pfy0/agent_trace_sdk.git@v0.7.0"`
- 装可选依赖（Excel 导出等）：
  `pip install "agent-trace-sdk[all] @ git+https://github.com/pfy0/agent_trace_sdk.git@v0.7.0"`

固定 tag 安装，版本可复现。完整说明（Token 配置、升级、离线 wheel、
DeerFlow/uv 安装）见 `docs/distribute.md`。

### v0.7.0 变更提示（新增 OpenCode 集成；工具成败判定收敛为单一事实来源）

**新增能力**：

- **OpenCode 集成（JS SDK）**：一个插件订阅 OpenCode 事件流，**进程内**
  归一成契约 `Trace` / `Step`，直接写本地 JSON（不需接收端、不读 SQLite）。
  安装：`opencode plugin add github:pfy0/agent_trace_sdk#js-sdk`。详见「框架集成：OpenCode」。

**内部重构（行为不变）**：

- **工具成败判定收敛到单一事实来源** `src/agent_trace/tool_outcome.py`
  （`judge_tool_success`），DeerFlow 与 OpenCode 共用同一套两层口径，避免各集成
  各判一套导致下游 `tool_stats` 不一致。判定结果与 v0.5.1 起的口径逐条等价。

### v0.6.0 变更提示（可信身份：身份属性只信服务端鉴权）

**新增能力**：

- **可信身份解析**（`agent_trace.identity`）。`user_id` / `app_id` / `app_name` /
  `system_code` 属于「身份属性」，多用户共用 agent 时不能靠环境变量或客户端自报；
  按 `trusted > explicit > reported > default` 解析，`trusted` 层由服务端凭证解出。
- **`Tracer.turn(trusted_identity={...})`** 按轮注入可信身份，优先级高于客户端自报值
  与 config 默认值。`source` 是请求属性，不采纳 trusted 层。
- **`Tracer(user_resolver=...)`** 钩子：在请求入口按当前请求自动解析可信身份；
  钩子抛异常不影响采集（`debug=True` 时上抛）。
- **provenance 审计**：默认把每个字段的 `{value, origin, trusted}` 写进
  `metadata.identity_provenance`，「有值但不可信」的身份字段另写
  `metadata.untrusted_identity_fields`；`record_identity_provenance=False` 可关闭。
- **DeerFlow 中间件**从 `runtime.context` 读取可信身份（顶层同名键或
  `trusted_identity` 子字典，子字典优先）。

### v0.5.2 变更提示（tool_stats 去掉 unknown 列）

**行为变化**：

- **`tool_stats` 每工具统计去掉 `unknown` 列**，只保留
  `{total, success, failure}`。`tool_success == -1`（判不出成败）不再单列，
  只计入 `total`，因此恒有 `total >= success + failure`（差值即判不出的次数），
  `tool_error_counts` 口径不变（仍只统计 `failure`）。

### v0.5.1 变更提示（tool_success 增加第二层兜底）

**行为变化**（v0.5.0 的「只认 JSON 显式成败」仍是第一层，本次在其后补兜底）：

- **`tool_success` 改为两层判定**。第一层**显式成败**：返回体 JSON **顶层**带
  `success` / `ok` / `is_success` / `succeeded`（布尔值或 `"true"`/`"false"`）
  时，为真 → `1`，为假 → `0`。第一层取不到值时才走第二层**兜底**：文本以
  `Error` 开头 → `0`；能解析成 JSON 对象、只是没带成败键（如搜索结果
  `{"query": ..., "results": [...]}`，结构化结果本身就说明调用跑通了）→ `1`；
  其余 → `-1`（未知），**不猜**。
  - **影响**：v0.5.0 里判为 `-1` 的无成败键结构化 JSON，现在记 `1`；纯文本工具
    仍记 `-1`，除非文本以 `Error` 开头（DeerFlow 各工具 `return f"Error: {e}"`
    的统一失败约定）。下游若按 `tool_success` 统计成功率，口径需同步调整。
  - 仍然**不采信框架的 `ToolMessage.status`**，也**不按 `Exit Code:` /
    `timeout:` 等文本特征猜**，失败文本只认 `Error` 前缀这一条约定。

### v0.5.0 变更提示（tool_success 只认 JSON + Trace 级统计自动聚合）

**不向后兼容**：

- **`tool_success` 判定收敛为「只看工具返回体 JSON」**。取返回体**顶层**的
  `success` / `ok` / `is_success` / `succeeded`（布尔值或 `"true"`/`"false"`
  字符串）：为真 → `1`，为假 → `0`；没有工具调用、返回体不是 JSON、或顶层没有
  这些键 → `-1`（未知）。
  - 不再采信框架的 `ToolMessage.status`：LangChain 无论成败都显式置 `"success"`，
    采信它会把失败记成成功。
  - 不再做文本特征匹配：早期版本按 `Exit Code:` / `Error:` /
    `timeout: failed to run command` 等猜成败，容易误伤合法输出，已整体移除。
  - **影响**：返回纯文本的工具（如 DeerFlow `bash`）无论实际成败都记 `-1`；
    `tool_error_counts` 只统计 JSON 里明确 `success: false` 的调用。下游若按
    `tool_success` 统计成功率，需同步调整口径。
  - 只看顶层键：嵌套在 `data` 里的 `ok` 属于业务子状态（如 `status: draft`），
    不代表这次调用本身失败。

**新增能力**：

- **Trace 级统计由 SDK 自动聚合**（此前是预留空位）：`toolsets_used`、
  `tool_stats`、`tool_error_counts`、`token_usage`，在 `as_trace()` 里按
  `conversations` 汇总。业务方仍可用 `Turn(..., tool_stats=...)` 等覆盖自动值。
  聚合规则见下方「Trace 级统计的聚合规则」。

### v0.4.4 变更提示（脱敏默认层增强 + 移除离线导入）

**行为变化**：

- **脱敏扩为三层防线**：`tool_calls[].function.arguments` 里键名命中
  `password` / `token` / `api_key` / `authorization` 等时，其值**整段**擦除
  （值是 dict/list 时连子结构一起擦）。此前只按值的文本形态匹配，
  `{"password": "hunter2"}` 这类结构化凭据会明文漏过。
  另新增高熵串检测（`EntropyRule`），抓没有已知前缀的随机密钥。
  新增 `Redactor(redact_keys=...)` / `sensitive_keys=[...]` 配置项。
- **移除离线导入功能**：`Trace.from_raw()`、`split_by_turn()`、`ingest.py`
  及示例 `examples/import_data.py` 一并删除。SDK 定位是采集 agent 运行轨迹，
  不再提供导入已有数据的入口。

### v0.4.2 变更提示（conversations_type 默认值变更）

- **`conversations_type` 默认值**：`openai` → `openai_messages`。
  下游若按 `conversations_type == "openai"` 过滤或入库，需要同步调整。
  显式传入该字段的调用不受影响。

### v0.4.1 变更提示（新增默认脱敏 + 修正 tool_success）

**行为变化**：

- **默认开启脱敏**：`step.content` 与 `tool_calls[].function.arguments` 里的
  凭据 / 个人标识会被替换为 `[REDACTED:规则名]`。需要原始数据时显式关闭：
  `Tracer(..., redact=False)`，或用 `redact_content` / `redact_arguments`
  分别控制。详见下方「脱敏」一节。
- **修正 `tool_success` 误判**：`ToolMessage.status` 的默认值是 `"success"`，
  旧版直接采信，导致 DeerFlow 里失败的工具调用被记成成功。现在按「显式 status
  → 返回文本错误特征 → 未知」三层判定，失败会正确记为 `0`。

### v0.4.3 变更提示（修正 tool_success 判定顺序）

**行为变化**：

- **`tool_success` 判定顺序改为「文本证据优先」**。v0.4.1 的「先看显式 status」
  前提不成立：LangChain 的 `BaseTool.run` 无论成败都会把 `status` **显式**传给
  `ToolMessage`（成功也是 `"success"`），所以那一层永远命中 `"success"`，DeerFlow
  里失败的工具调用仍被记成成功。现在按「文本错误特征 → 显式 `error` → 显式
  `success` → 未知」判定。
- **收紧错误特征**：只采信权威信号 —— 整行的 `Exit Code: <非0>`、整行的
  `Command timed out after ...`、开头的 `Error: ...`。不再把 `Std Error:`
  当作失败（沙箱只要 stderr 非空就追加，命令成功时也有，会把「打 warning 的成功
  命令」误判为失败），也去掉 `No such file or directory` / `Permission denied` /
  `command not found` / `Traceback` 等可能出现在合法输出里的弱特征。

仅影响工具成败判定（现已收敛到单一事实来源 `src/agent_trace/tool_outcome.py`，
DeerFlow 与 OpenCode 集成共用），Step 契约未变。详见下方框架集成的
`tool_success` 判定。

### v0.4.0 升级提示（不向后兼容，Step 去字段 + 契约收紧）

本版收窄了契约，Step 与 Trace 都**只接受规范内字段**：

- **Step 移除 4 个字段**：`completed`、`partial`、`error`、`meta`。
  结束状态只在 Trace 级；Step 级的扩展信息改用 Trace 级 `metadata`。
- **Trace 移除 2 个字段**：`duration_ms`、`error`（失败只体现为 `completed=false`）。
- **`extra="forbid"`**：`Step` / `Trace` / `ToolCall` / `Config` 都不再接受规范外字段。
  业务方若之前往 step 里塞自定义键（`meta={...}` 或直接 `**extra`），
  请改为 `Tracer(..., metadata={...})` 或 `Tracer(..., extra_info={...})`；
  写错的配置名（如 `apps_name=`）现在会直接报错，不再静默失效。

### v0.3.0 升级提示（不向后兼容）

v0.2.0 与 v0.3.0 按公司《轨迹数据规范》改造了字段，从 v0.1.0 升级需改代码。

**v0.3.0 相比 v0.2.0**（Step 层）：

| v0.2.0（Step 级） | v0.3.0 |
| :--- | :--- |
| `step["timestamp"]` | `step["start_time"]` / `step["end_time"]` |
| 无 | `span_id` / `parent_span_id`（SDK 自动生成 / 自动填） |
| 无 | `tool_name`、`tool_success`（-1 未知 / 0 失败 / 1 成功） |
| 无 | `prompt_tokens_delta` / `completion_tokens_delta` |

**v0.2.0 相比 v0.1.0**（Trace 层）：

| v0.1.0 | v0.2.0 |
| :--- | :--- |
| `Tracer(agent_name=..., agent_version=..., env=...)` | `Tracer(agent_id=..., app_name=...)` |
| `trace["started_at"]` / `trace["ended_at"]` | `trace["start_time"]` / `trace["end_time"]` |
| `trace["meta"]` | `trace["metadata"]` |
| `trace["conversation"]` | `trace["conversations"]` |
| `trace["status"]` | `trace["completed"]` / `trace["partial"]` |
| `input_tokens` / `output_tokens` / `model` / `tags` | 已移除 |
| `turn.span(...)` | 已移除（层级改由 SDK 按规则自动填 `parent_span_id`） |

**SDK 开发者（源码安装）**：

```bash
pip install -e .            # 仅核心（pydantic）
pip install -e ".[xlsx]"    # 需要 Excel 导出
pip install -e ".[all]"     # 全部可选依赖
```

## 快速开始

```python
from agent_trace import Tracer, JsonExporter, XlsxExporter

tracer = Tracer(
    agent_id="your-agent-id",      # 智能体注册市场里的 ID
    app_name="news-daily",         # 项目 / 产品 / 场景标识
    exporters=[
        JsonExporter("output/traces.json"),
        XlsxExporter("output/steps.xlsx"),
    ],
)

with tracer.turn(turn_id="turn-1", session_id="sess-1", turn_no=1) as turn:
    turn.record_user_query("查看今日 ai 新闻热点")
    turn.record_reasoning("先搜索今日 AI 新闻……")
    turn.record_tool_call([("web_search", {"count": 8}), ("web_search", {"count": 6})])
    for call in turn.steps[-1].tool_calls:
        turn.record_tool_response(tool_call_id=call.id, content='{"success": true}')
    turn.record_final_answer("今日 AI 要闻速递：……")
```

退出 `with` 时 SDK 自动：分配 `step_no` / `message_id`、校验成 `Trace`、
交给每个导出器。异常路径也会落一条终止 step 并把 Trace 级 `completed` 置为 `false`。

## 手动埋点 API

| 方法 | 说明 |
| :--- | :--- |
| `turn.record_system_prompt(content)` | 系统提示词与工具定义 |
| `turn.record_user_query(content, follow_up=False)` | 用户输入或反馈（`follow_up=True` 标记追问） |
| `turn.record_reasoning(content)` | 思考、规划、调度等推理过程 |
| `turn.record_tool_call(tool_calls)` | 发起工具调用（多个调用合并为 1 个 step） |
| `turn.record_tool_response(tool_call_id, content)` | 工具返回结果 |
| `turn.record_final_answer(content)` | 最终交付回答 |
| `turn.record_error(error)` | 落一条终止 step，并把 Trace 级 `completed` 置为 `false` |

`record_tool_call` 支持多种入参：`("name", arguments)` 元组、`{"name":..., "arguments":...}`、
OpenAI 风格的完整对象、以及它们的列表。

`with turn.step(message_type)` 会自动记录该步的起止时间与 `latency_ms`。

## 先后顺序与工具关联

`conversations` 是一维数组，**先后顺序由 `step_no` 表达**；工具调用与工具返回之间用
`tool_calls[].id` <-> `tool_call_id` 关联：

```
step 3  tool_call      tool_calls: [{id: "call_0964..."}, {id: "call_19bc..."}]
step 4  tool_response  tool_call_id: "call_0964..."   ← 回关到 step 3 的第一个调用
step 5  tool_response  tool_call_id: "call_19bc..."   ← 回关到 step 3 的第二个调用
```

- **span 树（规则 B）**：每个 step 自动分配 `span_id`；`tool_call` 是父，
  指向它的 `tool_response` 是子（`parent_span_id` 由 SDK 按 `tool_call_id`
  自动回填），业务方无需传。
- 多 agent 场景下每个 run 各自产出一条 trace，靠 `session_id` 关联。
- 需要额外归属信息（如"这一步由哪个子 agent 产出"）时，用 Trace 级 `metadata` 承载，
  例如 `Tracer(..., metadata={"agents": ["researcher"]})`。

## 装饰器

```python
@tracer.step("reasoning")
def plan(question: str) -> str:
    return "拆解问题：" + question

@tracer.trace
def answer(question: str) -> dict:
    plan(question)
    return {"answer": "……"}
```

## 导出器

| 导出器 | 输出 |
| :--- | :--- |
| `JsonExporter(path)` | 单个 JSON 文件，顶层是 trace 数组 |
| `JsonlExporter(path)` | 一行一个 step 的裸 JSON |
| `XlsxExporter(path)` | 一行一个 step 的 Excel |
| `MemoryExporter()` | 只留在内存，便于测试 |
| `HttpExporter(endpoint, ...)` | 非阻塞上报到采集服务，支持重试与失败落盘 |

Excel 里一条 tool_call 消息占一行，多个调用放在同一格（JSON 数组）。

## 字段契约

Step 字段（与公司《轨迹数据规范》对齐）：

- 基础：`message_id`（`msg_{缩写}_{序号}`）、`step_no`、`role`、`message_type`、
  `content`
- 时间：`start_time` / `end_time`（ISO 8601 UTC）、`latency_ms`
- span 树：`span_id`（SDK 自动生成）、`parent_span_id`（SDK 自动填）
- 工具：`tool_calls`、`tool_call_id`、`tool_name`（SDK 自动回填）、
  `tool_success`（-1 未知 / 0 失败 / 1 成功）
- token：`prompt_tokens_delta` / `completion_tokens_delta`（调 LLM 的步骤才有）
- 追问标记：`is_follow_up`（仅 `user_query` 有意义）

> `step_no` 用 Integer（规范文档写的是 String，按实际实现取 Integer）。

Trace 字段（与公司《轨迹数据规范》对齐）：

- 标识与时间：`turn_id`、`turn_no`（会话内第几轮）、`session_id`、
  `start_time` / `end_time`（ISO 8601 UTC）、`id` / `create_time`（入库侧生成，SDK 留空）。
- 业务标识：`app_name`、`app_id`、`system_code`、`user_id`、`source`、
  `agent_id`、`parent_agent_id`。其中前四个是**身份属性**、`source` 是**请求属性**，
  信任模型见下文「可信身份」。
- 会话内容：`conversations_type`（缺省 `openai_messages`）与 `conversations`
  （该轮全部 step 的数组）。
- 结束状态：`completed` / `partial`。
- 统计：`toolsets_used`、`tool_stats`、`tool_error_counts`、`token_usage`
  **由 SDK 在 `as_trace()` 里按 `conversations` 自动聚合**；`api_calls`
  仍为预留空位（SDK 不填）。没有工具调用 / token 数据的轮次不产出对应字段。
- 扩展（可选）：`metadata`、`extra_info`。

**契约严格性**：`Step` / `Trace` / `ToolCall` / `Config` 都是 `extra="forbid"`，
写入规范外字段会直接报错，不再静默丢弃。

结束状态说明：`completed` = 该轮是否**正常跑完**（异常/中断为 `false`）；
`partial` = 是否因超时等**外部条件被截断**。两者都可为 true/false 组合，
取代了早期的单一 `status` 字段。**结束状态只在 Trace 级，Step 级没有**。

`message_id` 校验**只查格式、不查序号连续性**（历史数据存在类型内自增与全局位置两种编号）。

### Trace 级统计的聚合规则

`as_trace()` 按 `conversations` 自动汇总（业务方可用 `Turn(..., tool_stats=...)`
等 Trace 字段覆盖自动值）：

| 字段 | 来源 | 规则 |
| :--- | :--- | :--- |
| `toolsets_used` | `tool_call` 的 `function.name`、`tool_response` 的 `tool_name` | 去重，按首次出现排序 |
| `tool_stats` | `tool_response` 的 `tool_name` + `tool_success` | 每工具 `{total, success, failure}`；`-1`（未知）只计入 `total` |
| `tool_error_counts` | `tool_stats` | 只保留 `failure > 0` 的工具，值为失败次数 |
| `token_usage` | 各 step 的 `prompt_tokens_delta` / `completion_tokens_delta` | 求和为 `{prompt_tokens, completion_tokens, total_tokens}` |

示例（一次调用 `read_file` 成功、一次 `draft_course` 失败）：

```json
{
  "toolsets_used": ["read_file", "draft_course"],
  "tool_stats": {
    "read_file": {"total": 1, "success": 1, "failure": 0},
    "draft_course": {"total": 1, "success": 0, "failure": 1}
  },
  "tool_error_counts": {"draft_course": 1},
  "token_usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}
}
```

`tool_success == -1`（判不出成败）只计入 `total`，不落 `success` / `failure`，
因此恒有 `total >= success + failure`（判不出的次数即差值）。

## 框架集成：DeerFlow

DeerFlow 是 LangGraph 系框架，集成方式是**插件 + 中间件**，不改动业务代码：

```yaml
# DeerFlow 的 config.yaml
plugins:
  - use: agent_trace.integrations.deerflow:install
    config:
      output: "outputs/agent_traces.json"   # 落盘路径（.jsonl 后缀走 JSONL）
      agent_id: "your-agent-id"
      app_name: deer-flow
      memory: false                          # true 时同时留一份在内存
```

采集范围覆盖 lead agent 与 subagent（`AgentScope.BOTH`）。自动映射规则：

| DeerFlow / LangChain | → step |
| :--- | :--- |
| `SystemMessage` | `system_prompt`（随 thread 固定，每轮采一次） |
| `HumanMessage`（真实用户输入） | `user_query`（同轮第 2 条起自动标 `is_follow_up`） |
| `HumanMessage`（框架注入，带 `hide_from_ui`） | 不算 `user_query`，记为 `reasoning`；来源标签记在 Trace 级 `metadata.deerflow_injected` |
| `AIMessage.content`（带 `tool_calls`） | `reasoning` |
| `AIMessage.content`（收尾那条） | `final_answer` |
| `AIMessage.tool_calls`（多个） | 合并为 **1 个** `tool_call` step |
| `ToolMessage` | `tool_response`（带 `tool_call_id`，失败时 `tool_success=0`） |

- `turn_id` 取 DeerFlow 的 `run_id`，`session_id` 取 `thread_id`；同一 thread 的多轮
  run 各自产出一条 trace，共享 `session_id`。
- **澄清续跑自动合并**：`ask_clarification` 会中断 run，用户回答后 DeerFlow 另起一个
  `run_id` 继续。此时不新开 trace，而是并入同一轮的 trace（`user_query` 标
  `is_follow_up`，Trace 级 `metadata.clarification_answer=true`），`turn_id` 沿用最初提问那轮，
  保证「提问 → 反问 → 澄清 → 完成」是一条完整 trace。若用户放弃澄清直接重新提问，
  暂存的澄清轮会被兜底提交，数据不丢。
- **框架注入的消息不记为用户输入**：带 `hide_from_ui` 的 `HumanMessage`（memory 画像、
  动态上下文提醒、待办提醒等）记为 `reasoning`，来源标签记在 Trace 级
  `metadata.deerflow_injected`（值为 `injected` / `dynamic_context`）。判定规则与 DeerFlow 自身的
  `_is_genuine_user_message` 一致：澄清答复虽带 `hide_from_ui`，但仍属真实用户输入。
- **`tool_success` 判定（两层）**：
  1. **显式成败**：返回体 JSON **顶层**带 `success` / `ok` / `is_success` /
     `succeeded`，为真 → `1`，为假 → `0`；
  2. **兜底**：文本以 `Error` 开头 → `0`；能解析成 JSON 对象但没带成败键
     （结构化结果本身说明调用跑通了，如搜索结果 `{"query": ..., "results": [...]}`）
     → `1`；其余 → `-1`（未知），**不猜**。

  **不看框架的 `ToolMessage.status`**（LangChain 无论成败都显式置 `"success"`，
  采信它会把失败记成成功），**也不按 `Exit Code:` / `timeout:` 等文本特征猜**
  （容易误伤合法输出）；失败文本只认 `Error` 前缀这一条约定，对应 DeerFlow
  各工具 `return f"Error: {e}"` 的统一失败写法。只看**顶层键**：嵌套在
  `data` 里的 `ok` 属于业务子状态（如 `status: draft`），不代表这次调用失败。
- 初始 state 里携带的历史消息不会重复采集（以最后一条真实用户消息为界）。
- 无 langchain 的环境下本模块仍可 import（中间件退化为普通类，便于纯逻辑单测）。

也支持手动挂载：

```python
from langchain.agents import create_agent
from agent_trace.integrations.deerflow import AgentTraceMiddleware

agent = create_agent(model=..., tools=[...], middleware=[AgentTraceMiddleware(tracer)])
```

## 框架集成：OpenCode（JS SDK，直接出 JSON）

OpenCode 运行时是 Node/Bun，插件只能用 JS/TS 写。因此 OpenCode 侧提供一个
**JS SDK**：订阅 OpenCode 事件流，**进程内**把一轮交互归一成契约 `Trace` / `Step`，
像 DeerFlow 的 `output:` 一样直接写本地 JSON——不需要接收端，也不依赖 SQLite 库表。

JS SDK 以独立 npm 包形式发布在 **`js-sdk` 分支**（包根就是包本身），
仓库内 `integrations/opencode/` 是开发副本，两者同步。

```bash
# 安装（一条命令，OpenCode 自带命令）：从 js-sdk 分支安装，并自动写入全局配置
opencode plugin add github:pfy0/agent_trace_sdk#js-sdk

# 配置输出路径后正常跑 OpenCode；每轮结束写一条 trace
AGENT_TRACE_OUTPUT=outputs/opencode_traces.json
```

- **包组成**：`index.js`（包入口）→ `agent-trace.js` 用 `ctx.event.subscribe()`
  订阅事件流；`session-collector.js` 归一，`trace-builder.js` 契约层，`exporters.js` 落盘。
  安装详情与环境变量见各分支的 `README.md`。
- **契约一致**：字段严格对齐 `schema/trace.schema.json`；工具成败判定与 Python
  `agent_trace.tool_outcome` 逐条等价。JS 输出可直接过 Python 的
  `Trace.model_validate`（`extra="forbid"`），由 `tests/test_opencode_js_sdk.py` 守护。

事件映射（OpenCode 事件 → step）：

| OpenCode 事件 | → step |
| :--- | :--- |
| `session.inbox.enqueued`（user） | `user_query`（同轮第 2 条起自动标 `is_follow_up`） |
| `session.reasoning.started/delta/ended` | `reasoning` |
| `session.text.started/delta/ended` | 先缓冲；收尾时最后一段升为 `final_answer` |
| `session.tool.input.*` / `session.tool.called` | `tool_call`（`tool_calls[].function.name/arguments`） |
| `session.tool.success` / `session.tool.failed` | `tool_response`（`tool_success` / `tool_name` / `parent_span_id` 自动回填） |
| `session.step.ended`（tokens） | 该 assistant 消息首个 step 的 `prompt/completion_tokens_delta` |
| `session.execution.succeeded/failed` | 收尾该轮并提交 |

要点：

- `turn_id` 按契约生成为 `turn_<32位hex>`（与 Python `generate_id("turn")` 同形）；`session_id` 取 OpenCode 的
  `sessionID`；每条 user 消息 + 其后的 assistant 归为一个 turn。
- **实时增量**：逐事件落步，不等整轮跑完再重放；**fail-open**：单条脏事件不打断整条流。
- 工具成败复用统一口径 `agent_trace.tool_outcome`（与 DeerFlow 同一套两层判定）。

## 接入方式总览

SDK 提供四种接入方式，全部含可运行示例，详见 **`docs/integration-methods.md`**：

| 方式 | 适用场景 | 改动量 |
| :--- | :--- | :--- |
| ① 手动埋点 API（含 `with turn.step(...)` 自动计时写法） | 任意框架、自研 Agent，逐步精确控制 | 每个 step 一行调用 |
| ② 装饰器 `@tracer.trace` / `@tracer.step` | 已函数化、边界清晰的一轮 / 一步 | 加一个装饰器 |
| ③ 框架集成（DeerFlow / LangGraph 中间件） | 跑在 DeerFlow / LangGraph 上 | **零业务改动** |
| ④ 框架集成（OpenCode JS SDK） | 跑在 OpenCode 上 | 装一个采集插件（进程内归一，直接出 JSON） |

> 该文档同时对比了阿里云 AgentLoop 的三条管道（eBPF / LoongSuite Pilot / 语言与框架探针），
> 说明本项目「不止手动埋点」的边界与取舍。

## 发布

当前用 **git tag** 分发（不依赖私有 PyPI）：

```bash
python tools/release.py --build-only     # 只出包，产物在 dist/
python tools/release.py --tag            # 构建 + 校验 + 推分支 + 打 tag 并推送
```

业务方固定 tag 安装即可复现版本。完整说明见 `docs/distribute.md`。

## 目录结构

```
src/agent_trace/
├── schemas.py       # 契约层：枚举、Trace/Step 模型、校验、id 生成
├── tracer.py        # 采集层：Tracer / Turn / 手动埋点 / 装饰器
├── context.py       # contextvars 上下文传播
├── config.py        # 配置：环境变量 + 代码初始化
├── identity.py      # 可信身份解析：凭证 > 显式 > 上报 > 默认
├── redact.py        # 脱敏：可配规则擦除凭据 / 个人标识
├── tool_outcome.py  # 工具成败判定的单一事实来源（各集成共用）
├── integrations/    # 框架适配（deerflow.py）
├── transport/       # HTTP 上报：队列 / 重试 / 失败落盘
└── exporters/       # json / jsonl / xlsx / http / base
integrations/opencode/  # OpenCode JS SDK（插件 + 归一 + 落盘）+ 安装说明
examples/            # 可运行示例
tools/release.py     # 一键构建 / 打 tag / 发布
schema/              # 语言无关的 JSON Schema 契约
```

## 示例

```bash
python examples/manual_api.py    # 手动埋点最小闭环 + 三种导出
python examples/decorator.py     # 装饰器与异常路径
python examples/deerflow_integration.py  # DeerFlow 中间件集成
python examples/trusted_identity.py      # 可信身份注入（多用户共用 agent）
```

## 环境变量

| 变量 | 说明 | 默认 |
| :--- | :--- | :--- |
| `AGENT_TRACE_AGENT_ID` | 智能体 ID | 空 |
| `AGENT_TRACE_PARENT_AGENT_ID` | 父智能体 ID | 空 |
| `AGENT_TRACE_APP_NAME` | 项目 / 产品 / 场景标识 | 空 |
| `AGENT_TRACE_APP_ID` | 应用 ID | 空 |
| `AGENT_TRACE_SYSTEM_CODE` | 触发事件的系统（ALM S 码） | 空 |
| `AGENT_TRACE_USER_ID` | 用户 ID（默认工号） | 空 |
| `AGENT_TRACE_SOURCE` | 事件来源入口 | 空 |
| `AGENT_TRACE_ENABLED` | 采集总开关 | `true` |
| `AGENT_TRACE_DEBUG` | 导出器异常是否向上抛 | `false` |
| `AGENT_TRACE_REDACT` | 脱敏总开关 | `true` |
| `AGENT_TRACE_REDACT_CONTENT` | 脱敏 `step.content` | `true` |
| `AGENT_TRACE_REDACT_ARGUMENTS` | 脱敏 `tool_calls.arguments` | `true` |

> OpenCode JS SDK 另用一组环境变量：`AGENT_TRACE_OUTPUT`（输出 JSON 路径）、
> `AGENT_TRACE_AGENT_ID`、`AGENT_TRACE_APP_NAME`、`AGENT_TRACE_DEBUG` 等，
> 见 `integrations/opencode/README.md`。

> 注意：环境变量里的 `user_id` / `app_id` / `app_name` / `system_code` 属于
> **不可信默认值**，会被服务端可信身份覆盖（见「可信身份」）。它只适合单机 /
> 单用户场景，不适合「一个 agent 多人用」。

## 可信身份（user_id / app_id / app_name / system_code）

一个 agent 常被多人共用，因此 `user_id` 这类业务标识**不能**只靠环境变量或
客户端自报——那是「谁调用的进程 / 谁填的字段」，不是「真正在用的人」。SDK 把
业务标识分成两类，并按「信任优先级」解析：

| 字段 | 类型 | 凭证能否解析 | 建议来源 |
| :--- | :--- | :--- | :--- |
| `user_id` | 身份属性 | ✅ | 服务端鉴权（工号） |
| `app_id` / `app_name` | 身份属性 | ✅ | 服务端凭证 / 应用注册 |
| `system_code` | 身份属性 | ✅ | 服务端凭证（S 码） |
| `source` | 请求属性 | ❌ | 客户端上报 / 服务端按入口推导 |

**解析优先级**（越靠前越可信，`identity.resolve_identity`）：

    1. trusted   —— 服务端鉴权 / 网关注入 / 已解出的凭证（可信）
    2. explicit  —— 代码里显式传入（如 tracer.turn(user_id=...)）
    3. reported  —— 客户端 / 前端上报的原始值（不可信，兜底）
    4. default   —— 环境变量 / Tracer 级默认值

`source` 不走 trusted 层——凭证解不出它，所以 trusted 里给的 `source` 会被忽略。

### 用法一：按轮注入可信身份（推荐）

```python
# 服务端在请求入口用凭证解出身份，再传给 turn
with tracer.turn(
    turn_id=tid, session_id=sid,
    trusted_identity={"user_id": "26037050", "app_id": "App_X", "system_code": "IDM"},
) as turn:
    ...
```

### 用法二：用 user_resolver 钩子自动解析

适合 Web / 服务：在请求入口 set 一个 contextvar 或从当前请求取用户，turn 创建时
自动解析，无需每处传参。

```python
tracer = Tracer(
    agent_id="agent-1",
    user_resolver=lambda: {"user_id": current_request_user()},
)
```

钩子抛异常不影响采集（会静默回落）；`debug=True` 时向上抛。

### 审计：每个字段来自哪一层

默认每条 trace 会写 `metadata.identity_provenance`，记录每个字段的
`origin`（trusted / explicit / reported / default）与 `trusted` 布尔值；若身份属性
有值但不可信，另写 `metadata.untrusted_identity_fields`。服务端据此判断
「这条 trace 的身份到底可不可信」。`tracer.turn(..., record_identity_provenance=False)`
可关闭。

### DeerFlow 集成

中间件会从 `runtime.context` 读可信身份并注入——顶层同名键
（`user_id` / `app_id` / `app_name` / `system_code`）或 `trusted_identity` 子字典
（子字典优先）。`source` 不从 context 注入，仍由 config / 显式传入决定。

```python
# 调用 LangGraph 时把身份放进 configurable/context
graph.invoke(inputs, config={"configurable": {"thread_id": sid, "user_id": "26037050"}})
```

## 脱敏

默认**开启**：在交给导出器之前，按规则擦除 `step.content` 与
`tool_calls[].function.arguments` 里的敏感信息，命中片段替换为
`[REDACTED:规则名]`（保留键名，便于排查哪类泄露过）。零第三方依赖，结果确定可复现。

三层防线，前一层兜住后一层认不出的：

1. **敏感键名**：工具参数里 key 命中 `password` / `token` / `api_key` /
   `authorization` 等时，其值**整段**替换（不看值的形态）。结构化凭据
   `{"password": "hunter2"}` 不符合任何前缀特征，只有这一层能兜住；值是
   dict/list 时连子结构一起擦除。
2. **文本正则**：API key（`sk-` / `apikey-` / Langfuse `lsv2_pt_` 等）、
   `API_KEY=` / `"password": "..."` 等凭据赋值（含 JSON 风格）、JWT、
   URL 内嵌凭据（`postgres://user:pass@host`）、邮箱、手机号、身份证、银行卡号。
3. **高熵串**：没有已知前缀、但字符分布像随机密钥的长 token（hex 与
   base64/alnum 分别设阈值，并要求同时含字母和数字，不误伤普通长单词）。

> 键名判定按分隔符切词（`MYSQL_PASSWORD` / `auth_token` / `apiKey` 都能命中），
> 但 `max_tokens` / `token_count` / `password_length` 这类**计数/描述键**会豁免。

```python
from agent_trace import Tracer, Redactor, RedactionRule, EntropyRule, default_rules

# 整体关闭
Tracer(agent_id="a", redact=False)

# 只脱敏 content，保留工具参数
Tracer(agent_id="a", redact_arguments=False)

# 关闭「按敏感键名整段擦除」，只跑文本规则
Tracer(agent_id="a", redactor=Redactor(redact_keys=False))

# 追加自定义敏感键名（与内置键名同等判定）
Tracer(agent_id="a", redactor=Redactor(sensitive_keys=["employee_no", "id_card_no"]))

# 自定义规则（在默认规则基础上追加）
rules = default_rules() + [RedactionRule.build("internal_id", r"EMP-\d{6}")]
Tracer(agent_id="a", redactor=Redactor(rules))

# 调整高熵阈值（熵越低越激进，越长越保守）
Tracer(agent_id="a", redactor=Redactor(default_rules()[:-1] + [EntropyRule(generic_entropy=4.0)]))
```

## 说明

传输层（HTTP/Collector）与存储还在演进，当前先跑通「采集 → 结构化 → 导出」链路。
`exporters/` 是插件式设计，后续接入 Collector 只需新增一个导出器。
