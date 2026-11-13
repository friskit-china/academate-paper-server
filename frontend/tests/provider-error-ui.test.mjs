import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const readerSource = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
const stylesSource = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");

test("provider rejections expose complete diagnostics without hiding the user-facing reason", () => {
  assert.match(readerSource, /function ProviderErrorDetails/);
  assert.match(readerSource, /内容安全策略拒绝了这次请求/);
  assert.match(readerSource, /完整 API 错误/);
  assert.match(readerSource, /translation\.provider_request_id/);
  assert.match(readerSource, /task-api-error/);
  assert.match(apiSource, /provider_request_id: string \| null/);
  assert.match(stylesSource, /\.provider-error-details pre/);
  assert.match(stylesSource, /white-space: pre-wrap/);
});
