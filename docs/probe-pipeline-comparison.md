# 第三种管道（语言与框架探针）对比方案

> 最后更新：2026-09-24
> 适用范围：AgentLoop「接入方式」中的第三种——语言与框架探针
> 结论先说：**这条管道不适合同时覆盖 DeerFlow 与 OpenClaw，且换不到审计能力。**
> 它的价值只在「只想观测、且人力极少」这一种场景下成立。

---

## 1. 第三种管道是什么

AgentLoop 接入中心提供三条数据管道，第三种即「语言与框架探针」：

- 本质是阿里云 ARMS 自研的 **OpenTelemetry 探针**，不是 AgentLoop 专有协议。
- 形态是**进程内注入**：先装安装器再启动应用。

```bash
pip3 install aliyun-bootstrap
aliyun-bootstrap -a install
export ARMS_APP_NAME=xxx ARMS_REGION_ID=xxx ARMS_LICENSE_KEY=xxx
aliyun-instrument python app.py
```

- 适配范围（官方口径）：Python / Node.js 自研 Agent；LangChain、LangGraph、AgentScope 框架；
  不覆盖 Rust 及其他语言。
- 能力面：**可观测 ✅ / 评估 ✅ / 审计 ❌**（审计链路官方标注尚未开放）。
- 数据终点是 ARMS「LLM 应用监控」，再由 AgentLoop 的 Pipeline 消费。

## 2. 对比的两条路线

| | A. 走第三种管道 | B. 自建 CK |
| :--- | :--- | :--- |
| 本质 | 装阿里云探针，终端换到 ARMS | `agent_trace` 现有契约 + 自建 Collector + CK |
| DeerFlow 可采 | ✅ 需重启应用 | ✅ 已有 `integrations/deerflow.py` |
| OpenClaw 可采 | ❌ 探针无 OpenClaw 适配 | ✅ 需自写适配器 |
| 可观测 | ✅ 开箱 + ARMS 界面 | ❌ 需自建看板 |
| 审计 | ❌ 未开放 | ❌ 需自建 |
| 评估 | ✅ | ❌ |
| 改动量 | 装探针 + 启动方式变更 + 重启 | Collector + 建表 + 看板 + 适配器 |
| 外部依赖 | 强（ARMS/AgentLoop/地域/计费） | 无 |
| 数据归属 | 出域上云 | 自持 |

## 4. 逐项对比：A vs B（核心两条）

| 维度 | A. 第三种管道 | B. 自建 CK |
| :--- | :--- | :--- |
| 接入成本（DeerFlow） | 装探针 + 改启动命令 + 重启；聊天 app 重启即断线 | 已有中间件，无需业务改动 |
| 接入成本（OpenClaw） | **不可行**，无适配 | 写适配器或让其 POST 同契约（约 1–2 人天） |
| 接入成本（其他框架） | 只能等官方适配 | 按框架自写，工作量可估 |
| Collector | 阿里云托管 | 需自写：收 `{"traces":[...]}`、按 `turn_id` 幂等去重、写 CK |
| 存储 | ARMS 托管，不可自定义 schema | CK 两张表（trace / step），列可控 |
| 查询 | ARMS 界面开箱 | SQL，或接 Grafana / 自建 |
| 忘记埋点的代价 | 无影响（自动埋点） | 缺失（依赖埋点完整性） |
| 可视化 / 告警 | 开箱 | 需自建，**这是自建最大的工作量** |
| 审计 / 语义检测 | 无 | 需自建 |
| 评估 / 实验 / 数据集 | 有 | 无 |
| 多语言支持 | Python / Node.js | 契约语言无关（`schema/trace.schema.json`），可扩 |
| 可定制字段 | 低（受约定约束） | 高 |
| 数据出境 | 是 | 否 |
| 长期成本 | 按量计费 + 地域绑定 | 主要是 CK 运维 + 看板维护 |

## 5. 建议

**推荐路线：B。**

理由：
- 你们的技术资产（`agent_trace` SDK + DeerFlow 集成 + JSON Schema 契约）正好是 B 的核心，
  选 A 等于把已有投入作废，再把自己降级成阿里云探针的字段转换层。
- 多框架是硬需求，而 A 在 OpenClaw 上直接断链 —— 
  **没有现成管道能同时覆盖这两者**，这本身就是自建的充分理由。
- A 换不到审计能力，这是官方文档明确的能力边界，不是实施问题。

**A 仍然成立的窄场景**：只想观测、不想维护看板和存储、且 Agent 栈全在探针支持范围内
（纯 LangChain/LangGraph）、团队人手极少。此时用探针换「开箱可观测」是划算的。



