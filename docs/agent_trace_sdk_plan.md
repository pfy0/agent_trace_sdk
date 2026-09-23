# Agent 运行轨迹采集 SDK — 现状与待办

> 状态：与代码对齐（v0.4.0；本版为契约破坏性变更：Step 去字段 + 契约收紧）
> 范围：Python 优先（`requires-python >= 3.9`）
> 最后更新：2026-09-23
>
> 本文取代此前的《实施计划 v0.3》草案。草案里 `trace_id`、`data/`、`output/`、
> `extract_steps.py`、`sanitize.py`、`exporters/otlp.py`、`integrations/openai.py`、
> xlsx 的 `split_tool_calls` 开关等内容，要么已被实现替代，要么尚未落地，
> 差异统一记在「与早期草案的差异」一节。

---

## 1. 这是什么

业务方 agent 以最小改动接入后，SDK 自动捕获**一轮交互（turn）**的完整轨迹：
系统提示词、用户输入、推理过程、工具调用、工具返回、最终回答，结构化后导出或上报。

- **Trace**：一次 agent 运行 = 一个 turn = 一轮交互，是采集/校验/上报的基本单位。
- **Step**：trace 中的一步，即 `conversations` 数组里的一条消息。
- 轮次边界由 `turn_id` 决定；`turn_id`、`session_id`、`turn_no`（会话内第几轮）
  均由业务方传入（`turn_id` / `session_id` 缺省时 SDK 生成，见第 3 节）。

一个 trace 只承载一轮交互，不做跨 turn 聚合；`session_id` 仅用于关联同一会话的多个
trace。同一 `turn_id` 下允许多条 `user_query`：第一条是初始输入，其余标记
`is_follow_up=True` 作为追问/补充，**不做切分**。

---

## 2. 现状：代码结构

```
src/agent_trace/
├── schemas.py        # 契约层：枚举、Trace/Step 模型、校验、id 生成
├── tracer.py         # 采集层：Tracer / Turn / 手动埋点 / 装饰器
├── context.py        # contextvars：当前 turn，支持 async / 并发
├── config.py         # 配置：环境变量 + 代码初始化
├── ingest.py         # 离线导入：多轮数据按 turn_id 切分（不改动入参）
├── integrations/
│   └── deerflow.py   # DeerFlow / LangGraph 中间件（自动采集轨迹）
├── transport/        # HTTP 上报：非阻塞队列 / 重试 / 失败落盘补发
│   ├── client.py     # HttpTransport、UrllibPoster、HttpxPoster、TransportError
│   ├── queue.py      # BatchQueue：非阻塞入队 + 后台 flush + spool 自动补发
│   ├── spool.py      # SpoolStore：失败批次落盘
│   └── exporter.py   # HttpExporter：把 trace 非阻塞送入队列
└── exporters/        # json / jsonl / xlsx / base(memory)
examples/             # 5 个可运行示例
tools/release.py      # 一键构建 / 打版本 tag
tools/export_schema.py# 导出语言无关 JSON Schema
schema/               # trace.schema.json、enums.json（由 tools/export_schema.py 生成）
docs/                 # 本文 + distribute.md（安装与发布）
```

### 2.1 契约层 `schemas.py`

枚举与映射：

| `message_type` | 缩写 | role | 说明 |
| :--- | :--- | :--- | :--- |
| `system_prompt` | sys | system | 系统提示词与工具定义 |
| `user_query` | usr | user | 用户输入或反馈 |
| `reasoning` | rsn | assistant | 思考、规划、调度等推理过程 |
| `tool_call` | cal | assistant | 发起工具调用 |
| `tool_response` | res | tool | 工具返回结果 |
| `final_answer` | ans | assistant | 最终交付回答 |

`message_id` 格式 `msg_{类型缩写}_{序号}`，序号为**类型内自增**；校验**只查格式**，
不查序号连续性（历史数据存在按全局位置编号的情况）。

