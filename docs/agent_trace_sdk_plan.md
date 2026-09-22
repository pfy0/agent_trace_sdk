# Agent 运行轨迹采集 SDK — 实施计划

> 状态：草案 v0.3
> 范围：Python 优先
> 最后更新：2026-09-21

---

## 1. 背景与目标

### 1.1 背景

当前项目已完成轨迹数据的**离线导出**能力（`extract_steps.py`）：把一段
`{"messages": [...]}` 的会话记录，按既定的 `message_type` 映射规则展平成 Excel 表，
一行一个步骤。这本质上已经是这套 SDK 的**第一个导出器**，其中的字段清单和枚举映射
就是 SDK 的核心契约。

下一步目标是把这件事从"事后拿一份 JSON 手工跑脚本"，升级为**运行期自动采集**。

### 1.2 目标

构建一个 Python SDK，让业务方的 agent 以最小改动接入后，能够：

1. **自动捕获**一个 turn 的完整轨迹：系统提示词、用户输入、推理过程、工具调用、
   工具返回、最终回答。
2. **结构化**成稳定的 schema，区分 trace（一个 turn）与 step（一个步骤）两个层级。
   一个 trace **只承载一轮交互**，不做跨 turn 聚合。
3. **可靠上报**到自建采集服务，最终落入 ClickHouse，供数据飞轮使用。
4. **可插拔导出**到多种下游：HTTP/Collector（主）、JSONL、Excel、OTLP（可选）。

**采集单位**：一个 trace = 一个 turn = 用户与 agent 的一次交互。该轮的全部步骤数据
放在 `conversation` 数组里（见 2.3），随 trace 一起采集与上报。

### 1.3 非目标（本期不做）

- 不做可视化 UI（查询与展示由下游平台负责）。
- 不做模型评测 / LLM-as-a-judge（Cozeloop、Langfuse 那类能力）。
- 不做 prompt 版本管理。
- 不做跨 turn 的会话级聚合：SDK 只产出 turn 粒度的 trace。
- 不追求兼容所有 agent 框架，只覆盖实际在用的。

---

## 2. 核心概念

### 2.1 术语与两层数据模型

| 术语 | 含义 | 对应现状 |
| :--- | :--- | :--- |
| **Turn** | 用户与 agent 的一次交互：从该轮的 `user_query` 开始，到 `final_answer` 或异常终止为止 | 现有 `{"messages": [...]}` 中的一段 |
| **Trace** | 一次 agent 运行的完整轨迹，**与一个 turn 一一对应**，是采集、校验、上报、入库的基本单位 | 现有 JSON 顶层内容 + 运行级元数据 |
| **Step** | 轨迹中的一步，即 `conversation` 里的一条消息 | 现有 Excel 的一行 |
| **Session** | 一次连续会话，可包含多个 turn（即多个 trace），靠 `session_id` 关联 | 现有数据中未体现 |

一个 trace 包含 N 个 step，按 `step_no` 从 1 递增排序。同一 session 的多轮交互是
**多个互相独立的 trace**，SDK 不做跨 turn 合并或关联，只在 trace 上记录 `session_id`。

### 2.2 message_type 枚举与映射（已确定，沿用现有规则）

| 枚举值 | 缩写 | 对应 role | 说明 |
| :--- | :--- | :--- | :--- |
| `system_prompt` | sys | system | 系统提示词和工具定义 |
| `user_query` | usr | user | 用户输入或反馈 |
| `reasoning` | rsn | assistant | 思考、规划、调度等推理过程 |
| `tool_call` | cal | assistant | 发起工具调用 |
| `tool_response` | res | tool | 工具返回结果 |
| `final_answer` | ans | assistant | 最终交付回答 |

`message_id` 格式：`msg_{类型缩写}_{序号}`。

**序号语义（已确定，Q8）**：`{序号}` 为**该类型内的自增计数**，从 1 开始，与该 step 的
全局位置无关。例如同一 turn 中第 2 个 `reasoning` 是 `msg_rsn_2`，第 1 个
`tool_response` 是 `msg_res_1`。

