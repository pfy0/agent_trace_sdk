/**
 * Agent Trace — OpenCode 插件入口（目录形态）。
 *
 * OpenCode 自动加载 `~/.config/opencode/plugins/<name>/index.js`，
 * 因此本目录整体拷进 `plugins/oc-plugin/` 后即可被识别，无需改配置。
 */
export { default } from "./agent-trace.js";