`Step` 字段（与公司《轨迹数据规范》对齐，共 17 个）：`message_id`、`step_no`、
`role`、`message_type`、`content`、`start_time`、`end_time`、`latency_ms`、
`span_id`、`parent_span_id`、`tool_calls`、`tool_call_id`、`tool_name`、
`tool_success`、`prompt_tokens_delta`、`completion_tokens_delta`、`is_follow_up`。

> 规范外的保留项：`latency_ms`、`is_follow_up`。
> `timestamp` 已按要求换成 `start_time` / `end_time`。
> **Step 不再有 `completed` / `partial` / `error` / `meta`**：结束状态只在 Trace 级，
> 扩展信息统一走 Trace 级 `metadata`。`Step` / `Trace` / `ToolCall` / `Config`
> 全部 `extra="forbid"`，规范外字段直接拒绝（`Trace.from_raw()` 导入历史数据时
> 会按模型字段过滤，不因此报错）。

`Trace` 字段（与公司《轨迹数据规范》逐项对齐，共 27 个）：

- 标识 / 时间：`id`（入库生成）、`turn_id`、`turn_no`、`session_id`、
  `start_time`、`end_time`、`create_time`（入库生成）
- 业务标识：`app_name`、`app_id`、`system_code`、`user_id`、`source`、
  `agent_id`、`parent_agent_id`
- 会话内容：`conversations_type`、`conversations`
- 结束状态：`completed`、`partial`
- 统计：`api_calls`、`toolsets_used`、`tool_stats`、`tool_error_counts`、`token_usage`
- 扩展：`metadata`、`extra_info`

> 已移除的字段：
> `input_tokens` / `output_tokens` / `model` / `tags`、
> `agent_name` / `agent_version` / `env`（由 `agent_id` / `app_name` 等取代）、
> `meta`（trace 级改名为 `metadata`；**Step 级 `meta` 已移除**）、
> `started_at` / `ended_at`（改名为 `start_time` / `end_time`）。

结束状态：Trace 级 `completed`（该轮是否真正结束）与 `partial`（是否因超时等外部
条件截断）取代了早期的单一 `status`。**结束状态只在 Trace 级**，Step 级不再有。

校验规则：

- `conversations` 非空，且至少含一条 `user_query`。
- 首步必须是 `system_prompt` 或 `user_query`。
- `step_no` 必须从 1 起连续递增。
- `message_id` 格式合法。
- `role` 与 `message_type` 一致。
- `message_type=tool_call` 时 `tool_calls` 必填非空。
- `message_type=tool_response` 时 `tool_call_id` 必填。
- `is_follow_up` 只能出现在 `user_query` 上。

`Trace.from_raw()` 兼容 `conversations` / `conversation` / `messages` 三种数组键名，
缺 `step_no` 时按数组顺序补 1..N；缺 `turn_no` / `completed` / `partial` /
`conversations_type` 时取默认值（1 / true / false / `openai`）。

> `Trace.from_raw()` 会按模型字段过滤入参：规范外的 trace 级 / step 级字段
> （如 `status`、Step 级 `meta`）在导入时被静默丢弃，以兼容历史数据。

### 2.2 采集层 `tracer.py`

```python
with tracer.turn(turn_id="t-1", session_id="s-1") as turn:
    turn.record_user_query("查看今日 ai 新闻热点")
    turn.record_reasoning("先调用搜索引擎……")
    turn.record_tool_call([("web_search", {"count": 8}), ("web_search", {"count": 6})])
    for call in turn.steps[-1].tool_calls:
        turn.record_tool_response(tool_call_id=call.id, content='{"success": true}')
    turn.record_final_answer("今日 AI 要闻速递：……")
```

- 退出 `with` 时自动分配 `step_no` / `message_id`、校验成 `Trace` 并交给每个
  导出器；异常路径会落一条终止 step，并把 Trace 级 `completed` 置为 `false`。
- `tracer.turn(turn_no=...)` 传入会话内轮次；`completed` / `partial` 缺省取
  `config.default_completed` / `config.default_partial`。