> **注意：历史数据不一致**。`data/cs.json` 符合本规则，但
> `data/sample_messages.json` 中只有一个 `tool_response` 却编号 `msg_res_4`、
> 单个 `final_answer` 编号 `msg_ans_3`（疑似按全局位置编号）。因此：
> - **校验器只校验 `msg_{类型缩写}_{数字}` 的格式**，不校验序号连续性；
> - 序号连续性只对 **SDK 生成**的数据保证；
> - M2 回归比对时，历史文件里的 `message_id` 按原样保留，不做重编号。

### 2.3 Trace 信封与 `conversation` 字段

trace 的序列化形态（= 上报 payload = 各导出器的统一输入）：

```json
{
  "trace_id": "tr_01J8...",
  "session_id": "sess_01J8...",
  "agent_name": "news-daily",
  "started_at": "2026-08-06T01:02:00.397Z",
  "ended_at": "2026-08-06T01:02:10.325Z",
  "status": "ok",
  "conversation": [
    {
      "step_no": 1,
      "message_id": "msg_usr_1",
      "role": "user",
      "message_type": "user_query",
      "content": "查看今日ai新闻热点",
      "timestamp": "2026-08-06T01:02:00.397Z"
    },
    {
      "step_no": 2,
      "message_id": "msg_rsn_1",
      "role": "assistant",
      "message_type": "reasoning",
      "content": "先搜索今日 AI 新闻……",
      "timestamp": "2026-08-06T01:02:10.325Z"
    }
  ]
}
```

- **`conversation` 就是需要采集的每个步骤数据**，其元素契约见 2.4，数组顺序即执行顺序。
- SDK 按发生顺序追加 step，不重排、不跨 turn 合并；`step_no` 由 SDK 统一分配。
- 离线侧现有的 `{"messages": [...]}` 是同一份数据的另一种壳（键名为 `messages`）。
  导出器需同时兼容 `conversation` 与 `messages` 两种键名，并沿用 `extract_steps.py`
  中 `find_messages()` 的递归查找策略（`{"conversations": {"messages": [...]}}` 这类
  多层嵌套也在兼容范围内）。

### 2.4 Step 字段契约（已确定）

| 字段名 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `message_id` | String | 是 | 格式 `msg_{类型缩写}_{序号}` |
| `role` | String | 是 | system / user / assistant / tool |
| `message_type` | String | 是 | 见 2.2 |
| `content` | String / null | 是 | 消息内容，`tool_call` 时为 null |
| `timestamp` | String | 是 | ISO 8601 时间戳 |
| `tool_calls` | Array | 否 | 工具调用数组；`message_type=tool_call` 时必填且非空 |
| `tool_call_id` | String | 否 | 工具调用 ID，`message_type=tool_response` 时必填 |

**`tool_call` 的粒度（已确定，Q10）：一条 `tool_call` 消息 = 一个 step。** 同一轮里
并发的多个工具调用**合并在同一条 step 的 `tool_calls` 数组里**，不拆成多个 step：

```json
{
  "step_no": 3,
  "message_id": "msg_cal_1",
  "role": "assistant",
  "message_type": "tool_call",
  "content": null,
  "timestamp": "2026-08-06T01:02:10.325Z",
  "tool_calls": [
    { "id": "call_0964...", "type": "function",
      "function": { "name": "web_search", "arguments": "{\"count\": 8, ...}" } },
    { "id": "call_19bc...", "type": "function",
      "function": { "name": "web_search", "arguments": "{\"count\": 6, ...}" } }
  ]
}
```

由此推导出两条契约：

- 本 SDK 的 `conversation` **不采用** `extract_steps.py` 现有的"按调用拆行"行为，
  `step_no` 与 `message_id` 都与消息一一对应。
- 对应的多个 `tool_response` 也是**各自独立的 step**（各带自己的 `tool_call_id`），
  通过 `tool_call_id` 回关到那一条 `tool_call` step。

**本项目增量字段**（已确认加入）：

| 字段名 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `step_no` | UInt32 | 是 | 事件步骤序号，从 1 递增 |
| `latency_ms` | UInt32 | 否 | 该步骤耗时；此后建议扩展到所有步骤 |

### 2.5 建议新增字段（待规范文档确认）

**Trace 级**（每个 trace 一份，与 `conversation` 同级）：

