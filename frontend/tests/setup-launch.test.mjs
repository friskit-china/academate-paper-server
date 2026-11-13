import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { consumeSetupTokenFragment } from "../app/setup/setup-token.ts";

const SETUP_TOKEN = "test-only-one-use-owner-setup-proof-00000001";

test("owner setup consumes only a fragment proof and clears browser history first", () => {
  const replacements = [];
  const token = consumeSetupTokenFragment(
    {
      hash: `#token=${encodeURIComponent(SETUP_TOKEN)}`,
      pathname: "/setup",
      search: "?ui=compact",
    },
    { replaceState: (...arguments_) => replacements.push(arguments_) },
  );
  assert.equal(token, SETUP_TOKEN);
  assert.deepEqual(replacements, [[null, "", "/setup?ui=compact"]]);
});

test("owner setup rejects query and malformed proofs without persisting either", async () => {
  for (const view of [
    { hash: "", pathname: "/setup", search: `?setup_token=${SETUP_TOKEN}` },
    { hash: "#token=short", pathname: "/setup", search: "" },
  ]) {
    const replacements = [];
    assert.equal(
      consumeSetupTokenFragment(view, {
        replaceState: (...arguments_) => replacements.push(arguments_),
      }),
      null,
    );
    assert.deepEqual(replacements, [[null, "", "/setup"]]);
  }

  const implementation = await readFile(
    new URL("../app/setup/setup-owner-form.tsx", import.meta.url),
    "utf8",
  );
  assert.match(implementation, /consumeSetupTokenFragment/);
  assert.doesNotMatch(implementation, /localStorage|sessionStorage|document\.cookie/);
  assert.doesNotMatch(implementation, /console\.(?:log|debug|info|warn|error)/);
});
