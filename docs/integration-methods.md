# 接入方式总览与示例

> 最后更新：2026-10-08
> 对应版本：agent-trace-sdk v0.7.0
> 参考：阿里云 AgentLoop《选择 AI Agent 接入方式》
> https://help.aliyun.com/zh/agentloop/select-the-ai-agent-integration-method
> 与《接入 LangChain & LangGraph 应用》
> https://help.aliyun.com/zh/agentloop/access-the-langchain-langgraph-application-1


---

## 1. 三种接入方式

| 方式 | 适用场景 | 改动量 | 自动化程度 |
| :--- | :--- | :--- | :--- |
| ① 手动埋点 API | 任意框架、自研 Agent，需要精确控制每一步 | 每个 step 一行调用 | 半手动 |
| ② 装饰器 `@tracer.trace` / `@tracer.step` | 已函数化、边界清晰的一轮 / 一步 | 加一个装饰器 | 半自动（自动出入参） |
| ③ 框架集成（DeerFlow / LangGraph 中间件） | 跑在 DeerFlow / LangGraph 上的 Agent | **零业务改动**，仅注册插件 | 全自动 |
| ④ 框架集成（OpenCode JS SDK） | 跑在 OpenCode 上的 Agent | 装一个采集插件（进程内归一，直接出 JSON） | 全自动 |

- ① 由 `src/agent_trace/tracer.py` 提供，其中 `with turn.step(...)` 是它的自动计时写法
  （记录该步起止时间与 `latency_ms`），不是独立的接入方式。
- ② 同样由 `tracer.py` 提供，可和 ① 在同一轮里混用。
- ③ 由 `src/agent_trace/integrations/deerflow.py` 提供，以 LangGraph 中间件形式
  自动把消息流还原成 step，不改业务执行代码。
- ④ 由 `integrations/opencode/` 提供（JS SDK）：OpenCode 侧订阅事件流，**进程内**
  按契约归一，直接写本地 JSON（见第 5.1 节）。

---

## 2. 与阿里云 AgentLoop 三条管道的对比

阿里云接入中心提供三种方式：AI 系统运行时日志（eBPF）、LoongSuite Pilot、
语言与框架探针。与本项目最可比的是**语言与框架探针**（`docs/probe-pipeline-comparison.md`
里分析的"第三种管道"）。

| 维度 | 阿里云语言与框架探针 | 本项目 |
| :--- | :--- | :--- |
| 接入形态 | 进程内注入，`aliyun-instrument python app.py` | 代码内调用 API，或注册中间件 |
| 是否改代码 | 零代码，但要**改启动方式 + 重启应用** | 手动模式写几行；DeerFlow 中间件零业务改动 |
| 框架覆盖 | 开箱覆盖 LangChain / LangGraph / AgentScope、Python / Node.js | 现成只有 DeerFlow；其他框架需手动埋点或自写适配器 |
| 采集内容 | Chain / Agent 链路、LLM 调用、工具调用、RAG、图节点状态 | system prompt、用户输入、推理、工具调用/返回、最终回答、token 增量、span 树 |
| 原生 Turn / Step 标识 | 探针拿不到框架内部 Turn / Step；`session_id` / `user_id` 靠 config metadata 传 | 以业务方传的 `turn_id` / `session_id` 为**一等主键**，轮次边界明确 |
| 可观测 / 审计 / 评估 | 可观测 ✅ 评估 ✅ 审计 ❌（官方标注未开放） | 采集 + 导出/上报；看板与审计需自建 |
| 数据归属 | 出域上云（ARMS / AgentLoop，按量计费、绑地域） | 自持（JSON / JSONL / XLSX / HTTP → 自建 Collector + CK） |
| 运行环境 | Linux / macOS / Windows 主机及容器，需重启应用 | 无额外要求，纯 Python 库 |

**结论**：本项目**不止手动埋点**——装饰器与中间件分别提供函数级自动采集和
零业务代码的框架级自动采集。真正的差距是：本项目没有**零代码的进程注入**，
也没有开箱覆盖 LangChain / LangGraph / AgentScope 之外的框架，换框架要自写适配器。
换到的是：`turn_id` / `session_id` 原生主键、可审计语义、数据自持、
契约语言无关（`schema/trace.schema.json`）——这些正是探针给不了的。

