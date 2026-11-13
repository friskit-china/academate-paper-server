export const PAPER_BLOCK_JUMP_EVENT = "academatepaper:paper-block-jump";
export const PAPER_READING_POSITION_PARAM = "block";

const PAPER_BLOCK_ELEMENT_PREFIX = "block-";
const PAPER_POSITION_HASH = /^#(?:block|bibliography-entry)-[A-Za-z0-9_-]+$/;
const PAPER_READING_POSITION_STORAGE_PREFIX = "paper-reader-ui:v1:reading-position:";

export type PaperBlockJumpOrigin = {
  top: number;
  url: string;
};

export type PaperViewportAnchor = {
  blockId: string;
  previousBlockId: string | null;
  nextBlockId: string | null;
  focusLine: number;
  focusRatio: number;
};

type PaperBlockJumpOptions = {
  block?: ScrollLogicalPosition;
  updateHash?: boolean;
};

function relativePaperUrl(url: URL): string {
  return `${url.pathname}${url.search}${url.hash}`;
}

function replacePaperUrl(url: URL): void {
  window.history.replaceState(window.history.state, "", relativePaperUrl(url));
}

function validReadingBlockId(value: string | null): value is string {
  return value !== null && /^[A-Za-z0-9_-]+$/.test(value);
}

function paperReadingPositionStorageKey(): string {
  return `${PAPER_READING_POSITION_STORAGE_PREFIX}${window.location.pathname}`;
}

function persistPaperReadingPosition(blockId: string): void {
  try {
    window.sessionStorage.setItem(paperReadingPositionStorageKey(), blockId);
  } catch {
    // URL synchronization remains authoritative when browser storage is unavailable.
  }
}

function storedPaperReadingPosition(): string | null {
  try {
    const blockId = window.sessionStorage.getItem(paperReadingPositionStorageKey());
    return validReadingBlockId(blockId) ? blockId : null;
  } catch {
    return null;
  }
}

function removeStoredPaperReadingPosition(): void {
  try {
    window.sessionStorage.removeItem(paperReadingPositionStorageKey());
  } catch {
    // Clearing the visible URL is still useful when browser storage is unavailable.
  }
}

/** True only for a full reload whose original document URL belongs to this paper route. */
export function paperNavigationWasReloaded(): boolean {
  const navigation = window.performance.getEntriesByType("navigation")[0] as
    | PerformanceNavigationTiming
    | undefined;
  if (navigation?.type !== "reload") return false;
  try {
    return new URL(navigation.name, window.location.href).pathname === window.location.pathname;
  } catch {
    return false;
  }
}

export function paperReadingBlockElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>("[data-reading-block-id]"));
}

function paperReadingFocusLine(): number {
  return Math.min(220, Math.max(112, window.innerHeight * 0.28));
}

/**
 * Capture the block under the reader's focus line, including the relative
 * position inside that block. The ratio lets a bilingual block map back to
 * the same neighbourhood after its translation panel is added or removed.
 */
export function capturePaperViewportAnchor(
  candidates: readonly HTMLElement[],
): PaperViewportAnchor | null {
  const focusLine = paperReadingFocusLine();
  let selectedIndex = -1;
  let selectedDistance = Number.POSITIVE_INFINITY;
  for (const [index, candidate] of candidates.entries()) {
    const rect = candidate.getBoundingClientRect();
    if (rect.bottom <= 0 || rect.top >= window.innerHeight) continue;
    const distance = focusLine < rect.top
      ? rect.top - focusLine
      : focusLine > rect.bottom
        ? focusLine - rect.bottom
        : 0;
    if (distance < selectedDistance) {
      selectedIndex = index;
      selectedDistance = distance;
    }
    if (distance === 0) break;
  }
  if (selectedIndex < 0) return null;

  const selected = candidates[selectedIndex];
  const blockId = selected.dataset.readingBlockId ?? null;
  if (!validReadingBlockId(blockId)) return null;
  const rect = selected.getBoundingClientRect();
  const focusRatio = Math.min(1, Math.max(0, (focusLine - rect.top) / Math.max(1, rect.height)));
  const adjacentBlockId = (index: number): string | null => {
    const value = candidates[index]?.dataset.readingBlockId ?? null;
    return validReadingBlockId(value) ? value : null;
  };
  return {
    blockId,
    previousBlockId: adjacentBlockId(selectedIndex - 1),
    nextBlockId: adjacentBlockId(selectedIndex + 1),
    focusLine,
    focusRatio,
  };
}

/** Restore a captured reading focus without creating a semantic jump origin. */
export function restorePaperViewportAnchor(
  candidates: readonly HTMLElement[],
  anchor: PaperViewportAnchor,
): string | null {
  const byBlockId = new Map(
    candidates.map((candidate) => [candidate.dataset.readingBlockId, candidate]),
  );
  const target = byBlockId.get(anchor.blockId)
    ?? (anchor.previousBlockId ? byBlockId.get(anchor.previousBlockId) : undefined)
    ?? (anchor.nextBlockId ? byBlockId.get(anchor.nextBlockId) : undefined);
  const restoredBlockId = target?.dataset.readingBlockId ?? null;
  if (!target || !validReadingBlockId(restoredBlockId)) return null;

  const rect = target.getBoundingClientRect();
  const focusLine = Math.min(window.innerHeight - 1, Math.max(0, anchor.focusLine));
  const focusRatio = Math.min(1, Math.max(0, anchor.focusRatio));
  const targetTop = window.scrollY + rect.top + rect.height * focusRatio - focusLine;
  const maximumTop = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
  const previousScrollBehavior = document.documentElement.style.scrollBehavior;
  document.documentElement.style.scrollBehavior = "auto";
  window.scrollTo({ behavior: "auto", top: Math.min(maximumTop, Math.max(0, targetTop)) });
  document.documentElement.style.scrollBehavior = previousScrollBehavior;
  return restoredBlockId;
}

