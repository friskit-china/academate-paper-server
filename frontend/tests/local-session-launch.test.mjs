import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  consumeLaunchTokenFragment,
  exchangeLocalLaunchToken,
} from "../app/local-launch/launch-token.ts";

const LAUNCH_TOKEN = "test-only-one-use-launch-token-000000000001";

test("local launch consumes a fragment, clears history first, and exchanges through same origin", async () => {
  const replacements = [];
  const token = consumeLaunchTokenFragment(
    {
      hash: `#token=${encodeURIComponent(LAUNCH_TOKEN)}`,
      pathname: "/local-launch",
      search: "",
    },
    {
      replaceState: (...arguments_) => replacements.push(arguments_),
    },
  );
  assert.equal(token, LAUNCH_TOKEN);
  assert.deepEqual(replacements, [[null, "", "/local-launch"]]);

  let requestedUrl = "";
  let requestedInit;
  await exchangeLocalLaunchToken(token, async (url, init) => {
    requestedUrl = String(url);
    requestedInit = init;
    return new Response(null, { status: 204 });
  });

  assert.equal(requestedUrl, "/api/v1/local-session/exchange");
  assert.equal(requestedUrl.includes(LAUNCH_TOKEN), false);
  assert.equal(requestedInit.method, "POST");
  assert.equal(requestedInit.cache, "no-store");
  assert.equal(requestedInit.credentials, "same-origin");
  assert.deepEqual(JSON.parse(requestedInit.body), { launch_token: LAUNCH_TOKEN });
});

test("missing or malformed launch fragments are still removed and never exchanged", () => {
  for (const hash of ["", "#token=short", "#token=%00invalid-token-00000000000000000000"]) {
    const replacements = [];
    const token = consumeLaunchTokenFragment(
      { hash, pathname: "/local-launch", search: "?ui=compact" },
      { replaceState: (...arguments_) => replacements.push(arguments_) },
    );
    assert.equal(token, null);
    assert.deepEqual(replacements, [[null, "", "/local-launch?ui=compact"]]);
  }

  const replacements = [];
  const token = consumeLaunchTokenFragment(
    {
      hash: "",
      pathname: "/local-launch",
      search: `?token=${encodeURIComponent(LAUNCH_TOKEN)}&ui=compact`,
    },
    { replaceState: (...arguments_) => replacements.push(arguments_) },
  );
  assert.equal(token, null);
  assert.deepEqual(replacements, [[null, "", "/local-launch?ui=compact"]]);
});

test("local launch source does not persist proofs or read the HttpOnly cookie", async () => {
  const helper = await readFile(
    new URL("../app/local-launch/launch-token.ts", import.meta.url),
    "utf8",
  );
  const component = await readFile(
    new URL("../app/local-launch/local-launch-exchange.tsx", import.meta.url),
    "utf8",
  );
  const implementation = `${helper}\n${component}`;

  assert.match(implementation, /replaceState/);
  assert.match(implementation, /credentials: "same-origin"/);
  assert.doesNotMatch(implementation, /localStorage|sessionStorage|document\.cookie/);
  assert.doesNotMatch(implementation, /console\.(?:log|debug|info|warn|error)/);
  assert.doesNotMatch(implementation, /\?(?:token|launch_token)=/);
});
