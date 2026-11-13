import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const apiSource = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
const readerSource = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const annotationSource = await readFile(
  new URL("../app/papers/[paperId]/annotation-workspace.tsx", import.meta.url),
  "utf8",
);
const blockNavigationSource = await readFile(
  new URL("../app/papers/[paperId]/paper-block-navigation.ts", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");

test("reader exposes persistent source and translation annotation targets", () => {
  assert.match(readerSource, /data-annotation-target="SOURCE"/);
  assert.match(readerSource, /data-annotation-target="TRANSLATION"/);
  assert.match(readerSource, /PersistentAnnotationHighlights/);
  assert.match(annotationSource, /start_offset/);
  assert.match(annotationSource, /exact_quote/);
  assert.match(annotationSource, /prefix/);
  assert.match(annotationSource, /suffix/);
  assert.match(annotationSource, /text_hash/);
  assert.match(annotationSource, /::highlight\(paper-highlights\)/);
  assert.match(readerSource, /readerRootRef=\{documentViewportRef\}/);
  assert.match(annotationSource, /new MutationObserver\(refreshHighlights\)/);
  assert.match(annotationSource, /observer\.observe\(readerRoot, \{ childList: true, characterData: true, subtree: true \}\)/);
  assert.match(annotationSource, /range\.getClientRects\(\)/);
  assert.match(annotationSource, /onAnnotationActivate/);
  assert.match(annotationSource, /aria-label="Edit annotation"/);
  assert.match(annotationSource, /annotation\.is_highlighted/);
  assert.match(annotationSource, /annotation-marker-button/);
  assert.match(annotationSource, /::highlight\(paper-activities\)/);
  assert.match(annotationSource, /document\.addEventListener\("pointerdown", onPointerDown, true\)/);
  assert.match(annotationSource, /toolbarRef\.current\?\.contains\(target\)/);
  assert.match(annotationSource, /window\.getSelection\(\)\?\.removeAllRanges\(\)/);
  assert.match(readerSource, /data-annotation-auxiliary="true"/);
  assert.match(readerSource, /className="paper-reference"[\s\S]*data-annotation-auxiliary="true"/);
  assert.match(readerSource, /paper-reference paper-reference-unresolved"[\s\S]*data-annotation-auxiliary="true"/);
  assert.match(readerSource, /segment\.type === "reference"[\s\S]*<span data-annotation-auxiliary="true"> <\/span>/);
  assert.match(annotationSource, /function annotationRangeText\(range: Range\)/);
  assert.match(annotationSource, /querySelectorAll\(ANNOTATION_AUXILIARY_SELECTOR\)/);
  assert.match(readerSource, /data-annotation-math=\{latex\.normalize\("NFC"\)\}/);
  assert.match(annotationSource, /function expandRangeToWholeMath\(root: Element, original: Range\)/);
  assert.match(annotationSource, /node\.dataset\.annotationMath/);
  assert.match(annotationSource, /function translatedAnnotationText\(translation: ContentBlock\["translation"\]\)/);
  assert.match(annotationSource, /item\.type === "math" \? item\.latex : item\.text/);
  assert.match(annotationSource, /materialized\.join\("\\n"\)/);
  assert.match(annotationSource, /range\.selectNodeContents\(segment\.node\)/);
  assert.match(annotationSource, /selection\.addRange\(range\)/);
  assert.match(annotationSource, /function textRangesForOffsets\(root: Element, start: number, end: number\): Range\[\]/);
  assert.match(annotationSource, /interactiveRanges\.push\(\.\.\.ranges\.map/);
  assert.match(annotationSource, /\.\.\.\(groups\.get\(name\) \?\? \[\]\), \.\.\.ranges/);
  assert.match(annotationSource, /domText === targetText && domStart < domEnd/);
  assert.match(annotationSource, /const quote = points\.slice\(start, end\)\.join\(""\)/);
  assert.match(annotationSource, /classList\.add\("annotation-selecting"\)/);
  assert.match(stylesSource, /\.annotation-selecting \.translation-entity-tooltip/);
  assert.match(readerSource, /!\/\^\\s\/\.test\(currentValue\)/);
  assert.match(readerSource, /!\/\\s\$\/\.test\(previousValue\)/);
  assert.match(readerSource, /const addSpace = !preserveSpacing/);
  assert.match(readerSource, /payload=\{\{ segments: unit\.segments \}\}[\s\S]*preserveSpacing/);
});

test("annotation mapping errors are friendly and long papers provide a fixed back-to-top action", () => {
  assert.match(annotationSource, /抱歉，暂时还不支持为这类文本区域添加标注。/);
  assert.doesNotMatch(annotationSource, /could not be mapped safely/);
  assert.match(readerSource, /window\.scrollY > 560/);
  assert.match(readerSource, /window\.scrollTo\(\{ behavior: reducedMotion \? "auto" : "smooth", top: 0 \}\)/);
  assert.match(readerSource, /window\.history\.replaceState/);
  assert.match(readerSource, /aria-label="回到页面顶部"/);
  assert.match(
    readerSource,
    /const scrollToPageTop = useCallback\(\(\) => \{[\s\S]*rememberPaperBlockJumpOrigin\(\);[\s\S]*clearPaperReadingPosition\(\);[\s\S]*top: 0/,
  );
  assert.match(stylesSource, /\.back-to-top-button \{[^}]*position: fixed;[^}]*right: 24px;/);
});

test("paper block jumps expose an interruptible return-to-origin action", () => {
  assert.match(blockNavigationSource, /PAPER_BLOCK_JUMP_EVENT/);
  assert.match(blockNavigationSource, /top: window\.scrollY/);
  assert.match(blockNavigationSource, /target\.scrollIntoView/);
  assert.match(blockNavigationSource, /jumpToPaperElement/);
  assert.match(blockNavigationSource, /options\.updateHash/);
  assert.match(readerSource, /paperBlockJumpOrigin/);
  assert.match(readerSource, /aria-label="返回到刚才位置"/);
  assert.match(readerSource, /window\.scrollTo\(\{ behavior: reducedMotion \? "auto" : "smooth", top: targetTop \}\)/);
  assert.match(readerSource, /Math\.abs\(window\.scrollY - targetTop\) <= 2/);
  assert.match(readerSource, /window\.addEventListener\("wheel", interruptReturn/);
  assert.match(readerSource, /window\.addEventListener\("touchstart", interruptReturn/);
  assert.match(readerSource, /window\.addEventListener\("pointerdown", interruptReturn/);
  assert.match(readerSource, /window\.scrollTo\(\{ behavior: "auto", top: interruptedTop \}\)/);
  assert.match(readerSource, /"Escape"/);
  assert.match(readerSource, /setPaperBlockJumpOrigin\(null\)/);
  assert.match(annotationSource, /jumpToPaperBlock\(citation\.block_id\)/);
  assert.match(annotationSource, /jumpToPaperBlock\(annotation\.block_id\)/);
  assert.match(stylesSource, /\.jump-return-button \{[^}]*animation: jump-return-breathe/);
  assert.match(stylesSource, /\.jump-return-button-stacked \{ bottom: 70px; \}/);
});

test("reader keeps the visible paper block in a refreshable share URL", () => {
  assert.match(blockNavigationSource, /PAPER_READING_POSITION_PARAM = "block"/);
  assert.match(blockNavigationSource, /currentVisiblePaperBlockId/);
  assert.match(blockNavigationSource, /searchParams\.set\(PAPER_READING_POSITION_PARAM, blockId\)/);
  assert.match(blockNavigationSource, /window\.history\.replaceState/);
  assert.match(blockNavigationSource, /restorePaperReadingPosition/);
  assert.match(blockNavigationSource, /window\.sessionStorage\.setItem/);
  assert.match(blockNavigationSource, /window\.sessionStorage\.getItem/);
  assert.match(blockNavigationSource, /performance\.getEntriesByType\("navigation"\)/);
  assert.match(blockNavigationSource, /navigation\?\.type !== "reload"/);
  assert.match(readerSource, /restorePaperReadingPosition\(\{ allowSessionFallback \}\)/);
  assert.match(blockNavigationSource, /target\.scrollIntoView\(\{ behavior: "auto", block: "start" \}\)/);
  assert.match(readerSource, /data-reading-block-id=\{block\.id\}/);
  assert.match(readerSource, /window\.history\.scrollRestoration = "manual"/);
  assert.match(readerSource, /window\.addEventListener\("scroll", scheduleReadingPositionUpdate/);
  assert.match(readerSource, /new ResizeObserver\(scheduleReadingPositionUpdate\)/);
  assert.match(readerSource, /window\.addEventListener\("hashchange", restoreFromLocation\)/);
  assert.match(readerSource, /window\.addEventListener\("popstate", restoreFromLocation\)/);
  assert.match(readerSource, /clearPaperReadingPosition\(\)/);
});

test("pipeline task cards jump to block or paper regions and preserve return navigation", () => {
  assert.match(readerSource, /function TaskJumpButton/);
  assert.match(readerSource, /PIPELINE_TASK_REGIONS/);
  assert.match(readerSource, /getPaperTaskDetail\(paperId, task\.id\)/);
  assert.match(readerSource, /jumpToPaperBlock\(detail\.target_block_id/);
  assert.match(readerSource, /updateHash: true/);
  assert.match(readerSource, /aria-label=\{`Jump to \$\{task\.task_type/);
  assert.match(readerSource, /id="paper-metadata"/);
  assert.match(readerSource, /id="paper-source-assets"/);
  assert.match(readerSource, /id="paper-structured-view"/);
  assert.match(stylesSource, /\.task-jump-button/);
});

test("source assets expose explicit archive downloads", () => {
  assert.match(apiSource, /export function paperSourceDownloadUrl/);
  assert.match(apiSource, /sources\/\$\{encodeURIComponent\(sourceType\)\}\/download/);
  assert.match(readerSource, /paperSourceDownloadUrl\(paperId, source\.source_type\)/);
  assert.match(readerSource, /Download compressed source/);
  assert.match(readerSource, /className="source-download-link"/);
  assert.match(stylesSource, /\.source-download-link/);
});

test("abstract source and translation participate in the annotation workspace", () => {
  assert.match(readerSource, /const abstractAnnotationBlock = useMemo<ContentBlock \| null>/);
  assert.match(readerSource, /data-annotation-block-id=\{abstractAnnotationBlock\?\.id\}/);
  assert.match(readerSource, /data-annotation-target=\{abstractAnnotationBlock \? "SOURCE" : undefined\}/);
  assert.match(readerSource, /block=\{abstractAnnotationBlock\}/);
  assert.match(readerSource, /blocks=\{annotationBlocks\}/);
  assert.match(readerSource, /summaryRootRef=\{abstractAnnotationRootRef\}/);
  assert.match(annotationSource, /const readerRoots = \[summaryRootRef\?\.current, readerRootRef\.current\]/);
  assert.match(annotationSource, /readerRoots\.forEach\(\(readerRoot\) =>/);
});

test("annotation panel supports comment timelines and durable paper-aware questions", () => {
  assert.match(apiSource, /createAnnotation/);
  assert.match(apiSource, /updateAnnotation/);
  assert.match(apiSource, /deleteAnnotation/);
  assert.match(apiSource, /retryQuestion/);
  assert.match(apiSource, /createAnnotationComment/);
  assert.match(apiSource, /updateAnnotationComment/);
  assert.match(apiSource, /deleteAnnotationComment/);
  assert.match(apiSource, /askAnnotation/);
  assert.match(annotationSource, /Add comment/);
  assert.match(annotationSource, /Save this comment/);
  assert.match(annotationSource, /Delete this comment/);
  assert.match(annotationSource, /formatAnnotationTime/);
  assert.match(annotationSource, /Confirm annotation deletion/);
  assert.match(annotationSource, /Ask AI/);
  assert.match(annotationSource, /Ask about this paper/);
  assert.match(annotationSource, /Retry answer/);
  assert.match(annotationSource, /Location unresolved/);
  assert.match(annotationSource, /response\.citations/);
  assert.match(annotationSource, /Verified answer evidence/);
  assert.match(annotationSource, /模型引用的论文片段；点击可定位到对应内容块/);
  assert.match(annotationSource, /latency_ms/);
  assert.match(apiSource, /is_highlighted/);
  assert.doesNotMatch(annotationSource, /Ask block/);
  assert.doesNotMatch(annotationSource, /Save changes/);
  assert.doesNotMatch(annotationSource, /window\.confirm/);
});

test("annotation composers retain focus when Escape is used by an IME", () => {
  assert.match(annotationSource, /composerEscapeFocusDeadline/);
  assert.match(annotationSource, /nativeEvent\.stopImmediatePropagation\(\)/);
  assert.match(annotationSource, /restoreComposerFocusAfterEscape/);
  assert.match(annotationSource, /focus\(\{ preventScroll: true \}\)/);
  assert.match(annotationSource, /onBlur=\{restoreComposerFocusAfterEscape\}/);
});

test("questions disclose audited LLM context and translations disclose block identity", () => {
  assert.match(apiSource, /qa_context: QAContextSnapshot \| null/);
  assert.match(annotationSource, /function QuestionContextInspector/);
  assert.match(annotationSource, /Context sent to the LLM/);
  assert.match(annotationSource, /Current user question/);
  assert.match(annotationSource, /characters sent/);
  assert.match(annotationSource, /QuestionContextInspector question=\{question\}/);
  assert.match(annotationSource, /QuestionContextInspector question=\{entry\}/);
  assert.match(readerSource, /function TranslationBlockLabel/);
  assert.match(readerSource, /Content block ID/);
  assert.match(readerSource, /<code>\{blockId\}<\/code>/);
  assert.match(stylesSource, /\.qa-context-inspector:hover \.qa-context-tooltip/);
  assert.match(stylesSource, /\.translation-block-label:hover \.translation-block-tooltip/);
});

test("paper discussion uses one composer for comments and AI without listing text annotations", () => {
  assert.match(annotationSource, /item\.target === "PAPER"/);
  assert.match(annotationSource, /annotation_type: kind === "comment" \? "USER_NOTE" : "AI_QUESTION"/);
  assert.match(annotationSource, />Comment<\/button>/);
  assert.match(annotationSource, />Ask AI<\/button>/);
  assert.match(annotationSource, /paper-discussion-list/);
  assert.doesNotMatch(annotationSource, /const topLevel = useMemo/);
  assert.doesNotMatch(annotationSource, /<AnnotationCard/);
});

test("annotation editor uses the same dark palette as selection actions", () => {
  assert.match(stylesSource, /\.selection-toolbar \{ background: #173c2c;/);
  assert.match(stylesSource, /\.annotation-popover \{ background: #173c2c;/);
  assert.match(stylesSource, /\.annotation-comment \{ background: #214936;/);
  assert.match(stylesSource, /\.annotation-question \{ background: #214936;/);
  assert.match(stylesSource, /\.annotation-comment-composer button, \.annotation-question-composer button \{ background: #dbe9de;/);
  assert.match(stylesSource, /\.annotation-popover \.qa-state:empty \{ display: none; \}/);
});

test("annotation editor opens after creation and stays document-anchored while draggable", () => {
  assert.match(annotationSource, /const created = await createAnnotation/);
  assert.match(annotationSource, /kind !== "highlight"/);
  assert.match(annotationSource, /onOpenAnnotation\(/);
  assert.match(annotationSource, /window\.scrollX \+ x/);
  assert.match(annotationSource, /window\.scrollY \+ y/);
  assert.match(annotationSource, /window\.scrollX \+ Math\.min/);
  assert.match(annotationSource, /top: window\.scrollY \+ Math\.max/);
  assert.match(annotationSource, /popoverPositionAtPage\(selected\.left, selected\.top \+ 12\)/);
  assert.match(annotationSource, /setPointerCapture\(event\.pointerId\)/);
  assert.match(annotationSource, /onPositionChange\(clampDraggedPopoverPosition/);
  assert.match(annotationSource, /onPointerCancel=\{finishDrag\}/);
  assert.match(readerSource, /onOpenAnnotation=\{openAnnotation\}/);
  assert.match(readerSource, /onPositionChange=\{moveAnnotation\}/);
  assert.match(stylesSource, /\.annotation-popover \{[^}]*position: absolute;/);
  assert.match(stylesSource, /\.selection-toolbar \{[^}]*position: absolute;/);
  assert.doesNotMatch(stylesSource, /\.selection-toolbar \{[^}]*position: fixed;/);
  assert.match(annotationSource, /className="selection-toolbar-close"/);
  assert.match(stylesSource, /\.selection-toolbar > \.selection-toolbar-close \{[^}]*position: absolute;[^}]*right: 5px;[^}]*top: 5px;/);
  assert.doesNotMatch(annotationSource, /closeOnEscape/);
  assert.doesNotMatch(annotationSource, /document\.addEventListener\("keydown", closeOnEscape\)/);
});