---

## 3. 接入方式①：手动埋点 API

最通用，适合任意框架或自研 Agent，逐步精确控制。**完整可运行示例见
`examples/manual_api.py`。**

```python
from agent_trace import JsonExporter, JsonlExporter, MemoryExporter, Tracer, XlsxExporter

memory = MemoryExporter()
tracer = Tracer(
    agent_id="agent-demo-1",
    app_name="news-daily",
    exporters=[
        memory,
        JsonExporter("output/traces.json"),
        JsonlExporter("output/steps.jsonl"),
        XlsxExporter("output/steps.xlsx"),
    ],
)

with tracer.turn(turn_id="turn-demo-1", session_id="sess-demo-1", turn_no=1) as turn:
    turn.record_system_prompt("你是新闻助手。")
    turn.record_user_query("查看今日 AI 新闻热点")
    turn.record_reasoning("先调用搜索引擎拿到今日 AI 新闻……")

    # 多个工具调用合并为 1 个 tool_call step
    turn.record_tool_call([
        ("web_search", {"count": 8, "query": "AI 新闻 头条", "timeRange": "OneDay"}),
        ("web_search", {"count": 6, "query": "大模型 融资 今日", "timeRange": "OneDay"}),
    ])
    for call in turn.steps[-1].tool_calls:
        turn.record_tool_response(tool_call_id=call.id, content='{"success": true}')

    turn.record_final_answer("今日 AI 要闻速递：……")

print("steps:", len(memory.traces[0].conversations))
```

要点：

- 退出 `with` 时 SDK 自动分配 `step_no` / `message_id`、校验成 `Trace`、交给导出器。
- `record_tool_call` 支持 `("name", args)` 元组、`{"name":..., "arguments":...}`、
  OpenAI 风格完整对象，以及它们的列表。
- `tool_name` 与 `parent_span_id` 由 SDK 按 `tool_call_id` 自动回填，无需手传。
- 异常路径也会落一条终止 step，并把 Trace 级 `completed` 置为 `false`。

### 需要自动计时时用 `with turn.step(...)`

同一套手动 API 的语法糖：进入 / 退出 `with` 自动记录该步的 `start_time` / `end_time` /
`latency_ms`，其余与上面的 `record_*` 完全等价，可混用。

```python
from agent_trace import MemoryExporter, Tracer

memory = MemoryExporter()
tracer = Tracer(agent_id="agent-demo-1", app_name="demo", exporters=[memory])

with tracer.turn(turn_id="t-ctx", session_id="s-1", turn_no=1) as turn:
    turn.record_user_query("帮我查杭州天气")

    with turn.step("reasoning") as step:          # 自动记录 start/end/latency_ms
        step.set_content("先确定城市，再调用天气工具")

    with turn.step("tool_call") as step:          # 返回值按工具调用规格解析
        step.set_content({"name": "get_weather", "arguments": {"city": "杭州"}})

    call_id = turn.steps[-1].tool_calls[0].id
    turn.record_tool_response(tool_call_id=call_id, content='{"success": true}')

    with turn.step("final_answer") as step:
        step.set_content("杭州：小雨 20℃")

for step in memory.traces[0].conversations:
    print(step.step_no, step.message_type.value, "latency_ms=", step.latency_ms)
```

- `step.set_content(...)` 记录该步产出：`tool_call` 步骤按工具调用规格解析，
  其余按文本处理。
- 步骤内抛异常时异常继续向外抛，且不落到 step 字段（Step 级无 error / completed），
  Trace 级 `completed` 由 `Turn.__exit__` 统一置 `false`。

---

## 4. 接入方式②：装饰器

代码已函数化时，用装饰器把「整函数 = 一轮」或「整函数 = 一步」自动化。
**完整示例见 `examples/decorator.py`。**

