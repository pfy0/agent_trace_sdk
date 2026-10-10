/**
 * Agent Trace JS SDK — 契约构建核心（OpenCode 事件流 → Trace/Step）。
 *
 * 这是「采集端即归一」的实现：直接按 `schema/trace.schema.json` 产出与
 * Python SDK 同构的 Trace/Step，不再需要服务端二次归一，也不需要接收端。
 *
 * 设计约束
 * -------
 * - **字段严格对齐 schema**：Step / Trace / ToolCall 只写 schema 允许的键
 *   （`additionalProperties: false`），序列化时丢弃 null/undefined，与 Python
 *   的 `serializable()`（`exclude_none=True`）保持一致。
 * - **判定口径复用**：工具成败走 `judgeToolSuccess`，与 Python 的
 *   `agent_trace.tool_outcome` 两层判定逐条等价，保证 `tool_stats` 跨语言一致。
 * - **OpenCode 插件编译器**只支持 JS 子集：用 class / Map / for..of / 可选链 /
 *   `??` / 模板串等（已实测），避免 `for await` 这类被拒的语法。
 */

// ---- 枚举与 message_id --------------------------------------------------------

// message_type -> [id 缩写, role]，与 Python MESSAGE_TYPE_META 一致
export const MESSAGE_TYPE_META = {
  system_prompt: ["sys", "system"],
  user_query: ["usr", "user"],
  reasoning: ["rsn", "assistant"],
  tool_call: ["cal", "assistant"],
  tool_response: ["res", "tool"],
  final_answer: ["ans", "assistant"],
};

/** 生成带前缀的唯一 id（与 Python generate_id 同形：`前缀_32位hex`）。 */
export function generateId(prefix) {
  const hex = crypto.randomUUID().replace(/-/g, "");
  return prefix + "_" + hex;
}

/** 按 `msg_{类型缩写}_{序号}` 生成 message_id。 */
export function generateMessageId(messageType, sequence) {
  const meta = MESSAGE_TYPE_META[messageType];
  if (!meta) throw new Error("未知 message_type: " + messageType);
  return "msg_" + meta[0] + "_" + sequence;
}

/** 格式化为 ISO 8601 UTC，毫秒精度、以 Z 结尾（与 Python iso_timestamp 一致）。 */
export function isoTimestamp(ms) {
  if (ms === undefined || ms === null) ms = Date.now();
  return new Date(ms).toISOString().replace(/(\.\d{3})\d*Z$/, "$1Z");
}

/** 把毫秒时间戳（数字或数字串）转成 ISO；非法返回 null。 */
export function epochMsToIso(value) {
  if (value === undefined || value === null || value === "") return null;
  const ms = Number(value);
  if (!Number.isFinite(ms)) return null;
  return isoTimestamp(ms);
}

// ---- tool_success：与 Python tool_outcome 逐条等价 ---------------------------

const JSON_VERDICT_KEYS = ["success", "ok", "is_success", "succeeded"];
const TRUE_STRINGS = ["true", "1", "yes"];
const FALSE_STRINGS = ["false", "0", "no"];
const ERROR_PREFIXES = ["error:", "error -", "error —"];

/** 把文本解析成 JSON 对象；不是对象返回 null（容忍 JSON 后追加的诊断文本）。 */
export function parseJsonObject(text) {
  if (!text || typeof text !== "string") return null;
  const stripped = text.trim();
  if (!stripped.startsWith("{")) return null;
  let payload = null;
  try {
    payload = JSON.parse(stripped);
  } catch (error) {
    const end = stripped.lastIndexOf("}");
    if (end <= 0) return null;
    try {
      payload = JSON.parse(stripped.slice(0, end + 1));
    } catch (error2) {
      return null;
    }
  }
  return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : null;
}

/** 第一层：JSON 顶层显式成败布尔值 → 1/0/null。 */
export function jsonVerdict(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  for (const key of JSON_VERDICT_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(payload, key)) continue;
    const value = payload[key];
    if (typeof value === "boolean") return value ? 1 : 0;
    if (typeof value === "string") {
      const lowered = value.trim().toLowerCase();
      if (TRUE_STRINGS.indexOf(lowered) >= 0) return 1;
      if (FALSE_STRINGS.indexOf(lowered) >= 0) return 0;
    }
  }
  return null;
}

/** 文本是否以 Error 开头（DeerFlow/业务工具的失败约定）。 */
export function looksLikeErrorText(text) {
  if (!text || typeof text !== "string") return false;
  const lowered = text.replace(/^\s+/, "").toLowerCase();
  return ERROR_PREFIXES.some((prefix) => lowered.startsWith(prefix));
}

