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
pip install "git+https://github.com/pfy0/agent_trace_sdk.git@v0.2.0"
```

- 需要：机器能访问 GitHub、装了 `git`、对仓库有读权限。
- 私有仓库还需带上凭证（SSH key 或 Token）：
  `pip install "git+ssh://git@github.com/pfy0/agent_trace_sdk.git@v0.2.0"`
- 装可选依赖（Excel 导出等）：
  `pip install "agent-trace-sdk[all] @ git+https://github.com/pfy0/agent_trace_sdk.git@v0.2.0"`

固定 tag 安装，版本可复现。完整说明（Token 配置、升级、离线 wheel、
DeerFlow/uv 安装）见 `docs/distribute.md`。

### v0.2.0 升级提示（不向后兼容）

v0.2.0 按公司《轨迹数据规范》改造了字段，从 v0.1.0 升级需改代码：

| v0.1.0 | v0.2.0 |
| :--- | :--- |
| `Tracer(agent_name=..., agent_version=..., env=...)` | `Tracer(agent_id=..., app_name=...)` |
| `trace["started_at"]` / `trace["ended_at"]` | `trace["start_time"]` / `trace["end_time"]` |
| `trace["meta"]` | `trace["metadata"]` |
| `trace["conversation"]` | `trace["conversations"]` |
| `trace["status"]` | `trace["completed"]` / `trace["partial"]` |
| `span_id` / `parent_span_id`（Step） | 恢复，改由 SDK 自动生成 / 自动填 |
| `input_tokens` / `output_tokens` / `model` / `tags` | 已移除（Step 级改用 `prompt_tokens_delta` / `completion_tokens_delta`） |
| `turn.span(...)` | 已移除（`span_id` / `parent_span_id` 改由 SDK 自动生成） |
| `trace["conversation"][i]["timestamp"]` | `start_time` / `end_time` |

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

退出 `with` 时 SDK 自动：分配 `step_no` / `message_id`、计算 `duration_ms`、
校验成 `Trace`、交给每个导出器。异常路径也会落一条终止 step 并把 `completed` 置为 `false`。

## 手动埋点 API

| 方法 | 说明 |
| :--- | :--- |
| `turn.record_system_prompt(content)` | 系统提示词与工具定义 |
| `turn.record_user_query(content, follow_up=False)` | 用户输入或反馈（`follow_up=True` 标记追问） |
| `turn.record_reasoning(content)` | 思考、规划、调度等推理过程 |
| `turn.record_tool_call(tool_calls)` | 发起工具调用（多个调用合并为 1 个 step） |
| `turn.record_tool_response(tool_call_id, content)` | 工具返回结果 |
| `turn.record_final_answer(content)` | 最终交付回答 |
| `turn.record_error(error)` | 落一条 error 终止 step |

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
- 需要额外归属信息（如"这一步由哪个子 agent 产出"）时，用 `meta` 自由扩展：
  `turn.record_reasoning("...", meta={"agent": "researcher"})`。

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
- 状态：`completed` / `partial` / `error`、`is_follow_up`、`meta`

> `step_no` 用 Integer（规范文档写的是 String，按实际实现取 Integer）。

Trace 字段（与公司《轨迹数据规范》对齐）：

- 标识与时间：`turn_id`、`turn_no`（会话内第几轮）、`session_id`、
  `start_time` / `end_time`（ISO 8601 UTC）、`id` / `create_time`（入库侧生成，SDK 留空）。
- 业务标识：`app_name`、`app_id`、`system_code`、`user_id`、`source`、
  `agent_id`、`parent_agent_id`。
- 会话内容：`conversations_type` 与 `conversations`（该轮全部 step 的数组）。
- 结束状态：`completed` / `partial`。
- 统计（可选）：`api_calls`、`toolsets_used`、`tool_stats`、`tool_error_counts`、
  `token_usage`。
- 扩展（可选）：`metadata`、`extra_info`；另有 `duration_ms` / `error` 两个便捷字段。

结束状态说明：`completed` = 该轮是否**正常跑完**（异常/中断为 `false`）；
`partial` = 是否因超时等**外部条件被截断**。两者都可为 true/false 组合，
取代了早期的单一 `status` 字段。

`message_id` 校验**只查格式、不查序号连续性**（历史数据存在类型内自增与全局位置两种编号）。

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
| `HumanMessage`（框架注入，带 `hide_from_ui`） | 不算 `user_query`，记为 `reasoning` 并标 `meta.deerflow_injected` |
| `AIMessage.content`（带 `tool_calls`） | `reasoning` |
| `AIMessage.content`（收尾那条） | `final_answer` |
| `AIMessage.tool_calls`（多个） | 合并为 **1 个** `tool_call` step |
| `ToolMessage` | `tool_response`（带 `tool_call_id`，失败时带 `completed=false`） |

- `turn_id` 取 DeerFlow 的 `run_id`，`session_id` 取 `thread_id`；同一 thread 的多轮
  run 各自产出一条 trace，共享 `session_id`。
- **澄清续跑自动合并**：`ask_clarification` 会中断 run，用户回答后 DeerFlow 另起一个
  `run_id` 继续。此时不新开 trace，而是并入同一轮的 trace（`user_query` 标
  `is_follow_up` + `meta.clarification_answer`），`turn_id` 沿用最初提问那轮，
  保证「提问 → 反问 → 澄清 → 完成」是一条完整 trace。若用户放弃澄清直接重新提问，
  暂存的澄清轮会被兜底提交，数据不丢。
- **框架注入的消息不记为用户输入**：带 `hide_from_ui` 的 `HumanMessage`（memory 画像、
  动态上下文提醒、待办提醒等）记为 `reasoning` 并标 `meta.deerflow_injected`
  （值为 `injected` / `dynamic_context`）。判定规则与 DeerFlow 自身的
  `_is_genuine_user_message` 一致：澄清答复虽带 `hide_from_ui`，但仍属真实用户输入。
- 初始 state 里携带的历史消息不会重复采集（以最后一条真实用户消息为界）。
- 无 langchain 的环境下本模块仍可 import（中间件退化为普通类，便于纯逻辑单测）。

也支持手动挂载：

```python
from langchain.agents import create_agent
from agent_trace.integrations.deerflow import AgentTraceMiddleware

