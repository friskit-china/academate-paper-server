"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";

import {
  askAnnotation,
  createAnnotation,
  createAnnotationComment,
  deleteAnnotation,
  deleteAnnotationComment,
  retryQuestion,
  updateAnnotation,
  updateAnnotationComment,
  type Annotation,
  type AnnotationComment,
  type AnnotationSelector,
  type ContentBlock,
} from "../../api";
import { jumpToPaperBlock } from "./paper-block-navigation";

type SelectionTarget = {
  blockId: string;
  target: "SOURCE" | "TRANSLATION";
  translationId: string | null;
  quote: string;
  selector: AnnotationSelector;
  left: number;
  top: number;
};

type HighlightConstructor = new (...ranges: Range[]) => unknown;
type HighlightRegistry = {
  set: (name: string, value: unknown) => void;
  delete: (name: string) => void;
};

const ANNOTATION_AUXILIARY_SELECTOR = "[data-annotation-auxiliary='true']";
const ANNOTATION_MATH_SELECTOR = "[data-annotation-math]";
const composerEscapeFocusDeadline = new WeakMap<HTMLTextAreaElement, number>();

function refocusComposerAfterEscape(composer: HTMLTextAreaElement) {
  const deadline = composerEscapeFocusDeadline.get(composer);
  if (
    deadline !== undefined
    && deadline >= performance.now()
    && document.contains(composer)
    && document.activeElement !== composer
  ) {
    composer.focus({ preventScroll: true });
  }
}

function preserveComposerFocusOnEscape(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
  if (event.key !== "Escape") return;
  event.stopPropagation();
  event.nativeEvent.stopImmediatePropagation();
  const composer = event.currentTarget;
  composerEscapeFocusDeadline.set(composer, performance.now() + 250);
  window.setTimeout(() => refocusComposerAfterEscape(composer), 0);
  window.requestAnimationFrame(() => {
    refocusComposerAfterEscape(composer);
  });
  window.setTimeout(() => {
    refocusComposerAfterEscape(composer);
  }, 80);
  window.setTimeout(() => composerEscapeFocusDeadline.delete(composer), 260);
}

function restoreComposerFocusAfterEscape(event: ReactFocusEvent<HTMLTextAreaElement>) {
  const composer = event.currentTarget;
  const deadline = composerEscapeFocusDeadline.get(composer) ?? 0;
  if (deadline < performance.now()) return;
  window.setTimeout(() => refocusComposerAfterEscape(composer), 0);
}

export type AnnotationPopoverPosition = {
  left: number;
  top: number;
};

function popoverPositionAt(x: number, y: number): AnnotationPopoverPosition {
  return popoverPositionAtPage(window.scrollX + x, window.scrollY + y);
}

function popoverPositionAtPage(pageX: number, pageY: number): AnnotationPopoverPosition {
  const estimatedHeight = 620;
  const halfWidth = 205;
  const viewportY = pageY - window.scrollY;
  const top = viewportY + estimatedHeight + 24 > window.innerHeight
    ? Math.max(window.scrollY + 12, pageY - estimatedHeight - 12)
    : pageY + 12;
  return {
    left: Math.min(
      window.scrollX + window.innerWidth - halfWidth - 12,
      Math.max(window.scrollX + halfWidth + 12, pageX),
    ),
    top,
  };
}

