// Node 侧 runner：读事件 JSONL，用 JS SDK 归一，打印 Trace 数组 JSON。
import fs from "node:fs";
import { OpencodeCollector } from "../integrations/opencode/session-collector.js";

const file = process.argv[2];
const lines = fs.readFileSync(file, "utf8").split("\n").filter((l) => l.trim());
const collector = new OpencodeCollector({ debug: false });
for (const line of lines) collector.ingest(JSON.parse(line));
collector.close();
process.stdout.write(JSON.stringify(collector.traces, null, 2));
