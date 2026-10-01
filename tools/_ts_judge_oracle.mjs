// _ts_judge_oracle.mjs —— 用 **TS 真产物** 跑 judge，把请求体原样 dump 出来做差分。
//
// 为什么不用"读 TS 源码再人工对照"：那验的是我读懂了没有，不是两端产出一样。
// 这里拦截全局 fetch（safeFetch 内部就是调它），所以拿到的是 TS **真正发出去**的字节。
//
// 输出: 一行一个 JSON（{case, url, headers, body}），交给 tools/verify_judge_diff.py 比对。
//
// ⚠️ **必须传具体产物文件的路径**，别传仓库根 —— 曾经踩过：
//    ``<repo>/src/llm-judge.js`` 是 9-05 的**陈旧 tsc 产物**（``src/**/*.js`` 在
//    .gitignore 里，没人重编），里面既没有 v1.6.1 的 ``thinking``也没有 v1.11.0 的
//    ``image_url`` ⇒ 差分"全线不一致"，而那是拿旧构建比新源码，纯噪声。
//    当前真产物在**部署目录的 dist**下：
//      /root/.openclaw/extensions/wechatpadpro/dist/llm-judge.js
//
// 用法: node tools/_ts_judge_oracle.mjs <llm-judge.js 的路径>
import { pathToFileURL } from "node:url";

const JS_PATH = process.argv[2];
if (!JS_PATH) {
  console.error("用法: node _ts_judge_oracle.mjs <path/to/llm-judge.js>");
  process.exit(2);
}

/** 每个用例：{case, reply, args} */
const CASES = [
  {
    case: "openai-text",
    reply: { choices: [{ message: { content: '{"overallScore":0.8}' }, finish_reason: "stop" }] },
    args: {
      model: "deepseek-v4-flash", userPrompt: "打分", systemPrompt: "只回 JSON",
      maxTokens: 300, timeoutMs: 5000,
      creds: { apiKey: "k", baseUrl: "https://api.deepseek.com", format: "openai" },
    },
  },
  {
    case: "openai-text-nosystem",
    reply: { choices: [{ message: { content: "x" }, finish_reason: "stop" }] },
    args: {
      model: "m", userPrompt: "P",
      creds: { apiKey: "k", baseUrl: "https://api.deepseek.com", format: "openai" },
    },
  },
  {
    case: "openai-nothink-off",
    reply: { choices: [{ message: { content: "x" }, finish_reason: "stop" }] },
    args: {
      model: "m", userPrompt: "P",
      creds: { apiKey: "k", baseUrl: "https://api.deepseek.com", format: "openai", noThink: false },
    },
  },
  {
    case: "openai-images",
    reply: { choices: [{ message: { content: "x" }, finish_reason: "stop" }] },
    args: {
      model: "m", userPrompt: "P", systemPrompt: "S",
      creds: { apiKey: "k", baseUrl: "https://api.deepseek.com", format: "openai" },
      images: ["https://a/x.jpg", "data:image/png;base64,AA", "file:///etc/passwd", ""],
    },
  },
  {
    case: "anthropic-text",
    reply: { content: [{ type: "text", text: "hi" }], stop_reason: "end_turn" },
    args: {
      model: "mini", userPrompt: "打分", systemPrompt: "SYS", maxTokens: 300, timeoutMs: 5000,
      creds: { apiKey: "k", baseUrl: "https://api.minimaxi.com/anthropic", format: "anthropic" },
    },
  },
  {
    case: "anthropic-nosystem",
    reply: { content: [{ type: "text", text: "hi" }], stop_reason: "end_turn" },
    args: {
      model: "m", userPrompt: "P",
      creds: { apiKey: "k", baseUrl: "https://api.minimaxi.com/anthropic", format: "anthropic" },
    },
  },
  {
    case: "anthropic-images-ignored",
    reply: { content: [{ type: "text", text: "hi" }], stop_reason: "end_turn" },
    args: {
      model: "m", userPrompt: "P",
      creds: { apiKey: "k", baseUrl: "https://api.minimaxi.com/anthropic", format: "anthropic" },
      images: ["https://a/x.jpg"],
    },
  },
];

let current = null;
const captured = [];

globalThis.fetch = async (url, init) => {
  captured.push({
    case: current.case,
    url: String(url),
    headers: init?.headers ?? {},
    // TS 侧 body 是 JSON.stringify 出来的字符串 —— 这里**不解析**，原样带出去，
    // 让比对看的是真字节（键序、空格、转义全在内）。
    body: init?.body,
  });
  return {
    ok: true,
    status: 200,
    async text() { return JSON.stringify(current.reply); },
    async json() { return current.reply; },
  };
};

const { callJudge } = await import(pathToFileURL(JS_PATH).href);

for (const c of CASES) {
  current = c;
  try {
    await callJudge(c.args);
  } catch (e) {
    console.log(JSON.stringify({ case: c.case, error: String(e) }));
  }
}

for (const r of captured) console.log(JSON.stringify(r));