function clampDraggedPopoverPosition(position: AnnotationPopoverPosition): AnnotationPopoverPosition {
  const halfWidth = 205;
  const documentWidth = Math.max(
    document.documentElement.clientWidth,
    document.documentElement.scrollWidth,
  );
  const documentHeight = Math.max(
    document.documentElement.clientHeight,
    document.documentElement.scrollHeight,
  );
  return {
    left: Math.min(documentWidth - halfWidth - 12, Math.max(halfWidth + 12, position.left)),
    top: Math.min(documentHeight - 80, Math.max(12, position.top)),
  };
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function annotationRangeText(range: Range): string {
  const container = document.createElement("div");
  container.append(range.cloneContents());
  container.querySelectorAll<HTMLElement>(ANNOTATION_MATH_SELECTOR).forEach((node) => {
    node.replaceWith(document.createTextNode(node.dataset.annotationMath ?? ""));
  });
  container.querySelectorAll(ANNOTATION_AUXILIARY_SELECTOR).forEach((node) => node.remove());
  return (container.textContent ?? "").normalize("NFC");
}

function containingAnnotationMath(root: Element, node: Node): HTMLElement | null {
  const element = node instanceof Element ? node : node.parentElement;
  const math = element?.closest<HTMLElement>(ANNOTATION_MATH_SELECTOR) ?? null;
  return math && root.contains(math) ? math : null;
}

function expandRangeToWholeMath(root: Element, original: Range): Range {
  const range = original.cloneRange();
  const startMath = containingAnnotationMath(root, range.startContainer);
  const endMath = containingAnnotationMath(root, range.endContainer);
  if (startMath) range.setStartBefore(startMath);
  if (endMath) range.setEndAfter(endMath);
  return range;
}

function annotationRootText(root: Element): string {
  const range = document.createRange();
  range.selectNodeContents(root);
  return annotationRangeText(range);
}

function annotationOffsetBeforeBoundary(root: Element, node: Node, offset: number): number {
  const range = document.createRange();
  range.selectNodeContents(root);
  range.setEnd(node, offset);
  return codePointLength(annotationRangeText(range));
}

type AnnotationDomSegment =
  | { kind: "text"; node: Text; value: string }
  | { kind: "math"; node: HTMLElement; value: string };

function annotationDomSegments(root: Element): AnnotationDomSegment[] {
  const segments: AnnotationDomSegment[] = [];
  const visit = (node: Node) => {
    if (node instanceof Element) {
      if (node.matches(ANNOTATION_AUXILIARY_SELECTOR)) return;
      if (node.matches(ANNOTATION_MATH_SELECTOR)) {
        segments.push({
          kind: "math",
          node: node as HTMLElement,
          value: (node as HTMLElement).dataset.annotationMath?.normalize("NFC") ?? "",
        });
        return;
      }
    }
    if (node instanceof Text) {
      segments.push({ kind: "text", node, value: node.data.normalize("NFC") });
      return;
    }
    node.childNodes.forEach(visit);
  };
  root.childNodes.forEach(visit);
  return segments;
}

async function sha256(value: string): Promise<string | undefined> {
  if (!window.crypto?.subtle) return undefined;
  const digest = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function findCodePointCandidates(text: string, exact: string): number[] {
  const candidates: number[] = [];
  let cursor = 0;
  while (cursor <= text.length) {
    const position = text.indexOf(exact, cursor);
    if (position < 0) break;
    candidates.push(codePointLength(text.slice(0, position)));
    cursor = position + Math.max(1, exact.length);
  }
  return candidates;
}

function translatedAnnotationText(translation: ContentBlock["translation"]): string | null {
  if (!translation?.translated_text) return null;
  const units = translation.translated_payload.units;
  if (!Array.isArray(units)) return translation.translated_text.normalize("NFC");
  const materialized = units.flatMap((unit) => {
    if (typeof unit !== "object" || unit === null) return [];
    const record = unit as Record<string, unknown>;
    if (!Array.isArray(record.segments)) {
      return typeof record.text === "string" ? [record.text] : [];
    }
    const parts: string[] = [];
    let valid = true;
    for (const segment of record.segments) {
      if (typeof segment !== "object" || segment === null) {
        valid = false;
        break;
      }
      const item = segment as Record<string, unknown>;
      const value = item.type === "math" ? item.latex : item.text;
      if (
        !["text", "entity", "math"].includes(String(item.type))
        || typeof value !== "string"
      ) {
        valid = false;
        break;
      }
      parts.push(value);
    }
    return valid ? [parts.join("")] : typeof record.text === "string" ? [record.text] : [];
  });
  return (materialized.length ? materialized.join("\n") : translation.translated_text)
    .normalize("NFC");
}

function canonicalText(
  blocks: ContentBlock[],
  blockId: string,
  target: "SOURCE" | "TRANSLATION",
  translationId: string | null,
): string | null {
  const block = blocks.find((item) => item.id === blockId);
  if (!block) return null;
  if (target === "SOURCE") return block.source_text?.normalize("NFC") ?? null;
  const translation = block.translation;
  if (!translation || translation.id !== translationId || translation.status !== "SUCCEEDED") {
    return null;
  }
  return translatedAnnotationText(translation);
}

function textRangesForOffsets(root: Element, start: number, end: number): Range[] {
  let consumed = 0;
  const ranges: Range[] = [];
  if (end <= start) return ranges;
  for (const segment of annotationDomSegments(root)) {
    const value = segment.value;
    const length = codePointLength(value);
    const nodeStart = consumed;
    const nodeEnd = nodeStart + length;
    consumed = nodeEnd;
    if (nodeEnd <= start) continue;
    if (nodeStart >= end) break;
    if (segment.kind === "math") {
      const range = document.createRange();
      range.selectNodeContents(segment.node);
      ranges.push(range);
      continue;
    }
    const segmentStart = Math.max(start, nodeStart) - nodeStart;
    const segmentEnd = Math.min(end, nodeEnd) - nodeStart;
    if (segmentStart < segmentEnd) {
      const points = Array.from(value);
      const range = document.createRange();
      range.setStart(segment.node, points.slice(0, segmentStart).join("").length);
      range.setEnd(segment.node, points.slice(0, segmentEnd).join("").length);
      ranges.push(range);
    }
  }
  return ranges;
}

export function PersistentAnnotationHighlights({
  annotations,
  onAnnotationActivate,
  onAnnotationHover,
  readerRootRef,
  summaryRootRef,
}: {
  annotations: Annotation[];
  onAnnotationActivate: (
    annotation: Annotation,
    position: AnnotationPopoverPosition,
  ) => void;
  onAnnotationHover?: (
    annotation: Annotation | null,
    position: AnnotationPopoverPosition | null,
  ) => void;
  readerRootRef: RefObject<HTMLElement | null>;
  summaryRootRef?: RefObject<HTMLElement | null>;
}) {
  useEffect(() => {
    const css = window.CSS as typeof CSS & { highlights?: HighlightRegistry };
    const HighlightClass = (window as unknown as { Highlight?: HighlightConstructor }).Highlight;
    const readerRoots = [summaryRootRef?.current, readerRootRef.current].filter(
      (root): root is HTMLElement => root !== null && root !== undefined,
    );
    if (!readerRoots.length) return;
    const names = ["paper-highlights", "paper-activities", "paper-anchors", "paper-questions"];
    const activityAnchorIds = new Set(
      annotations
        .filter((item) => item.annotation_type === "AI_QUESTION" && item.anchor_annotation_id)
        .map((item) => item.anchor_annotation_id as string),
    );
    let interactiveRanges: Array<{ annotation: Annotation; range: Range }> = [];
    let hoveredAnnotationId: string | null = null;

    const refreshHighlights = () => {
      names.forEach((name) => css.highlights?.delete(name));
      const groups = new Map<string, Range[]>();
      interactiveRanges = [];
      for (const annotation of annotations) {
        if (
          annotation.resolution_status === "UNRESOLVED"
          || annotation.resolved_start_offset === null
          || annotation.resolved_end_offset === null
          || !annotation.block_id
          || annotation.annotation_type === "AI_RESPONSE"
          || (annotation.annotation_type === "AI_QUESTION" && annotation.anchor_annotation_id)
        ) continue;
        const translationFilter = annotation.target === "TRANSLATION"
          ? `[data-annotation-translation-id="${annotation.target_translation_id}"]`
          : "";
        const selector = `[data-annotation-target="${annotation.target}"]`
          + `[data-annotation-block-id="${annotation.block_id}"]${translationFilter}`;
        const root = readerRoots
          .map((readerRoot) => readerRoot.querySelector(selector))
          .find((candidate) => candidate !== null);
        if (!root) continue;
        let start = annotation.resolved_start_offset;
        let end = annotation.resolved_end_offset;
        const exact = annotation.selector?.exact_quote;
        const domText = annotationRootText(root);
        const domPoints = Array.from(domText);
        if (exact && domPoints.slice(start, end).join("") !== exact) {
          const candidates = findCodePointCandidates(domText, exact);
          if (candidates.length !== 1) continue;
          start = candidates[0];
          end = start + codePointLength(exact);
        }
        const ranges = textRangesForOffsets(
          root,
          start,
          end,
        );
        if (!ranges.length) continue;
        if (
          annotation.creator_type === "USER"
          && ["ANNOTATION", "HIGHLIGHT", "USER_NOTE"].includes(annotation.annotation_type)
        ) {
          interactiveRanges.push(...ranges.map((range) => ({ annotation, range })));
        }
        const hasActivity = annotation.comments.length > 0 || activityAnchorIds.has(annotation.id);
        const name = annotation.annotation_type === "AI_QUESTION"
          ? "paper-questions"
          : hasActivity
          ? "paper-activities"
          : annotation.is_highlighted
          ? "paper-highlights"
          : "paper-anchors";
        if (!name) continue;
        groups.set(name, [...(groups.get(name) ?? []), ...ranges]);
      }
      if (css.highlights && HighlightClass) {
        groups.forEach((ranges, name) => css.highlights?.set(name, new HighlightClass(...ranges)));
      }
    };

    refreshHighlights();
    const observer = new MutationObserver(refreshHighlights);
    readerRoots.forEach((readerRoot) => {
      observer.observe(readerRoot, { childList: true, characterData: true, subtree: true });
    });
    const handleAnnotationClick = (event: MouseEvent) => {
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed) return;
      const matches = interactiveRanges
        .filter(({ range }) => Array.from(range.getClientRects()).some((rect) => (
          event.clientX >= rect.left
          && event.clientX <= rect.right
          && event.clientY >= rect.top
          && event.clientY <= rect.bottom
        )))
        .sort((left, right) => (
          ((left.annotation.resolved_end_offset ?? 0)
            - (left.annotation.resolved_start_offset ?? 0))
          - ((right.annotation.resolved_end_offset ?? 0)
            - (right.annotation.resolved_start_offset ?? 0))
        ));
      if (!matches.length) return;
      event.preventDefault();
      onAnnotationActivate(
        matches[0].annotation,
        popoverPositionAt(event.clientX, event.clientY),
      );
    };
    const handleAnnotationHover = (event: MouseEvent) => {
      const matches = interactiveRanges
        .filter(({ annotation, range }) => (
          (annotation.comments.length > 0 || activityAnchorIds.has(annotation.id))
          && Array.from(range.getClientRects()).some((rect) => (
            event.clientX >= rect.left
            && event.clientX <= rect.right
            && event.clientY >= rect.top
            && event.clientY <= rect.bottom
          ))
        ))
        .sort((left, right) => (
          ((left.annotation.resolved_end_offset ?? 0)
            - (left.annotation.resolved_start_offset ?? 0))
          - ((right.annotation.resolved_end_offset ?? 0)
            - (right.annotation.resolved_start_offset ?? 0))
        ));
      const annotation = matches[0]?.annotation ?? null;
      if (annotation?.id === hoveredAnnotationId) return;
      hoveredAnnotationId = annotation?.id ?? null;
      onAnnotationHover?.(
        annotation,
        annotation ? popoverPositionAt(event.clientX, event.clientY) : null,
      );
    };
    const clearAnnotationHover = () => {
      if (hoveredAnnotationId === null) return;
      hoveredAnnotationId = null;
      onAnnotationHover?.(null, null);
    };
    readerRoots.forEach((readerRoot) => {
      readerRoot.addEventListener("click", handleAnnotationClick);
      readerRoot.addEventListener("mousemove", handleAnnotationHover);
      readerRoot.addEventListener("mouseleave", clearAnnotationHover);
    });
    return () => {
      observer.disconnect();
      readerRoots.forEach((readerRoot) => {
        readerRoot.removeEventListener("click", handleAnnotationClick);
        readerRoot.removeEventListener("mousemove", handleAnnotationHover);
        readerRoot.removeEventListener("mouseleave", clearAnnotationHover);
      });
      onAnnotationHover?.(null, null);
      names.forEach((name) => css.highlights?.delete(name));
    };
  }, [annotations, onAnnotationActivate, onAnnotationHover, readerRootRef, summaryRootRef]);
  return (
    <style>{`
      ::highlight(paper-highlights) { background: rgba(250, 211, 92, 0.62); color: inherit; }
      ::highlight(paper-activities) { background: rgba(151, 202, 168, 0.38); color: inherit; }
      ::highlight(paper-anchors) { color: inherit; text-decoration: underline 1.5px dotted #6f9a78; }
      ::highlight(paper-questions) { background: rgba(125, 164, 213, 0.28); color: inherit; text-decoration: underline 2px #5b7fa8; }
    `}</style>
  );
}

export function SelectionToolbar({
  paperId,
  blocks,
  onChanged,
  onOpenAnnotation,
}: {
  paperId: string;
  blocks: ContentBlock[];
  onChanged: () => Promise<void>;
  onOpenAnnotation: (
    annotation: Annotation,
    position: AnnotationPopoverPosition,
  ) => void;
}) {
  const [selected, setSelected] = useState<SelectionTarget | null>(null);
  const [composer, setComposer] = useState<"comment" | "question" | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);

  const dismissSelection = useCallback(() => {
    window.getSelection()?.removeAllRanges();
    setSelected(null);
    setComposer(null);
    setDraft("");
    setError(null);
  }, []);

  const captureSelection = useCallback(async () => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return;
    const originalRange = selection.getRangeAt(0);
    const common = originalRange.commonAncestorContainer instanceof Element
      ? originalRange.commonAncestorContainer
      : originalRange.commonAncestorContainer.parentElement;
    const root = common?.closest<HTMLElement>("[data-annotation-target][data-annotation-block-id]");
    if (
      !root
      || !root.contains(originalRange.startContainer)
      || !root.contains(originalRange.endContainer)
    ) return;
    const range = expandRangeToWholeMath(root, originalRange);
    selection.removeAllRanges();
    selection.addRange(range);
    const target = root.dataset.annotationTarget;
    const blockId = root.dataset.annotationBlockId;
    const translationId = root.dataset.annotationTranslationId ?? null;
    if ((target !== "SOURCE" && target !== "TRANSLATION") || !blockId) return;
    const targetText = canonicalText(blocks, blockId, target, translationId);
    if (!targetText) return;
    const browserQuote = annotationRangeText(range);
    if (!browserQuote.trim()) return;
    const domText = annotationRootText(root);
    const domStart = annotationOffsetBeforeBoundary(root, range.startContainer, range.startOffset);
    const domEnd = annotationOffsetBeforeBoundary(root, range.endContainer, range.endOffset);
    let start: number;
    let end: number;
    if (domText === targetText && domStart < domEnd) {
      start = domStart;
      end = domEnd;
    } else {
      const candidates = findCodePointCandidates(targetText, browserQuote);
      if (!candidates.length) {
        setError("抱歉，暂时还不支持为这类文本区域添加标注。");
        return;
      }
      start = candidates.reduce((best, item) => (
        Math.abs(item - domStart) < Math.abs(best - domStart) ? item : best
      ), candidates[0]);
      end = start + codePointLength(browserQuote);
    }
    const points = Array.from(targetText);
    const quote = points.slice(start, end).join("");
    if (!quote.trim() || codePointLength(quote) > 2_000) return;
    const rect = range.getBoundingClientRect();
    const hash = await sha256(targetText);
    setSelected({
      blockId,
      target,
      translationId,
      quote,
      selector: {
        version: 1,
        block_id: blockId,
        start_offset: start,
        end_offset: end,
        exact_quote: quote,
        prefix: points.slice(Math.max(0, start - 64), start).join(""),
        suffix: points.slice(end, end + 64).join(""),
        ...(hash ? { text_hash: hash } : {}),
      },
      left: window.scrollX + Math.min(
        window.innerWidth - 24,
        Math.max(24, rect.left + rect.width / 2),
      ),
      top: window.scrollY + Math.max(12, rect.top - 12),
    });
    setComposer(null);
    setDraft("");
    setError(null);
  }, [blocks]);

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest("[data-annotation-target][data-annotation-block-id]")) {
        document.documentElement.classList.add("annotation-selecting");
      }
    };
    const onPointerUp = () => {
      document.documentElement.classList.remove("annotation-selecting");
      window.setTimeout(() => void captureSelection(), 0);
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.shiftKey || ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
        void captureSelection();
      }
      if (event.key === "Escape") dismissSelection();
    };
    document.addEventListener("mousedown", onPointerDown, true);
    document.addEventListener("mouseup", onPointerUp);
    document.addEventListener("keyup", onKeyUp);
    return () => {
      document.documentElement.classList.remove("annotation-selecting");
      document.removeEventListener("mousedown", onPointerDown, true);
      document.removeEventListener("mouseup", onPointerUp);
      document.removeEventListener("keyup", onKeyUp);
    };
  }, [captureSelection, dismissSelection]);

  useEffect(() => {
    if (!selected) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && toolbarRef.current?.contains(target)) return;
      dismissSelection();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [dismissSelection, selected]);

  async function save(kind: "highlight" | "comment" | "question") {
    if (!selected) return;
    setSaving(true);
    setError(null);
    try {
      const created = await createAnnotation({
        paper_id: paperId,
        block_id: selected.blockId,
        annotation_type: "ANNOTATION",
        target: selected.target,
        is_highlighted: kind === "highlight",
        selector: selected.selector,
        ...(kind === "comment" ? { initial_comment: draft } : {}),
        ...(kind === "question" ? { initial_question: draft } : {}),
        ...(selected.target === "TRANSLATION" && selected.translationId
          ? { target_translation_id: selected.translationId }
          : {}),
      });
      await onChanged();
      if (kind !== "highlight") {
        onOpenAnnotation(
          created.annotation,
          popoverPositionAtPage(selected.left, selected.top + 12),
        );
      }
      window.getSelection()?.removeAllRanges();
      setSelected(null);
      setComposer(null);
      setDraft("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save this annotation.");
    } finally {
      setSaving(false);
    }
  }

  if (!selected) return error ? <div className="selection-error" role="alert">{error}</div> : null;
  const needsDraft = composer !== null;
  return (
    <div
      aria-label="Text selection actions"
      className="selection-toolbar"
      onMouseDown={(event) => event.preventDefault()}
      ref={toolbarRef}
      role="dialog"
      style={{ left: selected.left, top: selected.top }}
    >
      <button
        aria-label="Close selection actions"
        className="selection-toolbar-close"
        onClick={dismissSelection}
        type="button"
      >×</button>
      <div className="selection-toolbar-actions">
        <button disabled={saving} onClick={() => void save("highlight")} type="button">Highlight</button>
        <button onClick={() => setComposer("comment")} type="button">Comment</button>
        <button onClick={() => setComposer("question")} type="button">Ask AI</button>
      </div>
      {needsDraft ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void save(composer === "comment" ? "comment" : "question");
          }}
        >
          <label htmlFor="selection-annotation-draft">
            {composer === "comment" ? "Add a comment" : "Ask about this selection"}
          </label>
          <textarea
            autoFocus
            id="selection-annotation-draft"
            maxLength={4000}
            onBlur={restoreComposerFocusAfterEscape}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={preserveComposerFocusOnEscape}
            onKeyUp={preserveComposerFocusOnEscape}
            placeholder={composer === "comment" ? "Write a comment…" : "What would you like to understand?"}
            rows={3}
            value={draft}
          />
          <button disabled={saving || !draft.trim()} type="submit">
            {saving ? "Saving…" : composer === "comment" ? "Add comment" : "Ask AI"}
          </button>
        </form>
      ) : null}
      {error ? <small role="alert">{error}</small> : null}
    </div>
  );
}