- 手动 API：`record_system_prompt` / `record_user_query(follow_up=)` /
  `record_reasoning` / `record_tool_call` / `record_tool_response` /
  `record_final_answer` / `record_error`。
- `record_tool_call` 入参形态：`("name", arguments)` 元组、`{"name":..., "arguments":...}`、
  工具名字符串、OpenAI 风格完整对象，以及它们的列表；也支持
  `record_tool_call(name="web_search", arguments={"count": 8})` 简写。
  多个调用**合并为 1 个 step**。
- `with turn.step(message_type)` 自动计时并写 `latency_ms`；`turn.step(...)` 的
  `content` 通过 `set_content()` 回填。
- `@tracer.trace`（整次调用 = 一个 turn）与 `@tracer.step(message_type)`（函数 = 一步）。
- **提交是旁路**：`commit()` 组装失败（例如没有任何 `user_query`）时默认只记
  warning 并丢弃该 trace，不把 `ValidationError` 抛给业务方；`config.debug=True`
  时才向上抛。

### 2.3 先后顺序、工具关联与 span 树

`conversations` 是一维数组，先后顺序由 `step_no` 表达；工具调用与返回用
`tool_calls[].id` <-> `tool_call_id` 关联。

**span 树（规则 B，全自动）**：每个 step 分配 `span_id`；`tool_call` 是父，
指向它的 `tool_response` 是子——`parent_span_id` 由 SDK 顺着 `tool_call_id`
自动回填，业务方无需传，也不需要 `turn.span()`：

```
step 3  tool_call       span=span_A   parent=null
step 4  tool_response   span=span_B   parent=span_A   (tool_call_id 指向 step 3 的调用)
step 5  tool_response   span=span_C   parent=span_A
```

- 多 agent 场景下每个 run 各自产出一条 trace，靠 `session_id` 关联；
  多 agent 的支持程度与已知限制见第 5 节待办。
- 需要额外归属信息时用 Trace 级 `metadata` 承载（如
  `metadata={"agents": ["researcher"]}`）；Step 已无 `meta` 字段。

### 2.4 上下文 `context.py`

`_current_turn` 一个 `ContextVar`。跨 context 调 reset 时按「尽力恢复」处理
（LangGraph 会把钩子调度到不同 context），不抛异常。

### 2.5 配置 `config.py`

优先级：显式传入 > 环境变量 > 默认值。

| 环境变量 | 字段 | 默认 |
| :--- | :--- | :--- |
| `AGENT_TRACE_AGENT_ID` | `agent_id` | 空 |
| `AGENT_TRACE_PARENT_AGENT_ID` | `parent_agent_id` | 空 |
| `AGENT_TRACE_APP_NAME` | `app_name` | 空 |
| `AGENT_TRACE_APP_ID` | `app_id` | 空 |
| `AGENT_TRACE_SYSTEM_CODE` | `system_code` | 空 |
| `AGENT_TRACE_USER_ID` | `user_id` | 空 |
| `AGENT_TRACE_SOURCE` | `source` | 空 |
| `AGENT_TRACE_ENABLED` | `enabled`（采集总开关） | `true` |
| `AGENT_TRACE_DEBUG` | `debug`（导出/校验异常是否上抛） | `false` |

另有 `default_completed=True`、`default_partial=False`、`exporters=None`。

`Config` 同样是 `extra="forbid"`：`Tracer(agent_id=..., apps_name=...)` 这类
写错的配置名会立即抛 `ValidationError`，不会静默失效。

### 2.6 导出层 `exporters/`

| 导出器 | 行为 |
| :--- | :--- |
| `Exporter` | 抽象基类，可选 `flush()` / `close()` |
| `MemoryExporter` | 留在内存，便于测试 |
| `JsonExporter` | 汇总为单个 JSON 数组，原子写（tmp + replace），按 `flush_every` 落盘 |
| `JsonlExporter` | 一行一个 step（带 trace 元数据），便于 jq / 灌 CK |
| `XlsxExporter` | 展平成一行一个 step 的 Excel（列：`step_no, message_id, role, message_type, content, start_time, end_time, latency_ms, span_id, parent_span_id, tool_calls, tool_call_id, tool_name, tool_success, prompt_tokens_delta, completion_tokens_delta`），依赖可选 `openpyxl` |
| `HttpExporter` | 见 2.7 |

