import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const readerSource = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
const stylesSource = await readFile(new URL("../app/styles.css", import.meta.url), "utf8");

test("structured bibliography renders at its parser-authored source marker", () => {
  assert.match(apiSource, /export type PaperBibliography/);
  assert.match(apiSource, /getPaperBibliography/);
  assert.match(readerSource, /block\.block_type === "BIBLIOGRAPHY"/);
  assert.match(readerSource, /markerBlockId=\{block\.id\}/);
  assert.match(readerSource, /!bibliographyMarker/);
  assert.match(readerSource, /"paper-bibliography"/);
  assert.match(readerSource, /id=\{`bibliography-entry-\$\{entry\.id\}`\}/);
});

test("citation clusters link each resolved key and preserve return navigation", () => {
  assert.match(readerSource, /segment\.type === "citation"/);
  assert.match(
    readerSource,
    /\["text", "math", "entity", "reference", "citation"\]\.includes/,
  );
  assert.match(readerSource, /function CitationPreviewLink/);
  assert.match(readerSource, /href=\{`#bibliography-entry-\$\{item\.entry_id\}`\}/);
  assert.match(readerSource, /CitationPreviewContext\.Provider/);
  assert.match(readerSource, /role="tooltip"/);
  assert.match(readerSource, /onMouseEnter=\{showPreview\}/);
  assert.match(readerSource, /onFocus=\{showPreview\}/);
  assert.match(readerSource, /entry\.title/);
  assert.match(readerSource, /bibliographyPeople\(entry\)/);
  assert.match(readerSource, /a\[href\^="#bibliography-entry-"\]/);
  assert.match(readerSource, /rememberPaperBlockJumpOrigin/);
  assert.match(stylesSource, /\.bibliography-entry:target/);
  assert.match(stylesSource, /\.paper-citation a/);
  assert.match(stylesSource, /\.citation-hover-card/);
});