agent = create_agent(model=..., tools=[...], middleware=[AgentTraceMiddleware(tracer)])
```

## 发布

当前用 **git tag** 分发（不依赖私有 PyPI）：

```bash
python tools/release.py --build-only     # 只出包，产物在 dist/
python tools/release.py --tag            # 构建 + 校验 + 打版本 tag 并推送
```

业务方固定 tag 安装即可复现版本。完整说明见 `docs/distribute.md`。

## 目录结构

```
src/agent_trace/
├── schemas.py       # 契约层：枚举、Trace/Step 模型、校验、id 生成
├── tracer.py        # 采集层：Tracer / Turn / 手动埋点 / 装饰器
├── context.py       # contextvars 上下文传播
├── config.py        # 配置：环境变量 + 代码初始化
├── ingest.py        # 离线导入与多轮按 turn_id 切分
├── integrations/    # 框架适配（deerflow.py）
├── transport/       # HTTP 上报：队列 / 重试 / 失败落盘
└── exporters/       # json / jsonl / xlsx / http / base
examples/            # 可运行示例
tools/release.py     # 一键构建 / 打 tag / 发布
schema/              # 语言无关的 JSON Schema 契约
```

## 示例

```bash
python examples/manual_api.py    # 手动埋点最小闭环 + 三种导出
python examples/decorator.py     # 装饰器与异常路径
python examples/import_data.py   # 导入现成数据、多轮切分
python examples/deerflow_integration.py  # DeerFlow 中间件集成
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

## 说明

传输层（HTTP/Collector）与存储还在演进，当前先跑通「采集 → 结构化 → 导出」链路。
`exporters/` 是插件式设计，后续接入 Collector 只需新增一个导出器。
