import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const settingsSource = readFileSync(new URL("../app/settings/page.tsx", import.meta.url), "utf8");
const stylesSource = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../app/api.ts", import.meta.url), "utf8");
const readerSource = readFileSync(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);

test("workspace settings expose a persistent default-off automatic translation control", () => {
  assert.match(settingsSource, />自动翻译</);
  assert.match(settingsSource, /文章导入后自动提交 LLM 翻译/);
  assert.match(settingsSource, /status\?\.automatic_translation_enabled \?\? false/);
  assert.match(settingsSource, /updateAutomaticTranslation\(enabled\)/);
  assert.match(settingsSource, /手动单块翻译和整篇翻译入口仍然保留/);
  assert.match(apiSource, /automatic_translation_enabled: boolean/);
  assert.match(apiSource, /\/settings\/automatic-translation/);
  assert.match(readerSource, /if \(translation === null\)/);
  assert.match(readerSource, /translation-manual-link/);
  assert.match(readerSource, /retrying \? "翻译中…" : "翻译"/);
  assert.doesNotMatch(readerSource, /"翻译此块"/);
  assert.doesNotMatch(readerSource, /自动翻译已关闭/);
});

test("automatic translation and repair share the same padded toggle-card layout", () => {
  assert.equal(
    [...settingsSource.matchAll(/settings-card automatic-toggle-settings-card/g)].length,
    2,
  );
  assert.match(stylesSource, /\.automatic-toggle-settings-card \{ margin-top: 18px; padding: 20px 24px; \}/);
  assert.doesNotMatch(settingsSource, /automatic-translation-settings-card/);
  assert.doesNotMatch(settingsSource, /automatic-repair-settings-card/);
});

test("personal settings contain usable controls rather than fixed deployment diagnostics", () => {
  assert.doesNotMatch(settingsSource, /storage_backend|storage_root|database_url|repair_render_reasoning_effort/);
  assert.doesNotMatch(settingsSource, /Storage|Database|Task execution|Render repair reasoning|SQLite|LLM runtime|Block translation/);
  assert.doesNotMatch(settingsSource, /LLM ready|LLM setup|translation_target_language|translation_max_attempts/);
  assert.match(settingsSource, /<ProtectedLlmCredentialSettings/);
  assert.match(settingsSource, /updateTranslationSystemPrompt\(promptDraft\)/);
  assert.match(settingsSource, /resetTranslationSystemPrompt\(\)/);
  assert.match(settingsSource, /保存翻译提示词/);
  assert.match(settingsSource, /role="alert">\{error\}/);
});

test("saved credentials refresh authoritative status instead of assuming every provider is usable", () => {
  assert.match(settingsSource, /onSaved=\{\(\) => void refreshSavedCredentialStatus\(\)\}/);
  assert.match(settingsSource, /async function refreshSavedCredentialStatus\(\)[\s\S]*setStatus\(await getSettingsStatus\(\)\)/);
  assert.doesNotMatch(settingsSource, /(?:llm|translation|qa)_configured: true/);
  assert.doesNotMatch(settingsSource, /llm_provider: metadata\.provider|llm_model: metadata\.model/);
});
