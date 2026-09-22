# Agent Trace SDK

Agent 运行轨迹采集 SDK。业务方以**最小改动**接入后，SDK 自动捕获一轮交互
（turn）的完整轨迹：系统提示词、用户输入、推理过程、工具调用、工具返回、
最终回答，并结构化导出。

## 采集单位

- **Trace**：一次 agent 运行 = 一个 turn = 一轮交互，是采集/校验/上报的基本单位。
- **Step**：trace 中的一步，即 `conversation` 数组里的一条消息。
- 轮次边界由 `turn_id` 决定：**同一 `turn_id` 下的消息属于同一轮**，一轮内可以出现
  多条 `user_query`（第一条是初始输入，其余是追问/补充，会标记 `is_follow_up=True`），
  不做切分；跨轮切分只按 `turn_id`。

`turn_id` 由业务方传入，作为 trace 的唯一标识；`session_id` 同样由业务方传入，
用于关联同一会话的多个 turn。

## 安装

```bash
pip install -e .            # 仅核心（pydantic）
pip install -e ".[xlsx]"    # 需要 Excel 导出
pip install -e ".[all]"     # 全部可选依赖
```

## 快速开始

```python
from agent_trace import Tracer, JsonExporter, XlsxExporter

tracer = Tracer(
    agent_name="news-daily",
    agent_version="1.0.0",
    env="prod",
    exporters=[
        JsonExporter("output/traces.json"),
        XlsxExporter("output/steps.xlsx"),
    ],
)

with tracer.turn(turn_id="turn-1", session_id="sess-1") as turn:
    turn.record_user_query("查看今日 ai 新闻热点")
    turn.record_reasoning("先搜索今日 AI 新闻……")
    turn.record_tool_call([("web_search", {"count": 8}), ("web_search", {"count": 6})])
    for call in turn.steps[-1].tool_calls:
        turn.record_tool_response(tool_call_id=call.id, content='{"success": true}')
    turn.record_final_answer("今日 AI 要闻速递：……")
```

退出 `with` 时 SDK 自动：分配 `step_no` / `message_id`、计算 `duration_ms`、
校验成 `Trace`、交给每个导出器。异常路径也会落一条终止 step 并把 `status` 置为 `error`。

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
| `XlsxExporter(path, split_tool_calls=False)` | 一行一个 step 的 Excel（表头对齐 `extract_steps.py`） |
| `MemoryExporter()` | 只留在内存，便于测试 |

`split_tool_calls=True` 时恢复离线脚本的旧行为（一条 tool_call 拆成 N 行），
用于与历史 Excel 逐格比对；默认 `False` 按新契约一条消息一行。

## 字段契约

Step 字段：`message_id`（`msg_{缩写}_{序号}`）、`role`、`message_type`、`content`、
`timestamp`、`step_no`、`latency_ms`、`tool_calls`、`tool_call_id`、`is_follow_up`，
以及预留的 `span_id` / `parent_span_id` / `status` / `error` / token 与 `model` 字段。

Trace 字段：`turn_id`、`session_id`、`agent_name` / `agent_version`、`env`、
`started_at` / `ended_at` / `duration_ms`、`status` / `error`、token 用量、
`tags` / `meta`、`conversation`。

`message_id` 校验**只查格式、不查序号连续性**（历史数据存在类型内自增与全局位置两种编号）。

## 目录结构

```
src/agent_trace/
├── schemas.py       # 契约层：枚举、Trace/Step 模型、校验、id 生成
├── tracer.py        # 采集层：Tracer / Turn / 手动埋点 / 装饰器
├── context.py       # contextvars 上下文传播
├── config.py        # 配置：环境变量 + 代码初始化
├── ingest.py        # 离线导入与多轮按 turn_id 切分
├── utils.py         # 通用工具
└── exporters/       # json / jsonl / xlsx / base
examples/            # 可运行示例
```

## 示例

```bash
python examples/manual_api.py    # 手动埋点最小闭环 + 三种导出
python examples/decorator.py     # 装饰器与异常路径
python examples/import_data.py   # 导入现成数据、多轮切分
```

## 环境变量

| 变量 | 说明 | 默认 |
| :--- | :--- | :--- |
| `AGENT_TRACE_AGENT_NAME` | agent 名称 | 空 |
| `AGENT_TRACE_ENV` | 运行环境 | 空 |
| `AGENT_TRACE_ENABLED` | 采集总开关 | `true` |
| `AGENT_TRACE_DEBUG` | 导出器异常是否向上抛 | `false` |

## 说明

传输层（HTTP/Collector）与存储还在演进，当前先跑通「采集 → 结构化 → 导出」链路。
`exporters/` 是插件式设计，后续接入 Collector 只需新增一个导出器。
