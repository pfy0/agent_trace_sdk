/**
 * Agent Trace — OpenCode 插件（采集端即归一，像 DeerFlow 一样直接出 JSON）。
 *
 * 与旧版「读 SQLite + 接收端」不同：本插件订阅 OpenCode 的事件流
 * （`ctx.event.subscribe()`），实时把一轮交互归一成契约 Trace，直接写本地
 * JSON 文件，不需要任何接收端、也不依赖库表结构。
 *
 * 安装：把本文件放到 `~/.config/opencode/plugins/`（目录会被自动加载）。
 * 配置（环境变量或插件 options）：
 *   AGENT_TRACE_OUTPUT    输出 JSON 路径（不设则只留在内存，供调试）
 *   AGENT_TRACE_AGENT_ID  默认 agent-opencode
 *   AGENT_TRACE_APP_NAME  默认 opencode
 *   AGENT_TRACE_DEBUG     true 打印调试日志
 *
 * 订阅用法（关键）：`ctx.event.subscribe()` 返回的是带 `Symbol.asyncIterator`
 * 的 ServerSentEvent 流；必须自己 `.next()` 泵取，回调式调用不会触发。
 */

import { OpencodeCollector } from "./session-collector.js";

export default {
  id: "agent-trace",
  async setup(ctx) {
    const collector = new OpencodeCollector(undefined);
    // 用插件 options 覆盖环境变量默认值
    collector.options = Object.assign({}, collector.options, optionsFrom(ctx));
    if (collector.options.output) {
      const { JsonExporter } = await import("./exporters.js");
      collector.exporter = new JsonExporter(collector.options.output);
    }

    const stream = ctx.event.subscribe();
    const iterator = stream[Symbol.asyncIterator]();
    const pump = () => {
      Promise.resolve(iterator.next())
        .then((result) => {
          if (result.done) return;
          collector.ingest(result.value);
          pump();
        })
        .catch((error) => {
          collector._log("event stream error", String(error && error.message));
        });
    };
    pump();

    const shutdown = () => {
      try {
        collector.close();
      } catch (error) {
        /* fail-open */
      }
    };
    process.once("beforeExit", shutdown);
    process.once("SIGINT", shutdown);
    process.once("SIGTERM", shutdown);
    return;
  },
};

function optionsFrom(ctx) {
  const opt = (ctx && ctx.options) || {};
  const out = {};
  if (opt.output !== undefined) out.output = opt.output;
  if (opt.agentId !== undefined) out.agentId = opt.agentId;
  if (opt.appName !== undefined) out.appName = opt.appName;
  if (opt.appId !== undefined) out.appId = opt.appId;
  if (opt.systemCode !== undefined) out.systemCode = opt.systemCode;
  if (opt.userId !== undefined) out.userId = opt.userId;
  if (opt.debug !== undefined) out.debug = String(opt.debug).toLowerCase() === "true";
  if (opt.debugLog !== undefined) out.debugLog = opt.debugLog;
  return out;
}