### 2.7 传输层 `transport/`

- `export()` 只入队立即返回，网络 IO 全在后台线程，**不阻塞业务**。
- 按 `batch_size` / `flush_interval` 组批；`max_retries` 次指数退避重试。
- 队列满丢最旧并计数（`dropped`），宁可丢数据也不拖慢业务。
- 发送失败落盘 `spool_dir`（`SpoolStore`，超上限丢最旧），后台线程检测到网络
  恢复后**自动补发**（失败按 `flush_interval` 指数退避，上限 60s）。
- 传输依赖优先标准库（`urllib`），装了 `httpx` 自动启用。
- 统计：`stats` 暴露 `sent` / `dropped` / `spooled`。

### 2.8 DeerFlow 集成 `integrations/deerflow.py`

- 插件入口 `agent_trace.integrations.deerflow:install`，或手动
  `create_agent(middleware=[AgentTraceMiddleware(tracer)])`。
- `before_agent` 开一轮（`turn_id=run_id`、`session_id=thread_id`），
  `after_model` 增量映射消息，`after_agent` 收尾提交。
- 消息映射：`HumanMessage` → `user_query`（同轮第 2 条起追问）；
  带 `tool_calls` 的 `AIMessage.content` → `reasoning`；
  末条不带 `tool_calls` 的 AI 文本升级为 `final_answer`；
  `AIMessage.tool_calls` 合并为 1 个 `tool_call` step；`ToolMessage` → `tool_response`。
- 框架注入的 `HumanMessage`（`hide_from_ui`，如 memory 画像、动态上下文）记为
  `reasoning`，来源标签记在 Trace 级 `metadata.deerflow_injected`，不算 `user_query`。
- 澄清续跑（`ask_clarification` 中断后另起 `run_id`）**并入同一轮 trace**，
  `user_query` 标 `is_follow_up`，并置 Trace 级 `metadata.clarification_answer=true`；
  用户放弃澄清直接
  重新提问时，暂存的澄清轮会兜底提交，数据不丢。
- 无 `langchain` 时中间件退化为普通类，仍可 import 做纯逻辑单测。

### 2.9 离线导入 `ingest.py`

`split_by_turn(payload, session_id=None, **trace_meta)`：在任意嵌套结构里递归定位
step 列表（兼容 `conversations` / `conversation` / `messages`），按 `turn_id` 分桶成
多条 trace（并按出现顺序自动填 `turn_no`）；
同轮第 2 条起的 `user_query` 自动标 `is_follow_up`。**不修改调用方传入的数据。**

### 2.10 工具与示例

- `tools/release.py`：`--build-only` 本地出包 / `--tag` 打版本 tag 并推送。
- `tools/export_schema.py`：从 pydantic 模型导出 `schema/trace.schema.json` 与
  `schema/enums.json`。
- `examples/`：`manual_api.py`（最小闭环 + 三种导出）、`decorator.py`（装饰器与
  异常路径）、`import_data.py`（离线切分）、`deerflow_integration.py`
  （DeerFlow 中间件）。

### 2.11 发布

走 **git tag** 分发（不依赖私有 PyPI），业务方
`pip install "git+https://github.com/pfy0/agent_trace_sdk.git@v0.4.0"`。
详见 `docs/distribute.md`。

---

## 3. 已确认的契约决策

