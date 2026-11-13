"use client";

import Link from "next/link";
import katex from "katex";
import "katex/contrib/mhchem";
import {
  createContext,
  Fragment,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import {
  addBlockBookmark,
  getPaperAnnotations,
  getBlockBookmarks,
  getPaper,
  getPaperBibliography,
  getPaperBlocks,
  getPaperOutline,
  getPaperTaskDetail,
  paperSourceDownloadUrl,
  getPaperTranslationProgress,
  getPaperTranslations,
  getSettingsStatus,
  markPaperOpened,
  paperBlockRenderAssetUrl,
  paperTableRenderAssetUrl,
  paperAssetUrl,
  reportRenderFailure,
  requestLatexFormulaRender,
  removeBlockBookmark,
  retranslateBlock,
  retranslatePaper,
  translateRemainingPaper,
  retryPaper,
  type BlockTranslation,
  type BlockBookmark,
  type Annotation,
  type BibliographyEntry,
  type ContentBlock,
  type Paper,
  type PaperBibliography,
  type PaperOutlineItem,
  type PaperTask,
  type PaperTaskDetail,
  type TranslationProgress,
} from "../../api";
import { PaperVisualMap } from "./paper-visual-map";
import {
  PAPER_BLOCK_JUMP_EVENT,
  capturePaperViewportAnchor,
  clearPaperReadingPosition,
  currentVisiblePaperBlockId,
  jumpToPaperBlock,
  jumpToPaperElement,
  paperNavigationWasReloaded,
  paperReadingBlockElements,
  rememberPaperBlockJumpOrigin,
  restorePaperViewportAnchor,
  restorePaperReadingPosition,
  updatePaperReadingPosition,
  type PaperBlockJumpOrigin,
  type PaperViewportAnchor,
} from "./paper-block-navigation";
import { BILINGUAL_MODE_CHANGE_EVENT, UI_STORAGE_KEYS } from "../../storage-keys";
import {
  AnnotationEditorPopover,
  AnnotationPanel,
  PersistentAnnotationHighlights,
  SelectionToolbar,
  type AnnotationPopoverPosition,
} from "./annotation-workspace";

function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

const DEFAULT_READER_LEFT_WIDTH = 220;
const DEFAULT_READER_RIGHT_WIDTH = 320;
const MIN_READER_LEFT_WIDTH = 168;
const MIN_READER_RIGHT_WIDTH = 260;
const MAX_READER_LEFT_WIDTH = 420;
const MAX_READER_RIGHT_WIDTH = 500;
const COLLAPSE_READER_LEFT_THRESHOLD = 142;
const COLLAPSE_READER_RIGHT_THRESHOLD = 220;

function clampReaderSidebarWidth(side: "left" | "right", value: number): number {
  const minimum = side === "left" ? MIN_READER_LEFT_WIDTH : MIN_READER_RIGHT_WIDTH;
  const maximum = side === "left" ? MAX_READER_LEFT_WIDTH : MAX_READER_RIGHT_WIDTH;
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

function CopyableCodeBlock({
  value,
  label = "Copy code",
  renderCodeElement = false,
}: {
  value: string;
  label?: string;
  renderCodeElement?: boolean;
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const resetTimer = useRef<number | null>(null);

  useEffect(() => () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
  }, []);

  async function copy() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        const copied = document.execCommand("copy");
        textarea.remove();
        if (!copied) throw new Error("Copy command was rejected.");
      }
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    resetTimer.current = window.setTimeout(() => setCopyState("idle"), 1800);
  }

  const buttonLabel = copyState === "copied"
    ? "Copied"
    : copyState === "failed"
      ? "Copy failed"
      : label;
  return (
    <div className={`copyable-code copyable-code-${copyState}`}>
      <button
        aria-label={buttonLabel}
        className="copy-code-button"
        onClick={() => void copy()}
        title={buttonLabel}
        type="button"
      >
        {copyState === "copied" ? (
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <path d="m5 12 4 4L19 6" />
          </svg>
        ) : (
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <rect height="13" rx="2" width="13" x="8" y="8" />
            <path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" />
          </svg>
        )}
      </button>
      <span aria-live="polite" className="copy-feedback">
        {copyState === "copied" ? "Copied to clipboard" : copyState === "failed" ? "Copy failed" : ""}
      </span>
      <pre>{renderCodeElement ? <code>{value}</code> : value}</pre>
    </div>
  );
}

