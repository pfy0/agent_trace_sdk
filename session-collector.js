/**
 * OpenCode 事件流 → Trace 采集器（采集端即归一）。
 *
 * 事件映射（实测 opencode 2.0.26，事件名见 session.* 系列）：
 *   session.inbox.enqueued(user)     -> user_query（同轮第 2 条起 is_follow_up）
 *   session.reasoning.started/ended  -> reasoning（ended 带完整 text）
 *   session.text.started/delta/ended -> 先缓冲；收尾时最后一段升级 final_answer
 *   session.tool.input.started/ended / called -> tool_call（按 toolCallID 去重）
 *   session.tool.success/failed      -> tool_response（tool_success 复用两层判定）
 *   session.step.ended               -> finish / tokens（挂到该 assistant 消息首个 step）
 *   session.execution.succeeded/failed -> 收尾该轮并提交
 *
 * 事件名里的 “.” 在插件加载时可能被替换成一个私有分隔符（BINARY_MARKER），
 * 本模块对两者都兼容（_has 同时匹配 “.” 与 “\\u0001” 形态）。
 */

import fs from "node:fs";
import { Turn, asText, judgeToolSuccess, epochMsToIso, generateId } from "./trace-builder.js";
import { JsonExporter } from "./exporters.js";

// 事件名分隔符：正常是 “.”，被编译器替换时是一个控制字符（用 “.” 兜底匹配）。
const BINARY_MARKER = "\u0001";
function canon(type) {
  return String(type || "").split(BINARY_MARKER).join(".");
}

function nowIso() {
  return new Date().toISOString();
}

function readOptions(ctx) {
  const opt = (ctx && ctx.options) || {};
  const env = process.env;
  const num = (value, fallback) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  };
  const bool = (value) => String(value || "").toLowerCase() === "true";
  return {
    output: opt.output || env.AGENT_TRACE_OUTPUT || "",
    debug: bool(opt.debug || env.AGENT_TRACE_DEBUG),
    debugLog: opt.debugLog || env.AGENT_TRACE_DEBUG_LOG || "",
    agentId: opt.agentId || env.AGENT_TRACE_AGENT_ID || "agent-opencode",
    appName: opt.appName || env.AGENT_TRACE_APP_NAME || "opencode",
    appId: opt.appId || env.AGENT_TRACE_APP_ID || "",
    systemCode: opt.systemCode || env.AGENT_TRACE_SYSTEM_CODE || "",
    userId: opt.userId || env.AGENT_TRACE_USER_ID || "",
    recordIdentityProvenance: opt.recordIdentityProvenance !== false,
  };
}

// ---- 单会话轮次状态 ----------------------------------------------------------

const REASONING = "reasoning";
const TEXT = "text";

class SessionState {
  constructor(sessionId, options) {
    this.sessionId = sessionId;
    this.options = options;
    this.turnNo = 0;
    this.open = null;      // 当前未收尾的轮
    this.systemPrompt = null;
    this.dirty = false;    // 是否有未落盘的已完成轮
  }

  startTurn(startMs) {
    this.turnNo += 1;
    this.open = new TurnState(this, this.turnNo, startMs);
    return this.open;
  }
}

class TurnState {
  constructor(session, turnNo, startMs) {
    const options = session.options;
    const turn = new Turn({
      turn_id: generateId("turn"),
      session_id: session.sessionId,
      turn_no: turnNo,
      start_time: epochMsToIso(startMs) || nowIso(),
      agent_id: options.agentId,
      app_name: options.appName,
      app_id: options.appId || null,
      system_code: options.systemCode || null,
      user_id: options.userId || null,
      metadata: { framework: "opencode", opencode_session_id: session.sessionId },
      record_identity_provenance: options.recordIdentityProvenance,
    });
    this.turn = turn;
    this.turnNo = turnNo;
    this.userCount = 0;
    this.failed = false;
    this.assistantMsgs = {};     // assistantMessageID -> true
    this.msgTokens = {};         // assistantMessageID -> { prompt, completion }
    this.msgFirstStep = {};      // assistantMessageID -> step（挂 token 增量）
    this.pending = [];           // 未落步的文本片段（保序）
    this.pendingById = {};       // partKey -> 片段
    this.textSteps = [];         // 待升级为 final_answer 的文本 step
    this.toolCallsSeen = {};     // callID -> { name, span }
    this.toolAnswered = {};      // callID -> true
    this.metadata = {};
  }
}