/** 两层判定，返回 -1（未知）/ 0（失败）/ 1（成功）。 */
export function judgeToolSuccess(text, jsonPayload) {
  let payload;
  if (jsonPayload && typeof jsonPayload === "object" && !Array.isArray(jsonPayload)) {
    payload = jsonPayload;
  } else if (jsonPayload !== undefined && jsonPayload !== null) {
    payload = null; // 非对象 JSON（数组/标量）不作为结构化成功的依据
  } else {
    payload = parseJsonObject(text);
  }
  const verdict = jsonVerdict(payload);
  if (verdict !== null) return verdict;
  if (looksLikeErrorText(text)) return 0;
  if (payload !== null) return 1;
  return -1;
}

// ---- 任意值 -> 文本（与 Python _as_text 一致） --------------------------------

export function asText(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch (error) {
    return String(value);
  }
}

// ---- Turn：单轮构建器 --------------------------------------------------------

export class Turn {
  /**
   * @param {object} options turn_id / session_id 必填；turn_no、identity、metadata 可选。
   */
  constructor(options) {
    const opts = options || {};
    if (!opts.turn_id) throw new Error("Turn 需要 turn_id");
    if (!opts.session_id) throw new Error("Turn 需要 session_id");
    this.turnId = opts.turn_id;
    this.sessionId = opts.session_id;
    this.turnNo = opts.turn_no === undefined ? 1 : opts.turn_no;
    this.agentId = opts.agent_id === undefined ? null : opts.agent_id;
    this.parentAgentId = opts.parent_agent_id === undefined ? null : opts.parent_agent_id;
    this.appName = opts.app_name === undefined ? null : opts.app_name;
    this.appId = opts.app_id === undefined ? null : opts.app_id;
    this.systemCode = opts.system_code === undefined ? null : opts.system_code;
    this.userId = opts.user_id === undefined ? null : opts.user_id;
    this.source = opts.source === undefined ? null : opts.source;
    this.conversationsType = opts.conversations_type || "openai_messages";
    this.metadata = opts.metadata || null;
    this.extraInfo = opts.extra_info ? Object.assign({}, opts.extra_info) : null;
    this.extra = opts.extra ? Object.assign({}, opts.extra) : {};
    this.partial = opts.partial === undefined ? false : Boolean(opts.partial);
    this.completed = opts.completed === undefined ? true : Boolean(opts.completed);
    this.apiCalls = opts.api_calls === undefined ? null : opts.api_calls;

    this.startTime = opts.start_time ? opts.start_time : isoTimestamp();
    this.endTime = null;

    this.steps = [];
    this.seq = {};
    this.toolCallSpans = {}; // callID -> { span_id, name }
  }

  _nextMessageId(messageType) {
    const current = this.seq[messageType] || 0;
    const next = current + 1;
    this.seq[messageType] = next;
    return generateMessageId(messageType, next);
  }

  _append(messageType, fields) {
    const f = fields || {};
    const meta = MESSAGE_TYPE_META[messageType];
    const messageId = f.message_id || this._nextMessageId(messageType);
    const startMs = f.start_ms === undefined || f.start_ms === null ? null : Number(f.start_ms);
    const startTime = f.start_time || (startMs === null ? isoTimestamp() : isoTimestamp(startMs));
    let endTime = f.end_time || null;
    if (!endTime && f.latency_ms) endTime = isoTimestamp(startMs + f.latency_ms);
    if (!endTime) endTime = startTime;

    let parentSpanId = f.parent_span_id === undefined ? null : f.parent_span_id;
    let toolName = f.tool_name === undefined ? null : f.tool_name;
    let toolSuccess = f.tool_success === undefined ? null : f.tool_success;

    if (messageType === "tool_response") {
      const linked = f.tool_call_id ? this.toolCallSpans[f.tool_call_id] : null;
      if (parentSpanId === null && linked) parentSpanId = linked.span_id;
      if (toolName === null && linked) toolName = linked.name;
      if (toolSuccess === null) toolSuccess = -1; // 拿不到明确状态记「未知」
    }

    const step = {
      message_id: messageId,
      step_no: this.steps.length + 1,
      role: meta[1],
      message_type: messageType,
      content: f.content === undefined ? null : f.content,
      start_time: startTime,
      end_time: endTime,
      latency_ms: f.latency_ms === undefined ? null : f.latency_ms,
      span_id: f.span_id || generateId("span"),
      parent_span_id: parentSpanId,
      is_follow_up: f.is_follow_up === undefined ? null : f.is_follow_up,
      tool_calls: f.tool_calls === undefined ? null : f.tool_calls,
      tool_call_id: f.tool_call_id === undefined ? null : f.tool_call_id,
      tool_name: toolName,
      tool_success: toolSuccess,
      prompt_tokens_delta: f.prompt_tokens_delta === undefined ? null : f.prompt_tokens_delta,
      completion_tokens_delta:
        f.completion_tokens_delta === undefined ? null : f.completion_tokens_delta,
    };
    this.steps.push(step);
    return step;
  }

