import assert from "node:assert/strict";
import test from "node:test";

import {
  REVIEW_STEP,
  maskSecret,
  validateSetupStep,
} from "../app/setup/setup-wizard-state.ts";

const completeDraft = {
  bootstrapToken: "test-only-bootstrap-proof-1234567890",
  password: "a-secure-password",
  passwordConfirmation: "a-secure-password",
  workspaceName: "我的文库",
  configureLlm: true,
  llmCredential: {
    provider: "glm",
    api_key: "temporary-api-key",
    api_base: "https://example.test/v1",
    model: "model-1",
  },
};

test("each editable wizard step validates before advancing", () => {
  for (let step = 0; step < REVIEW_STEP; step += 1) {
    assert.equal(validateSetupStep(step, completeDraft), null);
  }
  assert.match(
    validateSetupStep(0, { ...completeDraft, bootstrapToken: "short" }),
    /有效配置凭证/,
  );
  assert.match(
    validateSetupStep(1, { ...completeDraft, passwordConfirmation: "different-password" }),
    /密码不一致/,
  );
  assert.match(
    validateSetupStep(2, {
      ...completeDraft,
      llmCredential: { ...completeDraft.llmCredential, api_base: "file:///secret" },
    }),
    /HTTP\(S\)/,
  );
});

test("review masks secrets", () => {
  assert.equal(maskSecret("super-secret"), "••••••••（已提供）");
  assert.equal(maskSecret("super-secret", true), "********cret");
  assert.equal(maskSecret("12345678", true), "********");
  assert.equal(maskSecret(""), "未提供");
});