```python
from agent_trace import MemoryExporter, Tracer

memory = MemoryExporter()
tracer = Tracer(agent_id="agent-demo-1", app_name="demo", exporters=[memory])

@tracer.step("reasoning")
def plan(question: str) -> str:
    return "拆解问题：" + question

@tracer.step("tool_call")
def search(keyword: str) -> dict:
    # tool_call 步骤需返回工具调用规格：name + arguments
    return {"name": "web_search", "arguments": {"query": keyword, "count": 5}}

@tracer.trace                                   # 整次调用 = 一个 turn
def answer(question: str) -> dict:
    plan(question)
    search(question)
    return {"answer": "最终回答"}

answer("2026 世界人工智能大会的主题是什么",
       turn_id="turn-demo-2", session_id="sess-demo-1", turn_no=2)

print("steps:", len(memory.traces[0].conversations))
```

要点：

- `@tracer.trace` 自动把函数入参落成 `user_query`、返回值落成 `final_answer`；
  `turn_id` / `session_id` / `turn_no` 作为关键字参数传入（会被消费，不传给原函数）。
- `@tracer.step(msg_type)` 把函数执行记成当前 turn 的一步，自动计时、自动落返回值。
- **contextvars 不跨线程**：在线程池 / 子线程里执行时 `get_current_turn()` 返回 `None`，
  埋点会被跳过（`debug=True` 时告警）。需要线程内采集请显式传 `Turn`，或在线程入口用
  `contextvars.copy_context().run(...)` 带上 Context。asyncio 子任务会自动继承。

---

## 5. 接入方式③：框架集成（DeerFlow / LangGraph 中间件）

LangGraph 系框架（含 DeerFlow）用**插件 + 中间件**接入，不改业务代码。
**完整示例见 `examples/deerflow_integration.py`。**

### 方式 A（推荐）：插件注册

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

### 方式 B：手动挂中间件

```python
from agent_trace import MemoryExporter, Tracer
from agent_trace.integrations.deerflow import AgentTraceMiddleware

tracer = Tracer(agent_id="agent-demo-1", app_name="deer-flow", exporters=[MemoryExporter()])
middleware = AgentTraceMiddleware(tracer)

# 挂到自己的 agent 上：create_agent(model=..., tools=[...], middleware=[middleware])
```

自动映射规则：

| DeerFlow / LangChain 消息 | → step |
| :--- | :--- |
| `SystemMessage` | `system_prompt`（随 thread 固定，每轮采一次） |
| `HumanMessage`（真实用户输入） | `user_query`（同轮第 2 条起自动标 `is_follow_up`） |
| `HumanMessage`（框架注入，带 `hide_from_ui`） | 不算 `user_query`，记为 `reasoning`，来源标签记在 `metadata.deerflow_injected` |
| `AIMessage.content`（带 `tool_calls`） | `reasoning` |
| `AIMessage.content`（收尾那条） | `final_answer` |
| `AIMessage.tool_calls`（多个） | 合并为 **1 个** `tool_call` step |
| `ToolMessage` | `tool_response`（带 `tool_call_id`） |

要点：

- `turn_id` 取 DeerFlow 的 `run_id`，`session_id` 取 `thread_id`；
  采集范围覆盖 lead agent 与 subagent（`AgentScope.BOTH`）。
- **澄清续跑自动合并**：`ask_clarification` 中断后用户回答会另起 `run_id`，
  中间件并入同 session 的上一轮，保证"提问 → 反问 → 澄清 → 完成"是一条完整 trace。
- 无 langchain 的环境下本模块仍可 import（中间件退化为普通类，便于纯逻辑单测）。

### 5.1 接入方式④：OpenCode（JS SDK，进程内归一，直接出 JSON）

OpenCode 运行时是 Node/Bun，插件只能用 JS/TS 写，Python SDK 进不去那个进程。
因此 OpenCode 侧提供一个**JS SDK**（`integrations/opencode/`）：订阅 OpenCode 事件流，
**进程内**把一轮交互归一成契约 `Trace` / `Step`，像 DeerFlow 的 `config.output` 一样
直接写本地 JSON——不需要接收端，也不依赖在线服务：