| 字段名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `trace_id` | String | turn 唯一标识，关联该 trace 的所有 step |
| `session_id` | String | 会话标识，关联同一会话的多个 turn。**由业务方传入**（Q14），非 SDK 生成 |
| `agent_name` / `agent_version` | String | 产出该轨迹的 agent 身份 |
| `env` | String | 运行环境标识 |
| `started_at` / `ended_at` | String | 该 turn 的起止时间（ISO 8601） |
| `duration_ms` | UInt32 | 该 turn 总耗时 |
| `status` / `error` | String | ok / error / cancelled 及异常信息 |
| `model` | String | 该 turn 使用的主模型 |
| `input_tokens` / `output_tokens` | UInt32 | 该 turn 累计 token 用量 |
| `tags` / `meta` | Map / String | 自定义标签与扩展元数据 |

**Step 级**（落在 `conversation` 元素上）：

| 字段名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `span_id` | String | 步骤唯一标识，用于父子关系与去重 |
| `parent_span_id` | String | 父步骤，支撑嵌套 agent / 子工具链还原 |
| `status` / `error` | String | ok / error / cancelled 及异常信息 |
| `input_tokens` / `output_tokens` | UInt32 | token 用量 |
| `model` | String | 模型名 |

> **已确定（Q9）**：步骤耗时只保留 `latency_ms` 一个字段，**不引入** Step 级的
> `duration_ms`，避免同一语义两份数据。Trace 级的 `duration_ms`（整个 turn 的总耗时）
> 保留，二者层级不同、不冲突。

---

## 3. 架构

### 3.1 分层设计

```
┌─────────────────────────────────────────────────────────┐
│ 业务方 agent 代码                                        │
└───────────────┬─────────────────────────────────────────┘
                │ ① 埋点：手动 API / 装饰器 / 自动 wrap
┌───────────────▼─────────────────────────────────────────┐
│ 采集层  Tracer / Turn / Step / contextvars               │
│  - trace(=turn) 与 step 生命周期                         │
│  - 上下文传播（异步、并发、跨线程）                       │
└───────────────┬─────────────────────────────────────────┘
                │ ② 结构化对象（含 conversation 数组）
┌───────────────▼─────────────────────────────────────────┐
│ 契约层  schemas.py                                       │
│  - 枚举、字段、校验、message_id 生成规则                  │
└───────────────┬─────────────────────────────────────────┘
                │ ③ 已校验的 Trace / Step
┌───────────────▼─────────────────────────────────────────┐
│ 加工层  脱敏 / 截断 / 超长外置                            │
│  - PII、密钥、超长 tool_response                         │
└───────────────┬─────────────────────────────────────────┘
                │ ④ 处理后 Trace
┌───────────────▼─────────────────────────────────────────┐
│ 传输层  队列 + 批量 + 重试 + 落盘 spool                   │
│  - 非阻塞，不拖慢业务                                     │
└───────────────┬─────────────────────────────────────────┘
                │ ⑤ 批次
┌───────────────▼─────────────────────────────────────────┐
│ 导出层  exporters: HTTP(主) / JSONL / XLSX / OTLP        │
└───────────────┬─────────────────────────────────────────┘
                │
        ┌───────▼────────┐        ┌──────────────────┐
        │ Collector 服务 │───────▶│ ClickHouse       │
        │ (校验/补齐/限流)│        │ trace / step 表  │
        └────────────────┘        └──────────────────┘
```

**采集粒度的统一约定**：无论手动埋点还是自动 wrap，都先攒够一个 turn 的
`conversation`，再以 trace 为单位交给加工层与传输层。传输层不做"单 step 上报"。

### 3.2 关键设计决策

**决策 1：SDK 不直连 ClickHouse，走中间 Collector（建议）**

| 方案 | 优点 | 缺点 |
| :--- | :--- | :--- |
| A. SDK 直连 CK | 链路短，无需额外部署 | 每方都要配 CK 凭证；CK 表结构变更导致老版本 SDK 写不进去；写压直接打在 CK |
| **B. SDK → Collector → CK** | SDK 薄、鉴权单 token、格式演进与限流集中在服务端控制 | 需多写一个服务 |

倾向 B。理由是 schema 一定会演进，A 方案会让版本兼容变成噩梦。

**决策 2：字段命名对齐 OTel GenAI 还是自定义**

- 走 OTel 标准：以后能直接接 Langfuse / Phoenix / Grafana / Datadog，无需转换层；
  代价是字段名冗长（`gen_ai.request.model`），且标准本身还在演进。