class PendingText {
  constructor(key, kind, messageId) {
    this.key = key;
    this.kind = kind;           // REASONING | TEXT
    this.messageId = messageId;
    this.text = "";
  }
}

// ---- 采集器 ------------------------------------------------------------------

export class OpencodeCollector {
  constructor(options) {
    this.options = options || readOptions(null);
    this.sessions = {};
    this.traces = [];
    this.exporter = null;
    if (this.options.output) {
      this.exporter = new JsonExporter(this.options.output);
    }
  }

  /** 喂入一条原始事件（{type, data} 或 {type, ...data}）。 */
  ingest(event) {
    if (!event || typeof event !== "object") return;
    const type = canon(event.type);
    const data = event.data && typeof event.data === "object" ? event.data : event;
    const sessionId = data.sessionID || data.sessionId || (data.info && data.info.sessionID);
    if (!sessionId) {
      if (this.options.debug) this._log("drop event (no sessionID)", type);
      return;
    }
    const session = this.sessions[sessionId] || (this.sessions[sessionId] = new SessionState(sessionId, this.options));
    try {
      this._handle(session, type, data);
    } catch (error) {
      // 采集是旁路：单条脏事件不能打断整条流
      this._log("event handler error", type, String(error && error.stack));
    }
  }

  _handle(session, type, data) {
    if (type === "session.inbox.enqueued") return this._onInboxEnqueued(session, data);
    if (type === "session.execution.started") return;
    if (type === "session.execution.succeeded") return this._closeSession(session, false);
    if (type === "session.execution.failed") return this._closeSession(session, true);
    if (type === "session.error") return this._failSession(session);
    if (type === "session.step.started") return this._onStepStarted(session, data);
    if (type === "session.step.ended") return this._onStepEnded(session, data);
    if (type === "session.text.started") return;
    if (type === "session.text.delta") return this._onTextDelta(session, data, TEXT);
    if (type === "session.text.ended") return this._onTextEnded(session, data, TEXT);
    if (type === "session.reasoning.started") return;
    if (type === "session.reasoning.delta") return this._onTextDelta(session, data, REASONING);
    if (type === "session.reasoning.ended") return this._onTextEnded(session, data, REASONING);
    if (type === "session.tool.input.started") return this._onToolInputStarted(session, data);
    if (type === "session.tool.input.ended") return this._onToolInputEnded(session, data);
    if (type === "session.tool.called") return this._onToolCalled(session, data);
    if (type === "session.tool.success") return this._onToolFinished(session, data, "completed");
    if (type === "session.tool.failed") return this._onToolFinished(session, data, "error");
    if (type === "session.tool.progress") return;
    if (type === "session.usage.updated") return;
    if (type === "session.idle" || type === "session.compacted") return this._closeSession(session, false);
  }

  _state(session) {
    return session.open;
  }

  _ensureTurn(session, startMs) {
    if (!session.open) session.startTurn(startMs);
    return session.open;
  }

  // ---- 用户输入 ----
  _onInboxEnqueued(session, data) {
    const item = data.item || {};
    if (item.type !== "user") return;
    const payload = item.payload || {};
    const text = typeof payload.text === "string" ? payload.text : "";
    if (!text.trim()) return;
    const state = this._ensureTurn(session, Date.now());
    state.userCount += 1;
    if (session.systemPrompt && state.userCount === 1) {
      state.turn.recordSystemPrompt(session.systemPrompt, { start_ms: Date.now() });
    }
    if (state.pending.length || state.toolCallsSeen && Object.keys(state.toolCallsSeen).length) {
      this._flushText(state, false);
    }
    const followUp = state.userCount > 1;
    state.turn.recordUserQuery(text, { follow_up: followUp, start_ms: Date.now() });
  }

  // ---- 文本 / 推理 ----
  _buffer(state, partKey, kind, messageId, text, append) {
    let entry = state.pendingById[partKey];
    const key = partKey || (messageId || "") + ":" + kind;
    entry = state.pendingById[key];
    if (!entry) {
      entry = new PendingText(key, kind, messageId || "");
      state.pendingById[key] = entry;
      state.pending.push(entry);
    }
    if (append) entry.text += text;
    else if (text.length >= entry.text.length) entry.text = text;
    if (messageId) state.assistantMsgs[messageId] = true;
  }

