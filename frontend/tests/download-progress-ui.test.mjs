import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const apiSource = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
const readerSource = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");

test("source imports expose durable byte progress and resume state", () => {
  assert.match(apiSource, /download_progress:/);
  assert.match(apiSource, /bytes_remaining: number \| null/);
  assert.match(apiSource, /resumed_bytes: number/);
  assert.match(readerSource, /task-download-progress/);
  assert.match(readerSource, /remaining/);
  assert.match(readerSource, />resumed</);
  assert.match(stylesSource, /\.task-download-track/);
});

test("paper state refreshes when a suspended tab becomes visible again", () => {
  assert.match(readerSource, /visibilitychange/);
  assert.match(readerSource, /document\.visibilityState === "visible"/);
  assert.match(readerSource, /window\.addEventListener\("focus"/);
});