- 自定义：贴合业务，字段短（现有 `message_type` 就是自定义风格）；
  代价是将来对接外部平台需写适配层。

**建议：自定义为主 + 保留 OTLP 导出器做桥接。** 核心 schema 按业务命名，
需要对接外部平台时由 `exporters/otlp.py` 做字段映射。这样两头都不亏。

**决策 3：tool_call 的粒度 —— 一条消息 = 一个 step（已确定，Q10）**

并行发起的多个工具调用**合并为一条 `tool_call` step**，完整放在该 step 的
`tool_calls` 数组里；不再按调用拆成多个 step。该 step 的 `content` 为 `null`，
`latency_ms` 覆盖整批调用的等待时间。

**由此产生的既有实现差异（M2 必须处理）**：`extract_steps.py` 当前会把含 N 个调用的
一条消息**拆成 N 行**（`step_no` 连号、`message_id` 相同、每行只放一个调用对象）。
按新契约，SDK 的 `conversation` 是 1 条消息 1 个 step，因此：

- 新契约与 `extract_steps.py` 的现有输出**不再逐格一致**，M2 的回归比对需要放宽容差
  （见 M2 交付判据）。
- `xlsx.py` 需提供 `split_tool_calls: bool` 开关：`True` 走旧行为、便于与历史 Excel
  逐格比对；`False`（默认）按新契约 1 行 1 step 输出。
- 旧行为下的 `tool_call_id` 列仅用于历史比对；新契约下列里放该 step 的完整
  `tool_calls` 数组，`tool_call_id` 只由 `tool_response` step 填写。

**决策 4：超长内容外置（借鉴 Cozeloop）**

`tool_response` 动辄几十 KB 到 MB 级（真实样本里单条 11.8 KB）。
直接塞进 CK 宽表会拖垮存储与查询。

建议：超过阈值（可配，如 64 KB）的内容上传对象存储，表里只存 key +
摘要（长度、hash），字段命名如 `content_ref`。同时提供 `content_truncated` 标记。

**决策 5：依赖策略（已确定，Q12）**

- 契约层用 `pydantic`（v2），换取声明式校验与序列化，避免手写一堆 `if`。
- 传输层优先**标准库**（`urllib.request` + `threading` + `json`），
  `httpx` 作为可选 extras（`agent-trace[http]`），不装也能跑。
- 离线导出器（`xlsx.py`）沿用可选依赖 `openpyxl`，缺失时给出明确的安装提示
  （沿用 `extract_steps.py` 现有的 `ImportError` 处理风格）。
- 核心包 `pip install agent-trace` 只强依赖 `pydantic`，其余全部 optional。

---

## 4. 目录结构

```
agent-trace-sdk/
├── pyproject.toml
├── README.md
├── docs/
│   └── agent_trace_sdk_plan.md      # 本文档
├── src/
│   └── agent_trace/
│       ├── __init__.py              # 公共 API 出口
│       ├── schemas.py               # 枚举、Trace/Turn/Step 模型、校验（契约层）
│       ├── tracer.py                # Tracer / Turn / Step / 生命周期
│       ├── context.py               # contextvars、trace_id 传播、baggage
│       ├── config.py                # 配置：env var + 代码初始化
│       ├── sanitize.py              # 脱敏、截断、超长外置
│       ├── utils.py                 # id 生成、时间格式化
│       ├── transport/
│       │   ├── queue.py             # 内存队列 + 后台 flush
│       │   ├── spool.py             # 失败落盘 + 恢复补发
│       │   └── client.py            # HTTP 上报客户端
│       ├── exporters/
│       │   ├── base.py              # Exporter 抽象
│       │   ├── jsonl.py             # 本地 JSONL
│       │   ├── xlsx.py              # 收编现有 extract_steps.py
│       │   └── otlp.py              # OTLP（可选，用于对接外部平台）
│       └── integrations/
│           ├── __init__.py
│           └── openai.py            # 自动 wrap OpenAI client
├── examples/
│   ├── manual_api.py
│   ├── decorator.py
│   └── export_to_excel.py
└── tests/
    ├── test_schemas.py
    ├── test_tracer.py
    ├── test_transport.py
    └── test_exporters.py
```