/** Return the rendered block currently crossing the reader's upper focus line. */
export function currentVisiblePaperBlockId(candidates: readonly HTMLElement[]): string | null {
  if (!candidates.length) return null;
  const readingLine = paperReadingFocusLine();
  let low = 0;
  let high = candidates.length - 1;
  let precedingIndex = -1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (candidates[middle].getBoundingClientRect().top <= readingLine) {
      precedingIndex = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  const possibleIndexes = precedingIndex >= 0
    ? [precedingIndex, precedingIndex + 1]
    : [0];
  for (const index of possibleIndexes) {
    const candidate = candidates[index];
    if (!candidate) continue;
    const rect = candidate.getBoundingClientRect();
    if (rect.bottom <= 0 || rect.top >= window.innerHeight) continue;
    const blockId = candidate.dataset.readingBlockId;
    if (blockId !== undefined && validReadingBlockId(blockId)) return blockId;
  }
  return null;
}

/** Persist a passive reading position without adding browser-history entries. */
export function updatePaperReadingPosition(blockId: string): void {
  if (!validReadingBlockId(blockId)) return;
  persistPaperReadingPosition(blockId);
  const url = new URL(window.location.href);
  const matchingHash = `#${PAPER_BLOCK_ELEMENT_PREFIX}${blockId}`;
  if (url.hash === matchingHash) {
    if (url.searchParams.has(PAPER_READING_POSITION_PARAM)) {
      url.searchParams.delete(PAPER_READING_POSITION_PARAM);
      replacePaperUrl(url);
    }
    return;
  }
  if (
    url.searchParams.get(PAPER_READING_POSITION_PARAM) === blockId
    && !PAPER_POSITION_HASH.test(url.hash)
  ) return;
  url.searchParams.set(PAPER_READING_POSITION_PARAM, blockId);
  if (PAPER_POSITION_HASH.test(url.hash)) url.hash = "";
  replacePaperUrl(url);
}

export function clearPaperReadingPosition(): void {
  removeStoredPaperReadingPosition();
  const url = new URL(window.location.href);
  const hadParameter = url.searchParams.has(PAPER_READING_POSITION_PARAM);
  url.searchParams.delete(PAPER_READING_POSITION_PARAM);
  const hadPositionHash = PAPER_POSITION_HASH.test(url.hash);
  if (hadPositionHash) url.hash = "";
  if (hadParameter || hadPositionHash) replacePaperUrl(url);
}

/** Restore URL state, or the current tab's saved block when a browser reload drops the URL. */
export function restorePaperReadingPosition(
  options: { allowSessionFallback?: boolean } = {},
): boolean {
  const url = new URL(window.location.href);
  const hashElementId = url.hash.slice(1);
  let elementId = PAPER_POSITION_HASH.test(url.hash) ? hashElementId : null;
  let blockId: string | null = null;
  let restoredFromSession = false;
  if (elementId === null) {
    blockId = url.searchParams.get(PAPER_READING_POSITION_PARAM);
    if (validReadingBlockId(blockId)) elementId = `${PAPER_BLOCK_ELEMENT_PREFIX}${blockId}`;
  }
  if (elementId === null && options.allowSessionFallback) {
    blockId = storedPaperReadingPosition();
    if (blockId !== null) {
      elementId = `${PAPER_BLOCK_ELEMENT_PREFIX}${blockId}`;
      restoredFromSession = true;
    }
  }
  if (elementId === null) return false;
  const target = document.getElementById(elementId);
  if (target === null) {
    if (restoredFromSession) removeStoredPaperReadingPosition();
    return false;
  }
  target.scrollIntoView({ behavior: "auto", block: "start" });
  const targetBlockId = elementId.startsWith(PAPER_BLOCK_ELEMENT_PREFIX)
    ? elementId.slice(PAPER_BLOCK_ELEMENT_PREFIX.length)
    : null;
  if (validReadingBlockId(targetBlockId)) persistPaperReadingPosition(targetBlockId);
  if (restoredFromSession && blockId !== null) {
    url.searchParams.set(PAPER_READING_POSITION_PARAM, blockId);
    replacePaperUrl(url);
  }
  return true;
}

export function rememberPaperBlockJumpOrigin(): void {
  const detail: PaperBlockJumpOrigin = {
    top: window.scrollY,
    url: `${window.location.pathname}${window.location.search}${window.location.hash}`,
  };
  window.dispatchEvent(new CustomEvent<PaperBlockJumpOrigin>(PAPER_BLOCK_JUMP_EVENT, { detail }));
}

export function jumpToPaperElement(
  elementId: string,
  options: PaperBlockJumpOptions = {},
): boolean {
  const target = document.getElementById(elementId);
  if (!target) return false;
  rememberPaperBlockJumpOrigin();
  if (options.updateHash) {
    window.history.replaceState(
      window.history.state,
      "",
      `${window.location.pathname}${window.location.search}#${elementId}`,
    );
  }
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({
    behavior: reducedMotion ? "auto" : "smooth",
    block: options.block ?? "center",
  });
  return true;
}

export function jumpToPaperBlock(
  blockId: string,
  options: PaperBlockJumpOptions = {},
): boolean {
  return jumpToPaperElement(`${PAPER_BLOCK_ELEMENT_PREFIX}${blockId}`, options);
}
