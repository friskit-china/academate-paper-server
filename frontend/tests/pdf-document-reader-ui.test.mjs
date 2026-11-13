import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
  "utf8",
);

test("embedded PDF blocks use the manifest asset and expose page navigation", () => {
  assert.match(source, /block\.block_type === "PDF_DOCUMENT"/);
  assert.match(source, /function PDFDocumentReader/);
  assert.match(source, /Previous PDF page/);
  assert.match(source, /Next PDF page/);
  assert.match(source, /#page=\$\{page\}/);
  assert.match(source, /PDF continuous reader/);
  assert.match(source, /page_width_points/);
  assert.match(source, /page_height_points/);
  assert.match(source, /pdf-document-viewport-adaptive/);
  assert.match(source, /aspectRatio: contentAspectRatio/);
});

test("outline reuses parser numbering for root sections and appendices", () => {
  assert.match(source, /item\.parent_id === null && item\.display_number/);
  assert.match(source, /className="outline-number"/);
});

test("PDF-only papers use a dedicated reader mode without structured AI panels", () => {
  assert.match(source, /source\.source_type === "ARXIV_PDF"/);
  assert.match(source, /source\.source_type === "EXPANDED_SOURCE"/);
  assert.match(source, /本篇文章无LaTeX源代码，无法进行结构化理解，仅供只读阅读/);
  assert.match(source, /!isPdfOnly \? <PaperVisualMap/);
  assert.match(source, /!isPdfOnly && readingMode === "bilingual"/);
  assert.match(source, /isPdfOnly \? "reading-workspace-pdf-only"/);
  assert.match(source, /mode=\{isPdfOnly \? "original" : readingMode\}/);
  assert.match(source, /document_mode === "pdf-only"/);
});

test("PDF-only notice follows metadata assets and precedes the reading workspace", () => {
  const sourceAssets = source.indexOf('id="paper-source-assets"');
  const notice = source.indexOf('className="pdf-only-notice"');
  const readingWorkspace = source.indexOf('aria-label="Parsed paper content"');

  assert.ok(sourceAssets >= 0);
  assert.ok(notice > sourceAssets);
  assert.ok(readingWorkspace > notice);
});