**仓库划分（已确定，Q16）**：SDK **另建独立仓库**，上述结构属于新仓库。

本仓库（`data_fly_whele_sj`）保持现状，作为**离线工具仓 + 样本数据源**：

- 保留 `extract_steps.py`、`data/`、`output/`；
- 不迁走 `extract_steps.py`，新仓库的 `exporters/xlsx.py` 以它为**行为基准**
  （逻辑重写，输出对齐，见 M2 判据）；
- 新仓库的测试通过 fixture 引用本仓库的 `data/*.json` 与 `output/*.xlsx` 做回归，
  或先把这两组文件复制一份进新仓库的 `tests/fixtures/`，避免跨仓依赖。

**新仓库名与包名待定（Q18）**，暂以 `agent-trace-sdk` / `agent_trace` 指代。

---

## 5. 里程碑

### M0 — 契约层（`schemas.py`）

- [ ] 定义 `MessageType` / `Role` 枚举，含 message_type → 缩写、role 的映射表
- [ ] 定义 `Step`（pydantic 模型），落地 2.4 节字段与必填约束
- [ ] 定义 `Trace`（pydantic 模型），含 2.5 节 Trace 级元数据与 `conversation` 数组
- [ ] 校验规则：未知 `message_type` 报错；`tool_call` 必须带 `tool_calls`；
      `tool_response` 必须带 `tool_call_id`；`role` 与 `message_type` 一致性
- [ ] `message_id` 生成器：`msg_{缩写}_{序号}`
- [ ] 同时接受 `conversation` 与 `messages` 两种数组键名
- [ ] `message_id` 校验**只查格式**（`msg_{已知缩写}_{数字}`），不查序号连续性
- [ ] 契约测试：合法/非法样本各覆盖一遍

**交付判据**：

1. `data/cs.json`、`data/sample_messages.json` 都能被 schema 完整校验通过。
2. 两份语料经 schema 往返（解析 → 序列化 → 再解析）后语义不变。
3. 非法样本均被拒绝：未知 `message_type`、`tool_call` 无 `tool_calls`、
   `tool_response` 无 `tool_call_id`、`role` 与 `message_type` 不一致、
   `message_id` 格式错误、`step_no` 不从 1 递增。

### M1 — 核心采集（`tracer.py` + `context.py`）

- [ ] `Tracer` 初始化：配置 trace 级元数据（agent_name、env、endpoint）
- [ ] `session_id` **由业务方传入**（`tracer.turn(session_id=...)`），非 SDK 生成；
      是否允许缺省见 Q17
- [ ] turn 生命周期：`with tracer.turn(session_id=...) as turn:` 自动记录
      起止时间、`status`、`duration_ms`，退出时提交整条 trace
- [ ] `with turn.step(...)` 上下文管理器，自动记录起止时间与 status
- [ ] 装饰器形式 `@tracer.trace` / `@tracer.step`
- [ ] 手动 API：`turn.record_user_query()` / `record_reasoning()` /
      `record_tool_call()` / `record_tool_response()` / `record_final_answer()`
- [ ] `contextvars` 实现 trace 上下文自动传播，支持 async 与并发任务
- [ ] 异常路径：工具报错、超时、取消，均落一条终止 step

**交付判据**：`examples/manual_api.py` 能跑出一个 turn 的完整 6 步 `conversation`。

### M2 — 导出层（收编现有能力）

- [ ] `Exporter` 抽象基类
- [ ] `xlsx.py`：把 `extract_steps.py` 的逻辑迁入，列顺序与表头保持不变
- [ ] `jsonl.py`：一行一个 step 的裸 JSON
- [ ] 端到端：采集 → JSONL/XLSX，与现有 Excel 输出逐格比对

**交付判据**（分两档，因 Q10 改动了 tool_call 粒度）：

1. **严格一致**：`data/cs.json`（6 条消息、无多调用）跑出的 Excel 与
   `output/cs.xlsx` 逐格一致。两份文件都是 7 行（表头 + 6 步）。
2. **按新契约一致**：`data/sample_messages.json` 跑出的 Excel 为 7 行
   （表头 + 6 步，其中含 2 个工具调用的那条占 1 行）；与旧基准
   `output/messages_steps_1.xlsx`（8 行，该消息拆成 2 行）**不做逐格比对**，
   只断言：数据相同、行数差 1、`split_tool_calls=True` 时能复现旧文件的 8 行。

