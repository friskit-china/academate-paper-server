import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("frontend declares reproducible framework versions", async () => {
  const packageJson = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
  const dockerfile = await readFile(new URL("../Dockerfile", import.meta.url), "utf8");
  const nextConfig = await readFile(new URL("../next.config.ts", import.meta.url), "utf8");
  const apiSource = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
  assert.match(packageJson.dependencies.next, /^\d+\.\d+\.\d+$/);
  assert.match(packageJson.dependencies.react, /^\d+\.\d+\.\d+$/);
  assert.equal(packageJson.packageManager, "pnpm@11.19.0");
  assert.match(dockerfile, /node:24\.19\.0-alpine3\.23@sha256:[a-f0-9]{64}/);
  assert.match(dockerfile, /PNPM_VERSION=11\.19\.0/);
  assert.doesNotMatch(dockerfile, /ARG API_INTERNAL_URL|ENV API_INTERNAL_URL/);
  assert.match(dockerfile, /\.next\/standalone/);
  assert.match(dockerfile, /pnpm install --frozen-lockfile/);
  assert.doesNotMatch(nextConfig, /rewrites|API_INTERNAL_URL|127\.0\.0\.1:8000/);
  assert.match(apiSource, /export const API_BASE = "\/api\/v2"/);
  assert.doesNotMatch(apiSource, /NEXT_PUBLIC_API_URL|API_INTERNAL_URL/);
  assert.equal(packageJson.scripts.typecheck, "tsc --noEmit");
});

test("frontend branding is AcadeMate Paper and provider-neutral", async () => {
  const layoutSource = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  const settingsSource = await readFile(new URL("../app/settings/page.tsx", import.meta.url), "utf8");
  const readerSource = await readFile(
    new URL("../app/papers/[paperId]/paper-detail-client.tsx", import.meta.url),
    "utf8",
  );
  const userFacingSource = `${layoutSource}\n${settingsSource}\n${readerSource}`;

  assert.match(layoutSource, /AcadeMate Paper/);
  const forbiddenUserFacingBrands = new RegExp(
    ["Paper" + "Manager", "G" + "LM", "ZHIPU" + "AI", "big" + "model"].join("|"),
    "i",
  );
  assert.doesNotMatch(userFacingSource, forbiddenUserFacingBrands);
  assert.match(settingsSource, /个人翻译系统提示词/);
});

test("browser storage and import examples are independent of product branding", async () => {
  const storageSource = await readFile(new URL("../app/storage-keys.ts", import.meta.url), "utf8");
  const librarySource = await readFile(new URL("../app/library/page.tsx", import.meta.url), "utf8");

  assert.match(storageSource, /paper-reader-ui:v1/);
  assert.doesNotMatch(storageSource, /AcadeMate\s*Paper/i);
  assert.match(librarySource, /https:\/\/arxiv\.org\/abs\/1706\.03762/);
});
