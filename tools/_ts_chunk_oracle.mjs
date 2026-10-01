/**
 * 差分测试的 **TS 侧 oracle** —— 用真实 Blueprint 的分片器算一遍。
 *
 * 为什么不 `import` dist 里的模块：`dist/dispatch/outbound.js` 在**模块加载期**
 * 会拉起 db / api-client 一类重依赖（可能真连库）。这里改成从 dist 源码里
 * **只切出** chunkMarkdown / chunkLongParagraph / hardSplitLine 三段自包含函数，
 * 在无副作用的沙箱里 eval —— 语义逐字就是蓝本，但不拖任何运行时。
 *
 * 用法：`node _ts_chunk_oracle.mjs < corpus.json > chunks.json`
 */
import { readFileSync } from "node:fs";

const SRC = "/root/dev/wechatpadpro-openclaw/dist/dispatch/outbound.js";
const src = readFileSync(SRC, "utf8");

const start = src.indexOf("export function chunkMarkdown");
const capDecl = src.indexOf("const CODE_BLOCK_HARD_CAP");
const splitEnd = src.indexOf("function hardSplitLine");
if (start < 0 || capDecl < 0 || splitEnd < 0) {
  throw new Error("未能在 dist 中定位分片器源码（蓝本改版了？）");
}
// hardSplitLine 的结束 = 其后第一个只含 `}` 的行
const afterSplit = src.slice(splitEnd);
const closeIdx = afterSplit.indexOf("\n}\n");
if (closeIdx < 0) throw new Error("hardSplitLine 尾部未找到");

// CODE_BLOCK_HARD_CAP 的声明夹在 chunkMarkdown 与 hardSplitLine 之间 ⇒ 已被
// 下面这段 slice 覆盖，**不能**再单独拼一次（会 "already been declared"）。
if (capDecl < start || capDecl > splitEnd) {
  throw new Error("CODE_BLOCK_HARD_CAP 不在预期位置，蓝本改版了");
}
const body =
  src.slice(start, splitEnd).replace(/^export /gm, "") +
  afterSplit.slice(0, closeIdx + 3);

const factory = new Function(
  body + "\nreturn { chunkMarkdown, chunkLongParagraph, hardSplitLine, CODE_BLOCK_HARD_CAP };",
);
const { chunkMarkdown } = factory();

/** 蓝本入口：`chunker = (t) => t.length <= LIMIT ? [t] : chunkMarkdown(t, LIMIT)` */
const entry = (t, limit) => (t.length <= limit ? [t] : chunkMarkdown(t, limit));

const { corpus, limit } = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(corpus.map((t) => entry(t, limit))));