function TaskInspector({ paperId, task }: { paperId: string; task: PaperTask }) {
  const [detail, setDetail] = useState<PaperTaskDetail | null>(null);
  const [detailState, setDetailState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const tooltipId = `task-detail-${task.id}`;
  const detailFacts = detail ? [
    { label: "Method", value: detail.uses_llm ? "LLM-assisted" : detail.execution_kind },
    { label: "Engine", value: detail.engine },
    { label: "Reasoning", value: detail.reasoning_effort },
    { label: "Renderer", value: detail.renderer },
    {
      label: "Target",
      value: detail.target_block_type
        ? `${detail.target_block_type}${detail.target_block_id ? ` · ${detail.target_block_id.slice(0, 8)}…` : ""}`
        : null,
    },
    { label: "Source", value: detail.target_location },
  ].filter((fact): fact is { label: string; value: string } => Boolean(fact.value)) : [];

  async function loadDetail() {
    if (detail !== null || detailState !== "idle") return;
    setDetailState("loading");
    try {
      setDetail(await getPaperTaskDetail(paperId, task.id));
      setDetailState("ready");
    } catch {
      setDetailState("error");
    }
  }

  return (
    <div
      className="task-inspector"
      onFocus={() => void loadDetail()}
      onMouseEnter={() => void loadDetail()}
    >
      <button
        aria-describedby={tooltipId}
        aria-label={`Explain ${task.task_type.replaceAll("_", " ")}`}
        className="task-info-button"
        type="button"
      >
        i
      </button>
      <div className="task-tooltip" id={tooltipId} role="tooltip">
        <div className="task-tooltip-heading">
          <strong>{task.task_type.replaceAll("_", " ")}</strong>
          <div>
            {detail ? (
              <span className={`task-method-badge task-method-${detail.execution_kind}`}>
                {detail.uses_llm ? "LLM API" : detail.execution_kind}
              </span>
            ) : null}
            <span>
              {task.status}
              {task.attempt_count ? ` · ${task.attempt_count}/${task.max_attempts}` : ""}
            </span>
          </div>
        </div>
        {detailState === "loading" ? <p>Loading task details…</p> : null}
        {detailState === "error" ? <p>Task details are temporarily unavailable.</p> : null}
        {detail ? (
          <>
            <p>{detail.description}</p>
            {detailFacts.length ? (
              <dl className="task-detail-facts">
                {detailFacts.map((fact) => (
                  <div key={fact.label} title={fact.value}>
                    <dt>{fact.label}</dt>
                    <dd>{fact.value}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
            {detail.trigger_reason ? (
              <section className="task-detail-section">
                <span>Why this task ran</span>
                <p>{detail.trigger_reason}</p>
                {detail.complexity_reasons.length ? (
                  <div className="task-reason-list">
                    {detail.complexity_reasons.map((reason) => (
                      <small key={reason}>{reason}</small>
                    ))}
                  </div>
                ) : null}
              </section>
            ) : null}
            {detail.result_summary ? (
              <section className="task-detail-section">
                <span>Result</span>
                <p>{detail.result_summary}</p>
              </section>
            ) : null}
            {detail.safety_notes.length ? (
              <section className="task-detail-section">
                <span>Safety boundary</span>
                <ul>
                  {detail.safety_notes.map((note) => <li key={note}>{note}</li>)}
                </ul>
              </section>
            ) : null}
            {detail.system_prompt ? (
              <section>
                <span>LLM system prompt</span>
                <CopyableCodeBlock label="Copy system prompt" value={detail.system_prompt} />
              </section>
            ) : null}
            {detail.source_latex ? (
              <section>
                <span>{detail.uses_llm ? "LaTeX sent for repair" : "Table LaTeX input"}</span>
                <CopyableCodeBlock label="Copy LaTeX input" value={detail.source_latex} />
              </section>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}

const PIPELINE_TASK_REGIONS: Record<string, string> = {
  FETCH_METADATA: "paper-metadata",
  FETCH_SOURCE: "paper-source-assets",
  PARSE_SOURCE: "paper-structured-view",
};

function TaskJumpButton({ paperId, task }: { paperId: string; task: PaperTask }) {
  const [jumping, setJumping] = useState(false);

  async function jumpToTaskOutput() {
    if (jumping) return;
    setJumping(true);
    try {
      const regionId = PIPELINE_TASK_REGIONS[task.task_type];
      if (regionId) {
        jumpToPaperElement(regionId, { block: "center", updateHash: true });
        return;
      }
      const detail = await getPaperTaskDetail(paperId, task.id);
      if (
        detail.target_block_id
        && jumpToPaperBlock(detail.target_block_id, { block: "center", updateHash: true })
      ) {
        return;
      }
      jumpToPaperElement("paper-structured-view", { block: "start", updateHash: true });
    } catch {
      jumpToPaperElement("paper-structured-view", { block: "start", updateHash: true });
    } finally {
      setJumping(false);
    }
  }

  return (
    <button
      aria-label={`Jump to ${task.task_type.replaceAll("_", " ")} output`}
      className="task-jump-button"
      disabled={jumping}
      onClick={() => void jumpToTaskOutput()}
      title="Jump to the related paper content"
      type="button"
    >
      <span aria-hidden="true">↘</span>
    </button>
  );
}

type OutlineScrollMetrics = {
  scrollable: boolean;
  thumbHeight: number;
  thumbTop: number;
};

function OutlineScrollRegion({
  ariaLabel,
  children,
  className,
}: {
  ariaLabel: string;
  children: ReactNode;
  className: string;
}) {
  const scrollRef = useRef<HTMLElement>(null);
  const [metrics, setMetrics] = useState<OutlineScrollMetrics>({
    scrollable: false,
    thumbHeight: 0,
    thumbTop: 0,
  });

  const updateMetrics = useCallback(() => {
    const element = scrollRef.current;
    if (element === null) return;
    const trackHeight = Math.max(0, element.clientHeight - 16);
    const maxScroll = Math.max(0, element.scrollHeight - element.clientHeight);
    const scrollable = maxScroll > 1 && trackHeight > 0;
    const thumbHeight = scrollable
      ? Math.min(trackHeight, Math.max(28, trackHeight * element.clientHeight / element.scrollHeight))
      : trackHeight;
    const thumbTop = scrollable
      ? (trackHeight - thumbHeight) * element.scrollTop / maxScroll
      : 0;
    const next = {
      scrollable,
      thumbHeight: Math.round(thumbHeight * 100) / 100,
      thumbTop: Math.round(thumbTop * 100) / 100,
    };
    setMetrics((current) => (
      current.scrollable === next.scrollable
      && current.thumbHeight === next.thumbHeight
      && current.thumbTop === next.thumbTop
        ? current
        : next
    ));
  }, []);

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (element === null) return;
    updateMetrics();
    const observer = new ResizeObserver(updateMetrics);
    observer.observe(element);
    for (const child of element.children) observer.observe(child);
    return () => observer.disconnect();
  }, [children, updateMetrics]);

  return (
    <div className="outline-scroll-frame">
      <section
        aria-label={ariaLabel}
        className={className}
        data-outline-scroll-container
        onScroll={updateMetrics}
        ref={scrollRef}
        tabIndex={0}
      >
        {children}
      </section>
      <span
        aria-hidden="true"
        className={`outline-scroll-rail${metrics.scrollable ? " outline-scroll-rail-visible" : ""}`}
      >
        <span
          className="outline-scroll-thumb"
          style={{ height: `${metrics.thumbHeight}px`, transform: `translateY(${metrics.thumbTop}px)` }}
        />
      </span>
    </div>
  );
}

function payloadText(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function blockNumber(payload: Record<string, unknown>): string | null {
  const numbering = payload.numbering;
  if (typeof numbering !== "object" || numbering === null || !("display_number" in numbering)) {
    return null;
  }
  const value = numbering.display_number;
  return typeof value === "string" && value.trim() ? value : null;
}

type RenderSegment = {
  type: "text" | "math" | "entity" | "reference" | "citation";
  text?: string;
  latex?: string;
  source_term?: string;
  command?: string;
  label?: string;
  status?: "PENDING" | "PARTIAL" | "RESOLVED" | "UNRESOLVED" | "AMBIGUOUS" | "UNNUMBERED" | "UNSUPPORTED";
  prefix?: string | null;
  suffix?: string | null;
  style?: "author-year" | "label";
  separator?: string;
  items?: Array<{
    citation_key: string;
    entry_id?: string;
    status: "RESOLVED" | "UNRESOLVED" | "NOT_PRINTED";
    label?: string;
    author?: string | null;
    year?: string | null;
    text?: string;
  }>;
  target_block_id?: string;
  target_kind?: string;
  display_name?: string;
  display_number?: string;
  display?: boolean;
  latex_render?: LatexRenderState;
  marks?: Array<"strong" | "emphasis" | "code" | "underline" | "smallcaps">;
};

const CitationPreviewContext = createContext<ReadonlyMap<string, BibliographyEntry>>(
  new Map(),
);

type CitationItem = NonNullable<RenderSegment["items"]>[number];

function bibliographyPeople(entry: BibliographyEntry): string | null {
  const authors = entry.persons
    .filter((person) => person.role === "author")
    .sort((left, right) => left.position - right.position);
  const people = authors.length
    ? authors
    : entry.persons
      .filter((person) => person.role === "editor")
      .sort((left, right) => left.position - right.position);
  if (!people.length) return null;
  const visible = people.slice(0, 6).map((person) => person.display_name).filter(Boolean);
  if (!visible.length) return null;
  return people.length > visible.length
    ? `${visible.join(", ")} 等 ${people.length} 位作者`
    : visible.join(", ");
}

function CitationPreviewLink({
  item,
  label,
  command,
}: {
  item: CitationItem;
  label: string;
  command: string;
}) {
  const entries = useContext(CitationPreviewContext);
  const entry = item.entry_id ? entries.get(item.entry_id) : undefined;
  const anchorRef = useRef<HTMLAnchorElement>(null);
  const tooltipId = useId();
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{
    left: number;
    placement: "above" | "below";
    top: number;
    width: number;
  } | null>(null);

  const updatePosition = useCallback(() => {
    const anchor = anchorRef.current;
    if (!anchor) return;
    const rectangle = anchor.getBoundingClientRect();
    const width = Math.min(380, Math.max(240, window.innerWidth - 24));
    const halfWidth = width / 2;
    const left = Math.min(
      window.innerWidth - 12 - halfWidth,
      Math.max(12 + halfWidth, rectangle.left + rectangle.width / 2),
    );
    const placement = rectangle.top >= 230 ? "above" : "below";
    setPosition({
      left,
      placement,
      top: placement === "above" ? rectangle.top - 10 : rectangle.bottom + 10,
      width,
    });
  }, []);

  const showPreview = useCallback(() => {
    if (!entry) return;
    updatePosition();
    setOpen(true);
  }, [entry, updatePosition]);

  useEffect(() => {
    if (!open) return;
    const reposition = () => updatePosition();
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open, updatePosition]);

  const authors = entry ? bibliographyPeople(entry) : null;
  const title = entry?.title?.trim() || null;
  const formattedFallback = !title ? entry?.formatted_text?.trim() || null : null;
  const metadata = entry
    ? [entry.container_title, entry.publication_year].filter(Boolean).join(" · ")
    : "";
  return (
    <span
      className="citation-preview-anchor"
      onMouseEnter={showPreview}
      onMouseLeave={() => setOpen(false)}
    >
      <a
        aria-describedby={entry ? tooltipId : undefined}
        href={`#bibliography-entry-${item.entry_id}`}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen(false)}
        onFocus={showPreview}
        ref={anchorRef}
        title={`${command}{${item.citation_key}}`}
      >
        {label}
      </a>
      {open && entry && position && typeof document !== "undefined"
        ? createPortal(
          <span
            className={`citation-hover-card citation-hover-card-${position.placement}`}
            id={tooltipId}
            role="tooltip"
            style={{ left: position.left, top: position.top, width: position.width }}
          >
            <span className="citation-hover-kicker">
              引用 {entry.display_label ? `[${entry.display_label}]` : entry.citation_key}
            </span>
            {title ? <strong>{title}</strong> : null}
            {authors ? <span className="citation-hover-authors">{authors}</span> : null}
            {metadata ? <span className="citation-hover-metadata">{metadata}</span> : null}
            {entry.doi ? <code>DOI {entry.doi}</code> : null}
            {formattedFallback ? (
              <span className="citation-hover-fallback">{formattedFallback}</span>
            ) : null}
            <span className="citation-hover-action">点击跳转到文末参考文献</span>
          </span>,
          document.body,
        )
        : null}
    </span>
  );
}

type LatexRenderState = {
  status?: string;
  asset?: { path?: string; mime_type?: string; byte_size?: number };
  compiler?: string;
  renderer_version?: string;
};

type FormulaRepairSelector = {
  target: "BLOCK" | "TRANSLATION";
  path: Array<string | number>;
  exactLatex: string;
  translationId?: string;
};

type FormulaRepairContext = {
  paperId: string;
  blockId: string;
  onQueued: () => void;
  pathPrefix: Array<string | number>;
  target?: "BLOCK" | "TRANSLATION";
  translationId?: string;
};

type StatementPart = {
  type: "prose" | "equation" | "list";
  text?: string;
  segments?: RenderSegment[];
  latex?: string;
  environment?: string;
  items?: string[];
  item_segments?: RenderSegment[][];
  latex_render?: LatexRenderState;
};

function renderSegments(payload: Record<string, unknown>): RenderSegment[] {
  if (!Array.isArray(payload.segments)) return [];
  return payload.segments.filter((segment): segment is RenderSegment => {
    if (typeof segment !== "object" || segment === null || !("type" in segment)) return false;
    return ["text", "math", "entity", "reference", "citation"].includes(
      String(segment.type),
    );
  });
}

function statementParts(payload: Record<string, unknown>): StatementPart[] {
  if (!Array.isArray(payload.parts)) return [];
  return payload.parts.filter((part): part is StatementPart => (
    typeof part === "object"
    && part !== null
    && "type" in part
    && ["prose", "equation", "list"].includes(String(part.type))
  ));
}

type RepairTarget = {
  paperId: string;
  blockId: string;
  renderer: "katex" | "figure" | "table" | "latex";
  onQueued: () => void;
  formulaSelector?: FormulaRepairSelector;
};

function RenderRepairReporter({
  target,
  errorMessage,
}: {
  target: RepairTarget;
  errorMessage: string;
}) {
  const [state, setState] = useState<
    "idle" | "reporting-latex" | "reporting-ai" | "queued-latex" | "queued-ai"
    | "applied" | "local-error" | "error"
  >("idle");
  const [requestError, setRequestError] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);
  const activeTaskId = useRef<string | null>(null);
  const pollingDeadline = useRef(0);
  const activeMode = useRef<"latex" | "ai">("latex");
  const { paperId, blockId, renderer, onQueued, formulaSelector } = target;

  useEffect(() => () => {
    activeTaskId.current = null;
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
  }, []);

  useEffect(() => {
    if (state !== "applied") return;
    const timer = window.setTimeout(() => {
      setRequestError(
        "The repair task finished, but this formula still could not be rendered. You can retry the repair.",
      );
      setState("error");
    }, 6_000);
    return () => window.clearTimeout(timer);
  }, [state]);

  const pollRepair = useCallback(async function pollRepairTask(taskId: string) {
    if (activeTaskId.current !== taskId) return;
    try {
      const detail = await getPaperTaskDetail(paperId, taskId);
      if (activeTaskId.current !== taskId) return;
      if (detail.status === "SUCCEEDED") {
        activeTaskId.current = null;
        setState("applied");
        onQueued();
        return;
      }
      if (["FAILED", "CANCELLED"].includes(detail.status)) {
        activeTaskId.current = null;
        setRequestError(
          detail.task_error_message
          ?? detail.result_summary
          ?? `Repair task ${detail.status.toLowerCase()}.`,
        );
        setState(activeMode.current === "latex" ? "local-error" : "error");
        return;
      }
      if (Date.now() >= pollingDeadline.current) {
        activeTaskId.current = null;
        setRequestError(
          "The repair is taking longer than expected. Its task remains visible in the pipeline; retry after it reaches a terminal state.",
        );
        setState(activeMode.current === "latex" ? "local-error" : "error");
        return;
      }
    } catch (reason) {
      if (Date.now() >= pollingDeadline.current) {
        activeTaskId.current = null;
        setRequestError(
          reason instanceof Error ? reason.message : "Unable to refresh the repair task.",
        );
        setState("error");
        return;
      }
    }
    pollTimer.current = window.setTimeout(() => void pollRepairTask(taskId), 1200);
  }, [onQueued, paperId]);

  const requestLatex = useCallback(async () => {
    activeMode.current = "latex";
    setState("reporting-latex");
    setRequestError(null);
    try {
      const result = await requestLatexFormulaRender(
        paperId,
        blockId,
        errorMessage,
        formulaSelector,
      );
      if (result.applied || result.status === "SUCCEEDED") {
        setState("applied");
        onQueued();
      } else if (result.queued || ["QUEUED", "RUNNING"].includes(result.status)) {
        setState("queued-latex");
        activeTaskId.current = result.task_id;
        pollingDeadline.current = Date.now() + 120_000;
        onQueued();
        pollTimer.current = window.setTimeout(
          () => void pollRepair(result.task_id),
          500,
        );
      } else {
        setRequestError(`Repair task returned ${result.status}.`);
        setState("error");
      }
    } catch (reason) {
      setRequestError(reason instanceof Error ? reason.message : "Unable to queue TeX rendering.");
      setState("local-error");
    }
  }, [
    blockId,
    errorMessage,
    formulaSelector,
    onQueued,
    paperId,
    pollRepair,
  ]);

  const requestAiRepair = useCallback(async () => {
    activeMode.current = "ai";
    setState("reporting-ai");
    setRequestError(null);
    try {
      const result = await reportRenderFailure(
        paperId,
        blockId,
        renderer,
        errorMessage,
        formulaSelector,
        false,
      );
      if (result.applied || result.status === "SUCCEEDED") {
        setState("applied");
        onQueued();
      } else if (result.queued || ["QUEUED", "RUNNING"].includes(result.status)) {
        setState("queued-ai");
        activeTaskId.current = result.task_id;
        pollingDeadline.current = Date.now() + 120_000;
        onQueued();
        pollTimer.current = window.setTimeout(() => void pollRepair(result.task_id), 500);
      } else {
        setRequestError(`Repair task returned ${result.status}.`);
        setState("error");
      }
    } catch (reason) {
      setRequestError(reason instanceof Error ? reason.message : "Unable to queue AI repair.");
      setState("error");
    }
  }, [blockId, errorMessage, formulaSelector, onQueued, paperId, pollRepair, renderer]);

  if (state === "queued-latex") return <small>原版 LaTeX 正在本地渲染…</small>;
  if (state === "queued-ai") return <small>AI-assisted repair running…</small>;
  if (state === "applied") return <small>渲染结果已生成 · 正在刷新…</small>;
  return (
    <span className="ai-repair-action">
      {requestError ? <small title={requestError}>{requestError}</small> : null}
      {state === "local-error" || state === "error" || state === "reporting-ai" ? (
        <>
          <button
            className="ai-repair-button"
            disabled={state === "reporting-ai"}
            onClick={() => void requestLatex()}
            type="button"
          >
            重试原版 LaTeX 渲染
          </button>
          <button
            className="ai-repair-button"
            disabled={state === "reporting-ai"}
            onClick={() => void requestAiRepair()}
            type="button"
          >
            {state === "reporting-ai" ? "正在提交 AI…" : "尝试 AI 辅助修复"}
          </button>
        </>
      ) : (
        <button
          className="ai-repair-button"
          disabled={state === "reporting-latex"}
          onClick={() => void requestLatex()}
          type="button"
        >
          {state === "reporting-latex"
            ? "正在提交…"
            : "KaTeX 渲染失败，使用原版 LaTeX 渲染"}
        </button>
      )}
    </span>
  );
}

const KATEX_COMPATIBILITY_MACROS: Readonly<Record<string, string>> = {
  "\\slash": "/",
};

function normalizeKatexLatex(latex: string, environment?: string): string {
  let normalized = latex
    .replace(
      /\{\s*\\mbox\s*\{\s*\\boldmath\s*\{\s*\$([^$]+)\$\s*\}\s*\}\s*\}/gs,
      (_match, body: string) => `{\\boldsymbol{${body.trim()}}}`,
    )
    .replace(
      /\\mbox\s*\{\s*\\boldmath\s*\{\s*\$([^$]+)\$\s*\}\s*\}/gs,
      (_match, body: string) => `\\boldsymbol{${body.trim()}}`,
    )
    .replace(/\\(?:label|tag)\*?\s*\{[^{}]*\}|\\(?:notag|nonumber)\b/g, "")
    .trim();
  const outer = normalized.match(
    /^\\begin\s*\{(equation\*?|eqnarray\*?|align\*?|gather\*?|multline\*?|displaymath)\}([\s\S]*)\\end\s*\{\1\}\s*$/,
  );
  const sourceEnvironment = outer?.[1] ?? environment;
  if (outer) normalized = outer[2].trim();
  const compatibilityEnvironment = sourceEnvironment?.startsWith("eqnarray")
    ? "aligned"
    : sourceEnvironment?.startsWith("align")
    ? "aligned"
    : sourceEnvironment?.startsWith("gather")
      ? "gathered"
      : sourceEnvironment?.startsWith("multline")
        ? "aligned"
        : null;
  const alreadyWrapped = /^\\begin\s*\{(?:aligned|gathered)\}/.test(normalized);
  if (compatibilityEnvironment && !alreadyWrapped) {
    normalized = `\\begin{${compatibilityEnvironment}}${normalized}\\end{${compatibilityEnvironment}}`;
  }
  return normalized;
}

function MathExpression({
  latex,
  display = false,
  environment,
  repairTarget,
  latexRender,
}: {
  latex: string;
  display?: boolean;
  environment?: string;
  repairTarget?: RepairTarget;
  latexRender?: LatexRenderState;
}) {
  const renderLatex = useMemo(
    () => normalizeKatexLatex(latex, environment),
    [environment, latex],
  );
  const result = useMemo(() => {
    try {
      return {
        html: katex.renderToString(renderLatex, {
          displayMode: display,
          throwOnError: true,
          trust: false,
          strict: "warn",
          maxExpand: 1000,
          maxSize: 20,
          // Use a fresh object for every expression so source-defined global
          // macros cannot persist into another paper block.
          macros: { ...KATEX_COMPATIBILITY_MACROS },
          output: "htmlAndMathml",
        }),
        error: null,
      };
    } catch (reason) {
      return {
        html: null,
        error: reason instanceof Error ? reason.message : "Unable to render this expression.",
      };
    }
  }, [display, renderLatex]);

  const latexAsset = latexRender?.status === "succeeded" ? latexRender.asset : undefined;
  if (latexAsset?.path && repairTarget) {
    return (
      <span
        className={display ? "latex-formula-render latex-formula-render-display" : "latex-formula-render"}
        data-annotation-math={latex.normalize("NFC")}
        title={`Rendered locally with ${latexRender?.compiler ?? "TeX Live"}`}
      >
        <img
          alt={latex}
          src={paperBlockRenderAssetUrl(repairTarget.paperId, repairTarget.blockId, latexAsset.path)}
        />
      </span>
    );
  }
  if (result.html) {
    return (
      <span
        className={display ? "rendered-math rendered-math-display" : "rendered-math"}
        data-annotation-math={latex.normalize("NFC")}
        dangerouslySetInnerHTML={{ __html: result.html }}
      />
    );
  }
  return (
    <span
      className="math-fallback"
      data-annotation-math={latex.normalize("NFC")}
      title={result.error ?? undefined}
    >
      <code>{latex}</code>
      {repairTarget && result.error ? (
        <RenderRepairReporter target={repairTarget} errorMessage={result.error} />
      ) : <small>Formula rendering needs repair</small>}
    </span>
  );
}

function RichText({
  payload,
  fallback,
  preserveSpacing = false,
  formulaRepair,
}: {
  payload: Record<string, unknown>;
  fallback: string | null;
  preserveSpacing?: boolean;
  formulaRepair?: FormulaRepairContext;
}) {
  const segments = renderSegments(payload);
  if (!segments.length) return <>{fallback}</>;
  return (
    <>
      {segments.map((segment, index) => {
        const previous = index > 0 ? segments[index - 1] : null;
        const currentValue = segment.text ?? segment.latex ?? "";
        const previousValue = previous?.text ?? previous?.latex ?? "";
        const addSpace = !preserveSpacing
          && previous !== null
          && segment.type !== "entity"
          && previous.type !== "entity"
          && !/^\s/.test(currentValue)
          && !/\s$/.test(previousValue)
          && !/^[,.;:!?%)\]}]/.test(currentValue)
          && !/[({\[]$/.test(previousValue);
        return (
          <Fragment key={`${segment.type}-${index}-${currentValue}`}>
            {addSpace ? (
              segment.type === "reference" ? (
                <span data-annotation-auxiliary="true"> </span>
              ) : " "
            ) : null}
            <RichSegment
              segment={segment}
              repairTarget={
                formulaRepair && segment.type === "math" && segment.latex
                  ? {
                      paperId: formulaRepair.paperId,
                      blockId: formulaRepair.blockId,
                      renderer: "katex",
                      onQueued: formulaRepair.onQueued,
                      formulaSelector: {
                        target: formulaRepair.target ?? "BLOCK",
                        path: [...formulaRepair.pathPrefix, index],
                        exactLatex: segment.latex,
                        translationId: formulaRepair.translationId,
                      },
                    }
                  : undefined
              }
            />
          </Fragment>
        );
      })}
    </>
  );
}

function RichSegment({
  segment,
  repairTarget,
}: {
  segment: RenderSegment;
  repairTarget?: RepairTarget;
}) {
  if (segment.type === "citation") {
    const items = segment.items ?? [];
    if (!items.length) {
      return (
        <span
          className="paper-citation paper-citation-unresolved"
          data-annotation-auxiliary="true"
          title="暂时无法从论文的 bibliography 数据中解析这个引用。"
        >
          {segment.text ?? "[?]"}
        </span>
      );
    }
    const command = (segment.command ?? "cite").toLowerCase();
    const textual = segment.style === "author-year"
      && ["cite", "citealp", "citealt", "citeauthor", "citeyear", "citet", "textcite"]
        .includes(command);
    const opening = segment.style === "author-year" ? (textual ? "" : "(") : (
      ["citeauthor", "citeyear", "supercite"].includes(command) ? "" : "["
    );
    const closing = segment.style === "author-year" ? (textual ? "" : ")") : (
      ["citeauthor", "citeyear", "supercite"].includes(command) ? "" : "]"
    );
    const itemText = (item: NonNullable<RenderSegment["items"]>[number]) => {
      if (command.includes("author")) return item.author ?? item.text ?? "?";
      if (command.includes("year")) return item.year ?? item.text ?? "?";
      if (segment.style === "author-year" && ["cite", "citet", "textcite"].includes(command)) {
        return `${item.author ?? "?"} (${item.year ?? "?"})`;
      }
      if (segment.style === "author-year" && command === "citealt") {
        return `${item.author ?? "?"} ${item.year ?? "?"}`;
      }
      if (segment.style === "author-year") {
        return `${item.author ?? "?"}, ${item.year ?? "?"}`;
      }
      return item.label ?? item.text ?? "?";
    };
    return (
      <span
        aria-label={segment.text}
        className={`paper-citation${segment.status === "RESOLVED" ? "" : " paper-citation-unresolved"}`}
        data-annotation-auxiliary="true"
      >
        {opening}
        {segment.prefix ? <>{segment.prefix} </> : null}
        {items.map((item, index) => (
          <Fragment key={`${item.citation_key}-${index}`}>
            {index ? segment.separator ?? ", " : null}
            {item.entry_id && item.status === "RESOLVED" ? (
              <CitationPreviewLink
                command={segment.command ?? "cite"}
                item={item}
                label={itemText(item)}
              />
            ) : (
              <span title={`未能在文末参考文献中定位 ${item.citation_key}`}>
                {itemText(item)}
              </span>
            )}
          </Fragment>
        ))}
        {segment.suffix ? <>, {segment.suffix}</> : null}
        {closing}
      </span>
    );
  }
  if (segment.type === "reference") {
    if (segment.status === "RESOLVED" && segment.target_block_id && segment.text) {
      return (
        <a
          className="paper-reference"
          data-annotation-auxiliary="true"
          href={`#block-${segment.target_block_id}`}
          title={`${segment.command ?? "ref"}{${segment.label ?? ""}}`}
        >
          {segment.text}
        </a>
      );
    }
    const reason = segment.status === "AMBIGUOUS"
      ? "这个引用标签在源码中出现了多次，无法安全确定目标。"
      : segment.status === "UNSUPPORTED"
        ? "页面引用依赖原始 PDF 排版，当前阅读视图暂不支持。"
        : segment.status === "UNNUMBERED"
          ? "引用目标存在，但它没有可显示的编号。"
          : "暂时无法在论文源码中找到这个引用目标。";
    return (
      <span
        className="paper-reference paper-reference-unresolved"
        data-annotation-auxiliary="true"
        title={reason}
      >
        {segment.text ?? "??"}
      </span>
    );
  }
  if (segment.type === "entity" && segment.text && segment.source_term) {
    return (
      <span className="translation-entity" tabIndex={0}>
        {segment.text}
        <span
          aria-label="English source term"
          className="translation-entity-tooltip"
          data-annotation-auxiliary="true"
          role="tooltip"
        >
          <small>English source</small>
          <strong>{segment.source_term}</strong>
        </span>
      </span>
    );
  }
  let content: ReactNode = segment.type === "math" && segment.latex ? (
    <MathExpression
      display={segment.display}
      latex={segment.latex}
      latexRender={segment.latex_render}
      repairTarget={repairTarget}
    />
  ) : segment.text;
  const marks = segment.marks ?? [];
  for (const mark of [...marks].reverse()) {
    if (mark === "strong") content = <strong>{content}</strong>;
    else if (mark === "emphasis") content = <em>{content}</em>;
    else if (mark === "code") content = <code>{content}</code>;
    else if (mark === "underline") content = <u>{content}</u>;
    else if (mark === "smallcaps") content = <span className="rich-smallcaps">{content}</span>;
  }
  if (segment.type === "math" && !marks.length) return content;
  return <span className="rich-segment">{content}</span>;
}

function InlineLatexText({
  value,
  formulaRepair,
}: {
  value: string;
  formulaRepair?: FormulaRepairContext;
}) {
  const parts: Array<{ text?: string; latex?: string; display?: boolean }> = [];
  const pattern = /\$\$([\s\S]+?)\$\$|\$([^$]+?)\$|\\\[([\s\S]+?)\\\]|\\\((.+?)\\\)/g;
  let cursor = 0;
  for (const match of value.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) parts.push({ text: value.slice(cursor, index) });
    parts.push({
      latex: (match[1] ?? match[2] ?? match[3] ?? match[4]).trim(),
      display: match[1] !== undefined || match[3] !== undefined,
    });
    cursor = index + match[0].length;
  }
  if (cursor < value.length) parts.push({ text: value.slice(cursor) });
  return (
    <>
      {parts.map((part, index) => part.latex ? (
        <MathExpression
          display={part.display}
          key={`metadata-math-${index}`}
          latex={part.latex}
          repairTarget={formulaRepair ? {
            paperId: formulaRepair.paperId,
            blockId: formulaRepair.blockId,
            renderer: "katex",
            onQueued: formulaRepair.onQueued,
            formulaSelector: {
              target: formulaRepair.target ?? "BLOCK",
              path: [...formulaRepair.pathPrefix, index],
              exactLatex: part.latex,
              translationId: formulaRepair.translationId,
            },
          } : undefined}
        />
      ) : (
        <span key={`metadata-text-${index}`}>
          {part.text?.replaceAll("---", "—").replaceAll("--", "–").replaceAll("~", " ")}
        </span>
      ))}
    </>
  );
}

type RenderAsset = { path: string; mime_type: string; byte_size?: number };
type TableRenderAsset = { path: string; mime_type: string; byte_size?: number };
type PreviewMedia = {
  src: string;
  mimeType: string;
  alt: string;
  caption: string | null;
  label: string;
};
type TableRenderState = {
  strategy?: string;
  status?: string;
  complexity_reasons?: string[];
  error?: string;
  asset?: TableRenderAsset;
  fallback?: string;
  repair_task_id?: string;
  repair_eligible?: boolean;
  repair_ineligible_reason?: string;
};
type FigureRenderState = {
  strategy?: string;
  status?: string;
  error?: string;
  asset?: TableRenderAsset;
  repair_eligible?: boolean;
  repair_ineligible_reason?: string;
};
type LatexBlockRenderState = FigureRenderState & {
  render_kind?: "algorithm" | "visual";
};
type PDFDocumentState = {
  status?: string;
  error?: string;
  page_count?: number;
  page_width_points?: number | null;
  page_height_points?: number | null;
  viewer?: string;
};

function renderAssets(payload: Record<string, unknown>): RenderAsset[] {
  if (!Array.isArray(payload.assets)) return [];
  return payload.assets.filter((asset): asset is RenderAsset => (
    typeof asset === "object"
    && asset !== null
    && "path" in asset
    && typeof asset.path === "string"
    && "mime_type" in asset
    && typeof asset.mime_type === "string"
    && (asset.mime_type === "application/pdf" || asset.mime_type.startsWith("image/"))
  ));
}

function MediaPreviewTrigger({
  media,
  className = "",
  onPreview,
}: {
  media: PreviewMedia;
  className?: string;
  onPreview: (media: PreviewMedia) => void;
}) {
  const isPdf = media.mimeType === "application/pdf";
  return (
    <button
      aria-label={`Enlarge ${media.label.toLowerCase()}`}
      className={`media-preview-trigger ${className}`.trim()}
      onClick={() => onPreview(media)}
      type="button"
    >
      {isPdf ? (
        <object
          aria-hidden="true"
          data={`${media.src}#toolbar=0&navpanes=0&view=FitH`}
          tabIndex={-1}
          type="application/pdf"
        >
          <span>PDF preview</span>
        </object>
      ) : (
        <img alt={media.alt} loading="lazy" src={media.src} />
      )}
      <span aria-hidden="true" className="media-preview-hint">Expand</span>
    </button>
  );
}

function MediaPreviewDialog({
  media,
  onDismiss,
}: {
  media: PreviewMedia;
  onDismiss: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onDismiss();
        return;
      }
      if (event.key !== "Tab" || dialogRef.current === null) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        "button, [href], iframe, object, [tabindex]:not([tabindex='-1'])",
      )).filter((element) => !element.hasAttribute("disabled"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [onDismiss]);

  const isPdf = media.mimeType === "application/pdf";
  return (
    <div
      className="media-lightbox-backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onDismiss();
      }}
    >
      <section
        aria-label={media.label}
        aria-modal="true"
        className={`media-lightbox${isPdf ? " media-lightbox-pdf" : ""}`}
        ref={dialogRef}
        role="dialog"
      >
        <header className="media-lightbox-header">
          <strong>{media.label}</strong>
          <button
            aria-label="Close media preview"
            className="media-lightbox-close"
            onClick={onDismiss}
            ref={closeButtonRef}
            type="button"
          >
            ×
          </button>
        </header>
        <div className="media-lightbox-content">
          {isPdf ? (
            <object
              aria-label={media.alt}
              data={`${media.src}#toolbar=0&navpanes=0&view=FitH`}
              type="application/pdf"
            >
              <p>This browser cannot display the PDF preview.</p>
            </object>
          ) : (
            <img alt={media.alt} src={media.src} />
          )}
        </div>
        {media.caption ? <p className="media-lightbox-caption">{media.caption}</p> : null}
      </section>
    </div>
  );
}

function RawLatex({ block }: { block: ContentBlock }) {
  const latex = payloadText(block.source_payload, "latex");
  if (!latex) return null;
  return (
    <details className="raw-latex" data-annotation-auxiliary="true">
      <summary>View LaTeX source</summary>
      <CopyableCodeBlock label="Copy LaTeX source" value={latex} />
    </details>
  );
}

function StructuredTable({
  blockId,
  paperId,
  rows,
  rowSegments,
  onRepairQueued,
}: {
  blockId: string;
  paperId: string;
  rows: string[][];
  rowSegments: unknown[];
  onRepairQueued: () => void;
}) {
  return (
    <div className="rendered-table-scroll">
      <table>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={`${blockId}-row-${rowIndex}`}>
              {row.map((cell, cellIndex) => {
                const Cell = rowIndex === 0 ? "th" : "td";
                const segments = Array.isArray(rowSegments[rowIndex])
                  ? rowSegments[rowIndex][cellIndex]
                  : [];
                return (
                  <Cell key={`${blockId}-${rowIndex}-${cellIndex}`}>
                    <RichText
                      payload={{ segments }}
                      fallback={cell}
                      formulaRepair={{
                        paperId,
                        blockId,
                        onQueued: onRepairQueued,
                        pathPrefix: ["row_segments", rowIndex, cellIndex],
                      }}
                    />
                  </Cell>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PDFDocumentReader({
  asset,
  block,
  paperId,
  state,
  onMediaPreview,
}: {
  asset: RenderAsset;
  block: ContentBlock;
  paperId: string;
  state: PDFDocumentState;
  onMediaPreview: (media: PreviewMedia) => void;
}) {
  const [page, setPage] = useState(1);
  const pageCount = typeof state.page_count === "number" && state.page_count > 0
    ? state.page_count
    : 1;
  const pageWidth = typeof state.page_width_points === "number"
    && Number.isFinite(state.page_width_points)
    && state.page_width_points > 0
    ? state.page_width_points
    : null;
  const pageHeight = typeof state.page_height_points === "number"
    && Number.isFinite(state.page_height_points)
    && state.page_height_points > 0
    ? state.page_height_points
    : null;
  const contentAspectRatio = pageWidth !== null && pageHeight !== null
    ? `${pageWidth} / ${pageHeight * pageCount}`
    : null;
  const source = paperAssetUrl(paperId, asset.path);
  const pageSource = `${source}#page=${page}&toolbar=0&navpanes=0&view=FitH`;
  const title = asset.path.split("/").at(-1) ?? "Embedded PDF";

  return (
    <section className="pdf-document-reader" aria-label={`PDF reader: ${title}`}>
      <header className="pdf-document-toolbar" data-annotation-auxiliary="true">
        <span className="pdf-document-title" title={asset.path}>{title}</span>
        <nav aria-label="PDF page navigation">
          <button
            aria-label="Previous PDF page"
            disabled={page <= 1}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
            type="button"
          >
            ← 上一页
          </button>
          <output aria-live="polite">第 {page} / {pageCount} 页</output>
          <button
            aria-label="Next PDF page"
            disabled={page >= pageCount}
            onClick={() => setPage((current) => Math.min(pageCount, current + 1))}
            type="button"
          >
            下一页 →
          </button>
        </nav>
        <button
          className="pdf-document-expand"
          onClick={() => onMediaPreview({
            src: source,
            mimeType: "application/pdf",
            alt: title,
            caption: `Source document embedded by ${block.source_text ?? asset.path}`,
            label: "PDF continuous reader",
          })}
          type="button"
        >
          连续阅读 / 放大
        </button>
      </header>
      <div
        className={`pdf-document-viewport${contentAspectRatio ? " pdf-document-viewport-adaptive" : ""}`}
        style={contentAspectRatio ? { aspectRatio: contentAspectRatio } : undefined}
      >
        <object
          aria-label={`${title}, page ${page} of ${pageCount}`}
          data={pageSource}
          key={pageSource}
          type="application/pdf"
        >
          <p>
            当前浏览器无法内嵌显示 PDF。<a href={source} target="_blank" rel="noreferrer">打开原始 PDF</a>
          </p>
        </object>
      </div>
    </section>
  );
}

function BlockRenderer({
  block,
  paperId,
  onRepairQueued,
  onMediaPreview,
}: {
  block: ContentBlock;
  paperId: string;
  onRepairQueued: () => void;
  onMediaPreview: (media: PreviewMedia) => void;
}) {
  const text = payloadText(block.render_payload, "text") ?? block.source_text;
  const environment = payloadText(block.render_payload, "environment");
  const locatorFile = payloadText(block.source_locator, "root_file");
  const locatorLine = block.source_locator.expanded_line;
  const locator = locatorFile
    ? `${locatorFile}${typeof locatorLine === "number" ? `:${locatorLine}` : ""}`
    : null;
  const caption = payloadText(block.render_payload, "caption");
  const captionPayload = { segments: block.render_payload.caption_segments };
  const displayNumber = blockNumber(block.render_payload);

  if (block.block_type === "SECTION") {
    const level = block.heading_level ?? 2;
    const heading = <><span className="reader-heading-number">{displayNumber}</span><RichText payload={block.render_payload} fallback={text} /></>;
    if (level <= 2) return <h2 id={`block-${block.id}`} className="reader-heading">{heading}</h2>;
    if (level === 3) return <h3 id={`block-${block.id}`} className="reader-heading">{heading}</h3>;
    return <h4 id={`block-${block.id}`} className="reader-heading">{heading}</h4>;
  }

  if (block.block_type === "PARAGRAPH") {
    const centered = block.render_payload.presentation === "center";
    return (
      <p
        id={`block-${block.id}`}
        className={`reader-paragraph${centered ? " reader-paragraph-centered" : ""}`}
      >
        <RichText
          payload={block.render_payload}
          fallback={text}
          formulaRepair={{
            paperId,
            blockId: block.id,
            onQueued: onRepairQueued,
            pathPrefix: ["segments"],
          }}
        />
      </p>
    );
  }

  if (block.block_type === "LATEX_BLOCK") {
    const nativeRender = (
      typeof block.render_payload.latex_block_render === "object"
      && block.render_payload.latex_block_render !== null
    ) ? block.render_payload.latex_block_render as LatexBlockRenderState : {};
    const nativeAsset = (
      typeof nativeRender.asset === "object"
      && nativeRender.asset !== null
      && typeof nativeRender.asset.path === "string"
      && typeof nativeRender.asset.mime_type === "string"
    ) ? nativeRender.asset : null;
    const nativeAssetUrl = nativeAsset
      ? paperBlockRenderAssetUrl(paperId, block.id, nativeAsset.path)
      : null;
    return (
      <section id={`block-${block.id}`} className="reader-block native-latex-block">
        {nativeAssetUrl ? (
          <>
            <MediaPreviewTrigger
              className="latex-native-block-render"
              media={{
                src: nativeAssetUrl,
                mimeType: nativeAsset?.mime_type ?? "image/svg+xml",
                alt: `Rendered author-defined LaTeX environment${environment ? `: ${environment}` : ""}`,
                caption: null,
                label: "LaTeX visual block preview",
              }}
              onPreview={onMediaPreview}
            />
            {text ? (
              <details className="native-latex-accessible-fallback">
                <summary>查看可检索文本</summary>
                <p><RichText payload={block.render_payload} fallback={text} /></p>
              </details>
            ) : null}
          </>
        ) : nativeRender.status === "queued" ? (
          <div className="table-render-placeholder" role="status">
            <span aria-hidden="true" />
            正在使用本地 LaTeX 还原原始排版…
          </div>
        ) : (
          <div className="native-latex-fallback">
            {nativeRender.status === "failed" ? (
              <div className="render-diagnostic">
                <span>原版 LaTeX 视觉块渲染失败，暂时显示可读文本。</span>
                {nativeRender.error ? <small>{nativeRender.error}</small> : null}
              </div>
            ) : null}
            {text ? <p><RichText payload={block.render_payload} fallback={text} /></p> : null}
          </div>
        )}
        <RawLatex block={block} />
      </section>
    );
  }

  if (block.block_type === "PDF_DOCUMENT") {
    const assetValue = block.render_payload.asset;
    const asset = (
      typeof assetValue === "object"
      && assetValue !== null
      && "path" in assetValue
      && typeof assetValue.path === "string"
      && "mime_type" in assetValue
      && typeof assetValue.mime_type === "string"
    ) ? assetValue as RenderAsset : null;
    const state = (
      typeof block.render_payload.pdf_document === "object"
      && block.render_payload.pdf_document !== null
    ) ? block.render_payload.pdf_document as PDFDocumentState : {};
    const isPdfOnlyDocument = block.render_payload.document_mode === "pdf-only";
    return (
      <section
        id={`block-${block.id}`}
        className={`reader-block pdf-document-block${isPdfOnlyDocument ? " pdf-only-document-block" : ""}`}
      >
        <div className="block-label">
          {isPdfOnlyDocument ? "原始论文 PDF" : "内嵌 PDF 文档"}
          {state.status === "ready" && typeof state.page_count === "number"
            ? <span>{state.page_count} 页</span>
            : null}
        </div>
        {asset && state.status === "ready" ? (
          <PDFDocumentReader
            asset={asset}
            block={block}
            onMediaPreview={onMediaPreview}
            paperId={paperId}
            state={state}
          />
        ) : (
          <div className="render-diagnostic">
            <span>这份内嵌 PDF 暂时无法安全显示。</span>
            {state.error ? <small>{state.error}</small> : null}
          </div>
        )}
        {locator ? <small>{locator}</small> : null}
        <RawLatex block={block} />
      </section>
    );
  }

  if (block.block_type === "LIST") {
    const items = Array.isArray(block.render_payload.items)
      ? block.render_payload.items.filter((item): item is string => typeof item === "string")
      : [];
    const itemSegments = Array.isArray(block.render_payload.item_segments)
      ? block.render_payload.item_segments
      : [];
    const ListTag = environment === "enumerate" ? "ol" : "ul";
    return (
      <div id={`block-${block.id}`} className="reader-block reader-list">
        <ListTag>{items.map((item, index) => (
          <li key={`${block.id}-${index}`}>
            <RichText
              payload={{ segments: itemSegments[index] }}
              fallback={item}
              formulaRepair={{
                paperId,
                blockId: block.id,
                onQueued: onRepairQueued,
                pathPrefix: ["item_segments", index],
              }}
            />
          </li>
        ))}</ListTag>
      </div>
    );
  }

  if (block.block_type === "EQUATION") {
    const latex = payloadText(block.render_payload, "latex") ?? block.source_text;
    return (
      <figure id={`block-${block.id}`} className="reader-block equation-block">
        <figcaption>{displayNumber ? `Equation ${displayNumber}` : "Equation"}</figcaption>
        {latex ? (
          <MathExpression
            display
            environment={environment ?? undefined}
            latex={latex}
            latexRender={block.render_payload.latex_render as LatexRenderState | undefined}
            repairTarget={{
              paperId,
              blockId: block.id,
              renderer: "katex",
              onQueued: onRepairQueued,
            }}
          />
        ) : <p>Equation source unavailable.</p>}
        {block.render_payload.ai_repair ? <span className="repair-badge">AI-repaired render</span> : null}
        {locator ? <small>{locator}</small> : null}
        <RawLatex block={block} />
      </figure>
    );
  }

  if (block.block_type === "THEOREM") {
    const parts = statementParts(block.render_payload);
    const displayName = payloadText(block.render_payload, "display_name")
      ?? environment?.replaceAll("_", " ")
      ?? "Statement";
    const statementTitle = payloadText(block.render_payload, "title");
    const isProof = environment === "proof";
    const isContinuation = block.render_payload.continuation === true;
    const statementLabel = `${displayName}${displayNumber ? ` ${displayNumber}` : ""}${
      isContinuation ? " (continued)" : ""
    }`;
    return (
      <section
        aria-label={displayName}
        id={`block-${block.id}`}
        className={`reader-block theorem-block${isProof ? " proof-block" : ""}`}
      >
        <header className="theorem-header">
          <strong>{statementLabel}</strong>
          {statementTitle ? (
            <span>
              <RichText
                payload={{ segments: block.render_payload.title_segments }}
                fallback={statementTitle}
                formulaRepair={{
                  paperId,
                  blockId: block.id,
                  onQueued: onRepairQueued,
                  pathPrefix: ["title_segments"],
                }}
              />
            </span>
          ) : null}
        </header>
        <div className="theorem-body">
          {parts.length ? parts.map((part, index) => {
            if (part.type === "equation" && part.latex) {
              return (
                <MathExpression
                  display
                  environment={part.environment}
                  key={`${block.id}-equation-${index}`}
                  latex={part.latex}
                  latexRender={part.latex_render}
                  repairTarget={{
                    paperId,
                    blockId: block.id,
                    renderer: "katex",
                    onQueued: onRepairQueued,
                    formulaSelector: {
                      target: "BLOCK",
                      path: ["parts", index],
                      exactLatex: part.latex,
                    },
                  }}
                />
              );
            }
            if (part.type === "list" && part.items?.length) {
              const StatementList = part.environment === "enumerate" ? "ol" : "ul";
              return (
                <StatementList key={`${block.id}-list-${index}`}>
                  {part.items.map((item, itemIndex) => (
                    <li key={`${block.id}-list-${index}-${itemIndex}`}>
                      <RichText
                        payload={{ segments: part.item_segments?.[itemIndex] }}
                        fallback={item}
                        formulaRepair={{
                          paperId,
                          blockId: block.id,
                          onQueued: onRepairQueued,
                          pathPrefix: ["parts", index, "item_segments", itemIndex],
                        }}
                      />
                    </li>
                  ))}
                </StatementList>
              );
            }
            if (part.type === "prose" && part.text) {
              return (
                <p key={`${block.id}-prose-${index}`}>
                  <RichText
                    payload={{ segments: part.segments }}
                    fallback={part.text}
                    formulaRepair={{
                      paperId,
                      blockId: block.id,
                      onQueued: onRepairQueued,
                      pathPrefix: ["parts", index, "segments"],
                    }}
                  />
                </p>
              );
            }
            return null;
          }) : (
            <p>
              <RichText
                payload={block.render_payload}
                fallback={text}
                formulaRepair={{
                  paperId,
                  blockId: block.id,
                  onQueued: onRepairQueued,
                  pathPrefix: ["segments"],
                }}
              />
            </p>
          )}
        </div>
        <RawLatex block={block} />
      </section>
    );
  }

  if (block.block_type === "FIGURE") {
    const isEmbeddedDiagram = block.render_payload.embedded_diagram === true
      || environment === "tikzcd";
    const assets = renderAssets(block.render_payload);
    const missing = Array.isArray(block.render_payload.missing_assets)
      ? block.render_payload.missing_assets.filter((item): item is string => typeof item === "string")
      : [];
    const figureRender = (
      typeof block.render_payload.figure_render === "object"
      && block.render_payload.figure_render !== null
    ) ? block.render_payload.figure_render as FigureRenderState : {};
    const derivedAsset = (
      typeof figureRender.asset === "object"
      && figureRender.asset !== null
      && typeof figureRender.asset.path === "string"
      && typeof figureRender.asset.mime_type === "string"
    ) ? figureRender.asset : null;
    const derivedAssetUrl = derivedAsset
      ? paperBlockRenderAssetUrl(paperId, block.id, derivedAsset.path)
      : null;
    return (
      <figure
        id={`block-${block.id}`}
        className={`reader-block figure-block${isEmbeddedDiagram ? " diagram-block" : ""}`}
      >
        {isEmbeddedDiagram ? (
          displayNumber ? (
            <figcaption className="author-caption diagram-caption">
              Equation {displayNumber}
            </figcaption>
          ) : null
        ) : (
          <figcaption className="author-caption">
            {displayNumber ? <strong className="semantic-caption-number">Figure {displayNumber}. </strong> : null}
            <RichText
              payload={captionPayload}
              fallback={caption ?? "Figure"}
              formulaRepair={{
                paperId,
                blockId: block.id,
                onQueued: onRepairQueued,
                pathPrefix: ["caption_segments"],
              }}
            />
          </figcaption>
        )}
        <div className="figure-assets">
          {derivedAssetUrl ? (
            <MediaPreviewTrigger
              className="latex-figure-render"
              media={{
                src: derivedAssetUrl,
                mimeType: derivedAsset?.mime_type ?? "image/svg+xml",
                alt: isEmbeddedDiagram
                  ? `Rendered commutative diagram${displayNumber ? `, equation ${displayNumber}` : ""}`
                  : caption ? `Rendered figure: ${caption}` : "Rendered LaTeX figure",
                caption,
                label: isEmbeddedDiagram ? "Commutative diagram preview" : "Figure preview",
              }}
              onPreview={onMediaPreview}
            />
          ) : null}
          {assets.map((asset) => {
            const source = paperAssetUrl(paperId, asset.path);
            return (
              <MediaPreviewTrigger
                key={asset.path}
                media={{
                  src: source,
                  mimeType: asset.mime_type,
                  alt: caption ?? asset.path,
                  caption,
                  label: asset.mime_type === "application/pdf" ? "Figure PDF preview" : "Figure preview",
                }}
                onPreview={onMediaPreview}
              />
            );
          })}
        </div>
        {!derivedAssetUrl && figureRender.status === "queued" ? (
          <div className="table-render-placeholder" role="status">
            <span aria-hidden="true" />
            正在使用本地 LaTeX 生成图像…
          </div>
        ) : null}
        {!derivedAssetUrl && (
          figureRender.status === "failed"
          || (!assets.length && figureRender.status !== "queued")
        ) ? (
          <div className="render-diagnostic">
            <span>
              {figureRender.status === "failed"
                ? "Local LaTeX figure rendering failed."
                : "No renderable figure asset was resolved."}
            </span>
            {figureRender.error ? <small>{figureRender.error}</small> : null}
            {figureRender.repair_eligible === false ? (
              <small>
                {figureRender.repair_ineligible_reason
                  ?? "The deterministic figure context must be repaired first."}
              </small>
            ) : (
              <RenderRepairReporter
                errorMessage={figureRender.error ?? `No asset resolved. Missing: ${missing.join(", ") || "unknown"}`}
                target={{
                  paperId,
                  blockId: block.id,
                  renderer: "figure",
                  onQueued: onRepairQueued,
                }}
              />
            )}
          </div>
        ) : null}
        {missing.length ? (
          <p className="render-diagnostic">Missing source asset: {missing.join(", ")}</p>
        ) : null}
        <RawLatex block={block} />
      </figure>
    );
  }

  if (block.block_type === "TABLE") {
    const rows = Array.isArray(block.render_payload.rows)
      ? block.render_payload.rows.filter((row): row is string[] => (
        Array.isArray(row) && row.every((cell) => typeof cell === "string")
      ))
      : [];
    const rowSegments = Array.isArray(block.render_payload.row_segments)
      ? block.render_payload.row_segments
      : [];
    const tableRender = (
      typeof block.render_payload.table_render === "object"
      && block.render_payload.table_render !== null
    ) ? block.render_payload.table_render as TableRenderState : {};
    const renderAsset = (
      typeof tableRender.asset === "object"
      && tableRender.asset !== null
      && typeof tableRender.asset.path === "string"
      && typeof tableRender.asset.mime_type === "string"
    ) ? tableRender.asset : null;
    const renderAssetUrl = renderAsset
      ? paperTableRenderAssetUrl(paperId, block.id, renderAsset.path)
      : null;
    return (
      <figure id={`block-${block.id}`} className="reader-block rendered-table-block">
        <figcaption className="author-caption">
          {displayNumber ? <strong className="semantic-caption-number">Table {displayNumber}. </strong> : null}
          <RichText
            payload={captionPayload}
            fallback={caption ?? "Table"}
            formulaRepair={{
              paperId,
              blockId: block.id,
              onQueued: onRepairQueued,
              pathPrefix: ["caption_segments"],
            }}
          />
        </figcaption>
        {renderAssetUrl ? (
          <>
            <MediaPreviewTrigger
              className="latex-table-render"
              media={{
                src: renderAssetUrl,
                mimeType: renderAsset?.mime_type ?? "image/svg+xml",
                alt: caption ? `Rendered table: ${caption}` : "Rendered LaTeX table",
                caption,
                label: "Table preview",
              }}
              onPreview={onMediaPreview}
            />
            {rows.length ? (
              <details className="structured-table-fallback">
                <summary>View accessible table data</summary>
                <StructuredTable
                  blockId={block.id}
                  onRepairQueued={onRepairQueued}
                  paperId={paperId}
                  rows={rows}
                  rowSegments={rowSegments}
                />
              </details>
            ) : null}
          </>
        ) : tableRender.status === "queued" ? (
          <div className="table-render-placeholder" role="status">
            <span aria-hidden="true" />
            正在使用本地 LaTeX 生成表格图像…
          </div>
        ) : rows.length ? (
          <details className="structured-table-fallback">
            <summary>View accessible fallback table data</summary>
            <StructuredTable
              blockId={block.id}
              onRepairQueued={onRepairQueued}
              paperId={paperId}
              rows={rows}
              rowSegments={rowSegments}
            />
          </details>
        ) : (
          <div className="render-diagnostic">
            <span>This table could not be structured safely.</span>
            <RenderRepairReporter
              errorMessage="Deterministic table structure extraction returned no rows."
              target={{
                paperId,
                blockId: block.id,
                renderer: "table",
                onQueued: onRepairQueued,
              }}
            />
          </div>
        )}
        {tableRender.status === "queued" ? (
          <p className="table-render-status">
            The table is queued for the isolated local LaTeX renderer.
          </p>
        ) : null}
        {tableRender.status === "failed" ? (
          <div className="render-diagnostic">
            <span>Local LaTeX table rendering failed. Structured HTML is shown as a fallback.</span>
            {tableRender.error ? <small>{tableRender.error}</small> : null}
            {tableRender.repair_eligible === false ? (
              <small>
                {tableRender.repair_ineligible_reason
                  ?? "This table is too large for a lossless AI repair."}
              </small>
            ) : (
              <RenderRepairReporter
                errorMessage={tableRender.error ?? "Local LaTeX table compilation failed."}
                target={{
                  paperId,
                  blockId: block.id,
                  renderer: "table",
                  onQueued: onRepairQueued,
                }}
              />
            )}
          </div>
        ) : null}
        {tableRender.status === "repaired" ? (
          <p className="table-render-status table-render-repaired">
            <span className="repair-badge">AI-repaired table</span>
            Validated replacement LaTeX is queued for a new local render.
          </p>
        ) : null}
        <RawLatex block={block} />
      </figure>
    );
  }

  if (block.block_type === "ALGORITHM") {
    const nativeRender = (
      typeof block.render_payload.latex_block_render === "object"
      && block.render_payload.latex_block_render !== null
    ) ? block.render_payload.latex_block_render as LatexBlockRenderState : {};
    const nativeAsset = (
      typeof nativeRender.asset === "object"
      && nativeRender.asset !== null
      && typeof nativeRender.asset.path === "string"
      && typeof nativeRender.asset.mime_type === "string"
    ) ? nativeRender.asset : null;
    const nativeAssetUrl = nativeAsset
      ? paperBlockRenderAssetUrl(paperId, block.id, nativeAsset.path)
      : null;
    return (
      <section id={`block-${block.id}`} className="reader-block algorithm-block">
        {caption ? (
          <header className="author-caption">
            {displayNumber ? <strong className="semantic-caption-number">Algorithm {displayNumber}. </strong> : null}
            <RichText
              payload={captionPayload}
              fallback={caption}
              formulaRepair={{
                paperId,
                blockId: block.id,
                onQueued: onRepairQueued,
                pathPrefix: ["caption_segments"],
              }}
            />
          </header>
        ) : (
          <div className="block-label"><strong>{displayNumber ? `Algorithm ${displayNumber}` : "Algorithm"}</strong></div>
        )}
        {nativeAssetUrl ? (
          <>
            <MediaPreviewTrigger
              className="latex-native-block-render latex-algorithm-render"
              media={{
                src: nativeAssetUrl,
                mimeType: nativeAsset?.mime_type ?? "image/svg+xml",
                alt: caption ? `Rendered algorithm: ${caption}` : "Rendered LaTeX algorithm",
                caption,
                label: "Algorithm preview",
              }}
              onPreview={onMediaPreview}
            />
            {text ? (
              <details className="native-latex-accessible-fallback">
                <summary>查看可检索算法文本</summary>
                <div className="algorithm-body">
                  <RichText payload={block.render_payload} fallback={text} />
                </div>
              </details>
            ) : null}
          </>
        ) : nativeRender.status === "queued" ? (
          <div className="table-render-placeholder" role="status">
            <span aria-hidden="true" />
            正在使用本地 LaTeX 还原算法排版…
          </div>
        ) : text ? (
          <div className="algorithm-body">
            <RichText
              payload={block.render_payload}
              fallback={text}
              formulaRepair={{
                paperId,
                blockId: block.id,
                onQueued: onRepairQueued,
                pathPrefix: ["segments"],
              }}
            />
          </div>
        ) : null}
        {nativeRender.status === "failed" ? (
          <div className="render-diagnostic">
            <span>算法的原版 LaTeX 渲染失败，已保留可读文本和原始源码。</span>
            {nativeRender.error ? <small>{nativeRender.error}</small> : null}
          </div>
        ) : null}
        <RawLatex block={block} />
      </section>
    );
  }

  if (block.block_type === "CODE") {
    const code = payloadText(block.render_payload, "code") ?? block.source_text ?? "";
    const sourcePath = payloadText(block.render_payload, "source_path");
    const externalSourceStatus = payloadText(block.render_payload, "external_source_status");
    const externalSourceError = payloadText(block.render_payload, "error");
    return (
      <figure id={`block-${block.id}`} className="reader-block code-block">
        <figcaption>{sourcePath ?? environment ?? "Code"}</figcaption>
        {externalSourceStatus && externalSourceStatus !== "RESOLVED" ? (
          <div className="render-diagnostic" role="status">
            <span>外部文本文件未能从论文源码中加载。</span>
            {externalSourceError ? <small>{externalSourceError}</small> : null}
          </div>
        ) : (
          <CopyableCodeBlock label="Copy code block" renderCodeElement value={code} />
        )}
        <RawLatex block={block} />
      </figure>
    );
  }

  return (
    <section id={`block-${block.id}`} className="reader-block fallback-block">
      <div className="block-label">
        <strong>{block.block_type.replaceAll("_", " ")}</strong>
        {environment ? <span>{environment}</span> : null}
      </div>
      {caption ? (
        <p>
          <RichText
            payload={captionPayload}
            fallback={caption}
            formulaRepair={{
              paperId,
              blockId: block.id,
              onQueued: onRepairQueued,
              pathPrefix: ["caption_segments"],
            }}
          />
        </p>
      ) : text ? <p>{text}</p> : null}
      <RawLatex block={block} />
      {locator ? <small>{locator}</small> : null}
    </section>
  );
}

type TranslatedUnit = {
  key: string;
  text: string;
  segments?: RenderSegment[];
};

function translatedUnits(translation: BlockTranslation): TranslatedUnit[] {
  const units = translation.translated_payload.units;
  if (!Array.isArray(units)) return [];
  return units.filter((unit): unit is TranslatedUnit => (
    typeof unit === "object"
    && unit !== null
    && "key" in unit
    && typeof unit.key === "string"
    && "text" in unit
    && typeof unit.text === "string"
  ));
}

function TranslationBlockLabel({ blockId, failed = false }: { blockId: string; failed?: boolean }) {
  return (
    <span className="translation-block-label" tabIndex={0}>
      {failed ? "中文翻译失败" : "中文翻译"}
      <span className="translation-block-tooltip" role="tooltip">
        <small>Content block ID</small>
        <code>{blockId}</code>
      </span>
    </span>
  );
}

function supportsTranslation(block: ContentBlock): boolean {
  if (["ABSTRACT", "SECTION", "PARAGRAPH", "LATEX_BLOCK"].includes(block.block_type)) {
    return Boolean(block.source_text || block.render_payload.text);
  }
  if (block.block_type === "LIST") {
    return Array.isArray(block.render_payload.items) && block.render_payload.items.length > 0;
  }
  if (block.block_type === "THEOREM") {
    return Boolean(
      block.source_text
      || block.render_payload.title
      || (Array.isArray(block.render_payload.parts) && block.render_payload.parts.length > 0),
    );
  }
  return ["FIGURE", "TABLE", "ALGORITHM"].includes(block.block_type)
    && typeof block.render_payload.caption === "string"
    && block.render_payload.caption.trim().length > 0;
}

function isProviderApiFailure(errorType: string | null, errorMessage: string | null): boolean {
  return [
    "AUTHENTICATION",
    "CONTENT_REJECTED",
    "PROVIDER_5XX",
    "PROVIDER_HTTP",
    "RATE_LIMIT",
    "REQUEST_REJECTED",
  ].includes(errorType ?? "") || /\b(?:provider|api)\b/i.test(errorMessage ?? "");
}

function ProviderErrorDetails({
  provider,
  model,
  errorType,
  errorCode,
  errorMessage,
  requestId,
}: {
  provider?: string | null;
  model?: string | null;
  errorType: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  requestId?: string | null;
}) {
  const contentRejected = errorType === "CONTENT_REJECTED" || errorCode === "1301";
  const heading = contentRejected ? "内容安全策略拒绝了这次请求" : "LLM API 拒绝了这次请求";
  return (
    <div className={`provider-error-details${contentRejected ? " provider-error-safety" : ""}`}>
      <strong>{heading}</strong>
      <p>
        {contentRejected
          ? "这可能是正常学术文本触发了模型供应商的安全规则；英文原文和用户数据没有丢失。"
          : "下面保留了供应商返回的诊断信息，便于判断是参数、权限、额度还是服务策略问题。"}
      </p>
      <dl>
        {provider ? <><dt>Provider</dt><dd>{provider}</dd></> : null}
        {model ? <><dt>Model</dt><dd>{model}</dd></> : null}
        {errorType ? <><dt>Error type</dt><dd><code>{errorType}</code></dd></> : null}
        {errorCode ? <><dt>Provider code</dt><dd><code>{errorCode}</code></dd></> : null}
        {requestId ? <><dt>Request ID</dt><dd><code>{requestId}</code></dd></> : null}
      </dl>
      {errorMessage ? (
        <details open={contentRejected}>
          <summary>完整 API 错误</summary>
          <pre>{errorMessage}</pre>
        </details>
      ) : null}
    </div>
  );
}

function TranslationPanel({
  block,
  paperId,
  configured,
  defaultPrompt,
  onRepairQueued,
  onRetried,
}: {
  block: ContentBlock;
  paperId: string;
  configured: boolean;
  defaultPrompt: string;
  onRepairQueued: () => void;
  onRetried: () => Promise<void>;
}) {
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [showRetranslate, setShowRetranslate] = useState(false);
  const [promptDraft, setPromptDraft] = useState(defaultPrompt);
  const translation = block.translation;
  const units = translation ? translatedUnits(translation) : [];

  function togglePromptEditor() {
    setRetryError(null);
    setPromptDraft(defaultPrompt);
    setShowRetranslate((current) => !current);
  }

  async function submitRetranslation() {
    setRetrying(true);
    setRetryError(null);
    try {
      await retranslateBlock(paperId, block.id, promptDraft);
      setShowRetranslate(false);
      await onRetried();
    } catch (reason) {
      setRetryError(reason instanceof Error ? reason.message : "无法重新翻译此内容。");
    } finally {
      setRetrying(false);
    }
  }

  if (!configured && translation === null) return null;
  const promptEditor = showRetranslate ? (
    <div className="translation-retranslate-editor">
      <label htmlFor={`translation-prompt-${block.id}`}>自定义 System Prompt</label>
      <textarea
        id={`translation-prompt-${block.id}`}
        maxLength={8000}
        minLength={80}
        onChange={(event) => setPromptDraft(event.target.value)}
        rows={8}
        value={promptDraft}
      />
      <div>
        <small>{promptDraft.length}/8000</small>
        <button
          disabled={retrying || promptDraft.trim().length < 80}
          onClick={() => void submitRetranslation()}
          type="button"
        >
          {retrying ? "正在提交…" : "提交重新翻译"}
        </button>
      </div>
    </div>
  ) : null;
  if (translation === null) {
    return (
      <div className="translation-panel translation-manual" aria-live="polite">
        <button
          className="translation-manual-link"
          disabled={retrying}
          onClick={() => void submitRetranslation()}
          type="button"
        >
          {retrying ? "翻译中…" : "翻译"}
        </button>
        {retryError ? <small className="translation-action-error" role="alert">{retryError}</small> : null}
      </div>
    );
  }
  if (["PENDING", "RUNNING"].includes(translation.status)) {
    return (
      <div className="translation-panel translation-pending" aria-live="polite">
        <p>翻译中…</p>
      </div>
    );
  }
  if (translation.status === "FAILED") {
    return (
      <div className="translation-panel translation-failed" role="status">
        <TranslationBlockLabel blockId={block.id} failed />
        {isProviderApiFailure(translation.error_type, translation.error_message) ? (
          <ProviderErrorDetails
            errorCode={translation.error_code}
            errorMessage={translation.error_message}
            errorType={translation.error_type}
            model={translation.model}
            provider={translation.provider}
            requestId={translation.provider_request_id}
          />
        ) : <p>{translation.error_message ?? "此内容块暂时无法翻译，英文原文不受影响。"}</p>}
        <button
          aria-expanded={showRetranslate}
          disabled={retrying}
          onClick={togglePromptEditor}
          type="button"
        >
          {showRetranslate ? "收起 Prompt" : "重试此块"}
        </button>
        {promptEditor}
        {retryError ? <small role="alert">{retryError}</small> : null}
      </div>
    );
  }
  return (
    <div className="translation-panel translation-succeeded" lang="zh-CN">
      <button
        aria-expanded={showRetranslate}
        aria-label="使用自定义 System Prompt 重新翻译"
        className="translation-retranslate-trigger"
        onClick={togglePromptEditor}
        type="button"
      >
        ↻ 重新翻译
      </button>
      <div className="translation-label">
        <TranslationBlockLabel blockId={block.id} />
        {translation.cache_hit ? <small>缓存命中</small> : null}
      </div>
      <div
        className="translation-copy"
        data-annotation-block-id={block.id}
        data-annotation-target="TRANSLATION"
        data-annotation-translation-id={translation.id}
      >
        {units.length && block.block_type === "LIST" ? (
          <ul>
            {units.map((unit, index) => (
              <li key={unit.key}>
                {index > 0 ? "\n" : null}
                <RichText
                  fallback={unit.text}
                  formulaRepair={{
                    paperId,
                    blockId: block.id,
                    onQueued: onRepairQueued,
                    pathPrefix: ["units", index, "segments"],
                    target: "TRANSLATION",
                    translationId: translation.id,
                  }}
                  payload={{ segments: unit.segments }}
                  preserveSpacing
                />
              </li>
            ))}
          </ul>
        ) : units.length ? units.map((unit, index) => (
          <p key={unit.key}>
            {index > 0 ? "\n" : null}
            <RichText
              fallback={unit.text}
              formulaRepair={{
                paperId,
                blockId: block.id,
                onQueued: onRepairQueued,
                pathPrefix: ["units", index, "segments"],
                target: "TRANSLATION",
                translationId: translation.id,
              }}
              payload={{ segments: unit.segments }}
              preserveSpacing
            />
          </p>
        )) : <p>{translation.translated_text}</p>}
      </div>
      {promptEditor}
      {retryError ? <small className="translation-action-error" role="alert">{retryError}</small> : null}
    </div>
  );
}

function BlockBookmarkButton({
  bookmarked,
  busy,
  onToggle,
}: {
  bookmarked: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      aria-label={bookmarked ? "取消收藏这个区块" : "收藏这个区块"}
      aria-pressed={bookmarked}
      className={`block-bookmark-button${bookmarked ? " block-bookmark-button-active" : ""}`}
      data-annotation-auxiliary="true"
      disabled={busy}
      onClick={onToggle}
      title={bookmarked ? "取消收藏" : "收藏区块"}
      type="button"
    >
      <span aria-hidden="true">{bookmarked ? "★" : "☆"}</span>
    </button>
  );
}

function AnnotationHoverPreview({
  annotation,
  hasQuestion,
  position,
}: {
  annotation: Annotation;
  hasQuestion: boolean;
  position: AnnotationPopoverPosition;
}) {
  return (
    <aside
      aria-label="Annotation preview"
      className="annotation-hover-preview"
      style={{ left: position.left, top: position.top }}
    >
      <strong>{hasQuestion ? "Ask AI" : annotation.comments.length ? "Comment" : "Annotation"}</strong>
      {annotation.selector?.exact_quote ? <q>{annotation.selector.exact_quote}</q> : null}
      {annotation.comments.at(-1) ? <p>{annotation.comments.at(-1)?.body}</p> : null}
      <small>点击标注可打开完整详情</small>
    </aside>
  );
}

function BibliographySection({
  bibliography,
  bookmarkBusy = false,
  bookmarked = false,
  markerBlockId,
  onBookmarkToggle,
}: {
  bibliography: PaperBibliography;
  bookmarkBusy?: boolean;
  bookmarked?: boolean;
  markerBlockId?: string;
  onBookmarkToggle?: () => void;
}) {
  const entries = bibliography.entries.filter(
    (entry): entry is BibliographyEntry & { ordinal: number } => entry.ordinal !== null,
  );
  if (bibliography.status === "EMPTY" && !entries.length) return null;
  return (
    <section
      aria-labelledby="paper-bibliography-heading"
      className="reader-bibliography"
      data-reading-block-id={markerBlockId}
      id={markerBlockId ? `block-${markerBlockId}` : "paper-bibliography"}
    >
      {markerBlockId && onBookmarkToggle ? (
        <BlockBookmarkButton
          bookmarked={bookmarked}
          busy={bookmarkBusy}
          onToggle={onBookmarkToggle}
        />
      ) : null}
      <header
        className="reader-bibliography-heading"
        id={markerBlockId ? "paper-bibliography" : undefined}
      >
        <div>
          <p className="eyebrow">Bibliography</p>
          <h2 id="paper-bibliography-heading">References</h2>
        </div>
        {bibliography.resolved_style ? (
          <small>
            {bibliography.resolved_style}
            {bibliography.fallback_used ? " · fallback style" : ""}
          </small>
        ) : null}
      </header>
      {entries.length ? (
        <ol className="bibliography-list">
          {entries.map((entry) => (
            <li
              className={entry.display_label ? "bibliography-entry" : "bibliography-entry bibliography-entry-unlabelled"}
              data-citation-key={entry.citation_key}
              id={`bibliography-entry-${entry.id}`}
              key={entry.id}
            >
              {entry.display_label ? (
                <span aria-hidden="true" className="bibliography-label">
                  [{entry.display_label}]
                </span>
              ) : null}
              <div className="bibliography-entry-content">
                <RichText
                  fallback={entry.formatted_text ?? entry.title ?? entry.citation_key}
                  payload={entry.render_payload}
                />
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="bibliography-error" role="status">
          参考文献数据已经被记录，但本地 LaTeX bibliography 编译没有生成可展示的条目。
        </p>
      )}
      {bibliography.warnings.length ? (
        <details className="bibliography-diagnostics">
          <summary>Bibliography diagnostics</summary>
          <ul>
            {bibliography.warnings.map((warning) => <li key={warning}>{warning}</li>)}
          </ul>
        </details>
      ) : null}
    </section>
  );
}


function BilingualBlock({
  block,
  paperId,
  mode,
  translationConfigured,
  defaultPrompt,
  onTranslationRetried,
  onRepairQueued,
  onMediaPreview,
  bookmarked,
  bookmarkBusy,
  onBookmarkToggle,
}: {
  block: ContentBlock;
  paperId: string;
  mode: "bilingual" | "original";
  translationConfigured: boolean;
  defaultPrompt: string;
  onTranslationRetried: () => Promise<void>;
  onRepairQueued: () => void;
  onMediaPreview: (media: PreviewMedia) => void;
  bookmarked: boolean;
  bookmarkBusy: boolean;
  onBookmarkToggle: () => void;
}) {
  const original = (
    <div
      className="source-language-block"
      data-annotation-block-id={block.id}
      data-annotation-target="SOURCE"
    >
      <BlockRenderer
        block={block}
        onMediaPreview={onMediaPreview}
        onRepairQueued={onRepairQueued}
        paperId={paperId}
      />
    </div>
  );
  if (mode === "original" || !supportsTranslation(block)) {
    return (
      <div className="original-reading-block" data-reading-block-id={block.id}>
        <BlockBookmarkButton
          bookmarked={bookmarked}
          busy={bookmarkBusy}
          onToggle={onBookmarkToggle}
        />
        {original}
      </div>
    );
  }
  return (
    <div
      className={`bilingual-block bilingual-${block.block_type.toLowerCase()}`}
      data-reading-block-id={block.id}
    >
      <BlockBookmarkButton
        bookmarked={bookmarked}
        busy={bookmarkBusy}
        onToggle={onBookmarkToggle}
      />
      {original}
      <TranslationPanel
        block={block}
        configured={translationConfigured}
        defaultPrompt={defaultPrompt}
        onRepairQueued={onRepairQueued}
        onRetried={onTranslationRetried}
        paperId={paperId}
      />
    </div>
  );
}

export default function PaperDetailClient({ paperId }: { paperId: string }) {
  const [paper, setPaper] = useState<Paper | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [outline, setOutline] = useState<PaperOutlineItem[]>([]);
  const [blockBookmarks, setBlockBookmarks] = useState<BlockBookmark[]>([]);
  const [bookmarkBusyIds, setBookmarkBusyIds] = useState<Set<string>>(new Set());
  const [bookmarkError, setBookmarkError] = useState<string | null>(null);
  const [blocks, setBlocks] = useState<ContentBlock[]>([]);
  const [bibliography, setBibliography] = useState<PaperBibliography | null>(null);
  const [readingLoading, setReadingLoading] = useState(false);
  const [readingError, setReadingError] = useState<string | null>(null);
  const [translationProgress, setTranslationProgress] = useState<TranslationProgress | null>(null);
  const [translationError, setTranslationError] = useState<string | null>(null);
  const [abstractTranslation, setAbstractTranslation] = useState<BlockTranslation | null>(null);
  const [globalTranslationPrompt, setGlobalTranslationPrompt] = useState("");
  const [showPaperRetranslate, setShowPaperRetranslate] = useState(false);
  const [paperRetranslateDraft, setPaperRetranslateDraft] = useState("");
  const [paperRetranslating, setPaperRetranslating] = useState(false);
  const [paperRetranslateMessage, setPaperRetranslateMessage] = useState<string | null>(null);
  const [translatingRemaining, setTranslatingRemaining] = useState(false);
  const [translateRemainingMessage, setTranslateRemainingMessage] = useState<string | null>(null);
  const [renderRefreshRevision, setRenderRefreshRevision] = useState(0);
  const [showBackToTop, setShowBackToTop] = useState(false);
  const [paperBlockJumpOrigin, setPaperBlockJumpOrigin] = useState<PaperBlockJumpOrigin | null>(null);
  const [returningToJumpOrigin, setReturningToJumpOrigin] = useState(false);
  const [readingMode, setReadingMode] = useState<"bilingual" | "original">("bilingual");
  const [showAllTasks, setShowAllTasks] = useState(false);
  const [previewMedia, setPreviewMedia] = useState<PreviewMedia | null>(null);
  const [activeOutlineId, setActiveOutlineId] = useState<string | null>(null);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [annotationsLoading, setAnnotationsLoading] = useState(false);
  const [annotationsError, setAnnotationsError] = useState<string | null>(null);
  const [activeAnnotationId, setActiveAnnotationId] = useState<string | null>(null);
  const [annotationPopoverPosition, setAnnotationPopoverPosition] = useState<AnnotationPopoverPosition | null>(null);
  const [hoveredAnnotationId, setHoveredAnnotationId] = useState<string | null>(null);
  const [annotationHoverPosition, setAnnotationHoverPosition] = useState<AnnotationPopoverPosition | null>(null);
  const [leftSidebarWidth, setLeftSidebarWidth] = useState(DEFAULT_READER_LEFT_WIDTH);
  const [rightSidebarWidth, setRightSidebarWidth] = useState(DEFAULT_READER_RIGHT_WIDTH);
  const [leftSidebarCollapsed, setLeftSidebarCollapsed] = useState(false);
  const [rightSidebarCollapsed, setRightSidebarCollapsed] = useState(false);
  const [mobileOutlineOpen, setMobileOutlineOpen] = useState(false);
  const [sidebarPreferencesLoaded, setSidebarPreferencesLoaded] = useState(false);
  const annotationPanelRef = useRef<HTMLElement>(null);
  const abstractAnnotationRootRef = useRef<HTMLElement>(null);
  const documentViewportRef = useRef<HTMLElement>(null);
  const jumpReturnAnimationFrameRef = useRef<number | null>(null);
  const jumpReturnAttemptRef = useRef(0);
  const jumpReturnInProgressRef = useRef(false);
  const readingPositionRestoredRef = useRef(false);
  const readingPositionSyncFrameRef = useRef<number | null>(null);
  const readingPositionSyncSuspendedUntilRef = useRef(0);
  const readingModeRef = useRef<"bilingual" | "original">("bilingual");
  const pendingReadingModeAnchorRef = useRef<PaperViewportAnchor | null>(null);

  useEffect(() => {
    try {
      const storedLeftWidth = Number.parseInt(
        window.localStorage.getItem(UI_STORAGE_KEYS.readerLeftSidebarWidth) ?? "",
        10,
      );
      const storedRightWidth = Number.parseInt(
        window.localStorage.getItem(UI_STORAGE_KEYS.readerRightSidebarWidth) ?? "",
        10,
      );
      if (Number.isFinite(storedLeftWidth)) {
        setLeftSidebarWidth(clampReaderSidebarWidth("left", storedLeftWidth));
      }
      if (Number.isFinite(storedRightWidth)) {
        setRightSidebarWidth(clampReaderSidebarWidth("right", storedRightWidth));
      }
      setLeftSidebarCollapsed(
        window.localStorage.getItem(UI_STORAGE_KEYS.readerLeftSidebarCollapsed) === "true",
      );
      setRightSidebarCollapsed(
        window.localStorage.getItem(UI_STORAGE_KEYS.readerRightSidebarCollapsed) === "true",
      );
    } catch {
      // Reader sidebar preferences are optional.
    } finally {
      setSidebarPreferencesLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (!sidebarPreferencesLoaded) return;
    try {
      window.localStorage.setItem(
        UI_STORAGE_KEYS.readerLeftSidebarWidth,
        String(leftSidebarWidth),
      );
      window.localStorage.setItem(
        UI_STORAGE_KEYS.readerRightSidebarWidth,
        String(rightSidebarWidth),
      );
      window.localStorage.setItem(
        UI_STORAGE_KEYS.readerLeftSidebarCollapsed,
        String(leftSidebarCollapsed),
      );
      window.localStorage.setItem(
        UI_STORAGE_KEYS.readerRightSidebarCollapsed,
        String(rightSidebarCollapsed),
      );
    } catch {
      // The responsive reader still works when local storage is unavailable.
    }
  }, [
    leftSidebarCollapsed,
    leftSidebarWidth,
    rightSidebarCollapsed,
    rightSidebarWidth,
    sidebarPreferencesLoaded,
  ]);

  const startSidebarResize = useCallback((
    side: "left" | "right",
    event: ReactPointerEvent<HTMLButtonElement>,
  ) => {
    event.preventDefault();
    const pointerId = event.pointerId;
    const startX = event.clientX;
    const startWidth = side === "left" ? leftSidebarWidth : rightSidebarWidth;
    let latestRawWidth = startWidth;
    event.currentTarget.setPointerCapture(pointerId);
    const onPointerMove = (moveEvent: PointerEvent) => {
      latestRawWidth = side === "left"
        ? startWidth + moveEvent.clientX - startX
        : startWidth + startX - moveEvent.clientX;
      if (side === "left") {
        if (latestRawWidth >= COLLAPSE_READER_LEFT_THRESHOLD) {
          setLeftSidebarWidth(clampReaderSidebarWidth("left", latestRawWidth));
        }
      } else if (latestRawWidth >= COLLAPSE_READER_RIGHT_THRESHOLD) {
        setRightSidebarWidth(clampReaderSidebarWidth("right", latestRawWidth));
      }
    };
    const finish = () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      if (side === "left" && latestRawWidth < COLLAPSE_READER_LEFT_THRESHOLD) {
        setLeftSidebarCollapsed(true);
      }
      if (side === "right" && latestRawWidth < COLLAPSE_READER_RIGHT_THRESHOLD) {
        setRightSidebarCollapsed(true);
      }
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  }, [leftSidebarWidth, rightSidebarWidth]);

  const abstractAnnotationBlock = useMemo<ContentBlock | null>(() => {
    if (!paper?.abstract || !paper.abstract_block_id) return null;
    return {
      id: paper.abstract_block_id,
      parent_id: null,
      block_type: "ABSTRACT",
      ordinal_path: "!abstract",
      heading_level: null,
      source_text: paper.abstract,
      source_payload: {},
      render_payload: paper.abstract_render_payload ?? { text: paper.abstract },
      source_locator: { metadata_field: "abstract" },
      translation: abstractTranslation,
    };
  }, [abstractTranslation, paper?.abstract, paper?.abstract_block_id, paper?.abstract_render_payload]);
  const annotationBlocks = useMemo(
    () => abstractAnnotationBlock ? [abstractAnnotationBlock, ...blocks] : blocks,
    [abstractAnnotationBlock, blocks],
  );

  useEffect(() => {
    const applyReadingMode = (nextMode: "bilingual" | "original") => {
      if (readingModeRef.current === nextMode) return;
      const root = documentViewportRef.current;
      pendingReadingModeAnchorRef.current = root
        ? capturePaperViewportAnchor(paperReadingBlockElements(root))
        : null;
      if (pendingReadingModeAnchorRef.current !== null) {
        readingPositionSyncSuspendedUntilRef.current = performance.now() + 1_000;
      }
      readingModeRef.current = nextMode;
      setReadingMode(nextMode);
    };
    const syncStoredReadingMode = () => {
      applyReadingMode(
        window.localStorage.getItem(UI_STORAGE_KEYS.readingMode) === "original"
          ? "original"
          : "bilingual",
      );
    };
    const syncReadingModeEvent = (event: Event) => {
      const enabled = (event as CustomEvent<{ enabled?: boolean }>).detail?.enabled;
      if (typeof enabled === "boolean") {
        applyReadingMode(enabled ? "bilingual" : "original");
      } else {
        syncStoredReadingMode();
      }
    };
    syncStoredReadingMode();
    window.addEventListener("storage", syncStoredReadingMode);
    window.addEventListener(BILINGUAL_MODE_CHANGE_EVENT, syncReadingModeEvent);
    return () => {
      window.removeEventListener("storage", syncStoredReadingMode);
      window.removeEventListener(BILINGUAL_MODE_CHANGE_EVENT, syncReadingModeEvent);
    };
  }, []);

  useLayoutEffect(() => {
    const anchor = pendingReadingModeAnchorRef.current;
    pendingReadingModeAnchorRef.current = null;
    const root = documentViewportRef.current;
    if (anchor === null || root === null) return;
    let firstFrame: number | null = null;
    let secondFrame: number | null = null;
    const restore = () => {
      readingPositionSyncSuspendedUntilRef.current = performance.now() + 500;
      const restoredBlockId = restorePaperViewportAnchor(
        paperReadingBlockElements(root),
        anchor,
      );
      if (restoredBlockId !== null) updatePaperReadingPosition(restoredBlockId);
    };

    // Restore once before paint, then verify across two frames after layout has settled.
    restore();
    firstFrame = window.requestAnimationFrame(() => {
      restore();
      secondFrame = window.requestAnimationFrame(restore);
    });
    return () => {
      if (firstFrame !== null) window.cancelAnimationFrame(firstFrame);
      if (secondFrame !== null) window.cancelAnimationFrame(secondFrame);
    };
  }, [readingMode]);

  const stopJumpReturnAttempt = useCallback(() => {
    jumpReturnAttemptRef.current += 1;
    jumpReturnInProgressRef.current = false;
    if (jumpReturnAnimationFrameRef.current !== null) {
      window.cancelAnimationFrame(jumpReturnAnimationFrameRef.current);
      jumpReturnAnimationFrameRef.current = null;
    }
    setReturningToJumpOrigin(false);
  }, []);

  useEffect(() => {
    setPaperBlockJumpOrigin(null);
    stopJumpReturnAttempt();
    const rememberOrigin = (event: Event) => {
      const detail = (event as CustomEvent<PaperBlockJumpOrigin>).detail;
      if (!detail || !Number.isFinite(detail.top)) return;
      readingPositionSyncSuspendedUntilRef.current = performance.now() + 1_500;
      stopJumpReturnAttempt();
      setPaperBlockJumpOrigin(detail);
    };
    window.addEventListener(PAPER_BLOCK_JUMP_EVENT, rememberOrigin);
    return () => {
      window.removeEventListener(PAPER_BLOCK_JUMP_EVENT, rememberOrigin);
      stopJumpReturnAttempt();
    };
  }, [paperId, stopJumpReturnAttempt]);

  useEffect(() => {
    const interruptReturn = () => {
      if (!jumpReturnInProgressRef.current) return;
      const interruptedTop = window.scrollY;
      stopJumpReturnAttempt();
      window.scrollTo({ behavior: "auto", top: interruptedTop });
    };
    const interruptReturnWithKey = (event: KeyboardEvent) => {
      if (["Escape", "ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
        interruptReturn();
      }
    };
    window.addEventListener("wheel", interruptReturn, { capture: true, passive: true });
    window.addEventListener("touchstart", interruptReturn, { capture: true, passive: true });
    window.addEventListener("pointerdown", interruptReturn, { capture: true, passive: true });
    window.addEventListener("keydown", interruptReturnWithKey, true);
    return () => {
      window.removeEventListener("wheel", interruptReturn, true);
      window.removeEventListener("touchstart", interruptReturn, true);
      window.removeEventListener("pointerdown", interruptReturn, true);
      window.removeEventListener("keydown", interruptReturnWithKey, true);
    };
  }, [stopJumpReturnAttempt]);

  const returnToPaperBlockJumpOrigin = useCallback(() => {
    if (!paperBlockJumpOrigin) return;
    stopJumpReturnAttempt();
    const attempt = jumpReturnAttemptRef.current + 1;
    jumpReturnAttemptRef.current = attempt;
    jumpReturnInProgressRef.current = true;
    setReturningToJumpOrigin(true);
    const maximumTop = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    const targetTop = Math.min(maximumTop, Math.max(0, paperBlockJumpOrigin.top));
    const startedAt = performance.now();
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const verifyArrival = () => {
      if (!jumpReturnInProgressRef.current || jumpReturnAttemptRef.current !== attempt) return;
      if (Math.abs(window.scrollY - targetTop) <= 2) {
        jumpReturnInProgressRef.current = false;
        jumpReturnAnimationFrameRef.current = null;
        setReturningToJumpOrigin(false);
        setPaperBlockJumpOrigin(null);
        const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        if (currentUrl !== paperBlockJumpOrigin.url) {
          window.history.replaceState(window.history.state, "", paperBlockJumpOrigin.url);
        }
        return;
      }
      if (performance.now() - startedAt >= 4_000) {
        stopJumpReturnAttempt();
        return;
      }
      jumpReturnAnimationFrameRef.current = window.requestAnimationFrame(verifyArrival);
    };

    window.scrollTo({ behavior: reducedMotion ? "auto" : "smooth", top: targetTop });
    jumpReturnAnimationFrameRef.current = window.requestAnimationFrame(verifyArrival);
  }, [paperBlockJumpOrigin, stopJumpReturnAttempt]);

  const rememberBlockAnchorOrigin = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest<HTMLAnchorElement>(
      'a[href^="#block-"], a[href^="#bibliography-entry-"]',
    )) rememberPaperBlockJumpOrigin();
  }, []);

  useEffect(() => {
    const updateBackToTopVisibility = () => setShowBackToTop(window.scrollY > 560);
    updateBackToTopVisibility();
    window.addEventListener("scroll", updateBackToTopVisibility, { passive: true });
    return () => window.removeEventListener("scroll", updateBackToTopVisibility);
  }, []);

  const scrollToPageTop = useCallback(() => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    rememberPaperBlockJumpOrigin();
    readingPositionSyncSuspendedUntilRef.current = performance.now() + 1_500;
    clearPaperReadingPosition();
    window.scrollTo({ behavior: reducedMotion ? "auto" : "smooth", top: 0 });
  }, []);

  const load = useCallback(async () => {
    try {
      setPaper(await getPaper(paperId));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load this paper.");
    }
  }, [paperId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void markPaperOpened(paperId).catch(() => {
      // Reading remains available when optional recency tracking cannot be persisted.
    });
  }, [paperId]);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [load]);

  const processing = paper?.tasks.some((task) => ["QUEUED", "RUNNING"].includes(task.status));
  useEffect(() => {
    if (!processing) return;
    const timer = window.setInterval(() => void load(), 1500);
    return () => window.clearInterval(timer);
  }, [load, processing]);

  const readingReady = paper !== null && [
    "READING_READY",
    "SEMANTIC_READY",
    "KNOWLEDGE_READY",
  ].includes(paper.processing_stage);
  const isPdfOnly = paper !== null
    && paper.sources.some((source) => source.source_type === "ARXIV_PDF")
    && !paper.sources.some((source) => source.source_type === "EXPANDED_SOURCE");
  const readingRevision = useMemo(
    () => paper?.tasks
      .filter((task) => [
        "REPAIR_RENDER",
        "RENDER_TABLE",
        "RENDER_FIGURE",
        "RENDER_LATEX_BLOCK",
        "RENDER_LATEX_FORMULA",
      ].includes(task.task_type))
      .map((task) => `${task.id}:${task.status}:${task.finished_at ?? ""}`)
      .join("|") ?? "",
    [paper],
  );

  const loadTranslationState = useCallback(async () => {
    try {
      const [progress, translations, settings] = await Promise.all([
        getPaperTranslationProgress(paperId),
        getPaperTranslations(paperId),
        getSettingsStatus(),
      ]);
      const byBlock = new Map(
        translations
          .filter((translation) => translation.target_type !== "ABSTRACT")
          .map((translation) => [translation.block_id, translation]),
      );
      setTranslationProgress(progress);
      setAbstractTranslation(
        translations.find((translation) => translation.target_type === "ABSTRACT") ?? null,
      );
      setGlobalTranslationPrompt(settings.translation_system_prompt);
      setBlocks((current) => current.map((block) => ({
        ...block,
        translation: byBlock.get(block.id) ?? block.translation,
      })));
      setTranslationError(null);
    } catch (reason) {
      setTranslationError(
        reason instanceof Error ? reason.message : "Unable to refresh translation progress.",
      );
    }
  }, [paperId]);

  const openPaperRetranslation = useCallback(() => {
    setPaperRetranslateDraft(globalTranslationPrompt);
    setPaperRetranslateMessage(null);
    setShowPaperRetranslate(true);
  }, [globalTranslationPrompt]);

  const submitPaperRetranslation = useCallback(async () => {
    setPaperRetranslating(true);
    setPaperRetranslateMessage(null);
    setTranslateRemainingMessage(null);
    try {
      const result = await retranslatePaper(paperId, paperRetranslateDraft);
      setShowPaperRetranslate(false);
      setPaperRetranslateMessage(`已清空当前译文并排入 ${result.queued} 个重新翻译任务。`);
      await Promise.all([loadTranslationState(), load()]);
    } catch (reason) {
      setPaperRetranslateMessage(
        reason instanceof Error ? reason.message : "Unable to restart paper translation.",
      );
    } finally {
      setPaperRetranslating(false);
    }
  }, [load, loadTranslationState, paperId, paperRetranslateDraft]);

  const submitRemainingTranslations = useCallback(async () => {
    setTranslatingRemaining(true);
    setTranslateRemainingMessage(null);
    setPaperRetranslateMessage(null);
    try {
      const result = await translateRemainingPaper(paperId);
      const queuedDetails = [
        result.created ? `新增 ${result.created}` : "",
        result.retried ? `重试 ${result.retried}` : "",
      ].filter(Boolean).join("，");
      if (result.queued > 0) {
        setTranslateRemainingMessage(
          `已排入 ${result.queued} 个翻译任务${queuedDetails ? `（${queuedDetails}）` : ""}。`,
        );
      } else if (result.cached > 0) {
        setTranslateRemainingMessage(`已从缓存补齐 ${result.cached} 个区块。`);
      } else {
        setTranslateRemainingMessage("没有尚未翻译或翻译失败的区块。");
      }
      await loadTranslationState();
    } catch (reason) {
      setTranslateRemainingMessage(
        reason instanceof Error ? reason.message : "Unable to translate remaining blocks.",
      );
    } finally {
      setTranslatingRemaining(false);
    }
  }, [loadTranslationState, paperId]);

  const loadAnnotations = useCallback(async () => {
    setAnnotationsLoading(true);
    try {
      setAnnotations(await getPaperAnnotations(paperId));
      setAnnotationsError(null);
    } catch (reason) {
      setAnnotationsError(
        reason instanceof Error ? reason.message : "Unable to load annotations.",
      );
    } finally {
      setAnnotationsLoading(false);
    }
  }, [paperId]);

  const loadBlockBookmarks = useCallback(async () => {
    try {
      setBlockBookmarks(await getBlockBookmarks(paperId));
      setBookmarkError(null);
    } catch (reason) {
      setBookmarkError(
        reason instanceof Error ? reason.message : "Unable to load block bookmarks.",
      );
    }
  }, [paperId]);

  const toggleBlockBookmark = useCallback(async (blockId: string) => {
    if (bookmarkBusyIds.has(blockId)) return;
    const existing = blockBookmarks.find((item) => item.block_id === blockId) ?? null;
    setBookmarkBusyIds((current) => new Set(current).add(blockId));
    setBookmarkError(null);
    try {
      if (existing) {
        await removeBlockBookmark(paperId, blockId);
        setBlockBookmarks((current) => current.filter((item) => item.block_id !== blockId));
      } else {
        const created = await addBlockBookmark(paperId, blockId);
        setBlockBookmarks((current) => [
          created,
          ...current.filter((item) => item.block_id !== blockId),
        ]);
      }
    } catch (reason) {
      setBookmarkError(
        reason instanceof Error ? reason.message : "Unable to update this bookmark.",
      );
    } finally {
      setBookmarkBusyIds((current) => {
        const next = new Set(current);
        next.delete(blockId);
        return next;
      });
    }
  }, [blockBookmarks, bookmarkBusyIds, paperId]);

  const openAnnotation = useCallback((
    annotation: Annotation,
    position: AnnotationPopoverPosition,
  ) => {
    if (!["ANNOTATION", "HIGHLIGHT", "USER_NOTE"].includes(annotation.annotation_type)) return;
    setActiveAnnotationId(annotation.id);
    setAnnotationPopoverPosition(position);
  }, []);

  const closeAnnotation = useCallback(() => {
    setActiveAnnotationId(null);
    setAnnotationPopoverPosition(null);
  }, []);

  const moveAnnotation = useCallback((position: AnnotationPopoverPosition) => {
    setAnnotationPopoverPosition(position);
  }, []);

  const hoverAnnotation = useCallback((
    annotation: Annotation | null,
    position: AnnotationPopoverPosition | null,
  ) => {
    setHoveredAnnotationId(annotation?.id ?? null);
    setAnnotationHoverPosition(position);
  }, []);

  const activeAnnotation = annotations.find((item) => item.id === activeAnnotationId) ?? null;
  const hoveredAnnotation = annotations.find((item) => item.id === hoveredAnnotationId) ?? null;
  const hoveredAnnotationHasQuestion = hoveredAnnotation
    ? annotations.some((item) => (
      item.annotation_type === "AI_QUESTION"
      && item.anchor_annotation_id === hoveredAnnotation.id
    ))
    : false;
  const activeAnnotationQuestions = activeAnnotation
    ? annotations.filter((item) => (
      item.annotation_type === "AI_QUESTION"
      && item.anchor_annotation_id === activeAnnotation.id
    ))
    : [];
  const annotationResponsesByQuestion = new Map<string, Annotation[]>();
  annotations.filter((item) => item.annotation_type === "AI_RESPONSE" && item.parent_annotation_id)
    .forEach((item) => {
      const key = item.parent_annotation_id as string;
      annotationResponsesByQuestion.set(
        key,
        [...(annotationResponsesByQuestion.get(key) ?? []), item],
      );
    });

  useEffect(() => {
    if (!readingReady) return;
    let cancelled = false;
    async function loadReadingContent() {
      setReadingLoading(true);
      setReadingError(null);
      try {
        let nextOutline: PaperOutlineItem[] = [];
        let nextBibliography: PaperBibliography | null = null;
        if (!isPdfOnly) {
          [nextOutline, nextBibliography] = await Promise.all([
            getPaperOutline(paperId),
            getPaperBibliography(paperId),
          ]);
        }
        const nextBlocks: ContentBlock[] = [];
        let cursor: string | null = null;
        do {
          const page = await getPaperBlocks(paperId, cursor);
          nextBlocks.push(...page.items);
          cursor = page.next_cursor;
        } while (cursor !== null);
        if (!cancelled) {
          setOutline(nextOutline);
          setBlocks(nextBlocks);
          setBibliography(nextBibliography);
        }
      } catch (reason) {
        if (!cancelled) {
          setReadingError(
            reason instanceof Error ? reason.message : "Unable to load parsed paper content.",
          );
        }
      } finally {
        if (!cancelled) setReadingLoading(false);
      }
    }
    void loadReadingContent();
    return () => {
      cancelled = true;
    };
  }, [isPdfOnly, paperId, readingReady, readingRevision, renderRefreshRevision]);

  useEffect(() => {
    readingPositionRestoredRef.current = false;
    const previousScrollRestoration = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => {
      window.history.scrollRestoration = previousScrollRestoration;
    };
  }, [paperId]);

  useEffect(() => {
    if (!blocks.length || readingPositionRestoredRef.current) return;
    const root = documentViewportRef.current;
    if (root === null) return;
    const allowSessionFallback = paperNavigationWasReloaded();
    let frame: number | null = null;
    let settleTimer: number | null = null;
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      resizeObserver.disconnect();
      if (frame !== null) window.cancelAnimationFrame(frame);
      if (settleTimer !== null) window.clearTimeout(settleTimer);
      window.removeEventListener("wheel", finish, true);
      window.removeEventListener("touchstart", finish, true);
      window.removeEventListener("pointerdown", finish, true);
      window.removeEventListener("keydown", finish, true);
    };
    const restore = () => {
      if (finished) return;
      frame = null;
      const restored = restorePaperReadingPosition({ allowSessionFallback });
      readingPositionRestoredRef.current = true;
      if (!restored) {
        finish();
        return;
      }
      readingPositionSyncSuspendedUntilRef.current = performance.now() + 700;
    };
    const scheduleRestore = () => {
      if (finished || frame !== null) return;
      frame = window.requestAnimationFrame(restore);
    };
    const resizeObserver = new ResizeObserver(scheduleRestore);
    resizeObserver.observe(root);
    window.addEventListener("wheel", finish, { capture: true, passive: true });
    window.addEventListener("touchstart", finish, { capture: true, passive: true });
    window.addEventListener("pointerdown", finish, { capture: true, passive: true });
    window.addEventListener("keydown", finish, true);
    scheduleRestore();
    settleTimer = window.setTimeout(() => {
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = null;
      restore();
      finish();
    }, 2_500);
    return finish;
  }, [bibliography, blocks.length, paperId]);

  useEffect(() => {
    if (!blocks.length) return;
    const restoreFromLocation = () => {
      readingPositionSyncSuspendedUntilRef.current = performance.now() + 400;
      window.requestAnimationFrame(() => restorePaperReadingPosition());
    };
    window.addEventListener("hashchange", restoreFromLocation);
    window.addEventListener("popstate", restoreFromLocation);
    return () => {
      window.removeEventListener("hashchange", restoreFromLocation);
      window.removeEventListener("popstate", restoreFromLocation);
    };
  }, [blocks.length]);

  useEffect(() => {
    const root = documentViewportRef.current;
    if (!root || !blocks.length) return;
    const readingBlockElements = paperReadingBlockElements(root);
    if (!readingBlockElements.length) return;
    let resumeTimer: number | null = null;
    const updateReadingPosition = () => {
      readingPositionSyncFrameRef.current = null;
      if (
        jumpReturnInProgressRef.current
        || performance.now() < readingPositionSyncSuspendedUntilRef.current
      ) return;
      const blockId = currentVisiblePaperBlockId(readingBlockElements);
      if (blockId !== null) updatePaperReadingPosition(blockId);
    };
    const scheduleReadingPositionUpdate = () => {
      if (readingPositionSyncFrameRef.current !== null) return;
      readingPositionSyncFrameRef.current = window.requestAnimationFrame(updateReadingPosition);
    };
    const updateAfterPaperJump = () => {
      if (resumeTimer !== null) window.clearTimeout(resumeTimer);
      resumeTimer = window.setTimeout(scheduleReadingPositionUpdate, 1_550);
    };
    const resizeObserver = new ResizeObserver(scheduleReadingPositionUpdate);
    resizeObserver.observe(root);
    scheduleReadingPositionUpdate();
    window.addEventListener("scroll", scheduleReadingPositionUpdate, { passive: true });
    window.addEventListener("resize", scheduleReadingPositionUpdate);
    window.addEventListener(PAPER_BLOCK_JUMP_EVENT, updateAfterPaperJump);
    return () => {
      window.removeEventListener("scroll", scheduleReadingPositionUpdate);
      window.removeEventListener("resize", scheduleReadingPositionUpdate);
      window.removeEventListener(PAPER_BLOCK_JUMP_EVENT, updateAfterPaperJump);
      resizeObserver.disconnect();
      if (resumeTimer !== null) window.clearTimeout(resumeTimer);
      if (readingPositionSyncFrameRef.current !== null) {
        window.cancelAnimationFrame(readingPositionSyncFrameRef.current);
        readingPositionSyncFrameRef.current = null;
      }
    };
  }, [blocks.length, readingMode]);

  useEffect(() => {
    if (!readingReady || isPdfOnly) return;
    void loadTranslationState();
    void loadAnnotations();
    void loadBlockBookmarks();
  }, [isPdfOnly, loadAnnotations, loadBlockBookmarks, loadTranslationState, readingReady]);

  const qaActive = !isPdfOnly && annotations.some((annotation) => (
    annotation.annotation_type === "AI_QUESTION"
    && ["QUEUED", "RUNNING"].includes(annotation.qa_status ?? "")
  ));
  useEffect(() => {
    if (!qaActive) return;
    const timer = window.setInterval(() => void loadAnnotations(), 1600);
    return () => window.clearInterval(timer);
  }, [loadAnnotations, qaActive]);

  const translationActive = !isPdfOnly && translationProgress?.configured === true
    && (
      translationProgress.pending > translationProgress.missing
      || translationProgress.running > 0
    );
  useEffect(() => {
    if (!translationActive) return;
    const timer = window.setInterval(() => void loadTranslationState(), 1800);
    return () => window.clearInterval(timer);
  }, [loadTranslationState, translationActive]);

  useEffect(() => {
    if (!outline.length) {
      setActiveOutlineId(null);
      return;
    }
    let frame: number | null = null;
    const updateActiveSection = () => {
      frame = null;
      const readingLine = Math.min(180, Math.max(108, window.innerHeight * 0.24));
      let currentId = outline[0].id;
      for (const item of outline) {
        const heading = document.getElementById(`block-${item.id}`);
        if (heading === null) continue;
        if (heading.getBoundingClientRect().top <= readingLine) currentId = item.id;
        else break;
      }
      setActiveOutlineId((current) => current === currentId ? current : currentId);
    };
    const scheduleUpdate = () => {
      if (frame !== null) return;
      frame = window.requestAnimationFrame(updateActiveSection);
    };
    updateActiveSection();
    window.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", scheduleUpdate);
    return () => {
      window.removeEventListener("scroll", scheduleUpdate);
      window.removeEventListener("resize", scheduleUpdate);
      if (frame !== null) window.cancelAnimationFrame(frame);
    };
  }, [outline]);

  useEffect(() => {
    if (activeOutlineId === null) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    for (const container of document.querySelectorAll<HTMLElement>(
      "[data-outline-scroll-container]",
    )) {
      if (container.getClientRects().length === 0) continue;
      const activeLink = container.querySelector<HTMLElement>(
        `[data-outline-id="${activeOutlineId}"]`,
      );
      if (activeLink === null) continue;
      const containerRect = container.getBoundingClientRect();
      const linkRect = activeLink.getBoundingClientRect();
      const topBoundary = containerRect.top + 10;
      const bottomBoundary = containerRect.bottom - 10;
      if (linkRect.top >= topBoundary && linkRect.bottom <= bottomBoundary) continue;
      container.scrollBy({
        behavior: reducedMotion ? "auto" : "smooth",
        top: linkRect.top - containerRect.top - container.clientHeight * 0.35,
      });
    }
  }, [activeOutlineId, mobileOutlineOpen]);

  const progress = useMemo(() => {
    if (!paper?.tasks.length) return 0;
    const completed = paper.tasks.reduce((total, task) => {
      if (task.status === "SUCCEEDED") return total + 1;
      if (task.task_type === "FETCH_SOURCE" && task.download_progress?.percent != null) {
        return total + Math.min(1, Math.max(0, task.download_progress.percent / 100));
      }
      return total;
    }, 0);
    return Math.round(completed / paper.tasks.length * 100);
  }, [paper]);
  const bibliographyEntriesById = useMemo<ReadonlyMap<string, BibliographyEntry>>(
    () => new Map((bibliography?.entries ?? []).map((entry) => [entry.id, entry])),
    [bibliography],
  );
  const bibliographyMarker = useMemo(
    () => blocks.find((block) => block.block_type === "BIBLIOGRAPHY") ?? null,
    [blocks],
  );
  const bookmarkedBlockIds = useMemo(
    () => new Set(blockBookmarks.map((bookmark) => bookmark.block_id)),
    [blockBookmarks],
  );
  const readerWorkspaceStyle = {
    "--reader-left-width": `${leftSidebarWidth}px`,
    "--reader-right-width": `${rightSidebarWidth}px`,
  } as CSSProperties;
  const handleRepairQueued = useCallback(() => {
    setRenderRefreshRevision((current) => current + 1);
    void load();
  }, [load]);
  const dismissMediaPreview = useCallback(() => setPreviewMedia(null), []);

  function renderOutlineNavigation(onNavigate?: () => void) {
    if (!outline.length) {
      return <p>{readingLoading ? "Building outline…" : "No explicit section commands found."}</p>;
    }
    return (
      <nav aria-label="Paper outline">
        {outline.map((item) => {
          const isActive = activeOutlineId === item.id;
          const outlineNumber = item.parent_id === null && item.display_number
            ? `${item.display_number}.`
            : null;
          return (
            <a
              aria-current={isActive ? "location" : undefined}
              className={isActive ? "outline-link-active" : undefined}
              data-outline-id={item.id}
              href={`#block-${item.id}`}
              key={item.id}
              onClick={() => {
                setActiveOutlineId(item.id);
                onNavigate?.();
              }}
              style={{ paddingLeft: `${10 + Math.max(0, item.heading_level - 2) * 12}px` }}
            >
              {outlineNumber ? (
                <><span className="outline-number">{outlineNumber}</span>{" "}</>
              ) : null}
              <RichText fallback={item.title} payload={{ segments: item.title_segments }} />
            </a>
          );
        })}
      </nav>
    );
  }

  function renderBookmarkList(onNavigate?: () => void) {
    return (
      <section className="reader-bookmarks" aria-label="Bookmarked blocks">
        <header>
          <div>
            <p className="eyebrow">Bookmarks</p>
            <strong>本篇收藏</strong>
          </div>
          <span>{blockBookmarks.length}</span>
        </header>
        {bookmarkError ? <small className="reader-bookmark-error" role="alert">{bookmarkError}</small> : null}
        {blockBookmarks.length ? (
          <div className="reader-bookmark-list">
            {blockBookmarks.map((bookmark) => {
              const sectionLabel = [
                bookmark.section_display_number
                  ? `${bookmark.section_display_number}.`
                  : null,
                bookmark.section_title,
              ].filter(Boolean).join(" ");
              return (
                <article className="reader-bookmark-card" key={bookmark.id}>
                  <button
                    className="reader-bookmark-jump"
                    onClick={() => {
                      jumpToPaperBlock(bookmark.block_id);
                      onNavigate?.();
                    }}
                    type="button"
                  >
                    <span>{sectionLabel || bookmark.block_type.replaceAll("_", " ")}</span>
                    <p>{bookmark.preview || "此区块没有可用的文本预览。"}</p>
                    <code>{bookmark.block_id}</code>
                  </button>
                  <button
                    aria-label="取消收藏这个区块"
                    className="reader-bookmark-remove"
                    disabled={bookmarkBusyIds.has(bookmark.block_id)}
                    onClick={() => void toggleBlockBookmark(bookmark.block_id)}
                    title="取消收藏"
                    type="button"
                  >★</button>
                </article>
              );
            })}
          </div>
        ) : <p className="reader-bookmark-empty">将鼠标移到正文区块附近，点击星标即可收藏。</p>}
      </section>
    );
  }

  async function retry() {
    setRetrying(true);
    try {
      const result = await retryPaper(paperId);
      setPaper(result.paper);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Retry failed.");
    } finally {
      setRetrying(false);
    }
  }

  if (!paper) {
    return (
      <div className="center-state">
        <div className="loader-mark" aria-hidden="true">P</div>
        <p className="eyebrow">Paper workspace</p>
        <h1>{error ?? "Loading paper…"}</h1>
        {error ? <Link className="primary-link" href="/library">Return to library</Link> : null}
      </div>
    );
  }

  const failed = paper.tasks.some((task) => task.status === "FAILED");
  const visibleTasks = showAllTasks ? paper.tasks : paper.tasks.slice(0, 16);
  const hiddenTaskCount = Math.max(0, paper.tasks.length - visibleTasks.length);
  return (
    <CitationPreviewContext.Provider value={bibliographyEntriesById}>
      <div className="paper-page" onClickCapture={rememberBlockAnchorOrigin}>
      <Link className="back-link" href="/library">← Library</Link>
      <header className="paper-header" id="paper-metadata">
        <div>
          <p className="eyebrow">arXiv:{paper.canonical_arxiv_id}</p>
          <h1>{paper.title}</h1>
          <p className="paper-byline">
            {paper.authors.length ? paper.authors.map((author) => author.name).join(", ") : "Fetching authors…"}
          </p>
        </div>
        <a
          className="secondary-link"
          href={`https://arxiv.org/abs/${paper.canonical_arxiv_id}`}
          target="_blank"
          rel="noreferrer"
        >
          View on arXiv ↗
        </a>
      </header>

      <section className="progress-card">
        <div className="progress-heading">
          <div>
            <p className="eyebrow">Import pipeline</p>
            <h2>{paper.processing_stage.replaceAll("_", " ")}</h2>
          </div>
          <strong>{progress}%</strong>
        </div>
        <div className="progress-track" aria-label={`Import progress ${progress}%`}>
          <span style={{ width: `${progress}%` }} />
        </div>
        <div className="task-list" aria-label="Processing tasks">
          {visibleTasks.map((task) => (
            <article
              className="task-row"
              key={task.id}
              title={task.error_message ?? undefined}
            >
              <span className={`task-dot task-${task.status.toLowerCase()}`} />
              <div className="task-copy">
                <strong>{task.task_type.replaceAll("_", " ")}</strong>
                <small>
                  {task.status}{task.attempt_count ? ` · ${task.attempt_count}/${task.max_attempts}` : ""}
                </small>
                {task.download_progress && task.status !== "SUCCEEDED" ? (
                  <div className="task-download-progress">
                    <span>
                      {bytes(task.download_progress.bytes_downloaded)}
                      {task.download_progress.bytes_total !== null
                        ? ` / ${bytes(task.download_progress.bytes_total)}`
                        : " downloaded"}
                      {task.download_progress.bytes_remaining !== null
                        && task.download_progress.phase !== "RESTART_REQUIRED"
                        ? ` · ${bytes(task.download_progress.bytes_remaining)} remaining`
                        : ""}
                    </span>
                    {task.download_progress.resumed_bytes > 0 ? <em>resumed</em> : null}
                    {task.download_progress.percent !== null ? (
                      <div
                        className="task-download-track"
                        aria-label={`Source download ${task.download_progress.percent}%`}
                      >
                        <span style={{ width: `${task.download_progress.percent}%` }} />
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {task.error_message && isProviderApiFailure(task.error_code, task.error_message) ? (
                  <details className="task-api-error" open={task.error_code === "CONTENT_REJECTED"}>
                    <summary>查看完整 API 错误</summary>
                    <pre role="alert">{task.error_message}</pre>
                  </details>
                ) : task.error_message ? <p role="alert">{task.error_message}</p> : null}
              </div>
              <div className="task-row-actions">
                <TaskJumpButton paperId={paperId} task={task} />
                <TaskInspector paperId={paperId} task={task} />
              </div>
            </article>
          ))}
        </div>
        {paper.tasks.length > 16 || failed ? (
          <div className="task-actions">
            {paper.tasks.length > 16 ? (
              <button
                className="task-list-toggle"
                type="button"
                onClick={() => setShowAllTasks((current) => !current)}
              >
                {showAllTasks ? "Show less" : `Show more (${hiddenTaskCount})`}
              </button>
            ) : null}
            {failed ? (
              <button className="retry-button" type="button" onClick={retry} disabled={retrying}>
                {retrying ? "Retrying…" : "Retry failed tasks"}
              </button>
            ) : null}
          </div>
        ) : null}
      </section>

      <div className="paper-content-grid">
        <section
          className="content-card"
          id={abstractAnnotationBlock ? `block-${abstractAnnotationBlock.id}` : undefined}
          ref={abstractAnnotationRootRef}
        >
          <p className="eyebrow">Abstract</p>
          <p
            data-annotation-block-id={abstractAnnotationBlock?.id}
            data-annotation-target={abstractAnnotationBlock ? "SOURCE" : undefined}
          >
            {paper.abstract ? (
              paper.abstract_render_payload && Array.isArray(paper.abstract_render_payload.segments)
                ? (
                    <RichText
                      fallback={paper.abstract}
                      formulaRepair={abstractAnnotationBlock ? {
                        paperId,
                        blockId: abstractAnnotationBlock.id,
                        onQueued: handleRepairQueued,
                        pathPrefix: ["segments"],
                      } : undefined}
                      payload={paper.abstract_render_payload}
                      preserveSpacing
                    />
                  )
                : (
                    <InlineLatexText
                      formulaRepair={abstractAnnotationBlock ? {
                        paperId,
                        blockId: abstractAnnotationBlock.id,
                        onQueued: handleRepairQueued,
                        pathPrefix: ["segments"],
                      } : undefined}
                      value={paper.abstract}
                    />
                  )
            ) : "Metadata is still being fetched from arXiv."}
          </p>
          {!isPdfOnly && readingMode === "bilingual" && abstractAnnotationBlock ? (
            <TranslationPanel
              block={abstractAnnotationBlock}
              configured={translationProgress?.configured ?? false}
              defaultPrompt={globalTranslationPrompt || abstractAnnotationBlock.translation?.system_prompt || ""}
              onRepairQueued={handleRepairQueued}
              onRetried={loadTranslationState}
              paperId={paperId}
            />
          ) : null}
        </section>
        <aside className="source-card" id="paper-source-assets">
          <p className="eyebrow">Source assets</p>
          {paper.sources.length ? paper.sources.map((source) => (
            <div className="source-row" key={source.source_type}>
              <div>
                <strong>{source.source_type.replaceAll("_", " ")}</strong>
                <small>{bytes(source.byte_size)} · SHA-256 {source.sha256.slice(0, 10)}…</small>
              </div>
              <div className="source-row-actions">
                <span>Stored</span>
                <a
                  aria-label={`Download ${source.source_type.replaceAll("_", " ").toLowerCase()}`}
                  className="source-download-link"
                  download
                  href={paperSourceDownloadUrl(paperId, source.source_type)}
                  title="Download compressed source"
                >
                  <span aria-hidden="true">↓</span>
                  Download
                </a>
              </div>
            </div>
          )) : <p>Waiting for source download and archive validation.</p>}
        </aside>
      </div>

      {isPdfOnly ? (
        <section className="pdf-only-notice" role="status">
          <strong>本篇文章无LaTeX源代码，无法进行结构化理解，仅供只读阅读</strong>
          <span>当前版本保留并直接显示 arXiv 原始 PDF。</span>
        </section>
      ) : null}

      {!isPdfOnly ? <PaperVisualMap paperId={paperId} /> : null}

      {readingReady ? (
        <section
          aria-label="Parsed paper content"
          className={[
            "reading-workspace",
            isPdfOnly ? "reading-workspace-pdf-only" : "",
            leftSidebarCollapsed ? "reader-left-collapsed" : "",
            rightSidebarCollapsed ? "reader-right-collapsed" : "",
          ].filter(Boolean).join(" ")}
          id="paper-structured-view"
          style={readerWorkspaceStyle}
        >
          {!isPdfOnly ? (
            <>
              <button
                aria-expanded={mobileOutlineOpen}
                aria-label="打开论文目录"
                className="mobile-outline-trigger"
                onClick={() => setMobileOutlineOpen(true)}
                type="button"
              >☰</button>
              {mobileOutlineOpen ? (
                <div
                  className="mobile-outline-backdrop"
                  onClick={() => setMobileOutlineOpen(false)}
                  role="presentation"
                >
                  <aside
                    aria-label="Mobile paper outline"
                    aria-modal="true"
                    className="mobile-outline-sheet"
                    onClick={(event) => event.stopPropagation()}
                    role="dialog"
                  >
                    <header>
                      <div><p className="eyebrow">Navigate</p><strong>Document outline</strong></div>
                      <button aria-label="关闭论文目录" onClick={() => setMobileOutlineOpen(false)} type="button">×</button>
                    </header>
                    <OutlineScrollRegion ariaLabel="Document outline sections" className="mobile-outline-links">
                      {renderOutlineNavigation(() => setMobileOutlineOpen(false))}
                    </OutlineScrollRegion>
                    {renderBookmarkList(() => setMobileOutlineOpen(false))}
                  </aside>
                </div>
              ) : null}
              <aside className="outline-panel reader-left-sidebar">
                <header className="reader-sidebar-heading">
                  <p className="eyebrow">Document outline</p>
                  <button aria-label="折叠目录边栏" onClick={() => setLeftSidebarCollapsed(true)} title="折叠目录" type="button">‹</button>
                </header>
                <OutlineScrollRegion ariaLabel="Document outline sections" className="reader-outline-section">
                  {renderOutlineNavigation()}
                </OutlineScrollRegion>
                {renderBookmarkList()}
                <button
                  aria-label="调整目录边栏宽度"
                  className="reader-sidebar-resizer reader-sidebar-resizer-left"
                  onDoubleClick={() => setLeftSidebarWidth(DEFAULT_READER_LEFT_WIDTH)}
                  onPointerDown={(event) => startSidebarResize("left", event)}
                  title="拖动调整目录宽度；向左拖到底可折叠"
                  type="button"
                />
              </aside>
            </>
          ) : null}
          <article
            className={`document-viewport${isPdfOnly ? " pdf-only-document-viewport" : ""}`}
            ref={documentViewportRef}
          >
            <div className="reader-intro">
              <div>
                <p className="eyebrow">
                  {isPdfOnly ? "PDF paper view" : "Structured paper view"}
                </p>
                <span>
                  {isPdfOnly ? "arXiv 原始文档" : `${blocks.length} content blocks`}
                </span>
              </div>
            </div>
            {!isPdfOnly && readingMode === "bilingual" && translationProgress ? (
              <section className="translation-progress-card" aria-label="Translation progress">
                <div className="translation-progress-heading">
                  <div>
                    <strong>
                      {translationProgress.succeeded}/{translationProgress.eligible} 已翻译
                    </strong>
                    <span>
                      {translationProgress.running} 进行中 · {translationProgress.failed} 失败
                    </span>
                  </div>
                  <div className="translation-progress-actions">
                    <button
                      disabled={
                        !translationProgress.configured
                        || paperRetranslating
                        || translatingRemaining
                      }
                      onClick={openPaperRetranslation}
                      type="button"
                    >
                      重新翻译全部
                    </button>
                    <button
                      disabled={
                        !translationProgress.configured
                        || paperRetranslating
                        || translatingRemaining
                        || (translationProgress.missing === 0 && translationProgress.failed === 0)
                      }
                      onClick={() => void submitRemainingTranslations()}
                      title="仅翻译尚未翻译的区块，并重试失败区块；成功译文保持不变"
                      type="button"
                    >
                      {translatingRemaining ? "正在排队…" : "翻译剩余内容"}
                    </button>
                  </div>
                </div>
                <div
                  aria-label={`Translation progress ${translationProgress.eligible
                    ? Math.round((translationProgress.succeeded / translationProgress.eligible) * 100)
                    : 0}%`}
                  className="translation-progress-track"
                >
                  <span style={{
                    width: `${translationProgress.eligible
                      ? (translationProgress.succeeded / translationProgress.eligible) * 100
                      : 0}%`,
                  }} />
                </div>
                <small>
                  {translationProgress.configured
                    ? "LLM-based Translation"
                    : <>尚未配置可用的个人 LLM 凭据，请前往 <Link href="/settings">设置</Link> 检查服务商、模型和密钥。</>}
                  {translationProgress.input_tokens || translationProgress.output_tokens
                    ? ` · ${translationProgress.input_tokens + translationProgress.output_tokens} tokens`
                    : ""}
                  {translationProgress.cache_hits
                    ? ` · ${translationProgress.cache_hits} cache hits`
                    : ""}
                  {translationProgress.retry_count
                    ? ` · ${translationProgress.retry_count} retries`
                    : ""}
                  {translationProgress.average_latency_ms !== null
                    ? ` · avg ${translationProgress.average_latency_ms} ms`
                    : ""}
                </small>
                {showPaperRetranslate ? (
                  <div
                    aria-label="重新翻译全部"
                    aria-modal="false"
                    className="translation-retranslate-all-editor"
                    role="dialog"
                  >
                    <div>
                      <strong>重新翻译整篇论文</strong>
                      <button
                        aria-label="取消重新翻译全部"
                        disabled={paperRetranslating}
                        onClick={() => setShowPaperRetranslate(false)}
                        type="button"
                      >
                        ×
                      </button>
                    </div>
                    <p>
                      提交后，当前中文译文会立即从阅读视图清空，并以此 System Prompt
                      为所有可翻译 block 创建一批全新的持久化任务。
                    </p>
                    <textarea
                      aria-label="整篇重新翻译 System Prompt"
                      maxLength={8000}
                      onChange={(event) => setPaperRetranslateDraft(event.target.value)}
                      value={paperRetranslateDraft}
                    />
                    <div className="translation-retranslate-all-actions">
                      <small>{paperRetranslateDraft.length}/8000</small>
                      <button
                        disabled={paperRetranslating || paperRetranslateDraft.trim().length < 80}
                        onClick={() => void submitPaperRetranslation()}
                        type="button"
                      >
                        {paperRetranslating ? "正在提交…" : "确认清空并重新翻译"}
                      </button>
                    </div>
                  </div>
                ) : null}
                {paperRetranslateMessage ? (
                  <span
                    className={showPaperRetranslate ? "translation-action-error" : "translation-retranslate-status"}
                    role="status"
                  >
                    {paperRetranslateMessage}
                  </span>
                ) : null}
                {translateRemainingMessage ? (
                  <span className="translation-retranslate-status" role="status">
                    {translateRemainingMessage}
                  </span>
                ) : null}
              </section>
            ) : null}
            {!isPdfOnly && readingMode === "bilingual" && translationError ? (
              <p className="error-banner" role="alert">{translationError}</p>
            ) : null}
            {readingError ? <p className="error-banner" role="alert">{readingError}</p> : null}
            {readingLoading && !blocks.length ? <p>Loading parsed content…</p> : null}
            {blocks.map((block) => block.block_type === "BIBLIOGRAPHY" ? (
              block.id === bibliographyMarker?.id && bibliography ? (
                <BibliographySection
                  bibliography={bibliography}
                  bookmarkBusy={bookmarkBusyIds.has(block.id)}
                  bookmarked={bookmarkedBlockIds.has(block.id)}
                  key={block.id}
                  markerBlockId={block.id}
                  onBookmarkToggle={() => void toggleBlockBookmark(block.id)}
                />
              ) : null
            ) : (
              <BilingualBlock
                block={block}
                defaultPrompt={globalTranslationPrompt || block.translation?.system_prompt || ""}
                key={block.id}
                mode={isPdfOnly ? "original" : readingMode}
                onMediaPreview={setPreviewMedia}
                onRepairQueued={handleRepairQueued}
                onTranslationRetried={loadTranslationState}
                bookmarkBusy={bookmarkBusyIds.has(block.id)}
                bookmarked={bookmarkedBlockIds.has(block.id)}
                onBookmarkToggle={() => void toggleBlockBookmark(block.id)}
                paperId={paperId}
                translationConfigured={translationProgress?.configured ?? false}
              />
            ))}
            {!isPdfOnly && bibliography && !bibliographyMarker ? (
              <BibliographySection bibliography={bibliography} />
            ) : null}
          </article>
          {!isPdfOnly ? (
            <>
              <div className="reader-right-sidebar">
                <button
                  aria-label="调整批注边栏宽度"
                  className="reader-sidebar-resizer reader-sidebar-resizer-right"
                  onDoubleClick={() => setRightSidebarWidth(DEFAULT_READER_RIGHT_WIDTH)}
                  onPointerDown={(event) => startSidebarResize("right", event)}
                  title="拖动调整批注宽度；向右拖到底可折叠"
                  type="button"
                />
                <AnnotationPanel
                  annotations={annotations}
                  error={annotationsError}
                  hoveredAnnotationId={hoveredAnnotationId}
                  loading={annotationsLoading}
                  onAnnotationActivate={openAnnotation}
                  onCollapse={() => setRightSidebarCollapsed(true)}
                  onRefresh={loadAnnotations}
                  panelRef={annotationPanelRef}
                  paperId={paperId}
                />
              </div>
              <PersistentAnnotationHighlights
                annotations={annotations}
                onAnnotationActivate={openAnnotation}
                onAnnotationHover={hoverAnnotation}
                readerRootRef={documentViewportRef}
                summaryRootRef={abstractAnnotationRootRef}
              />
              <SelectionToolbar
                blocks={annotationBlocks}
                onChanged={loadAnnotations}
                onOpenAnnotation={openAnnotation}
                paperId={paperId}
              />
              {activeAnnotation && annotationPopoverPosition ? (
                <AnnotationEditorPopover
                  annotation={activeAnnotation}
                  onChanged={loadAnnotations}
                  onClose={closeAnnotation}
                  onPositionChange={moveAnnotation}
                  position={annotationPopoverPosition}
                  questions={activeAnnotationQuestions}
                  responsesByQuestion={annotationResponsesByQuestion}
                />
              ) : null}
              {hoveredAnnotation && annotationHoverPosition ? (
                <AnnotationHoverPreview
                  annotation={hoveredAnnotation}
                  hasQuestion={hoveredAnnotationHasQuestion}
                  position={annotationHoverPosition}
                />
              ) : null}
              {leftSidebarCollapsed ? (
                <button
                  aria-label="展开目录边栏"
                  className="reader-sidebar-edge-toggle reader-sidebar-edge-left"
                  onClick={() => setLeftSidebarCollapsed(false)}
                  title="展开目录"
                  type="button"
                >›</button>
              ) : null}
              {rightSidebarCollapsed ? (
                <button
                  aria-label="展开批注边栏"
                  className="reader-sidebar-edge-toggle reader-sidebar-edge-right"
                  onClick={() => setRightSidebarCollapsed(false)}
                  title="展开批注"
                  type="button"
                >‹</button>
              ) : null}
            </>
          ) : null}
        </section>
      ) : (
        <section className="reader-pending" id="paper-structured-view">
          <p className="eyebrow">Reading engine</p>
          <h2>Source parsing will begin after the archive is ready.</h2>
          <p>The parser follows the paper&apos;s actual LaTeX hierarchy and does not assume standard section names.</p>
        </section>
      )}
      {previewMedia ? (
        <MediaPreviewDialog media={previewMedia} onDismiss={dismissMediaPreview} />
      ) : null}
      {showBackToTop ? (
        <button
          aria-label="回到页面顶部"
          className="back-to-top-button"
          onClick={scrollToPageTop}
          title="回到顶部"
          type="button"
        >
          <span aria-hidden="true">↑</span>
          顶部
        </button>
      ) : null}
      {paperBlockJumpOrigin ? (
        <button
          aria-label="返回到刚才位置"
          className={`jump-return-button${showBackToTop ? " jump-return-button-stacked" : ""}`}
          onClick={returnToPaperBlockJumpOrigin}
          title="返回到刚才位置"
          type="button"
        >
          <span aria-hidden="true">↩</span>
          {returningToJumpOrigin ? "正在返回…" : "返回刚才位置"}
        </button>
      ) : null}
      </div>
    </CitationPreviewContext.Provider>
  );
}