  _onTextDelta(session, data, kind) {
    const state = this._state(session);
    if (!state) return;
    const messageId = data.assistantMessageID || data.messageID || "";
    const ordinal = data.ordinal === undefined ? 0 : data.ordinal;
    const partKey = kind + ":" + (messageId || "") + ":" + ordinal;
    const delta = data.delta;
    if (typeof delta === "string" && delta) this._buffer(state, partKey, kind, messageId, delta, true);
  }

  _onTextEnded(session, data, kind) {
    const state = this._state(session);
    if (!state) return;
    const messageId = data.assistantMessageID || data.messageID || "";
    const ordinal = data.ordinal === undefined ? 0 : data.ordinal;
    const partKey = kind + ":" + (messageId || "") + ":" + ordinal;
    const text = typeof data.text === "string" ? data.text : "";
    if (text) this._buffer(state, partKey, kind, messageId, text, false);
  }

  _flushText(state, final) {
    if (!state.pending.length) return;
    let lastTextKey = null;
    if (final) {
      for (const entry of state.pending) {
        if (entry.kind === TEXT && entry.text.trim()) lastTextKey = entry.key;
      }
    }
    for (const entry of state.pending) {
      const text = entry.text;
      if (!text.trim()) continue;
      let step;
      if (entry.kind === REASONING) {
        step = state.turn.recordReasoning(text, { start_ms: Date.now() });
      } else if (final && entry.key === lastTextKey) {
        step = state.turn.recordFinalAnswer(text, { start_ms: Date.now() });
      } else {
        step = state.turn.recordReasoning(text, { start_ms: Date.now() });
      }
      if (entry.messageId && !state.msgFirstStep[entry.messageId]) {
        state.msgFirstStep[entry.messageId] = step;
      }
    }
    state.pending = [];
    state.pendingById = {};
  }

  // ---- 工具 ----
  _toolName(state, callId) {
    const seen = state.toolCallsSeen[callId];
    return seen ? seen.name : "tool";
  }

  _emitToolCall(state, callId, name, args) {
    if (state.toolCallsSeen[callId]) return;
    this._flushText(state, false);
    const step = state.turn.recordToolCall(callId, name || "tool", args, { start_ms: Date.now() });
    state.toolCallsSeen[callId] = { name: name || "tool", span_id: step.span_id };
  }

  _onToolInputStarted(session, data) {
    const state = this._state(session);
    if (!state) return;
    const callId = data.id || data.callID;
    if (!callId) return;
    state.toolCallsSeen[callId] = state.toolCallsSeen[callId] || {
      name: data.name || "tool",
      span_id: null,
      pending: true,
    };
  }

  _onToolInputEnded(session, data) {
    const state = this._state(session);
    if (!state) return;
    const callId = data.id || data.callID;
    if (!callId) return;
    const args = this._parseArgs(data.text);
    const name = this._toolName(state, callId);
    delete state.toolCallsSeen[callId]; // 允许 _emitToolCall 真正落步
    this._emitToolCall(state, callId, name, args);
  }

  _onToolCalled(session, data) {
    const state = this._state(session);
    if (!state) return;
    const callId = data.id || data.callID;
    if (!callId) return;
    if (state.toolCallsSeen[callId] && state.toolCallsSeen[callId].span_id) return;
    const name = data.name || this._toolName(state, callId);
    delete state.toolCallsSeen[callId];
    this._emitToolCall(state, callId, name, data.input);
  }

  _parseArgs(text) {
    if (typeof text !== "string" || !text.trim()) return {};
    try {
      return JSON.parse(text);
    } catch (error) {
      return { raw: text };
    }
  }

  _onToolFinished(session, data, status) {
    const state = this._state(session);
    if (!state) return;
    const callId = data.id || data.callID;
    if (!callId) return;
    if (state.toolAnswered[callId]) return;
    state.toolAnswered[callId] = true;
    if (!state.toolCallsSeen[callId]) {
      this._emitToolCall(state, callId, this._toolName(state, callId), data.input);
    }
    const content = this._toolContent(data);
    const metadata = data.metadata || {};
    const exitCode = metadata.exit;
    let success;
    if (status === "error") {
      success = 0;
    } else if (exitCode !== undefined && exitCode !== null && exitCode !== 0) {
      success = 0;
    } else if (status === "completed") {
      success = judgeToolSuccess(content, undefined);
      if (success === -1) success = 1; // completed 且无 error/exit 0，以明确状态为准
    } else {
      success = judgeToolSuccess(content, undefined);
    }
    const fields = { start_ms: Date.now(), tool_name: this._toolName(state, callId), tool_success: success };
    const step = state.turn.recordToolResponse(callId, content, fields);
    if (status === "error" && data.error) step.content = asText(data.error);
  }

