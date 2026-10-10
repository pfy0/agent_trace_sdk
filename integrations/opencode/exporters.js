/**
 * 导出器：把 Trace 写到本地文件或内存（对齐 Python 的 JsonExporter / JsonlExporter）。
 *
 * - JsonExporter：单个 JSON 文件，顶层是 Trace 数组，每条 trace 落盘即增量写（异常退出不丢）。
 * - JsonlExporter：一行一个 step 的裸 JSON（便于 jq / ClickHouse）。
 * - MemoryExporter：只留内存，测试用。
 */

import fs from "node:fs";
import path from "node:path";

function ensureParent(filePath) {
  const dir = path.dirname(filePath);
  if (dir && !fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

export class JsonExporter {
  constructor(filePath, options) {
    const opts = options || {};
    this.path = filePath || null;
    this.indent = opts.indent === undefined ? 2 : opts.indent;
    this.traces = [];
  }

  export(trace) {
    this.traces.push(trace);
    if (this.path) this.flush();
  }

  flush() {
    if (!this.path) return;
    ensureParent(this.path);
    const payload =
      this.indent === null
        ? JSON.stringify(this.traces)
        : JSON.stringify(this.traces, null, this.indent);
    const tmp = this.path + ".tmp";
    fs.writeFileSync(tmp, payload, "utf8");
    fs.renameSync(tmp, this.path);
  }
}

export class JsonlExporter {
  constructor(filePath) {
    this.path = filePath || null;
    this.rows = [];
    if (this.path && fs.existsSync(this.path)) fs.unlinkSync(this.path);
  }

  export(trace) {
    const meta = Object.assign({}, trace);
    const steps = meta.conversations || [];
    delete meta.conversations;
    for (const step of steps) {
      const row = Object.assign({}, meta, step);
      this.rows.push(row);
      if (this.path) {
        ensureParent(this.path);
        fs.appendFileSync(this.path, JSON.stringify(row) + "\n", "utf8");
      }
    }
  }

  flush() {}
}

export class MemoryExporter {
  constructor() {
    this.traces = [];
  }

  export(trace) {
    this.traces.push(trace);
  }

  flush() {}
}
