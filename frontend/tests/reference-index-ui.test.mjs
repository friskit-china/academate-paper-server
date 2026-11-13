import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const readerSource = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");

test("document references render safe block links and explicit unresolved states", () => {
  assert.match(readerSource, /type: "text" \| "math" \| "entity" \| "reference"/);
  assert.match(readerSource, /segment\.status === "RESOLVED" && segment\.target_block_id/);
  assert.match(readerSource, /href={`#block-\$\{segment\.target_block_id\}`}/);
  assert.match(readerSource, /这个引用标签在源码中出现了多次/);
  assert.match(readerSource, /页面引用依赖原始 PDF 排版/);
  assert.match(stylesSource, /\.paper-reference-unresolved/);
});

test("referenceable blocks expose the shared semantic number", () => {
  assert.match(readerSource, /function blockNumber/);
  assert.match(readerSource, /Equation \$\{displayNumber\}/);
  assert.match(readerSource, /Figure \{displayNumber\}/);
  assert.match(readerSource, /Table \{displayNumber\}/);
  assert.match(readerSource, /Algorithm \{displayNumber\}/);
  assert.match(stylesSource, /\.semantic-caption-number/);
});

test("reference jumps reveal the whole block above the sticky header", () => {
  assert.match(stylesSource, /--paper-anchor-offset: 104px/);
  assert.match(stylesSource, /scroll-margin-top: var\(--paper-anchor-offset\)/);
  assert.match(stylesSource, /\.reader-heading:target, \.reader-block:target/);
  assert.match(stylesSource, /@keyframes paper-anchor-arrival/);
  assert.match(stylesSource, /prefers-reduced-motion: reduce/);
});

test("normalized theorem continuations remain visually explicit", () => {
  assert.match(readerSource, /block\.render_payload\.continuation === true/);
  assert.match(readerSource, /isContinuation \? " \(continued\)"/);
});