| 项 | 结论 | 落点 |
| :--- | :--- | :--- |
| 采集单位 | 一个 trace = 一个 turn，不做跨 turn 聚合 | `schemas.Trace` |
| 唯一标识 | 不引入独立 `trace_id`，`turn_id` 即 trace 标识 | `schemas.Trace` |
| `turn_id` / `session_id` | 由业务方传入；缺省时 SDK 生成（`turn_xxx` / `sess_xxx`） | `Tracer.turn()` |
| `turn_no` | 会话内第几轮，业务方传入，缺省 1 | `schemas.Trace` |
| 字段名 | 步骤数组用 `conversations`（复数，数组类型） | `schemas.Trace` |
| `conversations_type` | 会话格式标识，业务方传入，缺省 `openai` | `schemas.Trace` |
| 结束状态 | 用 `completed` + `partial` 取代单一 `status`；**仅 Trace 级**，Step 不再有 | `schemas.py` |
| 必填性 | 新字段都给安全默认值，不破坏既有调用 | `schemas.py` / `config.py` |
| 字段集 | Trace 级与公司《轨迹数据规范》逐项对齐（27 字段） | `schemas.Trace` |
| 智能体身份 | `agent_id` / `parent_agent_id` 取代 `agent_name` / `agent_version` / `env` | `config.py` |
| 时间字段 | `start_time` / `end_time` 取代 `started_at` / `ended_at` | `schemas.Trace` |
| 扩展元数据 | 只在 Trace 级，字段名 `metadata`；**Step 级 `meta` 已移除** | `schemas.py` |
| 入库字段 | `id` / `create_time` 由入库侧生成，SDK 留空 | `schemas.Trace` |
| 多轮数据 | 一个 trace 只收一轮；多轮切分只能按 `turn_id`（`split_by_turn`） | `ingest.py` |
| 同轮多条用户输入 | 不切分，第 2 条起标 `is_follow_up=True` | `schemas.Step` |
| `message_id` 序号 | 类型内自增；校验只查格式 | `schemas.py` |
| `tool_call` 粒度 | 一条消息 = 一个 step，多个调用合并进 `tool_calls` | `tracer.py` |
| Step 字段集 | 与规范对齐（17 字段）；`timestamp` 拆成 `start_time` / `end_time` | `schemas.Step` |
| span 树 | 规则 B：`tool_call` 为父、`tool_response` 为子，SDK 全自动 | `tracer.py` |
| `tool_name` / `tool_success` | `tool_name` 按 `tool_call_id` 自动回填；`tool_success` 取 -1/0/1 | `tracer.py` |
| Step 时间 | 有耗时则 `end_time = start_time + latency_ms`，否则 `end_time = start_time` | `tracer.py` |
| token 增量 | Step 级 `prompt_tokens_delta` / `completion_tokens_delta`（调 LLM 时才有） | `schemas.Step` |
| 耗时字段 | Step 只有 `latency_ms`；Trace 不设耗时字段 | `schemas.py` |
| 依赖策略 | 核心仅 `pydantic`；`xlsx` / `http` / `deerflow` 为 optional extras | `pyproject.toml` |
| 发布渠道 | git tag，不依赖私有 PyPI | `tools/release.py` |

---

## 4. 与早期草案的差异

草案 v0.3 的部分描述已不适用于当前代码：

| 草案描述 | 现状 |
| :--- | :--- |
| Trace 信封含 `trace_id` | 无 `trace_id`，用 `turn_id` |
| `session_id` 是否必传待定（Q17） | 必填，缺省由 SDK 生成 |
| Trace 级单一 `status` | 已换成 `completed` + `partial` |
| 步骤数组名 `conversation` | 已改名 `conversations` |
| 多轮对话是否切分待定（Q13） | 已定：不切分，同轮多 `user_query` 走追问标记 |
| 本仓库保留 `data/`、`output/`、`extract_steps.py` | 本仓库不含这些内容 |
| 规划 `sanitize.py`、`exporters/otlp.py`、`integrations/openai.py` | 尚未实现，见第 5 节 |
| `xlsx.py` 提供 `split_tool_calls` 开关 | 未实现；当前恒为「一条消息 = 一行」 |
| 超长内容外置 `content_ref` / 截断 | 未实现，见第 5 节 |
| Step 级 `status` | 已移除；结束状态只在 Trace 级 |

---

## 5. 待办

### 5.1 质量与工程