function shortIdentity(value: string | null): string {
  if (!value) return "—";
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function qaContextKindLabel(kind: string): string {
  return ({
    selection: "Selected text",
    thread_user: "Earlier user question",
    thread_assistant: "Earlier AI answer",
    thread_history_notice: "History budget notice",
    current_block_source: "Current block source",
    current_block_translation: "Current block translation",
    paper_abstract_source: "Paper abstract source",
    paper_abstract_translation: "Paper abstract translation",
    paper_overview_source: "Paper overview source",
    paper_overview_translation: "Paper overview translation",
    section: "Section context",
    paper_metadata: "Paper metadata",
  } as Record<string, string>)[kind] ?? kind;
}

function QuestionContextInspector({ question }: { question: Annotation }) {
  const snapshot = question.qa_context;
  return (
    <span className="qa-context-inspector" tabIndex={0}>
      LLM context
      <span className="qa-context-tooltip" role="tooltip">
        <strong>Context sent to the LLM</strong>
        {snapshot ? (
          <>
            <dl>
              <div><dt>Generation</dt><dd>{snapshot.generation}</dd></div>
              <div><dt>Prompt policy</dt><dd>{snapshot.prompt_version}</dd></div>
              <div><dt>Prompt hash</dt><dd>{shortIdentity(snapshot.prompt_hash)}</dd></div>
              <div><dt>Input budget</dt><dd>{snapshot.context_token_budget} tokens</dd></div>
              <div><dt>Output limit</dt><dd>{snapshot.max_output_tokens} tokens</dd></div>
            </dl>
            <section>
              <small>Current user question</small>
              <p>{snapshot.user_payload.question}</p>
            </section>
            <div className="qa-context-items">
              {snapshot.user_payload.contexts.map((item, index) => {
                const compact = [
                  "current_block_source",
                  "current_block_translation",
                  "paper_abstract_source",
                  "paper_abstract_translation",
                  "paper_overview_source",
                  "paper_overview_translation",
                  "section",
                ].includes(item.kind);
                return (
                  <section key={`${question.id}-context-${index}`}>
                    <small>{qaContextKindLabel(item.kind)}{item.truncated ? " · truncated" : ""}</small>
                    {compact ? (
                      <p>
                        Block {shortIdentity(item.block_id)} · {item.target ?? "metadata"}
                        {item.translation_id ? ` · revision ${shortIdentity(item.translation_id)}` : ""}
                        {` · ${Array.from(item.content).length} characters sent`}
                      </p>
                    ) : <p>{item.content}</p>}
                  </section>
                );
              })}
            </div>
            <small className="qa-context-note">
              The fixed safety system prompt is identified by version/hash above; secrets and internal policy text are not exposed.
            </small>
          </>
        ) : (
          <p>The durable answer task has not captured its provider-bound context yet.</p>
        )}
      </span>
    </span>
  );
}

function AnswerCitations({ response }: { response: Annotation }) {
  if (!response.citations.length) return null;
  return (
    <div aria-label="Verified answer evidence" className="qa-citations">
      <span className="qa-citations-label">
        已验证依据
        <small>模型引用的论文片段；点击可定位到对应内容块</small>
      </span>
      {response.citations.map((citation, index) => (
        <button
          aria-label={`查看回答依据 ${index + 1}`}
          key={`${response.id}-citation-${index}`}
          onClick={() => jumpToPaperBlock(citation.block_id)}
          type="button"
        >
          [{index + 1}] {citation.target === "SOURCE" ? "原文" : "译文"} · {shortIdentity(citation.block_id)}
          <span className="qa-citation-tooltip" role="tooltip">
            <q>{citation.quote}</q>
            <code>Block {citation.block_id}</code>
            {citation.translation_id ? <code>Revision {citation.translation_id}</code> : null}
          </span>
        </button>
      ))}
    </div>
  );
}

export function AnnotationEditorPopover({
  annotation,
  questions,
  responsesByQuestion,
  onChanged,
  onClose,
  onPositionChange,
  position,
}: {
  annotation: Annotation;
  questions: Annotation[];
  responsesByQuestion: Map<string, Annotation[]>;
  onChanged: () => Promise<void>;
  onClose: () => void;
  onPositionChange: (position: AnnotationPopoverPosition) => void;
  position: AnnotationPopoverPosition;
}) {
  const [isHighlighted, setIsHighlighted] = useState(annotation.is_highlighted);
  const [newComment, setNewComment] = useState("");
  const [questionDraft, setQuestionDraft] = useState("");
  const [editingCommentId, setEditingCommentId] = useState<string | null>(null);
  const [editingCommentDraft, setEditingCommentDraft] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    pointerId: number;
    startPageX: number;
    startPageY: number;
    startLeft: number;
    startTop: number;
  } | null>(null);

  useEffect(() => {
    setIsHighlighted(annotation.is_highlighted);
    setError(null);
  }, [annotation.id, annotation.is_highlighted, annotation.revision]);

  useEffect(() => {
    const closeOnPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && popoverRef.current?.contains(target)) return;
      onClose();
    };
    document.addEventListener("pointerdown", closeOnPointerDown, true);
    return () => {
      document.removeEventListener("pointerdown", closeOnPointerDown, true);
    };
  }, [onClose]);

  function startDrag(event: ReactPointerEvent<HTMLElement>) {
    const target = event.target;
    if (target instanceof Element && target.closest("button, input, textarea, a")) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startPageX: event.pageX,
      startPageY: event.pageY,
      startLeft: position.left,
      startTop: position.top,
    };
    setDragging(true);
  }

  function moveDrag(event: ReactPointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    onPositionChange(clampDraggedPopoverPosition({
      left: drag.startLeft + event.pageX - drag.startPageX,
      top: drag.startTop + event.pageY - drag.startPageY,
    }));
  }

  function finishDrag(event: ReactPointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragRef.current = null;
    setDragging(false);
  }

  async function toggleHighlight() {
    const next = !isHighlighted;
    setIsHighlighted(next);
    setBusy("highlight");
    setError(null);
    try {
      await updateAnnotation(annotation.id, {
        is_highlighted: next,
        revision: annotation.revision,
      });
      await onChanged();
    } catch (reason) {
      setIsHighlighted(!next);
      setError(reason instanceof Error ? reason.message : "Unable to update this annotation.");
    } finally {
      setBusy(null);
    }
  }

  async function addComment() {
    const body = newComment.trim();
    if (!body) return;
    setBusy("new-comment");
    setError(null);
    try {
      await createAnnotationComment(annotation.id, body);
      setNewComment("");
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to add this comment.");
    } finally {
      setBusy(null);
    }
  }

  function beginCommentEdit(comment: AnnotationComment) {
    setEditingCommentId(comment.id);
    setEditingCommentDraft(comment.body);
  }

  async function saveComment(comment: AnnotationComment) {
    const body = editingCommentDraft.trim();
    if (!body) return;
    setBusy(`comment-${comment.id}`);
    setError(null);
    try {
      await updateAnnotationComment(comment.id, { body, revision: comment.revision });
      setEditingCommentId(null);
      setEditingCommentDraft("");
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to update this comment.");
    } finally {
      setBusy(null);
    }
  }

  async function removeComment(comment: AnnotationComment) {
    setBusy(`comment-${comment.id}`);
    setError(null);
    try {
      await deleteAnnotationComment(comment.id);
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to delete this comment.");
    } finally {
      setBusy(null);
    }
  }

  async function submitQuestion() {
    const body = questionDraft.trim();
    if (!body) return;
    setBusy("question");
    setError(null);
    try {
      await askAnnotation(annotation.id, body);
      setQuestionDraft("");
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to ask this question.");
    } finally {
      setBusy(null);
    }
  }

  async function retry(question: Annotation) {
    setBusy(`question-${question.id}`);
    setError(null);
    try {
      await retryQuestion(question.id);
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to retry this answer.");
    } finally {
      setBusy(null);
    }
  }

  async function removeAnnotation() {
    setBusy("delete");
    setError(null);
    try {
      await deleteAnnotation(annotation.id);
      await onChanged();
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to delete this annotation.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div
      aria-label="Edit annotation"
      className={`annotation-popover${dragging ? " annotation-popover-dragging" : ""}`}
      ref={popoverRef}
      role="dialog"
      style={{ left: position.left, top: position.top }}
    >
      <header
        onPointerCancel={finishDrag}
        onPointerDown={startDrag}
        onPointerMove={moveDrag}
        onPointerUp={finishDrag}
        title="Drag annotation"
      >
        <div>
          <strong>Annotation</strong>
          <small>{targetLabel(annotation)}</small>
        </div>
        <button aria-label="Close annotation editor" onClick={onClose} type="button">×</button>
      </header>
      {annotation.selector?.exact_quote ? (
        <blockquote>“{annotation.selector.exact_quote}”</blockquote>
      ) : null}
      <div className="annotation-quick-actions">
        <button
          aria-label={isHighlighted ? "Remove yellow highlight" : "Add yellow highlight"}
          aria-pressed={isHighlighted}
          className="annotation-marker-button"
          disabled={busy !== null}
          onClick={() => void toggleHighlight()}
          title={isHighlighted ? "Remove highlight" : "Highlight this text"}
          type="button"
        >
          <span aria-hidden="true">▰</span>
          {isHighlighted ? "Highlighted" : "Highlight"}
        </button>
        {busy === "highlight" ? <small>Saving…</small> : null}
      </div>

      <section className="annotation-thread" aria-label="Comments">
        <div className="annotation-thread-heading">
          <strong>Comments</strong>
          <small>{annotation.comments.length}</small>
        </div>
        <div className="annotation-comment-list">
          {annotation.comments.map((comment) => (
            <article className="annotation-comment" key={comment.id}>
              {editingCommentId === comment.id ? (
                <textarea
                  autoFocus
                  maxLength={4000}
                  onBlur={restoreComposerFocusAfterEscape}
                  onChange={(event) => setEditingCommentDraft(event.target.value)}
                  onKeyDown={preserveComposerFocusOnEscape}
                  onKeyUp={preserveComposerFocusOnEscape}
                  rows={3}
                  value={editingCommentDraft}
                />
              ) : (
                <button
                  className="annotation-comment-body"
                  onClick={() => beginCommentEdit(comment)}
                  type="button"
                >
                  {comment.body}
                </button>
              )}
              <footer>
                <time dateTime={comment.created_at}>{formatAnnotationTime(comment.created_at)}</time>
                {editingCommentId === comment.id ? (
                  <button
                    aria-label="Save this comment"
                    disabled={busy !== null || !editingCommentDraft.trim()}
                    onClick={() => void saveComment(comment)}
                    title="Save comment"
                    type="button"
                  >✓</button>
                ) : (
                  <button
                    aria-label="Delete this comment"
                    disabled={busy !== null}
                    onClick={() => void removeComment(comment)}
                    title="Delete this comment"
                    type="button"
                  >×</button>
                )}
              </footer>
            </article>
          ))}
          {!annotation.comments.length ? <p>No comments yet.</p> : null}
        </div>
        <form
          className="annotation-comment-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void addComment();
          }}
        >
          <textarea
            aria-label="New comment"
            maxLength={4000}
            onBlur={restoreComposerFocusAfterEscape}
            onChange={(event) => setNewComment(event.target.value)}
            onKeyDown={preserveComposerFocusOnEscape}
            onKeyUp={preserveComposerFocusOnEscape}
            placeholder="Add to the discussion…"
            rows={2}
            value={newComment}
          />
          <button disabled={busy !== null || !newComment.trim()} type="submit">
            {busy === "new-comment" ? "Adding…" : "Add comment"}
          </button>
        </form>
      </section>

      <section className="annotation-qa-thread" aria-label="AI questions">
        <div className="annotation-thread-heading">
          <strong>Ask AI</strong>
          <small>{questions.length}</small>
        </div>
        {questions.map((question) => (
          <article className="annotation-question" key={question.id}>
            <p>{question.body}</p>
            <div className="annotation-question-meta">
              <small>{formatAnnotationTime(question.created_at)}</small>
              <QuestionContextInspector question={question} />
            </div>
            <div className={`qa-state qa-${question.qa_status?.toLowerCase() ?? "unknown"}`}>
              {question.qa_status === "QUEUED" ? "Answer queued…" : null}
              {question.qa_status === "RUNNING" ? "Generating answer…" : null}
              {question.qa_status === "FAILED" ? (
                <>
                  <span>{question.qa_error_message ?? "The answer task failed."}</span>
                  <button
                    disabled={busy !== null}
                    onClick={() => void retry(question)}
                    type="button"
                  >{busy === `question-${question.id}` ? "Retrying…" : "Retry answer"}</button>
                </>
              ) : null}
            </div>
            {(responsesByQuestion.get(question.id) ?? []).map((response) => (
              <section className="qa-response" key={response.id}>
                <div className="qa-response-label">AI answer · generation {response.qa_generation}</div>
                <p>{response.body}</p>
                <AnswerCitations response={response} />
                <small>
                  {response.input_tokens !== null || response.output_tokens !== null
                    ? `${(response.input_tokens ?? 0) + (response.output_tokens ?? 0)} tokens`
                    : "Usage unavailable"}
                  {response.latency_ms !== null ? ` · ${response.latency_ms} ms` : ""}
                </small>
              </section>
            ))}
          </article>
        ))}
        <form
          className="annotation-question-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void submitQuestion();
          }}
        >
          <textarea
            aria-label="Ask AI about this annotation"
            maxLength={4000}
            onBlur={restoreComposerFocusAfterEscape}
            onChange={(event) => setQuestionDraft(event.target.value)}
            onKeyDown={preserveComposerFocusOnEscape}
            onKeyUp={preserveComposerFocusOnEscape}
            placeholder="Ask about this passage…"
            rows={2}
            value={questionDraft}
          />
          <button disabled={busy !== null || !questionDraft.trim()} type="submit">
            {busy === "question" ? "Queuing…" : "Ask AI"}
          </button>
        </form>
      </section>

      {error ? <small className="annotation-error" role="alert">{error}</small> : null}
      <footer className="annotation-popover-footer">
        {confirmDelete ? (
          <div className="annotation-delete-confirm" role="alertdialog" aria-label="Confirm annotation deletion">
            <span>Delete this Annotation and its discussion?</span>
            <button disabled={busy !== null} onClick={() => setConfirmDelete(false)} type="button">Cancel</button>
            <button disabled={busy !== null} onClick={() => void removeAnnotation()} type="button">
              {busy === "delete" ? "Deleting…" : "Delete annotation"}
            </button>
          </div>
        ) : (
          <button className="annotation-delete-trigger" onClick={() => setConfirmDelete(true)} type="button">
            Delete annotation
          </button>
        )}
      </footer>
    </div>
  );
}

function formatAnnotationTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function targetLabel(annotation: Annotation): string {
  if (annotation.target === "TRANSLATION") return "Chinese translation";
  if (annotation.target === "SOURCE") return "English source";
  if (annotation.target === "BLOCK") return "Content block";
  return "Paper";
}

function AnnotationCard({
  annotation,
  questions,
  responsesByQuestion,
  onChanged,
  onOpen,
}: {
  annotation: Annotation;
  questions: Annotation[];
  responsesByQuestion: Map<string, Annotation[]>;
  onChanged: () => Promise<void>;
  onOpen: (
    annotation: Annotation,
    position: AnnotationPopoverPosition,
  ) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove() {
    setBusy(true);
    try {
      await deleteAnnotation(annotation.id);
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to delete this annotation.");
      setBusy(false);
    }
  }

  function openEditor(event: ReactMouseEvent<HTMLButtonElement>) {
    if (annotation.block_id) {
      jumpToPaperBlock(annotation.block_id);
    }
    onOpen(annotation, popoverPositionAt(event.clientX, event.clientY));
  }

  async function retry(questionId: string) {
    setBusy(true);
    try {
      await retryQuestion(questionId);
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to retry this answer.");
    } finally {
      setBusy(false);
    }
  }

  const quote = annotation.selector?.exact_quote;
  const isAnchor = ["ANNOTATION", "HIGHLIGHT", "USER_NOTE"].includes(annotation.annotation_type);
  const visibleQuestions = isAnchor
    ? questions
    : annotation.annotation_type === "AI_QUESTION"
      ? [annotation]
      : [];
  return (
    <article className={`annotation-card annotation-${annotation.annotation_type.toLowerCase()}`}>
      <header>
        <span>
          {isAnchor
            ? "ANNOTATION"
            : annotation.annotation_type.replaceAll("_", " ")}
        </span>
        <small>{targetLabel(annotation)}</small>
      </header>
      {isAnchor ? (
        <div className="annotation-state-badges">
          {annotation.is_highlighted ? <span>Highlighted</span> : null}
          {annotation.comments.length ? <span>{annotation.comments.length} comments</span> : null}
          {questions.length ? <span>{questions.length} AI questions</span> : null}
        </div>
      ) : null}
      {annotation.resolution_status === "UNRESOLVED" ? (
        <p className="annotation-unresolved" role="status">
          Location unresolved · {annotation.resolution_reason?.replaceAll("_", " ")}
        </p>
      ) : null}
      {quote ? (
        <button
          className="annotation-quote"
          onClick={openEditor}
          type="button"
        >
          “{quote}”
        </button>
      ) : null}
      {annotation.comments.slice(-2).map((comment) => (
        <div className="annotation-card-comment" key={comment.id}>
          <p>{comment.body}</p>
          <time dateTime={comment.created_at}>{formatAnnotationTime(comment.created_at)}</time>
        </div>
      ))}
      {visibleQuestions.map((question) => (
        <section className="annotation-card-question" key={question.id}>
          <p>{question.body}</p>
          <QuestionContextInspector question={question} />
          <div className={`qa-state qa-${question.qa_status?.toLowerCase() ?? "unknown"}`}>
            {question.qa_status === "QUEUED" ? "Answer queued…" : null}
            {question.qa_status === "RUNNING" ? "Generating answer…" : null}
            {question.qa_status === "FAILED" ? (
              <>
                <span>{question.qa_error_message ?? "The answer task failed."}</span>
                <button disabled={busy} onClick={() => void retry(question.id)} type="button">
                  {busy ? "Retrying…" : "Retry answer"}
                </button>
              </>
            ) : null}
          </div>
          {(responsesByQuestion.get(question.id) ?? []).map((response) => (
            <section className="qa-response" key={response.id}>
              <div className="qa-response-label">AI answer · generation {response.qa_generation}</div>
              <p>{response.body}</p>
              <AnswerCitations response={response} />
              <small>
                {response.input_tokens !== null || response.output_tokens !== null
                  ? `${(response.input_tokens ?? 0) + (response.output_tokens ?? 0)} tokens`
                  : "Usage unavailable"}
                {response.latency_ms !== null ? ` · ${response.latency_ms} ms` : ""}
              </small>
            </section>
          ))}
        </section>
      ))}
      <footer>
        {isAnchor ? (
          <button onClick={openEditor} type="button">Open</button>
        ) : null}
        {confirmDelete ? (
          <span className="annotation-card-delete-confirm">
            <button disabled={busy} onClick={() => setConfirmDelete(false)} type="button">Cancel</button>
            <button disabled={busy} onClick={() => void remove()} type="button">
              {busy ? "Deleting…" : "Confirm delete"}
            </button>
          </span>
        ) : (
          <button disabled={busy} onClick={() => setConfirmDelete(true)} type="button">Delete</button>
        )}
      </footer>
      {error ? <small className="annotation-error" role="alert">{error}</small> : null}
    </article>
  );
}

export function AnnotationPanel({
  paperId,
  annotations,
  loading,
  error,
  hoveredAnnotationId = null,
  onAnnotationActivate,
  onCollapse,
  onRefresh,
  panelRef,
}: {
  paperId: string;
  annotations: Annotation[];
  loading: boolean;
  error: string | null;
  hoveredAnnotationId?: string | null;
  onAnnotationActivate?: (
    annotation: Annotation,
    position: AnnotationPopoverPosition,
  ) => void;
  onCollapse?: () => void;
  onRefresh: () => Promise<void>;
  panelRef?: RefObject<HTMLElement | null>;
}) {
  const [paperDraft, setPaperDraft] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [paperError, setPaperError] = useState<string | null>(null);
  const paperEntries = useMemo(
    () => annotations
      .filter((item) => (
        item.target === "PAPER"
        && ["USER_NOTE", "AI_QUESTION"].includes(item.annotation_type)
      ))
      .sort((left, right) => left.created_at.localeCompare(right.created_at)),
    [annotations],
  );
  const responsesByQuestion = useMemo(() => {
    const grouped = new Map<string, Annotation[]>();
    annotations.filter((item) => item.annotation_type === "AI_RESPONSE" && item.parent_annotation_id).forEach((item) => {
      const key = item.parent_annotation_id as string;
      grouped.set(key, [...(grouped.get(key) ?? []), item]);
    });
    return grouped;
  }, [annotations]);
  const questionsByAnchor = useMemo(() => {
    const grouped = new Map<string, Annotation[]>();
    annotations.filter((item) => (
      item.annotation_type === "AI_QUESTION" && item.anchor_annotation_id
    )).forEach((item) => {
      const key = item.anchor_annotation_id as string;
      grouped.set(key, [...(grouped.get(key) ?? []), item]);
    });
    return grouped;
  }, [annotations]);
  const anchoredEntries = useMemo(
    () => annotations
      .filter((item) => (
        item.target !== "PAPER"
        && item.creator_type === "USER"
        && ["ANNOTATION", "HIGHLIGHT", "USER_NOTE"].includes(item.annotation_type)
      ))
      .sort((left, right) => right.created_at.localeCompare(left.created_at)),
    [annotations],
  );

  useEffect(() => {
    if (!hoveredAnnotationId || !panelRef?.current) return;
    panelRef.current.querySelector<HTMLElement>(
      `[data-margin-annotation-id="${hoveredAnnotationId}"]`,
    )?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [hoveredAnnotationId, panelRef]);

  async function submitPaperEntry(kind: "comment" | "question") {
    const body = paperDraft.trim();
    if (!body) return;
    setBusy(kind);
    setPaperError(null);
    try {
      await createAnnotation({
        paper_id: paperId,
        annotation_type: kind === "comment" ? "USER_NOTE" : "AI_QUESTION",
        target: "PAPER",
        body,
      });
      setPaperDraft("");
      await onRefresh();
    } catch (reason) {
      setPaperError(reason instanceof Error ? reason.message : "Unable to save this paper entry.");
    } finally {
      setBusy(null);
    }
  }

  async function removePaperEntry(entry: Annotation) {
    setBusy(`delete-${entry.id}`);
    setPaperError(null);
    try {
      await deleteAnnotation(entry.id);
      await onRefresh();
    } catch (reason) {
      setPaperError(reason instanceof Error ? reason.message : "Unable to delete this paper entry.");
    } finally {
      setBusy(null);
    }
  }

  async function retryPaperQuestion(question: Annotation) {
    setBusy(`retry-${question.id}`);
    setPaperError(null);
    try {
      await retryQuestion(question.id);
      await onRefresh();
    } catch (reason) {
      setPaperError(reason instanceof Error ? reason.message : "Unable to retry this answer.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <aside className="annotation-panel" ref={panelRef}>
      <div className="annotation-panel-heading">
        <div>
          <p className="eyebrow">Ask about this paper</p>
          <strong>Paper discussion</strong>
        </div>
        <div className="annotation-panel-heading-actions">
          <button aria-label="Refresh paper discussion" onClick={() => void onRefresh()} type="button">↻</button>
          {onCollapse ? (
            <button
              aria-label="折叠批注边栏"
              className="annotation-panel-collapse"
              onClick={onCollapse}
              title="折叠批注"
              type="button"
            >›</button>
          ) : null}
        </div>
      </div>
      <form
        className="paper-question-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submitPaperEntry("question");
        }}
      >
        <label htmlFor="paper-question">Add a paper-level comment or ask AI</label>
        <textarea
          id="paper-question"
          maxLength={4000}
          onBlur={restoreComposerFocusAfterEscape}
          onChange={(event) => setPaperDraft(event.target.value)}
          onKeyDown={preserveComposerFocusOnEscape}
          onKeyUp={preserveComposerFocusOnEscape}
          placeholder="Write a comment or a paper-level question…"
          rows={3}
          value={paperDraft}
        />
        <div className="paper-composer-actions">
          <button
            disabled={busy !== null || !paperDraft.trim()}
            onClick={() => void submitPaperEntry("comment")}
            type="button"
          >
            {busy === "comment" ? "Saving…" : "Comment"}
          </button>
          <button disabled={busy !== null || !paperDraft.trim()} type="submit">
            {busy === "question" ? "Queuing…" : "Ask AI"}
          </button>
        </div>
        {paperError ? <small role="alert">{paperError}</small> : null}
      </form>
      {loading && !annotations.length ? <p>Loading paper discussion…</p> : null}
      {error ? <p className="annotation-error" role="alert">{error}</p> : null}
      <div className="paper-discussion-list">
        {paperEntries.map((entry) => entry.annotation_type === "USER_NOTE" ? (
          <article className="paper-discussion-entry paper-comment" key={entry.id}>
            <header><strong>Comment</strong><time dateTime={entry.created_at}>{formatAnnotationTime(entry.created_at)}</time></header>
            <p>{entry.body}</p>
            <button
              aria-label="Delete paper-level comment"
              disabled={busy !== null}
              onClick={() => void removePaperEntry(entry)}
              title="Delete comment"
              type="button"
            >×</button>
          </article>
        ) : (
          <article className="paper-discussion-entry paper-question" key={entry.id}>
            <header>
              <strong>Question</strong>
              <span><time dateTime={entry.created_at}>{formatAnnotationTime(entry.created_at)}</time><QuestionContextInspector question={entry} /></span>
            </header>
            <p>{entry.body}</p>
            <div className={`qa-state qa-${entry.qa_status?.toLowerCase() ?? "unknown"}`}>
              {entry.qa_status === "QUEUED" ? "Answer queued…" : null}
              {entry.qa_status === "RUNNING" ? "Generating answer…" : null}
              {entry.qa_status === "FAILED" ? (
                <>
                  <span>{entry.qa_error_message ?? "The answer task failed."}</span>
                  <button disabled={busy !== null} onClick={() => void retryPaperQuestion(entry)} type="button">
                    {busy === `retry-${entry.id}` ? "Retrying…" : "Retry answer"}
                  </button>
                </>
              ) : null}
            </div>
            {(responsesByQuestion.get(entry.id) ?? []).map((response) => (
              <section className="qa-response" key={response.id}>
                <div className="qa-response-label">AI answer · generation {response.qa_generation}</div>
                <p>{response.body}</p>
                <AnswerCitations response={response} />
                <small>
                  {response.input_tokens !== null || response.output_tokens !== null
                    ? `${(response.input_tokens ?? 0) + (response.output_tokens ?? 0)} tokens`
                    : "Usage unavailable"}
                  {response.latency_ms !== null ? ` · ${response.latency_ms} ms` : ""}
                </small>
              </section>
            ))}
          </article>
        ))}
        {!loading && !paperEntries.length ? (
          <p className="annotation-empty">No paper-level comments or questions yet.</p>
        ) : null}
      </div>
      <section className="annotation-margin-section" aria-label="Text annotations">
        <header>
          <div><p className="eyebrow">In the margins</p><strong>Text annotations</strong></div>
          <span>{anchoredEntries.length}</span>
        </header>
        {anchoredEntries.length ? (
          <div className="annotation-margin-list">
            {anchoredEntries.map((entry) => {
              const questions = questionsByAnchor.get(entry.id) ?? [];
              const latestComment = entry.comments.at(-1) ?? null;
              return (
                <button
                  className={`annotation-margin-card${hoveredAnnotationId === entry.id ? " annotation-margin-card-hovered" : ""}`}
                  data-margin-annotation-id={entry.id}
                  key={entry.id}
                  onClick={(event) => onAnnotationActivate?.(
                    entry,
                    popoverPositionAt(event.clientX, event.clientY),
                  )}
                  type="button"
                >
                  <span className="annotation-margin-kind">
                    {questions.length ? "Ask AI" : entry.comments.length ? "Comment" : "Annotation"}
                    <small>{entry.target === "SOURCE" ? "原文" : "译文"}</small>
                  </span>
                  {entry.selector?.exact_quote ? <q>{entry.selector.exact_quote}</q> : null}
                  {latestComment ? <p>{latestComment.body}</p> : null}
                  {questions.length ? (
                    <small>{questions.length} 个问题 · 点击查看详情</small>
                  ) : <small>{entry.comments.length} 条评论 · 点击查看详情</small>}
                </button>
              );
            })}
          </div>
        ) : <p className="annotation-empty">正文中的评论和 Ask AI 标注会像页边批注一样显示在这里。</p>}
      </section>
    </aside>
  );
}
