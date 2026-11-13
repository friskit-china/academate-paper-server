"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  CSSProperties,
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  deletePapers,
  importPaper,
  listPapers,
  updatePaperPreferences,
  type Paper,
} from "../api";
import { UI_STORAGE_KEYS } from "../storage-keys";
import { formatLastOpened } from "./last-opened";

const COLUMN_STORAGE_KEY = UI_STORAGE_KEYS.libraryVisibleColumns;
const COLUMN_WIDTH_STORAGE_KEY = UI_STORAGE_KEYS.libraryColumnWidths;
const COLUMN_ORDER_STORAGE_KEY = UI_STORAGE_KEYS.libraryColumnOrder;
const COLUMN_SCHEMA_VERSION_KEY = UI_STORAGE_KEYS.libraryColumnSchemaVersion;
const CURRENT_COLUMN_SCHEMA_VERSION = "3";
const LAST_OPENED_COLUMN_ADDED_SCHEMA_VERSION = 2;
const LAST_OPENED_COMPACT_WIDTH_SCHEMA_VERSION = 3;
const LEGACY_LAST_OPENED_DEFAULT_WIDTH = 180;
const LEGACY_LAST_OPENED_MIN_WIDTH = 145;
const ACTIONS_COLUMN_WIDTH = 108;
const COLUMN_OPTIONS = [
  { id: "title", label: "Title", defaultWidth: 300, minWidth: 180 },
  { id: "authors", label: "Authors", defaultWidth: 240, minWidth: 160 },
  { id: "abstract", label: "Abstract", defaultWidth: 360, minWidth: 200 },
  { id: "published", label: "Published", defaultWidth: 140, minWidth: 110 },
  { id: "imported", label: "Imported", defaultWidth: 180, minWidth: 140 },
  { id: "source", label: "Source", defaultWidth: 160, minWidth: 120 },
  { id: "last_opened", label: "Last Opened", defaultWidth: 128, minWidth: 108 },
  { id: "status", label: "Status", defaultWidth: 140, minWidth: 110 },
] as const;
type ColumnId = (typeof COLUMN_OPTIONS)[number]["id"];
type SortDirection = "asc" | "desc";
type SortState = { columnId: ColumnId; direction: SortDirection };
type ColumnDropTarget = {
  columnId: ColumnId;
  position: "before" | "after";
};
const DEFAULT_COLUMNS = COLUMN_OPTIONS.map((column) => column.id);
const DEFAULT_COLUMN_WIDTHS = Object.fromEntries(
  COLUMN_OPTIONS.map((column) => [column.id, column.defaultWidth]),
) as Record<ColumnId, number>;

function isColumnId(value: unknown): value is ColumnId {
  return typeof value === "string" && DEFAULT_COLUMNS.includes(value as ColumnId);
}

function normalizeColumnOrder(values: readonly ColumnId[]): ColumnId[] {
  const movableColumns = Array.from(new Set(values.filter((columnId) => columnId !== "title")));
  const missingColumns = DEFAULT_COLUMNS.filter(
    (columnId) => columnId !== "title" && !movableColumns.includes(columnId),
  );
  return ["title", ...movableColumns, ...missingColumns];
}

function formatPublishedDate(value: string | null): string {
  if (!value) return "Fetching…";
  return new Intl.DateTimeFormat("en", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(new Date(value));
}

function formatImportedDate(value: string): string {
  const includesTimeZone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
  const date = new Date(includesTimeZone ? value : `${value}Z`);
  return new Intl.DateTimeFormat("en", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date);
}

function clampColumnWidth(columnId: ColumnId, value: number): number {
  const column = COLUMN_OPTIONS.find((option) => option.id === columnId);
  return Math.max(column?.minWidth ?? 100, Math.round(value));
}

function paperSortValue(paper: Paper, columnId: ColumnId): string | null {
  if (columnId === "title") return paper.title;
  if (columnId === "authors") return paper.authors.map((author) => author.name).join(", ");
  if (columnId === "abstract") return paper.abstract;
  if (columnId === "published") return paper.submitted_at;
  if (columnId === "imported") return paper.created_at;
  if (columnId === "source") return paper.canonical_arxiv_id;
  if (columnId === "last_opened") return paper.last_opened_at;
  return paper.processing_stage;
}

function sortPapers(papers: Paper[], sort: SortState): Paper[] {
  return [...papers].sort((left, right) => {
    if (left.pinned_at && right.pinned_at) {
      const pinOrder = right.pinned_at.localeCompare(left.pinned_at);
      if (pinOrder !== 0) return pinOrder;
    }
    if (left.pinned_at) return -1;
    if (right.pinned_at) return 1;
    const leftValue = paperSortValue(left, sort.columnId);
    const rightValue = paperSortValue(right, sort.columnId);
    if (leftValue === null && rightValue === null) {
      return right.created_at.localeCompare(left.created_at);
    }
    if (leftValue === null) return 1;
    if (rightValue === null) return -1;
    const order = leftValue.localeCompare(rightValue, undefined, {
      numeric: true,
      sensitivity: "base",
    });
    if (order !== 0) return sort.direction === "asc" ? order : -order;
    return right.created_at.localeCompare(left.created_at);
  });
}

function PinIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <path d="M8.1 3.5h7.8l-1.4 5 3.1 3.1v1.7H13v6.2l-1 1.5-1-1.5v-6.2H6.4v-1.7l3.1-3.1-1.4-5Z" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <path d="M8 4.5h8l.8 1.8H20v2H4v-2h3.2L8 4.5Zm-1.7 5.2h11.4l-.7 10H7l-.7-10Zm3 2v5.8h1.8v-5.8H9.3Zm3.6 0v5.8h1.8v-5.8h-1.8Z" />
    </svg>
  );
}

