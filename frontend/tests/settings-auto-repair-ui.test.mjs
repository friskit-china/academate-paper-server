import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const settingsSource = readFileSync(new URL("../app/settings/page.tsx", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../app/api.ts", import.meta.url), "utf8");

test("workspace settings expose a persistent default-off automatic repair control", () => {
  assert.match(settingsSource, /自动 AI 辅助修复/);
  assert.match(settingsSource, /自动提交排版失败内容的 AI 辅助修复/);
  assert.match(settingsSource, /status\?\.automatic_ai_repair_enabled \?\? false/);
  assert.match(settingsSource, /updateAutomaticAiRepair\(enabled\)/);
  assert.match(settingsSource, /仅在你手动发起修复时使用 AI/);
  assert.match(settingsSource, /可能产生模型费用/);
  assert.match(apiSource, /automatic_ai_repair_enabled: boolean/);
  assert.match(apiSource, /\/settings\/automatic-ai-repair/);
});