> 表头沿用英文 `step_no, message_id, role, message_type, content, timestamp,
> tool_calls, tool_call_id, latency_ms`。v0.1 文档中"中文表头"的描述与
> `extract_steps.py` 实际实现不符，已按实际实现修正。

### M3 — 传输层（可靠性）

- [ ] 内存队列 + 后台 flush 线程，采集路径完全非阻塞
- [ ] 批量上报（可配 batch size / flush interval）
- [ ] 指数退避重试
- [ ] `spool.py`：网络失败落盘，恢复后补发
- [ ] `atexit` 兜底 flush，避免进程退出丢数据
- [ ] 队列满的策略：丢弃 + 计数（不阻塞业务）

**交付判据**：断网场景下数据不丢，恢复后自动补齐。

### M4 — 加工层（合规与容量）

- [ ] 脱敏规则：密钥、手机号、邮箱、身份证等可配正则
- [ ] 按字段的脱敏开关（`content` / `tool_calls.arguments` 分开控制）
- [ ] 截断策略：普通字段与 input/output 字段分别设上限（借鉴 Cozeloop）
- [ ] 超长内容外置到对象存储，表内只留 key
- [ ] 用户/租户级 opt-out

### M5 — 自动埋点与落地

- [ ] `integrations/openai.py`：wrap OpenAI client，自动捕获请求/响应/token 用量
- [ ] 按需扩展第二个框架适配器（待定，见 Q5）
- [ ] ClickHouse 建表 DDL 与 Collector 服务
- [ ] 文档、示例、版本化发布

---

## 6. ClickHouse 表结构草案

```sql
-- 运行级（一个 trace = 一个 turn）
CREATE TABLE agent_trace
(
    trace_id        String,
    session_id      String,
    agent_name      LowCardinality(String),
    agent_version   LowCardinality(String),
    env             LowCardinality(String),
    status          LowCardinality(String),
    error           String,
    started_at      DateTime64(3, 'UTC'),
    ended_at        DateTime64(3, 'UTC'),
    duration_ms     UInt32,
    step_count      UInt32,
    model           LowCardinality(String),
    input_tokens    UInt32,
    output_tokens   UInt32,
    tags            Map(String, String),
    meta            String
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(started_at)
ORDER BY (trace_id);

-- 步骤级（conversation 里的每个元素一行）
CREATE TABLE agent_trace_step
(
    trace_id        String,
    step_no         UInt32,
    span_id         String,
    parent_span_id  String,
    message_id      String,
    role            LowCardinality(String),
    message_type    LowCardinality(String),
    content         String,
    content_ref     String,
    tool_calls      String,
    tool_call_id    String,
    timestamp       DateTime64(3, 'UTC'),
    latency_ms      UInt32,
    status          LowCardinality(String),
    error           String,
    input_tokens    UInt32,
    output_tokens   UInt32,
    model           LowCardinality(String),
    tags            Map(String, String),
    meta            String
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (trace_id, step_no);
```

**去重键待定**：若同一 `trace_id` 会多次补写，需要用 `(trace_id, span_id)`
或 `(trace_id, step_no)` 配 `ReplacingMergeTree` 版本列；若只追加，`MergeTree` 即可
（见 Q2）。

**列语义说明**：

- `agent_trace_step` 一行对应 `conversation` 里的一个 step。因 Q10 确定
  `tool_call` 一条消息 = 一个 step，含 2 个并发调用的那条消息在这里是**一行**，
  两个调用对象序列化在 `tool_calls` 里。
- `tool_call_id` 仅在 `message_type=tool_response` 的行上有值。
- Step 表**没有** `duration_ms` 列（Q9），步骤耗时统一看 `latency_ms`。
- `agent_trace` 表的 `duration_ms` 是整个 turn 的总耗时，与步骤级无关。

---

## 7. 待确认清单

### 7.1 已确认

