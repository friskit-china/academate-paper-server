import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const apiSource = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
const readerSource = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const mapSource = await readFile(
  new URL("../app/papers/[paperId]/paper-visual-map.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");
const settingsSource = await readFile(new URL("../app/settings/page.tsx", import.meta.url), "utf8");

test("paper visual map is placed between abstract and structured reading workspace", () => {
  const abstractEnd = readerSource.indexOf("<PaperVisualMap paperId={paperId} />");
  const workspaceStart = readerSource.indexOf("{readingReady ? (");
  assert.ok(abstractEnd > 0);
  assert.ok(workspaceStart > abstractEnd);
});

test("visual map uses React Flow, ELK layout, pan controls, and durable UI preferences", () => {
  assert.match(mapSource, /from "@xyflow\/react"/);
  assert.match(mapSource, /elkjs\/lib\/elk\.bundled\.js/);
  assert.match(mapSource, /"elk\.algorithm": "layered"/);
  assert.match(mapSource, /sourcePosition: Position\.Right/);
  assert.match(mapSource, /targetPosition: Position\.Left/);
  assert.match(mapSource, /<Controls position="bottom-left"/);
  assert.match(mapSource, /<MiniMap/);
  assert.match(mapSource, /window\.localStorage\.setItem/);
  assert.match(mapSource, /onNodeDragStop/);
  assert.match(mapSource, /paper-map-resizer/);
  assert.match(stylesSource, /\.paper-visual-map/);
});

test("paper map adapts wheel input for trackpad pan without changing mouse or pinch controls", () => {
  assert.match(mapSource, /function paperMapWheelMode/);
  assert.match(mapSource, /event\.deltaMode !== 0/);
  assert.match(mapSource, /wheelDeltaY/);
  assert.match(mapSource, /event\.ctrlKey/);
  assert.match(mapSource, /addEventListener\("wheel", handleAdaptiveWheel, \{ capture: true, passive: false \}\)/);
  assert.match(mapSource, /event\.preventDefault\(\)/);
  assert.match(mapSource, /event\.stopPropagation\(\)/);
  assert.match(mapSource, /requestAnimationFrame\(applyTrackpadPan\)/);
  assert.match(mapSource, /instance\.setViewport\(next\)/);
  assert.match(mapSource, /panOnDrag/);
  assert.match(mapSource, /zoomOnPinch/);
  assert.match(mapSource, /zoomOnScroll/);
});

test("visual map exposes progressive generation, verified evidence, and explicit retries", () => {
  assert.match(apiSource, /getPaperVisualMap/);
  assert.match(apiSource, /generatePaperVisualMap/);
  assert.match(apiSource, /generatePaperMapAnswer/);
  assert.match(mapSource, /回答这个问题/);
  assert.match(mapSource, /重试回答/);
  assert.match(mapSource, /显式重试/);
  assert.match(mapSource, /原文证据/);
  assert.match(mapSource, /jumpToPaperBlock\(citation\.block_id\)/);
  assert.match(mapSource, /requestedGeneration/);
  assert.match(mapSource, /requestedGenerationPending/);
  assert.match(mapSource, /shouldPoll/);
  assert.match(mapSource, /研究图生成中…/);
  assert.match(mapSource, /部分已完成/);
  assert.match(mapSource, /已完成/);
  assert.match(mapSource, /paper-map-generation-state/);
  assert.match(mapSource, /paper-map-generation-active/);
  assert.match(stylesSource, /paper-map-generation-dot/);
  assert.match(stylesSource, /paper-map-status-breathe/);
});

test("paper map generation is user initiated and regeneration is confirmed in-app", () => {
  assert.match(mapSource, /开始生成/);
  assert.match(mapSource, /确认重新生成论文理解图/);
  assert.match(mapSource, /确认重新生成/);
  assert.match(mapSource, /setConfirmRegeneration\(true\)/);
  assert.doesNotMatch(mapSource, /window\.confirm|window\.alert|alert\(/);
});

test("questions own answers and concepts remain typed graph nodes", () => {
  assert.match(apiSource, /concept_type: string \| null/);
  assert.match(mapSource, /CONCEPT:/);
  assert.match(mapSource, /selectedQuestion\.answer_text/);
  assert.match(mapSource, /selectedQuestion\?\.citations/);
  assert.match(mapSource, /自动整理/);
  assert.match(mapSource, /fitView/);
});

test("paper map supports explicit child creation, connections, edge labels, and safe deletion", () => {
  assert.match(apiSource, /createPaperMapFollowup/);
  assert.match(apiSource, /createPaperMapConcept/);
  assert.match(apiSource, /createPaperMapEdge/);
  assert.match(apiSource, /updatePaperMapEdge/);
  assert.match(apiSource, /deletePaperMapEdge/);
  assert.match(apiSource, /deletePaperMapNode/);
  assert.match(mapSource, /新增子节点/);
  assert.match(mapSource, /向 AI 提问/);
  assert.match(mapSource, /记录灵感和概念/);
  assert.match(mapSource, /getPaperMapQuestionLibrary/);
  assert.match(mapSource, /从问题库选择问题/);
  assert.match(mapSource, /canChooseFromQuestionLibrary/);
  assert.match(mapSource, /可以从问题库选择并继续修改，也可以直接输入自己的问题/);
  assert.doesNotMatch(mapSource, /Question · AI 回答|Concept · 手动填写/);
  assert.match(mapSource, /onConnect=/);
  assert.match(mapSource, /onEdgeDoubleClick=/);
  assert.match(mapSource, /event\.key !== "Delete"/);
  assert.match(mapSource, /确认删除节点/);
  assert.doesNotMatch(mapSource, /paper-map-followup|继续追问/);
  assert.match(stylesSource, /OUT · 出/);
  assert.match(stylesSource, /IN · 入/);
  assert.match(stylesSource, /paper-map-cross-edge/);
  assert.match(stylesSource, /\.paper-map-child-composer > h3 \{[^}]*font-size: 16px/);
  assert.match(stylesSource, /\.paper-map-child-composer \{[^}]*padding: 16px[^}]*width: min\(340px/);
  assert.match(stylesSource, /\.paper-map-child-type-actions button \{[^}]*font-size: 10px/);
  assert.match(stylesSource, /\.paper-map-child-type-actions button \{[^}]*min-height: 48px/);
});

test("settings question library has card spacing and inline edit-save-delete controls", () => {
  assert.match(stylesSource, /\.paper-map-question-settings \{[^}]*margin-top: 18px/);
  assert.match(stylesSource, /\.paper-map-question-settings \{[^}]*padding: 22px 24px/);
  assert.match(apiSource, /updatePaperMapQuestion/);
  assert.match(settingsSource, /beginEditingQuestion/);
  assert.match(settingsSource, /Save question:/);
  assert.match(settingsSource, /Delete question:/);
  assert.match(settingsSource, /question-save-button/);
});

test("tree collapse compacts visible nodes while anchoring the toggled node and viewport", () => {
  assert.match(mapSource, /hiddenDescendantIds/);
  assert.match(mapSource, /collapsedNodeIds/);
  assert.match(mapSource, /hidden: hiddenNodeIds\.has\(node\.id\)/);
  assert.match(mapSource, /saveStoredUi\(paperId, \{ collapsedNodeIds:/);
  assert.match(mapSource, /折叠子节点/);
  assert.match(mapSource, /展开子节点/);
  const toggleSource = mapSource.match(
    /const toggleNodeCollapse = useCallback[\s\S]*?\n  }, \[/,
  )?.[0] ?? "";
  assert.match(toggleSource, /await layoutMap/);
  assert.match(toggleSource, /anchorBefore/);
  assert.match(toggleSource, /anchorAfter/);
  assert.match(toggleSource, /offsetX/);
  assert.match(toggleSource, /offsetY/);
  assert.doesNotMatch(toggleSource, /fitView|setViewport/);
  assert.match(mapSource, /treeChildCounts/);
  assert.match(stylesSource, /right: -16px/);
  assert.match(stylesSource, /\.paper-map-node-collapse/);
});

test("layout changes retain selected node or edge focus and highlight edge endpoints", () => {
  assert.match(mapSource, /function selectedFocusPoint/);
  assert.match(mapSource, /selectedNodeIdRef/);
  assert.match(mapSource, /selectedEdgeIdRef/);
  assert.match(mapSource, /graphStructureSignature/);
  assert.match(mapSource, /graphStructureSignatureRef/);
  assert.match(mapSource, /shouldFocusAfterLayout/);
  assert.match(mapSource, /if \(shouldFocusAfterLayout\) focusSelectedItem\(nextNodes, nextEdges\)/);
  assert.match(mapSource, /duration: 320/);
  assert.match(mapSource, /ease: \(progress\) => 1 - \(1 - progress\) \*\* 3/);
  assert.doesNotMatch(mapSource, /setCenter\(point\.x, point\.y, \{ zoom, duration: 0 \}\)/);
  assert.match(mapSource, /focusSelectedItem\(compacted, nextEdges\)/);
  assert.match(mapSource, /!focusSelectedItem\(arranged, edgesRef\.current\)/);
  assert.match(mapSource, /paper-map-edge-source/);
  assert.match(mapSource, /paper-map-edge-target/);
  assert.match(mapSource, /selected: node\.id === selectedNodeId/);
  assert.match(mapSource, /selectedFlowEdge\?\.target, selectedNodeId/);
  assert.match(mapSource, /edges=\{renderedEdges\}/);
  assert.match(mapSource, /nodes=\{renderedNodes\}/);
  assert.match(stylesSource, /\.paper-map-flow-node\.paper-map-edge-source/);
  assert.match(stylesSource, /\.paper-map-flow-node\.paper-map-edge-target/);
});