- **插件入口**：`integrations/opencode/index.js`（目录形态入口，OpenCode 自动加载）
  → `agent-trace.js`，用 `ctx.event.subscribe()`
  订阅事件流并喂给采集器（退出时兜底收尾）。
- **归一**：`session-collector.js`（事件 → 轮次/步）、`trace-builder.js`（契约层）、
  `exporters.js`（JSON / JSONL 落盘）。安装见 `integrations/opencode/README.md`。

> **为什么是订阅事件而不是读库**：旧方案读 OpenCode 的 SQLite 再上报服务端归一。
> 实测 opencode 2.0.26 发现官方事件通道可用（`session.reasoning.*` / `session.text.*`
> / `session.tool.*` / `session.step.ended` / `session.execution.*`），于是改为订阅
> 事件流：实时增量、不依赖库表结构、无需常驻接收端。
> 订阅要点：`ctx.event.subscribe()` 返回带 `Symbol.asyncIterator` 的事件流，必须自己
> 反复 `.next()` 泵取（回调式不触发，`for await` 会被插件编译器拒绝）。

```bash
# 从 js-sdk 分支一键安装（OpenCode 自带命令，自动写入全局配置）
opencode plugin add github:pfy0/agent_trace_sdk#js-sdk

# 配置输出路径后正常跑 OpenCode；每轮结束写一条 trace
AGENT_TRACE_OUTPUT=outputs/opencode_traces.json
```

> JS SDK 以独立 npm 包发布在 `js-sdk` 分支，包根即包本身。
> 仓库内 `integrations/opencode/` 为开发副本，两者同步维护。

要点：

- **字段严格对齐 schema**：序列化丢弃空值，对齐 Python `serializable()`；工具成败
  复用统一口径（与 DeerFlow 同一套两层判定）。JS 输出可直接过 Python
  `Trace.model_validate`（`extra="forbid"`），由 `tests/test_opencode_js_sdk.py` 守护。
- `turn_id` 自动生成为 `opencode:<sessionID>:<turn_no>`，`session_id` 取 OpenCode 的
  `sessionID`；每条 user 消息 + 其后的 assistant 归为一个 turn。
- **非阻塞 / fail-open**：事件旁路泵取，逐事件处理；单条脏事件不打断整条流。
- 契约语言无关：schema 是唯一真源，Python 侧的 `schema/trace.schema.json` 可直接校验
  JS 输出。
前面三种方式采完的 trace，都可以通过 `HttpExporter` 非阻塞上报
（后台线程组批、指数退避重试、失败落盘 spool、进程重启后可补发）：

```python
from agent_trace import HttpExporter, Tracer

exporter = HttpExporter(
    "http://collector:8080/api/traces",
    headers={"Authorization": "Bearer <token>"},
    batch_size=20,
    flush_interval=5.0,
    spool_dir="spool/",          # 失败落盘目录；None 表示失败即丢弃
)
tracer = Tracer(agent_id="agent-demo-1", exporters=[exporter])

with tracer.turn(turn_id="t-http", session_id="s-1") as turn:
    turn.record_user_query("你好")
    turn.record_final_answer("好的")

exporter.flush()
exporter.close()
```

上报体为 `{"traces": [ ... ]}`，每个元素是一条 `Trace.serializable()`。
`exporter.stats` 可查 `{"sent", "dropped", "spooled"}`。

---

## 6. 选型建议

- **跑在 DeerFlow / LangGraph 上** → 直接用方式③，零业务改动，优先。
- **自研 Agent / 其他框架** → 方式① 为主（需要耗时时用 `with turn.step(...)`），
  函数化边界清晰处配合方式② 减样板；契约语言无关，其他语言可照
  `schema/trace.schema.json` 实现上报。
- **要审计语义、数据自持、原生 turn 主键** → 本项目；这条阿里云探针给不了。
- **只想开箱观测、不想维护看板 / 存储、且栈全在探针支持范围内** → 阿里云探针更省事
  （详见 `docs/probe-pipeline-comparison.md` 的"窄场景"结论）。