  _toolContent(data) {
    if (Array.isArray(data.content)) {
      const texts = [];
      for (const block of data.content) {
        if (block && typeof block.text === "string") texts.push(block.text);
      }
      if (texts.length) return texts.join("\n");
    }
    if (typeof data.content === "string") return data.content;
    if (data.error !== undefined && data.error !== null) return asText(data.error);
    return null;
  }

  // ---- step / token ----
  _onStepStarted(session, data) {
    const state = this._ensureTurn(session, data.started || Date.now());
    const messageId = data.assistantMessageID;
    if (messageId) state.assistantMsgs[messageId] = true;
  }

  _onStepEnded(session, data) {
    const state = this._state(session);
    if (!state) return;
    const messageId = data.assistantMessageID;
    const tokens = data.tokens;
    if (messageId && tokens && typeof tokens === "object") {
      const prompt = Number(tokens.input) || 0;
      const rawOutput = Number(tokens.output) || 0;
      const reasoning = Number(tokens.reasoning) || 0;
      const output = reasoning && rawOutput < reasoning ? rawOutput + reasoning : rawOutput;
      state.msgTokens[messageId] = { prompt: prompt, completion: output };
    }
  }

  _applyTokens(state) {
    for (const messageId of Object.keys(state.msgTokens)) {
      const step = state.msgFirstStep[messageId];
      if (!step) continue;
      const tok = state.msgTokens[messageId];
      if (tok.prompt) step.prompt_tokens_delta = tok.prompt;
      if (tok.completion) step.completion_tokens_delta = tok.completion;
    }
  }

  // ---- 收尾 ----
  _closeSession(session, failed) {
    const state = session.open;
    if (!state) return;
    if (failed) state.failed = true;
    session.open = null;
    this._closeTurn(session, state);
  }

  _failSession(session) {
    if (session.open) session.open.failed = true;
  }

  _closeTurn(session, state) {
    this._flushText(state, true);
    this._applyTokens(state);
    if (state.userCount === 0) return; // 没有用户输入的一轮不构成合法 trace

    if (state.failed) state.turn.completed = false;

    const last = state.turn.steps[state.turn.steps.length - 1];
    if (!last || last.message_type !== "final_answer") {
      state.turn.recordFinalAnswer("", { start_ms: Date.now() });
    }

    const meta = state.turn.metadata || (state.turn.metadata = {});
    meta.opencode_turn_no = state.turnNo;
    const assistantCount = Object.keys(state.assistantMsgs).length;
    if (assistantCount) {
      meta.opencode_assistant_messages = assistantCount;
      if (state.turn.apiCalls === null) state.turn.apiCalls = String(assistantCount);
    }

    let trace;
    try {
      trace = state.turn.asTrace();
    } catch (error) {
      this._log("build trace failed", String(error && error.stack));
      return;
    }
    this.traces.push(trace);
    if (this.exporter) {
      try {
        this.exporter.export(trace);
      } catch (error) {
        this._log("export failed", String(error && error.message));
      }
    }
  }

  /** 收尾所有未结束的轮（进程退出/缺 execution.succeeded 时的兜底）。 */
  close() {
    for (const sessionId of Object.keys(this.sessions)) {
      const session = this.sessions[sessionId];
      if (session.open) {
        const state = session.open;
        session.open = null;
        this._closeTurn(session, state);
      }
    }
    if (this.exporter && this.exporter.flush) this.exporter.flush();
    return this.traces;
  }

  _log(...args) {
    if (!this.options.debug) return;
    const line = "[agent-trace] " + args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" ");
    console.error(line);
    if (this.options.debugLog) {
      try {
        fs.appendFileSync(this.options.debugLog, "[" + process.pid + "] " + line + "\n");
      } catch (error) {
        /* ignore */
      }
    }
  }
}

export { canon, readOptions, generateId };