  recordSystemPrompt(content, fields) {
    return this._append("system_prompt", Object.assign({}, fields, { content }));
  }

  recordUserQuery(content, fields) {
    const f = Object.assign({}, fields);
    if (f.follow_up && f.is_follow_up === undefined) f.is_follow_up = true;
    return this._append("user_query", Object.assign(f, { content }));
  }

  recordReasoning(content, fields) {
    return this._append("reasoning", Object.assign({}, fields, { content }));
  }

  recordFinalAnswer(content, fields) {
    return this._append("final_answer", Object.assign({}, fields, { content }));
  }

  /** 记录一次工具调用；返回该 step，同时登记 callID → span 供 tool_response 挂父。 */
  recordToolCall(callId, name, args, fields) {
    const f = Object.assign({}, fields);
    const call = {
      id: callId,
      type: "function",
      function: { name: name, arguments: args === undefined || args === null ? {} : args },
    };
    const step = this._append("tool_call", Object.assign(f, { content: null, tool_calls: [call] }));
    this.toolCallSpans[callId] = { span_id: step.span_id, name: name };
    return step;
  }

  recordToolResponse(callId, content, fields) {
    return this._append(
      "tool_response",
      Object.assign({}, fields, { content: content, tool_call_id: callId })
    );
  }

  /** 生成已按 schema 校验、丢弃空值的 Trace 字典。 */
  asTrace() {
    this._recomputeStats();
    const payload = {
      turn_id: this.turnId,
      turn_no: this.turnNo,
      session_id: this.sessionId,
      start_time: this.startTime,
      end_time: this.endTime || isoTimestamp(),
      app_name: this.appName,
      app_id: this.appId,
      system_code: this.systemCode,
      user_id: this.userId,
      source: this.source,
      agent_id: this.agentId,
      parent_agent_id: this.parentAgentId,
      conversations_type: this.conversationsType,
      conversations: this.steps.map((step) => prune(step)),
      completed: this.completed,
      partial: this.partial,
      api_calls: this.apiCalls,
      toolsets_used: this.toolsetsUsed,
      tool_stats: this.toolStats,
      tool_error_counts: this.toolErrorCounts,
      token_usage: this.tokenUsage,
      metadata: this.metadata,
      extra_info: this.extraInfo,
    };
    // extra 里是 Trace 级字段覆盖（如业务方显式传的 turn_no/app_name）
    const merged = Object.assign({}, this.extra, payload);
    const pruned = prune(merged);
    return pruned;
  }

  _recomputeStats() {
    const toolsets = [];
    const seen = {};
    const remember = (name) => {
      if (name && !seen[name]) {
        seen[name] = true;
        toolsets.push(name);
      }
    };
    for (const step of this.steps) {
      if (step.message_type === "tool_call" && step.tool_calls) {
        for (const call of step.tool_calls) {
          if (call.function) remember(call.function.name);
        }
      } else if (step.message_type === "tool_response") {
        remember(step.tool_name);
      }
    }

    const stats = {};
    for (const step of this.steps) {
      if (step.message_type !== "tool_response" || !step.tool_name) continue;
      const entry = stats[step.tool_name] || { total: 0, success: 0, failure: 0 };
      stats[step.tool_name] = entry;
      entry.total += 1;
      if (step.tool_success === 1) entry.success += 1;
      else if (step.tool_success === 0) entry.failure += 1;
    }
    const errorCounts = {};
    let hasError = false;
    for (const name of Object.keys(stats)) {
      if (stats[name].failure) {
        errorCounts[name] = stats[name].failure;
        hasError = true;
      }
    }

    let promptTokens = 0;
    let completionTokens = 0;
    let hasTokens = false;
    for (const step of this.steps) {
      if (step.prompt_tokens_delta !== null && step.prompt_tokens_delta !== undefined) {
        promptTokens += step.prompt_tokens_delta;
        hasTokens = true;
      }
      if (step.completion_tokens_delta !== null && step.completion_tokens_delta !== undefined) {
        completionTokens += step.completion_tokens_delta;
        hasTokens = true;
      }
    }

    this.toolsetsUsed = toolsets.length ? toolsets : null;
    this.toolStats = Object.keys(stats).length ? stats : null;
    this.toolErrorCounts = hasError ? errorCounts : null;
    this.tokenUsage = hasTokens
      ? {
          prompt_tokens: promptTokens,
          completion_tokens: completionTokens,
          total_tokens: promptTokens + completionTokens,
        }
      : null;
  }
}

/** 递归丢弃 null / undefined（对齐 Python `exclude_none=True`）。 */
export function prune(value) {
  if (Array.isArray(value)) return value.map((item) => prune(item));
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value)) {
      const pruned = prune(value[key]);
      if (pruned !== null && pruned !== undefined) out[key] = pruned;
    }
    return out;
  }
  return value;
}
