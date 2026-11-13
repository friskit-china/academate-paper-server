import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const reader = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const annotations = await readFile(
  new URL("../app/papers/[paperId]/annotation-workspace.tsx", import.meta.url),
  "utf8",
);
const library = await readFile(new URL("../app/library/page.tsx", import.meta.url), "utf8");
const api = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");

test("reader sidebars resize, collapse independently, and expose edge recovery controls", () => {
  assert.match(reader, /startSidebarResize\("left"/);
  assert.match(reader, /startSidebarResize\("right"/);
  assert.match(reader, /reader-left-collapsed/);
  assert.match(reader, /reader-right-collapsed/);
  assert.match(reader, /reader-sidebar-edge-left/);
  assert.match(reader, /reader-sidebar-edge-right/);
  assert.match(styles, /--reader-left-width/);
  assert.match(styles, /@media \(min-width: 821px\) and \(max-width: 1180px\)/);
});

test("single-column readers use a dismissing mobile outline sheet", () => {
  assert.match(reader, /className="mobile-outline-trigger"/);
  assert.match(reader, /className="mobile-outline-sheet"/);
  assert.match(reader, /setMobileOutlineOpen\(false\)/);
  assert.match(styles, /@media \(max-width: 820px\)/);
});

test("outline scroll position stays visible and follows the active section", () => {
  assert.match(reader, /function OutlineScrollRegion/);
  assert.match(reader, /data-outline-scroll-container/);
  assert.match(reader, /className={`outline-scroll-rail/);
  assert.match(reader, /container\.scrollBy\(/);
  assert.doesNotMatch(reader, /outlinePanelRef\.current/);
  assert.match(styles, /\.outline-scroll-rail-visible \{ opacity: 1/);
  assert.match(styles, /scrollbar-color: #7f9a88 #e7ece8/);
});

test("block bookmarks are persistent and appear beside blocks and below the outline", () => {
  assert.match(api, /getBlockBookmarks/);
  assert.match(api, /addBlockBookmark/);
  assert.match(api, /removeBlockBookmark/);
  assert.match(reader, /className="reader-bookmarks"/);
  assert.match(reader, /className={`block-bookmark-button/);
  assert.match(styles, /\.reader-bookmark-card/);
});

test("annotation activity supports hover previews and Word-like margin cards", () => {
  assert.match(annotations, /handleAnnotationHover/);
  assert.match(annotations, /annotation-margin-card/);
  assert.match(reader, /AnnotationHoverPreview/);
  assert.match(styles, /\.annotation-margin-card-hovered/);
});

test("library exposes favorite, pin, delete actions and favorite-only confirmation", () => {
  assert.match(api, /updatePaperPreferences/);
  assert.match(library, /className="actions-column"/);
  assert.match(library, /paper-favorite-action/);
  assert.match(library, /paper-pin-action/);
  assert.match(library, /paper-delete-action/);
  assert.match(library, /pendingFavoriteRemoval/);
  assert.match(library, /favorite-filter-button/);
});

test("library keeps title and actions frozen while rows stay compact", () => {
  assert.match(library, /normalizeColumnOrder/);
  assert.match(library, /disabled={column.id === "title"}/);
  assert.match(library, /column.id === "title" \? "locked-column-header"/);
  assert.match(styles, /\.paper-table \.title-column, \.paper-table \.title-cell[^}]*position: sticky/);
  assert.match(styles, /\.paper-table \.actions-column, \.paper-table \.paper-actions-cell[^}]*position: sticky/);
  assert.match(styles, /\.paper-table \.title-column::after, \.paper-table \.title-cell::after/);
  assert.match(styles, /\.paper-table \.actions-column::before, \.paper-table \.paper-actions-cell::before/);
  assert.match(styles, /background: var\(--frozen-divider\)/);
  assert.match(styles, /\.collapsible-text \{[^}]*display: flex/);
  assert.match(styles, /\.collapsible-text button \{[^}]*margin: 0/);
});

test("library action buttons cannot be ellipsized and wide screens use the viewport", () => {
  assert.match(library, /className="page-grid library-page-grid"/);
  assert.match(library, /className="paper-action-buttons"/);
  assert.match(styles, /\.paper-action-buttons \{[^}]*display: flex/);
  assert.match(styles, /\.paper-table \.paper-actions-cell \{[^}]*text-overflow: clip/);
  assert.match(styles, /@media \(min-width: 1440px\)[\s\S]*\.library-page-grid[\s\S]*max-width: none/);
});

test("library previews follow rendered column width and data columns are sortable", () => {
  assert.match(library, /new ResizeObserver\(update\)/);
  assert.match(library, /className="collapsible-text-measure"/);
  assert.doesNotMatch(library, /Preview character limit/);
  assert.match(library, /columnId: "last_opened"/);
  assert.match(library, /id: "last_opened", label: "Last Opened"/);
  assert.match(library, /id: "last_opened", label: "Last Opened", defaultWidth: 128, minWidth: 108/);
  assert.match(library, /CURRENT_COLUMN_SCHEMA_VERSION = "3"/);
  assert.match(library, /className="date-cell last-opened-cell"/);
  assert.match(styles, /\.paper-table \.last_opened-column \{[^}]*padding-left: 6px;[^}]*padding-right: 6px/);
  assert.match(styles, /\.paper-table \.last-opened-cell \{[^}]*padding-left: 7px;[^}]*padding-right: 7px/);
  assert.match(library, /column.id === "title" \? "locked-column-header" : "resizable-column-header"/);
  assert.match(library, /aria-label={`Drag \${column.label} to reorder`}/);
  assert.match(library, /className="column-drag-handle"/);
  assert.match(library, /aria-label={`Resize \${column.label} column`}/);
  assert.match(library, /formatLastOpened\(paper.last_opened_at, relativeTimeNow\)/);
  assert.match(library, /setInterval\(\(\) => setRelativeTimeNow\(new Date\(\)\), 60_000\)/);
  assert.match(library, /className="column-sort-button"/);
  assert.match(library, /aria-sort=/);
  assert.match(api, /markPaperOpened/);
  assert.match(reader, /markPaperOpened\(paperId\)/);
});

test("library title wraps and drives the preview line budget for its row", () => {
  assert.match(library, /function WrappedPaperTitle/);
  assert.match(library, /titleElement\.scrollHeight \/ lineHeight/);
  assert.match(library, /const \[titleLineCounts, setTitleLineCounts\]/);
  assert.match(library, /maxLines={rowLineCount}/);
  assert.match(library, /--library-row-lines/);
  assert.match(styles, /\.title-cell a \{[^}]*overflow-wrap: anywhere;[^}]*text-overflow: clip;[^}]*white-space: normal/);
  assert.match(styles, /\.collapsible-text-value \{[^}]*line-clamp: var\(--library-cell-lines, 1\)/);
  assert.match(styles, /\.library-row-preview \{[^}]*line-clamp: var\(--library-row-lines, 1\)/);
  assert.doesNotMatch(styles, /\.title-cell a \{[^}]*text-overflow: ellipsis/);
});
