import assert from "node:assert/strict";
import test from "node:test";

import { parseSetupStatus } from "../app/setup/setup-status.ts";

const valid = {
  setup_state: "AWAITING_OWNER",
  owner_required: true,
  mutations_open: true,
  state_version: 7,
  installation_mode: null,
  allowed_installation_modes: ["SINGLE_USER", "MULTI_USER"],
  external_toolchain: {
    kind: "MACTEX",
    state: "INVALID",
    official_url: "https://www.tug.org/mactex/",
    restart_required: true,
    components: {
      mactex: "PRESENT",
      ghostscript: "PRESENT",
      ghostscript_extras: "MISSING",
    },
  },
};

test("setup status accepts installation modes and the compatibility toolchain contract", () => {
  assert.deepEqual(parseSetupStatus(valid), valid);
  assert.equal(parseSetupStatus({ ...valid, external_toolchain: null })?.external_toolchain, null);
  const available = parseSetupStatus({
    ...valid,
    external_toolchain: {
      ...valid.external_toolchain,
      state: "AVAILABLE",
      restart_required: false,
      components: {
        mactex: "PRESENT",
        ghostscript: "PRESENT",
        ghostscript_extras: "PRESENT",
      },
    },
  });
  assert.equal(available?.external_toolchain?.state, "AVAILABLE");
  assert.deepEqual(available?.external_toolchain?.components, {
    mactex: "PRESENT",
    ghostscript: "PRESENT",
    ghostscript_extras: "PRESENT",
  });
});

test("setup status validates version and installation-mode consistency", () => {
  assert.equal(parseSetupStatus({
    ...valid,
    installation_mode: "SINGLE_USER",
    allowed_installation_modes: [],
  })?.installation_mode, "SINGLE_USER");
  for (const candidate of [
    { ...valid, state_version: -1 },
    { ...valid, state_version: 1.5 },
    { ...valid, allowed_installation_modes: [] },
    { ...valid, allowed_installation_modes: ["SINGLE_USER", "SINGLE_USER"] },
    { ...valid, allowed_installation_modes: ["UNKNOWN"] },
    { ...valid, installation_mode: "UNKNOWN" },
    { ...valid, installation_mode: "MULTI_USER", allowed_installation_modes: ["SINGLE_USER"] },
    { ...valid, installation_mode: "MULTI_USER" },
  ]) {
    assert.equal(parseSetupStatus(candidate), null);
  }
});

test("setup status rejects injected URLs, paths, extra fields, and inconsistent restart state", () => {
  for (const external_toolchain of [
    { ...valid.external_toolchain, official_url: "https://example.invalid/installer" },
    { ...valid.external_toolchain, path: "/Users/example/bin" },
    { ...valid.external_toolchain, state: "AVAILABLE" },
    { ...valid.external_toolchain, state: "UNKNOWN" },
  ]) {
    assert.equal(parseSetupStatus({ ...valid, external_toolchain }), null);
  }
  assert.equal(parseSetupStatus({ ...valid, command: "sudo installer" }), null);
});

test("setup status rejects malformed or non-exact nested component diagnostics", () => {
  const componentVariants = [
    { ...valid.external_toolchain.components, mactex: "UNKNOWN" },
    { ...valid.external_toolchain.components, ghostscript: "UNKNOWN" },
    { ...valid.external_toolchain.components, ghostscript_extras: "UNKNOWN" },
    { ...valid.external_toolchain.components, ghostscript_extras: true },
    { ...valid.external_toolchain.components, command: "installer" },
    { mactex: "PRESENT", ghostscript: "PRESENT" },
    null,
    ["PRESENT", "PRESENT", "MISSING"],
  ];
  for (const components of componentVariants) {
    assert.equal(parseSetupStatus({
      ...valid,
      external_toolchain: { ...valid.external_toolchain, components },
    }), null);
  }

  const { components: _components, ...withoutComponents } = valid.external_toolchain;
  assert.equal(parseSetupStatus({ ...valid, external_toolchain: withoutComponents }), null);

  assert.equal(parseSetupStatus({
    ...valid,
    external_toolchain: {
      ...valid.external_toolchain,
      state: "AVAILABLE",
      restart_required: false,
      components: valid.external_toolchain.components,
    },
  }), null);
});