function CollapsibleText({
  maxLines,
  text,
}: {
  maxLines: number;
  text: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [collapsible, setCollapsible] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    const measure = measureRef.current;
    if (!container || !measure) return;
    let cancelled = false;
    const update = () => {
      if (cancelled) return;
      const lineHeight = Number.parseFloat(window.getComputedStyle(measure).lineHeight);
      const naturalLineCount = Number.isFinite(lineHeight) && lineHeight > 0
        ? Math.max(1, Math.round(measure.scrollHeight / lineHeight))
        : 1;
      const overflows = naturalLineCount > maxLines;
      setCollapsible(overflows);
      if (!overflows) setExpanded(false);
    };
    update();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(container);
    window.addEventListener("resize", update);
    void document.fonts?.ready.then(update);
    return () => {
      cancelled = true;
      observer?.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [maxLines, text]);

  return (
    <div
      className={`collapsible-text${expanded && collapsible ? " is-expanded" : ""}`}
      ref={containerRef}
      style={{ "--library-cell-lines": maxLines } as CSSProperties}
    >
      <span className="collapsible-text-value">{text}</span>
      <span aria-hidden="true" className="collapsible-text-measure" ref={measureRef}>{text}</span>
      {collapsible ? (
        <button aria-expanded={expanded} onClick={() => setExpanded((current) => !current)} type="button">
          {expanded ? "Show less" : "Show more"}
        </button>
      ) : null}
    </div>
  );
}

function WrappedPaperTitle({
  onLineCountChange,
  paperId,
  title,
}: {
  onLineCountChange: (paperId: string, lineCount: number) => void;
  paperId: string;
  title: string;
}) {
  const titleRef = useRef<HTMLAnchorElement>(null);

  useEffect(() => {
    const titleElement = titleRef.current;
    if (!titleElement) return;
    let cancelled = false;
    const update = () => {
      if (cancelled) return;
      const lineHeight = Number.parseFloat(window.getComputedStyle(titleElement).lineHeight);
      const lineCount = Number.isFinite(lineHeight) && lineHeight > 0
        ? Math.max(1, Math.round(titleElement.scrollHeight / lineHeight))
        : 1;
      onLineCountChange(paperId, lineCount);
    };
    update();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(titleElement);
    window.addEventListener("resize", update);
    void document.fonts?.ready.then(update);
    return () => {
      cancelled = true;
      observer?.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [onLineCountChange, paperId, title]);

  return (
    <Link href={`/papers/${paperId}`} ref={titleRef}>{title}</Link>
  );
}

export default function LibraryPage() {
  const router = useRouter();
  const [papers, setPapers] = useState<Paper[]>([]);
  const [source, setSource] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteConfirmationOpen, setDeleteConfirmationOpen] = useState(false);
  const [pendingDeletePaper, setPendingDeletePaper] = useState<Paper | null>(null);
  const [pendingFavoriteRemoval, setPendingFavoriteRemoval] = useState<Paper | null>(null);
  const [favoriteOnly, setFavoriteOnly] = useState(false);
  const [paperActionBusy, setPaperActionBusy] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [visibleColumns, setVisibleColumns] = useState<Set<ColumnId>>(
    () => new Set(DEFAULT_COLUMNS),
  );
  const [columnWidths, setColumnWidths] = useState<Record<ColumnId, number>>(
    () => ({ ...DEFAULT_COLUMN_WIDTHS }),
  );
  const [columnOrder, setColumnOrder] = useState<ColumnId[]>(() => [...DEFAULT_COLUMNS]);
  const [sortState, setSortState] = useState<SortState>({
    columnId: "last_opened",
    direction: "desc",
  });
  const [relativeTimeNow, setRelativeTimeNow] = useState(() => new Date());
  const [titleLineCounts, setTitleLineCounts] = useState<Record<string, number>>({});
  const [draggedColumnId, setDraggedColumnId] = useState<ColumnId | null>(null);
  const [columnDropTarget, setColumnDropTarget] = useState<ColumnDropTarget | null>(null);
  const [displayPreferencesLoaded, setDisplayPreferencesLoaded] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [libraryError, setLibraryError] = useState<string | null>(null);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const allSelected = papers.length > 0 && selectedIds.size === papers.length;

  useEffect(() => {
    try {
      const storedColumnSchemaVersion = window.localStorage.getItem(COLUMN_SCHEMA_VERSION_KEY);
      const storedColumnSchemaRevision = Number.parseInt(storedColumnSchemaVersion ?? "0", 10);
      const stored = window.localStorage.getItem(COLUMN_STORAGE_KEY);
      if (stored !== null) {
        const parsed: unknown = JSON.parse(stored);
        if (Array.isArray(parsed)) {
          const storedColumns = parsed.filter(isColumnId);
          if (!storedColumns.includes("title")) {
            storedColumns.unshift("title");
          }
          if (
            storedColumnSchemaRevision < LAST_OPENED_COLUMN_ADDED_SCHEMA_VERSION
            && !storedColumns.includes("last_opened")
          ) {
            storedColumns.push("last_opened");
          }
          setVisibleColumns(new Set(storedColumns));
        }
      }
      const storedColumnWidths = window.localStorage.getItem(COLUMN_WIDTH_STORAGE_KEY);
      if (storedColumnWidths !== null) {
        const parsedColumnWidths: unknown = JSON.parse(storedColumnWidths);
        if (
          typeof parsedColumnWidths === "object"
          && parsedColumnWidths !== null
          && !Array.isArray(parsedColumnWidths)
        ) {
          const storedWidths = parsedColumnWidths as Record<string, unknown>;
          setColumnWidths((current) => {
            const next = { ...current };
            for (const column of COLUMN_OPTIONS) {
              const width = storedWidths[column.id];
              if (typeof width === "number" && Number.isFinite(width)) {
                if (
                  column.id === "last_opened"
                  && storedColumnSchemaRevision < LAST_OPENED_COMPACT_WIDTH_SCHEMA_VERSION
                ) {
                  if (width <= LEGACY_LAST_OPENED_MIN_WIDTH) {
                    next[column.id] = column.minWidth;
                    continue;
                  }
                  if (width === LEGACY_LAST_OPENED_DEFAULT_WIDTH) {
                    next[column.id] = column.defaultWidth;
                    continue;
                  }
                }
                next[column.id] = clampColumnWidth(column.id, width);
              }
            }
            return next;
          });
        }
      }
      const storedColumnOrder = window.localStorage.getItem(COLUMN_ORDER_STORAGE_KEY);
      if (storedColumnOrder !== null) {
        const parsedColumnOrder: unknown = JSON.parse(storedColumnOrder);
        if (Array.isArray(parsedColumnOrder)) {
          setColumnOrder(normalizeColumnOrder(parsedColumnOrder.filter(isColumnId)));
        }
      }
    } catch {
      // Column preferences are optional; use defaults when storage is unavailable.
    } finally {
      setDisplayPreferencesLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (!displayPreferencesLoaded) return;
    try {
      window.localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify(Array.from(visibleColumns)));
      window.localStorage.setItem(COLUMN_SCHEMA_VERSION_KEY, CURRENT_COLUMN_SCHEMA_VERSION);
      window.localStorage.setItem(COLUMN_WIDTH_STORAGE_KEY, JSON.stringify(columnWidths));
      window.localStorage.setItem(COLUMN_ORDER_STORAGE_KEY, JSON.stringify(columnOrder));
    } catch {
      // The table still works when private browsing blocks local storage.
    }
  }, [columnOrder, columnWidths, displayPreferencesLoaded, visibleColumns]);

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = selectedIds.size > 0 && !allSelected;
    }
  }, [allSelected, selectedIds.size]);

  useEffect(() => {
    const timer = window.setInterval(() => setRelativeTimeNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const refresh = useCallback(async () => {
    try {
      setPapers(await listPapers(favoriteOnly));
      setLibraryError(null);
    } catch (reason) {
      setLibraryError(reason instanceof Error ? reason.message : "Unable to load the paper library.");
    } finally {
      setLoading(false);
    }
  }, [favoriteOnly]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!source.trim()) return;
    setSubmitting(true);
    setImportError(null);
    try {
      const result = await importPaper(source);
      router.push(`/papers/${result.paper.id}`);
    } catch (reason) {
      setImportError(reason instanceof Error ? reason.message : "Import failed.");
      setSubmitting(false);
    }
  }

  function togglePaper(paperId: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(paperId)) next.delete(paperId);
      else next.add(paperId);
      return next;
    });
  }

  function toggleColumn(columnId: ColumnId) {
    if (columnId === "title") return;
    setVisibleColumns((current) => {
      const next = new Set(current);
      if (next.has(columnId)) next.delete(columnId);
      else next.add(columnId);
      return next;
    });
  }

  function setColumnWidth(columnId: ColumnId, width: number) {
    setColumnWidths((current) => ({
      ...current,
      [columnId]: clampColumnWidth(columnId, width),
    }));
  }

  function startColumnResize(
    columnId: ColumnId,
    event: ReactPointerEvent<HTMLButtonElement>,
  ) {
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startWidth = columnWidths[columnId];
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);

    const onPointerMove = (moveEvent: PointerEvent) => {
      setColumnWidth(columnId, startWidth + moveEvent.clientX - startX);
    };
    const stopResizing = () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", stopResizing);
      window.removeEventListener("pointercancel", stopResizing);
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stopResizing);
    window.addEventListener("pointercancel", stopResizing);
  }

  function resizeColumnWithKeyboard(
    columnId: ColumnId,
    event: ReactKeyboardEvent<HTMLButtonElement>,
  ) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    setColumnWidth(columnId, columnWidths[columnId] + (event.key === "ArrowRight" ? 20 : -20));
  }

  function resetColumnWidths() {
    setColumnWidths({ ...DEFAULT_COLUMN_WIDTHS });
  }

  function startColumnReorder(
    columnId: ColumnId,
    event: ReactPointerEvent<HTMLElement>,
  ) {
    if (columnId === "title" || event.button !== 0) return;
    event.preventDefault();
    const startX = event.clientX;
    const startY = event.clientY;
    const captureTarget = event.currentTarget;
    const header = captureTarget.closest<HTMLTableCellElement>("th[data-column-id]");
    if (header === null) return;
    let isDragging = false;
    let latestDropTarget: ColumnDropTarget | null = null;
    captureTarget.setPointerCapture(event.pointerId);

    const onPointerMove = (moveEvent: PointerEvent) => {
      if (!isDragging && Math.hypot(moveEvent.clientX - startX, moveEvent.clientY - startY) < 5) {
        return;
      }
      if (!isDragging) {
        isDragging = true;
        setDraggedColumnId(columnId);
      }
      const targetHeader = document
        .elementFromPoint(moveEvent.clientX, moveEvent.clientY)
        ?.closest<HTMLTableCellElement>("th[data-column-id]");
      const targetColumnId = targetHeader?.dataset.columnId;
      if (
        !targetHeader
        || !isColumnId(targetColumnId)
        || targetColumnId === "title"
        || targetColumnId === columnId
      ) {
        latestDropTarget = null;
        setColumnDropTarget(null);
        return;
      }
      const bounds = targetHeader.getBoundingClientRect();
      latestDropTarget = {
        columnId: targetColumnId,
        position: moveEvent.clientX < bounds.left + bounds.width / 2 ? "before" : "after",
      };
      setColumnDropTarget(latestDropTarget);
    };

    const cleanUp = () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", finishReordering);
      window.removeEventListener("pointercancel", cancelReordering);
      setDraggedColumnId(null);
      setColumnDropTarget(null);
    };
    const finishReordering = () => {
      const dropTarget = latestDropTarget;
      if (isDragging && dropTarget !== null) {
        setColumnOrder((current) => {
          const next = current.filter((item) => item !== columnId && item !== "title");
          const targetIndex = next.indexOf(dropTarget.columnId);
          const insertionIndex = targetIndex + (dropTarget.position === "after" ? 1 : 0);
          next.splice(insertionIndex, 0, columnId);
          return ["title", ...next];
        });
      }
      cleanUp();
    };
    const cancelReordering = () => cleanUp();
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", finishReordering);
    window.addEventListener("pointercancel", cancelReordering);
  }

  function resetColumnOrder() {
    setColumnOrder(normalizeColumnOrder(DEFAULT_COLUMNS));
  }

  function resetDisplaySettings() {
    setVisibleColumns(new Set(DEFAULT_COLUMNS));
    resetColumnWidths();
    resetColumnOrder();
  }

  async function removeSelected() {
    if (!selectedIds.size || deleting) return;
    setDeleting(true);
    setLibraryError(null);
    try {
      const result = await deletePapers(Array.from(selectedIds));
      const deleted = new Set(result.deleted_ids);
      setPapers((current) => current.filter((paper) => !deleted.has(paper.id)));
      setSelectedIds(new Set());
      setDeleteConfirmationOpen(false);
    } catch (reason) {
      setLibraryError(reason instanceof Error ? reason.message : "Unable to delete the selected papers.");
      setDeleteConfirmationOpen(false);
    } finally {
      setDeleting(false);
    }
  }

  async function setPaperFavorite(paper: Paper, favorite: boolean) {
    const busyKey = `favorite:${paper.id}`;
    setPaperActionBusy(busyKey);
    setLibraryError(null);
    try {
      const updated = await updatePaperPreferences(paper.id, { is_favorite: favorite });
      setPapers((current) => favoriteOnly && !updated.is_favorite
        ? current.filter((item) => item.id !== updated.id)
        : current.map((item) => item.id === updated.id ? updated : item));
      setSelectedIds((current) => {
        if (favorite || !favoriteOnly || !current.has(paper.id)) return current;
        const next = new Set(current);
        next.delete(paper.id);
        return next;
      });
      setPendingFavoriteRemoval(null);
    } catch (reason) {
      setLibraryError(reason instanceof Error ? reason.message : "Unable to update favorite status.");
    } finally {
      setPaperActionBusy(null);
    }
  }

  async function togglePaperFavorite(paper: Paper) {
    if (favoriteOnly && paper.is_favorite) {
      setPendingFavoriteRemoval(paper);
      return;
    }
    await setPaperFavorite(paper, !paper.is_favorite);
  }

  async function togglePaperPin(paper: Paper) {
    const busyKey = `pin:${paper.id}`;
    setPaperActionBusy(busyKey);
    setLibraryError(null);
    try {
      const updated = await updatePaperPreferences(paper.id, { is_pinned: !paper.pinned_at });
      setPapers((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setLibraryError(reason instanceof Error ? reason.message : "Unable to update pin status.");
    } finally {
      setPaperActionBusy(null);
    }
  }

  async function removeOnePaper() {
    if (!pendingDeletePaper || deleting) return;
    setDeleting(true);
    setLibraryError(null);
    try {
      const result = await deletePapers([pendingDeletePaper.id]);
      const deleted = new Set(result.deleted_ids);
      setPapers((current) => current.filter((paper) => !deleted.has(paper.id)));
      setSelectedIds((current) => {
        const next = new Set(current);
        deleted.forEach((id) => next.delete(id));
        return next;
      });
      setPendingDeletePaper(null);
    } catch (reason) {
      setLibraryError(reason instanceof Error ? reason.message : "Unable to delete this paper.");
    } finally {
      setDeleting(false);
    }
  }

  const visibleColumnOptions = normalizeColumnOrder(columnOrder)
    .map((columnId) => COLUMN_OPTIONS.find((column) => column.id === columnId))
    .filter((column): column is (typeof COLUMN_OPTIONS)[number] => (
      column !== undefined && visibleColumns.has(column.id)
    ));
  const tableWidth = 46 + ACTIONS_COLUMN_WIDTH + visibleColumnOptions.reduce(
    (total, column) => total + columnWidths[column.id],
    0,
  );
  const sortedPapers = useMemo(() => sortPapers(papers, sortState), [papers, sortState]);
  const updateTitleLineCount = useCallback((paperId: string, lineCount: number) => {
    setTitleLineCounts((current) => (
      current[paperId] === lineCount ? current : { ...current, [paperId]: lineCount }
    ));
  }, []);

  function toggleSort(columnId: ColumnId) {
    setSortState((current) => {
      if (current.columnId === columnId) {
        return { columnId, direction: current.direction === "asc" ? "desc" : "asc" };
      }
      const descendingByDefault = columnId === "published"
        || columnId === "imported"
        || columnId === "last_opened";
      return { columnId, direction: descendingByDefault ? "desc" : "asc" };
    });
  }

  function renderPaperCell(paper: Paper, columnId: ColumnId) {
    const rowLineCount = titleLineCounts[paper.id] ?? 1;
    if (columnId === "title") {
      return (
        <td className="title-cell" key={columnId}>
          <WrappedPaperTitle
            onLineCountChange={updateTitleLineCount}
            paperId={paper.id}
            title={paper.title}
          />
        </td>
      );
    }
    if (columnId === "authors") {
      return (
        <td className="authors-cell" key={columnId}>
          <CollapsibleText
            maxLines={rowLineCount}
            text={paper.authors.length ? paper.authors.map((author) => author.name).join(", ") : "Fetching authors…"}
          />
        </td>
      );
    }
    if (columnId === "abstract") {
      return (
        <td className="abstract-cell" key={columnId}>
          <CollapsibleText
            maxLines={rowLineCount}
            text={paper.abstract ?? "Fetching abstract…"}
          />
        </td>
      );
    }
    if (columnId === "published") {
      return (
        <td className="date-cell" key={columnId}>
          <span className="library-row-preview">{formatPublishedDate(paper.submitted_at)}</span>
        </td>
      );
    }
    if (columnId === "imported") {
      return (
        <td className="date-cell" key={columnId}>
          <span className="library-row-preview">{formatImportedDate(paper.created_at)}</span>
        </td>
      );
    }
    if (columnId === "source") {
      return (
        <td key={columnId}>
          <a className="arxiv-link library-row-preview" href={`https://arxiv.org/abs/${paper.canonical_arxiv_id}`} rel="noreferrer" target="_blank">
            arXiv:{paper.canonical_arxiv_id}
          </a>
        </td>
      );
    }
    if (columnId === "last_opened") {
      return (
        <td className="date-cell last-opened-cell" key={columnId}>
          {paper.last_opened_at ? (
            <time className="library-row-preview" dateTime={paper.last_opened_at} title={formatImportedDate(paper.last_opened_at)}>
              {formatLastOpened(paper.last_opened_at, relativeTimeNow)}
            </time>
          ) : <span className="library-row-preview">Never</span>}
        </td>
      );
    }
    return (
      <td key={columnId}>
        <span className={`health health-${paper.processing_health.toLowerCase()}`}>
          {paper.processing_stage.replaceAll("_", " ")}
        </span>
      </td>
    );
  }

  return (
    <div className="page-grid library-page-grid">
      <section className="hero-card">
        <div>
          <p className="eyebrow">Personal research library</p>
          <h1>把阅读过程，沉淀为可持续的研究资产。</h1>
          <p className="hero-copy">
            导入 arXiv 论文，在保留原始结构的中英对照页面中阅读、提问与批注。
          </p>
        </div>
        <form className="import-form" aria-label="导入 arXiv 论文" onSubmit={submit}>
          <label htmlFor="arxiv-source">arXiv URL or ID</label>
          <div className="input-row">
            <input
              id="arxiv-source"
              name="source"
              placeholder="https://arxiv.org/abs/1706.03762"
              value={source}
              onChange={(event) => setSource(event.target.value)}
              disabled={submitting}
              autoComplete="off"
            />
            <button disabled={submitting || !source.trim()} type="submit">
              {submitting ? "Importing…" : "Import"}
            </button>
          </div>
          <small>支持 arXiv ID、abstract、PDF 和 source URL；重复导入会打开已有论文。</small>
          {importError ? <p className="form-error" role="alert">{importError}</p> : null}
        </form>
      </section>

      <section className="section-heading">
        <div>
          <p className="eyebrow">Workspace</p>
          <h2>Your papers</h2>
        </div>
        <div className="library-actions">
          {selectedIds.size > 0 ? (
            <button className="delete-papers-button" disabled={deleting} onClick={() => setDeleteConfirmationOpen(true)} type="button">
              {deleting ? "Deleting…" : `Delete selected (${selectedIds.size})`}
            </button>
          ) : null}
          <button
            aria-label={favoriteOnly ? "Show all papers" : "Show favorite papers only"}
            aria-pressed={favoriteOnly}
            className={`favorite-filter-button${favoriteOnly ? " favorite-filter-button-active" : ""}`}
            onClick={() => {
              setSelectedIds(new Set());
              setFavoriteOnly((current) => !current);
            }}
            title={favoriteOnly ? "显示全部论文" : "只显示收藏"}
            type="button"
          >
            <span aria-hidden="true">★</span>
          </button>
          <details className="display-settings">
            <summary>Display settings</summary>
            <div className="display-settings-panel">
              <div className="display-settings-heading">
                <div>
                  <strong>Display settings</strong>
                  <small>Customize the paper list</small>
                </div>
                <button onClick={resetDisplaySettings} type="button">Reset</button>
              </div>
              <section className="display-settings-section">
                <div className="settings-section-heading">
                  <div>
                    <strong>Columns</strong>
                    <small>{visibleColumns.size} of {COLUMN_OPTIONS.length} shown</small>
                  </div>
                  <button onClick={() => setVisibleColumns(new Set(DEFAULT_COLUMNS))} type="button">Show all</button>
                </div>
                <div className="column-options">
                  {COLUMN_OPTIONS.map((column) => (
                    <label
                      className={column.id === "title" ? "column-option-locked" : undefined}
                      key={column.id}
                    >
                      <input
                        checked={column.id === "title" || visibleColumns.has(column.id)}
                        disabled={column.id === "title"}
                        onChange={() => toggleColumn(column.id)}
                        type="checkbox"
                      />
                      <span>{column.label}</span>
                      {column.id === "title" ? <small>Fixed</small> : null}
                    </label>
                  ))}
                </div>
                <button className="reset-column-widths" onClick={resetColumnWidths} type="button">
                  Reset column widths
                </button>
                <button className="reset-column-widths" onClick={resetColumnOrder} type="button">
                  Reset column order
                </button>
              </section>
            </div>
          </details>
          <span className="status-pill"><i /> {loading ? "Loading" : `${papers.length} papers`}</span>
        </div>
      </section>

      {libraryError ? <p className="library-error" role="alert">{libraryError}</p> : null}

      {!loading && papers.length === 0 ? (
        <section className="empty-library">
          <div className="empty-symbol" aria-hidden="true">{favoriteOnly ? "☆" : "⌁"}</div>
          <h3>{favoriteOnly ? "还没有收藏的论文" : "Library is ready for its first paper"}</h3>
          <p>
            {favoriteOnly
              ? "点击上方实心星标返回全部论文，再从 Actions 中添加收藏。"
              : "粘贴一个 arXiv 地址，metadata 和源码会在后台安全下载。"}
          </p>
        </section>
      ) : (
        <section className="paper-table-shell" aria-live="polite">
          <table className="paper-table" style={{ minWidth: "100%", width: `${tableWidth}px` }}>
            <colgroup>
              <col style={{ width: "46px" }} />
              {visibleColumnOptions.map((column) => (
                <col key={column.id} style={{ width: `${columnWidths[column.id]}px` }} />
              ))}
              <col style={{ width: `${ACTIONS_COLUMN_WIDTH}px` }} />
            </colgroup>
            <thead>
              <tr>
                <th className="selection-column" scope="col">
                  <input
                    aria-label="Select all papers"
                    checked={allSelected}
                    onChange={() => setSelectedIds(allSelected ? new Set() : new Set(papers.map((paper) => paper.id)))}
                    ref={selectAllRef}
                    type="checkbox"
                  />
                </th>
                {visibleColumnOptions.map((column) => (
                  <th
                    aria-sort={sortState.columnId === column.id
                      ? (sortState.direction === "asc" ? "ascending" : "descending")
                      : "none"}
                    className={[
                      `${column.id}-column`,
                      column.id === "title" ? "locked-column-header" : "resizable-column-header",
                      draggedColumnId === column.id ? "is-dragging" : "",
                      columnDropTarget?.columnId === column.id
                        ? `is-drop-target drop-${columnDropTarget.position}`
                        : "",
                    ].filter(Boolean).join(" ")}
                    data-column-id={column.id}
                    key={column.id}
                    onPointerDown={column.id === "title"
                      ? undefined
                      : (event) => startColumnReorder(column.id, event)}
                    scope="col"
                    title={column.id === "title"
                      ? "Title is fixed as the first column; drag its right edge to resize"
                      : `Drag ${column.label} to reorder`}
                  >
                    <button
                      className="column-sort-button"
                      onClick={() => toggleSort(column.id)}
                      onPointerDown={(event) => event.stopPropagation()}
                      title={`Sort by ${column.label}`}
                      type="button"
                    >
                      <span>{column.label}</span>
                      <span
                        aria-hidden="true"
                        className={`column-sort-indicator${sortState.columnId === column.id ? " is-active" : ""}`}
                      >
                        {sortState.columnId === column.id
                          ? (sortState.direction === "asc" ? "↑" : "↓")
                          : "↕"}
                      </span>
                    </button>
                    {column.id === "title" ? null : (
                      <button
                        aria-label={`Drag ${column.label} to reorder`}
                        className="column-drag-handle"
                        onClick={(event) => event.preventDefault()}
                        onPointerDown={(event) => {
                          event.stopPropagation();
                          startColumnReorder(column.id, event);
                        }}
                        title={`Drag ${column.label} to reorder`}
                        type="button"
                      >
                        <span aria-hidden="true">⠿</span>
                      </button>
                    )}
                    <button
                      aria-label={`Resize ${column.label} column`}
                      className="column-resize-handle"
                      onDoubleClick={() => setColumnWidth(column.id, column.defaultWidth)}
                      onKeyDown={(event) => resizeColumnWithKeyboard(column.id, event)}
                      onPointerDown={(event) => startColumnResize(column.id, event)}
                      title={`Drag to resize ${column.label}; double-click to reset`}
                      type="button"
                    />
                  </th>
                ))}
                <th className="actions-column" scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sortedPapers.map((paper) => (
                <tr
                  className={selectedIds.has(paper.id) ? "selected" : undefined}
                  key={paper.id}
                  style={{ "--library-row-lines": titleLineCounts[paper.id] ?? 1 } as CSSProperties}
                >
                  <td className="selection-column">
                    <input
                      aria-label={`Select ${paper.title}`}
                      checked={selectedIds.has(paper.id)}
                      onChange={() => togglePaper(paper.id)}
                      type="checkbox"
                    />
                  </td>
                  {visibleColumnOptions.map((column) => renderPaperCell(paper, column.id))}
                  <td className="paper-actions-cell">
                    <div className="paper-action-buttons">
                      <button
                        aria-label={paper.is_favorite ? `Remove ${paper.title} from favorites` : `Add ${paper.title} to favorites`}
                        aria-pressed={paper.is_favorite}
                        className={`paper-action-button paper-favorite-action${paper.is_favorite ? " is-active" : ""}`}
                        disabled={paperActionBusy !== null}
                        onClick={() => void togglePaperFavorite(paper)}
                        title={paper.is_favorite ? "取消收藏" : "收藏"}
                        type="button"
                      ><span aria-hidden="true">{paper.is_favorite ? "★" : "☆"}</span></button>
                      <button
                        aria-label={paper.pinned_at ? `Unpin ${paper.title}` : `Pin ${paper.title}`}
                        aria-pressed={Boolean(paper.pinned_at)}
                        className={`paper-action-button paper-pin-action${paper.pinned_at ? " is-active" : ""}`}
                        disabled={paperActionBusy !== null}
                        onClick={() => void togglePaperPin(paper)}
                        title={paper.pinned_at ? "取消置顶" : "置顶"}
                        type="button"
                      ><PinIcon /></button>
                      <button
                        aria-label={`Delete ${paper.title}`}
                        className="paper-action-button paper-delete-action"
                        disabled={deleting}
                        onClick={() => setPendingDeletePaper(paper)}
                        title="删除"
                        type="button"
                      ><TrashIcon /></button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {deleteConfirmationOpen ? (
        <div className="dialog-backdrop">
          <section aria-labelledby="delete-dialog-title" aria-modal="true" className="delete-dialog" role="dialog">
            <p className="eyebrow">Permanent action</p>
            <h2 id="delete-dialog-title">删除选中的 {selectedIds.size} 篇论文？</h2>
            <p>已下载的源码、处理任务和相关数据也会一并删除。此操作无法撤销。</p>
            <div className="dialog-actions">
              <button className="dialog-cancel" disabled={deleting} onClick={() => setDeleteConfirmationOpen(false)} type="button">
                Cancel
              </button>
              <button className="dialog-confirm" disabled={deleting} onClick={removeSelected} type="button">
                {deleting ? "Deleting…" : "Permanently delete"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
      {pendingDeletePaper ? (
        <div className="dialog-backdrop">
          <section aria-labelledby="delete-paper-dialog-title" aria-modal="true" className="delete-dialog" role="dialog">
            <p className="eyebrow">Permanent action</p>
            <h2 id="delete-paper-dialog-title">删除这篇论文？</h2>
            <p>“{pendingDeletePaper.title}”的源码、处理任务和相关数据也会一并删除。此操作无法撤销。</p>
            <div className="dialog-actions">
              <button className="dialog-cancel" disabled={deleting} onClick={() => setPendingDeletePaper(null)} type="button">Cancel</button>
              <button className="dialog-confirm" disabled={deleting} onClick={() => void removeOnePaper()} type="button">
                {deleting ? "Deleting…" : "Permanently delete"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
      {pendingFavoriteRemoval ? (
        <div className="dialog-backdrop">
          <section aria-labelledby="remove-favorite-dialog-title" aria-modal="true" className="delete-dialog" role="dialog">
            <p className="eyebrow">Favorites filter</p>
            <h2 id="remove-favorite-dialog-title">取消收藏并移出当前列表？</h2>
            <p>“{pendingFavoriteRemoval.title}”会立即从“只显示收藏”列表中移除，但论文数据不会被删除。</p>
            <div className="dialog-actions">
              <button className="dialog-cancel" disabled={paperActionBusy !== null} onClick={() => setPendingFavoriteRemoval(null)} type="button">Cancel</button>
              <button className="dialog-confirm" disabled={paperActionBusy !== null} onClick={() => void setPaperFavorite(pendingFavoriteRemoval, false)} type="button">
                {paperActionBusy ? "Updating…" : "Remove favorite"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
