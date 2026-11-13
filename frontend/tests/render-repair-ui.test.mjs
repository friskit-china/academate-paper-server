import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const readerSource = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");

test("table repair queues replacement LaTeX rendering and retains accessible fallback data", () => {
  assert.match(readerSource, /tableRender\.status === "repaired"/);
  assert.match(readerSource, /AI-repaired table/);
  assert.match(readerSource, /Validated replacement LaTeX is queued/);
  assert.match(stylesSource, /\.table-render-repaired/);
  assert.match(readerSource, /正在使用本地 LaTeX 生成表格图像/);
  assert.match(readerSource, /View accessible fallback table data/);
  assert.match(stylesSource, /\.table-render-placeholder/);
  assert.match(readerSource, /tableRender\.repair_eligible === false/);
  assert.match(readerSource, /This table is too large for a lossless AI repair/);
});

test("self-contained LaTeX figures use their durable derived SVG state", () => {
  assert.match(readerSource, /block\.render_payload\.figure_render/);
  assert.match(readerSource, /paperBlockRenderAssetUrl\(paperId, block\.id/);
  assert.match(readerSource, /正在使用本地 LaTeX 生成图像/);
  assert.match(stylesSource, /\.latex-figure-render/);
  assert.match(readerSource, /"RENDER_FIGURE"/);
  assert.match(
    readerSource,
    /asset\.mime_type === "application\/pdf" \|\| asset\.mime_type\.startsWith\("image\/"\)/,
  );
  assert.match(readerSource, /figureRender\.status === "failed"/);
  assert.match(readerSource, /isEmbeddedDiagram/);
  assert.match(readerSource, /environment === "tikzcd"/);
  assert.match(readerSource, /Rendered commutative diagram/);
  assert.match(readerSource, /figureRender\.repair_eligible === false/);
});

test("layout-sensitive algorithms and custom environments use native LaTeX block assets", () => {
  assert.match(readerSource, /block\.block_type === "LATEX_BLOCK"/);
  assert.match(readerSource, /block\.render_payload\.latex_block_render/);
  assert.match(readerSource, /latex-native-block-render/);
  assert.match(readerSource, /正在使用本地 LaTeX 还原算法排版/);
  assert.match(readerSource, /正在使用本地 LaTeX 还原原始排版/);
  assert.match(readerSource, /查看可检索算法文本/);
  assert.match(readerSource, /"RENDER_LATEX_BLOCK"/);
  assert.match(stylesSource, /\.latex-native-block-render/);
});

test("formula repair distinguishes applied, queued, and retryable error states", () => {
  assert.match(readerSource, /result\.applied \|\| result\.status === "SUCCEEDED"/);
  assert.match(readerSource, /渲染结果已生成 · 正在刷新/);
  assert.match(readerSource, /原版 LaTeX 正在本地渲染/);
  assert.match(readerSource, /AI-assisted repair running…/);
  assert.match(readerSource, /重试原版 LaTeX 渲染/);
  assert.match(readerSource, /尝试 AI 辅助修复/);
  assert.match(readerSource, /requestLatexFormulaRender/);
  assert.match(readerSource, /getPaperTaskDetail\(paperId, taskId\)/);
  assert.match(readerSource, /detail\.task_error_message/);
  assert.match(readerSource, /pollingDeadline\.current/);
  assert.match(readerSource, /this formula still could not be rendered/);
  assert.doesNotMatch(readerSource, /AI-assisted repair unavailable/);
  assert.match(readerSource, /setRenderRefreshRevision/);
});

test("inline formulas use syntax-only normalization and verified fallback selectors", () => {
  assert.match(readerSource, /import "katex\/contrib\/mhchem"/);
  assert.doesNotMatch(readerSource, /"\\\\SI":/);
  assert.match(readerSource, /function normalizeKatexLatex/);
  assert.match(readerSource, /\\\\boldmath/);
  assert.match(readerSource, /\\\\boldsymbol/);
  assert.match(readerSource, /align\\\*\?\|gather\\\*\?\|multline\\\*\?/);
  assert.match(readerSource, /compatibilityEnvironment/);
  assert.match(readerSource, /environment=\{environment \?\? undefined\}/);
  assert.match(readerSource, /environment=\{part\.environment\}/);
  assert.match(readerSource, /label\|tag/);
  assert.match(readerSource, /formulaSelector/);
  assert.match(readerSource, /path: \[\.\.\.formulaRepair\.pathPrefix, index\]/);
  assert.match(readerSource, /target: "TRANSLATION"/);
  assert.match(readerSource, /translationId: translation\.id/);
});

test("browser normalization recognizes standard eqnarray wrappers", () => {
  assert.match(readerSource, /equation\\\*\?\|eqnarray\\\*\?/);
  assert.match(readerSource, /sourceEnvironment\?\.startsWith\("eqnarray"\)/);
  assert.match(readerSource, /KATEX_COMPATIBILITY_MACROS/);
  assert.match(readerSource, /"\\\\slash": "\/"/);
  assert.match(readerSource, /macros: \{ \.\.\.KATEX_COMPATIBILITY_MACROS \}/);
});

test("failed inline formulas require user-controlled TeX fallback before AI", () => {
  assert.doesNotMatch(readerSource, /AutomaticAiRepairContext/);
  assert.match(readerSource, /KaTeX 渲染失败，使用原版 LaTeX 渲染/);
  assert.match(readerSource, /state === "local-error"/);
  assert.match(readerSource, /requestAiRepair/);
  assert.match(readerSource, /paper\.abstract_block_id/);
  assert.match(readerSource, /paper\.abstract_render_payload/);
  assert.match(readerSource, /pathPrefix: \["segments"\]/);
  assert.match(readerSource, /latex-formula-render/);
});
