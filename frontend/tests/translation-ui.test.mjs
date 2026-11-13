import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const apiSource = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
const readerSource = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const settingsSource = await readFile(new URL("../app/settings/page.tsx", import.meta.url), "utf8");
const stylesSource = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");
const layoutSource = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
const toggleSource = await readFile(
  new URL("../app/bilingual-mode-toggle.tsx", import.meta.url),
  "utf8",
);
const blockNavigationSource = await readFile(
  new URL("../app/papers/[paperId]/paper-block-navigation.ts", import.meta.url),
  "utf8",
);

test("long unbroken list tokens wrap in source and translation views", () => {
  assert.match(stylesSource, /\.reader-list li, \.translation-copy li/);
  assert.match(stylesSource, /overflow-wrap: anywhere/);
  assert.match(stylesSource, /word-break: break-word/);
});

test("reader exposes progressive bilingual translation controls", () => {
  assert.match(apiSource, /getPaperTranslationProgress/);
  assert.match(apiSource, /retranslateBlock/);
  assert.match(apiSource, /retranslatePaper/);
  assert.match(apiSource, /translateRemainingPaper/);
  assert.match(apiSource, /updateTranslationSystemPrompt/);
  assert.match(apiSource, /resetTranslationSystemPrompt/);
  assert.match(layoutSource, /BilingualModeToggle/);
  assert.match(toggleSource, /双语对照模式/);
  assert.match(toggleSource, /role="switch"/);
  assert.match(readerSource, /BILINGUAL_MODE_CHANGE_EVENT/);
  assert.match(readerSource, /readingMode === "bilingual" && translationProgress/);
  assert.doesNotMatch(readerSource, /className="reading-mode-toggle"/);
  assert.match(readerSource, /TranslationPanel/);
  assert.match(readerSource, /translation\.status === "FAILED"/);
  assert.match(readerSource, /translation\.cache_hit/);
  assert.match(readerSource, /translation-entity-tooltip/);
  assert.match(readerSource, /ABSTRACT/);
  assert.match(readerSource, /自定义 System Prompt/);
  assert.match(readerSource, /重新翻译全部/);
  assert.match(readerSource, /翻译剩余内容/);
  assert.match(readerSource, /translationProgress\.missing === 0 && translationProgress\.failed === 0/);
  assert.match(readerSource, /translationProgress\.pending > translationProgress\.missing/);
  assert.match(readerSource, /result\.created \? `新增/);
  assert.match(readerSource, /result\.retried \? `重试/);
  assert.match(readerSource, /确认清空并重新翻译/);
  assert.match(readerSource, /togglePromptEditor/);
  assert.match(readerSource, /translation-manual-link/);
  assert.match(readerSource, /翻译中…/);
  assert.match(readerSource, /English source/);
  assert.match(readerSource, /aria-label="English source term"/);
  assert.doesNotMatch(readerSource, /下次保留英文/);
  assert.doesNotMatch(readerSource, /addDoNotTranslateTerm/);
  assert.doesNotMatch(settingsSource, /Do-not-translate terminology/);
  assert.doesNotMatch(settingsSource, /Save terminology list/);
  assert.match(settingsSource, /恢复原状/);
  assert.doesNotMatch(settingsSource, /terminology-tag/);
  assert.doesNotMatch(settingsSource, /Filter do-not-translate terms/);
  assert.doesNotMatch(readerSource, /await retryBlockTranslation/);
});

test("bilingual mode changes preserve the reader's block-relative viewport anchor", () => {
  assert.match(blockNavigationSource, /export function capturePaperViewportAnchor/);
  assert.match(blockNavigationSource, /focusRatio/);
  assert.match(blockNavigationSource, /previousBlockId/);
  assert.match(blockNavigationSource, /nextBlockId/);
  assert.match(blockNavigationSource, /export function restorePaperViewportAnchor/);
  assert.match(blockNavigationSource, /rect\.height \* focusRatio/);
  assert.match(blockNavigationSource, /document\.documentElement\.style\.scrollBehavior = "auto"/);
  assert.match(readerSource, /pendingReadingModeAnchorRef/);
  assert.match(readerSource, /capturePaperViewportAnchor\(paperReadingBlockElements\(root\)\)/);
  assert.match(readerSource, /useLayoutEffect\(\(\) => \{/);
  assert.match(readerSource, /restorePaperViewportAnchor/);
  assert.match(readerSource, /window\.requestAnimationFrame/);
});