| 编号 | 结论 | 落点 |
| :--- | :--- | :--- |
| Q7 | 先做 Python | 全项目 |
| Q8 | `message_id` 序号 = **类型内自增**（校验器只校验格式，不校验连续性） | 2.2 |
| Q9 | 步骤耗时只保留 `latency_ms`，不引入 Step 级 `duration_ms` | 2.4 / 2.5 |
| Q10 | `tool_call` **一条消息 = 一个 step**，多个调用合并在 `tool_calls` 数组 | 2.4 / 决策 3 |
| Q12 | 允许引入 `pydantic`（传输层依赖待定，倾向标准库优先 + `httpx` 可选） | M0 / 决策 5 |
| Q14 | `session_id` 由**业务方传入** | M1 / 2.5 |
| Q16 | SDK 另建独立仓库，本仓库保留离线导出脚本与样本数据 | 4 |

### 7.2 待确认

| 编号 | 问题 | 影响 | 当前倾向 |
| :--- | :--- | :--- | :--- |
| Q1 | SDK 直连 CK 还是走 Collector | 决定传输层形态与代码量 | 走 Collector |
| Q2 | 轨迹是只追加，还是会更新/补写 | 决定 CK 引擎与去重键 | 只追加，用 MergeTree 系 |
| Q3 | 是否需要对接外部可观测平台 | 决定字段命名与是否写 OTLP 导出器 | 自定义命名 + 保留 OTLP 桥接 |
| Q4 | 外部《轨迹数据规范》文档（字段定义与校验规则来源） | 可能还有 trace 级字段与额外校验规则 | 待提供 |
| Q5 | 首批要支持哪个框架/模型 SDK | 决定 M5 自动埋点适配器写法 | 待定，倾向先 OpenAI |
| Q6 | 超长内容阈值与是否落对象存储 | 决定加工层实现与存储成本 | 64 KB，落对象存储 |
| Q13 | 业务方若一次传入多轮对话，SDK 如何处理 | 决定采集层是否要切分 | 倾向不切分，一个 trace 只收一个 turn |
| Q15 | Collector 服务由谁实现 | 决定 M5 范围 | 待定，本期先不做 |
| Q17 | `session_id` 是否必传，缺省时是否由 SDK 生成 | 决定必填校验的严格程度 | 倾向可选，缺省自动生成 |
| Q18 | 新仓库的名字、包名与发布渠道（私有 PyPI / 源码分发） | 影响 `pyproject.toml` 与导入路径 | 待定 |

---

## 8. 风险

| 风险 | 影响 | 应对 |
| :--- | :--- | :--- |
| 规范文档未到，字段可能返工 | 契约层需重写 | M0 先把字段集中在 `schemas.py` 单点，便于改 |
| CK 单行过大（长 tool_response） | 存储膨胀、查询变慢 | M4 超长外置 + 截断 |
| 采集阻塞业务 | 拖慢线上 agent | M3 全异步 + 非阻塞 + 队列满即丢 |
| 敏感信息外泄 | 合规风险 | M4 脱敏，默认开启 |
| schema 演进导致老版本不兼容 | 数据写入失败 | 走 Collector，服务端做兼容 |
| 异步/并发上下文丢失 | 轨迹错乱、串线 | M1 用 contextvars 并专项测试 |
| 业务方一次传入多轮对话，被错误地塞进一个 trace | 数据不可用 | 明确一个 trace = 一个 turn；M1 对要落盘的 `conversation` 校验只含一个 `user_query`（Q13） |
| 历史样本的 `message_id` 与 SDK 生成规则不一致 | 回归比对出现假失败 | 校验只查格式不查连续性，回归比对按原样保留（见 2.2） |

---

## 9. 建议的开工顺序

契约层的已确认结论（Q8/Q9/Q10/Q14）已经足够开工，**M0 不受任何待定项阻塞**：

1. `schemas.py` — 契约层。样本语料用本仓库现有的
   `data/sample_messages.json` 与 `data/cs.json`（新仓库通过测试 fixture 引用）
2. `tracer.py` + `context.py` — 手动埋点最小闭环（turn 级）
3. `exporters/xlsx.py` — 收编 `extract_steps.py` 的逻辑，按 M2 的两档判据做回归比对

这三步做完即可得到一个**能跑通的最小闭环**。之后按 Q1–Q18 的答复推进传输层与落地。

**开工前还需拍板**：Q18（仓库名 / 包名）与 Q13（多轮对话是否切分）——前者决定
`pyproject.toml`，后者决定 `Tracer` 的入口签名。