- [ ] **补测试**：仓库目前无 `tests/`，`pyproject.toml` 也没有 pytest 依赖；
      M0–M3 的交付判据（契约校验、异常路径、传输可靠性）都还没有自动化覆盖。
      计划覆盖：契约合法/非法样本、turn 生命周期与异常路径、
      contextvars 并发隔离、spool 落盘与补发、各导出器输出。
- [ ] `HttpExporter.close()` 目前只停后台线程，不 flush 队列里剩余的数据；
      进程退出（`atexit`）时可能丢尾部数据，需要补一次收尾 flush。
- [x] 契约已全部收紧为 `extra="forbid"`（`Step` / `Trace` / `ToolCall` / `Config`）。
      OpenAI 风格的 tool_call 常带 `index` 等附加键，`_as_tool_calls()` 与
      `Trace.from_raw()` 会先按模型字段过滤再校验。

### 5.2 多 agent 支持（已知限制）

当前按「一个 run = 一条 trace」采集，多 agent 场景下有两个已确认的问题：

- [ ] **子 agent 会被当成用户的新一轮**：DeerFlow 里子 agent 有自己的 `run_id`，
      中间件按 session 自增 `turn_no`，于是「一次用户提问 → 主 agent 调起子 agent」
      被拆成 2 条 trace（分别 `turn_no=1`、`turn_no=2`），用户视角的轮次统计失真，
      主 / 子 agent 的因果关系也丢失（只能靠相同 `session_id` 猜测）。
      需要决定：子 agent 的 trace 继承所属用户轮次的 `turn_no`，还是引入 trace 间的
      父子关系字段。
- [ ] **子 agent 内的步骤归属无法表达**：若多个子 agent 共用同一 `run_id` 且消息
      交错，`step_no` 只能给顺序、给不出分组。当前 span 规则 B 只能表达
      「工具调用 → 返回」，表达不了「哪些 step 属于同一个子 agent」，暂借
      Trace 级 `metadata`（如 `metadata={"agents": ["researcher"]}`）由业务方自标；
      若将来需要自动还原，再评估是否引入「tool_call 派生子 agent」的信号（原路线 C）。

### 5.3 加工层（合规与容量）

- [ ] 脱敏：密钥 / 手机号 / 邮箱 / 身份证等可配正则，`content` 与
      `tool_calls.arguments` 分开控制开关。
- [ ] 截断：普通字段与 input/output 字段分别设上限。
- [ ] 超长内容外置到对象存储，表内只留 `content_ref`，并标 `content_truncated`。
- [ ] 用户 / 租户级 opt-out。

### 5.4 上报落地

- [ ] Collector 服务（校验 / 补齐 / 限流）——由谁实现待定。
- [ ] ClickHouse 建表 DDL（草案见早期规划，需按当前字段重写：`turn_id` 取代
      `turn_id`，Step 含 `step_no` / `latency_ms` / `tool_success` 等）。
- [ ] 去重键与引擎选型（只追加则 `MergeTree` 系；需要补写则定版本列）。

### 5.5 适配与桥接

- [ ] `integrations/openai.py`：wrap OpenAI client，自动捕获请求 / 响应 / token 用量。
- [ ] `exporters/otlp.py`：给需要对接外部可观测平台（Langfuse / Phoenix 等）的场景
      做字段映射。
- [ ] 其余框架适配器按需扩展。

### 5.6 可选

- [ ] `xlsx.py` 的 `split_tool_calls` 开关：仅在需要与历史 Excel 逐格比对时再加。

---

## 6. 验证方式

```powershell
pip install -e ".[all]"

python examples/manual_api.py           # 6 步闭环 + JSON/JSONL/XLSX
python examples/decorator.py            # 装饰器与异常路径
python examples/import_data.py          # 多轮按 turn_id 切分
python examples/deerflow_integration.py # DeerFlow 中间件（无 langchain 时优雅退化）

python tools/export_schema.py           # 重新生成 schema/*.json
python tools/release.py --build-only    # 本地出包自检
```
