import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { consumeInvitationTokenFragment } from "../app/register/invite-token.ts";
import { validateRegistrationDraft } from "../app/register/registration-state.ts";
import {
  onboardingCredentialPayload,
  validateOnboardingStep,
} from "../app/onboarding/onboarding-state.ts";
import { isCanonicalRegistrationUrl } from "../app/users/invitation-url.ts";

const INVITE_TOKEN = "invitation-token-with-at-least-thirty-two-bytes-0001";

test("registration consumes exactly one fragment invite and clears URL proofs immediately", () => {
  const replacements = [];
  const token = consumeInvitationTokenFragment(
    {
      pathname: "/register",
      search: "?language=zh-CN&invite=query-proof&invite_token=second-query-proof",
      hash: `#invite=${encodeURIComponent(INVITE_TOKEN)}`,
    },
    {
      replaceState(state, title, url) {
        replacements.push({ state, title, url });
      },
    },
  );
  assert.equal(token, INVITE_TOKEN);
  assert.deepEqual(replacements, [{ state: null, title: "", url: "/register?language=zh-CN" }]);

  for (const hash of [
    "",
    `#invite=${INVITE_TOKEN}&invite=${INVITE_TOKEN}`,
    "#invite=short",
    `#token=${INVITE_TOKEN}`,
  ]) {
    const cleared = [];
    assert.equal(consumeInvitationTokenFragment(
      { pathname: "/register", search: "?invite=query-only", hash },
      { replaceState: (...args) => cleared.push(args) },
    ), null);
    assert.deepEqual(cleared, [[null, "", "/register"]]);
  }
});

test("registration form has only self-registration fields and sends the exact public payload", async () => {
  const form = await readFile(new URL("../app/register/registration-form.tsx", import.meta.url), "utf8");
  const page = await readFile(new URL("../app/register/page.tsx", import.meta.url), "utf8");
  const api = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");

  assert.equal((form.match(/<input/g) ?? []).length, 4);
  for (const field of ["用户名", "显示名称", "密码", "再次输入密码"]) {
    assert.match(form, new RegExp(field));
  }
  assert.match(form, /consumeInvitationTokenFragment\(window\.location, window\.history\)/);
  assert.match(form, /invite_token: inviteToken/);
  assert.match(form, /username: username\.trim\(\)/);
  assert.match(form, /display_name: displayName\.trim\(\)/);
  assert.match(form, /window\.location\.replace\("\/onboarding"\)/);
  assert.doesNotMatch(form, /window\.(?:localStorage|sessionStorage)|console\.|document\.cookie/);
  assert.doesNotMatch(form, /workspace_id|group_id|permission|initial_password/);

  assert.match(page, /capabilities\.setup_state !== "READY"/);
  assert.match(page, /capabilities\.installation_mode !== "MULTI_USER"/);
  assert.match(page, /capabilities\.capabilities\.registration/);
  assert.match(page, /redirect\("\/login"\)/);

  const registerApi = api.slice(
    api.indexOf("export async function registerWebSession"),
    api.indexOf("export async function createAutomaticOwnerSession"),
  );
  for (const field of ["invite_token", "username", "display_name", "password"]) {
    assert.match(registerApi, new RegExp(`${field}: string`));
  }
  assert.doesNotMatch(registerApi, /workspace|role|group|permission/);
  assert.match(registerApi, /credentials: "same-origin"/);
  assert.match(registerApi, /PRESERVE_UNAUTHORIZED_RESPONSE/);
});

test("registration draft validation handles passwords locally without broadening the payload", () => {
  const valid = {
    username: "reader",
    displayName: "研究者",
    password: "twelve-chars-or-more",
    passwordConfirmation: "twelve-chars-or-more",
  };
  assert.equal(validateRegistrationDraft(valid), null);
  assert.match(validateRegistrationDraft({ ...valid, username: "ab" }), /3 至 64/);
  assert.match(validateRegistrationDraft({ ...valid, username: "-reader" }), /字母或数字开头/);
  assert.match(validateRegistrationDraft({ ...valid, displayName: " " }), /显示名称/);
  assert.match(validateRegistrationDraft({ ...valid, displayName: "两行\n名称" }), /单行文字/);
  assert.match(validateRegistrationDraft({ ...valid, password: "short", passwordConfirmation: "short" }), /12 至 1024/);
  assert.match(validateRegistrationDraft({ ...valid, passwordConfirmation: "different-password" }), /不一致/);
});

test("personal onboarding supports optional credentials and validates only entered secrets", () => {
  const skipped = {
    displayName: "读者",
    configureLlm: false,
    credential: { provider: "", api_key: "", api_base: null, model: null },
  };
  assert.equal(validateOnboardingStep(1, skipped), null);
  assert.equal(validateOnboardingStep(2, skipped), null);
  assert.equal(onboardingCredentialPayload(skipped), null);

  const configured = {
    ...skipped,
    configureLlm: true,
    credential: {
      provider: " provider ",
      api_key: "secret-value",
      api_base: " https://llm.example/v1 ",
      model: " model-a ",
    },
  };
  assert.equal(validateOnboardingStep(2, configured), null);
  assert.deepEqual(onboardingCredentialPayload(configured), {
    provider: "provider",
    api_key: "secret-value",
    api_base: "https://llm.example/v1",
    model: "model-a",
  });
  assert.match(validateOnboardingStep(2, {
    ...configured,
    credential: { ...configured.credential, api_base: "file:///tmp/key" },
  }), /HTTP\(S\)/);
});

test("Chinese onboarding is self-scoped, resumable, masked, and idempotent-facing", async () => {
  const page = await readFile(new URL("../app/onboarding/page.tsx", import.meta.url), "utf8");
  const wizard = await readFile(new URL("../app/onboarding/onboarding-wizard.tsx", import.meta.url), "utf8");
  const state = await readFile(new URL("../app/onboarding/onboarding-state.ts", import.meta.url), "utf8");
  const implementation = `${page}\n${wizard}\n${state}`;

  for (const copy of [
    "先完成你的个人设置",
    "个人资料",
    "个人 LLM",
    "使用偏好",
    "自动翻译",
    "自动 AI 辅助修复",
    "检查你的个人设置",
    "保存并进入文库",
  ]) assert.match(implementation, new RegExp(copy));
  assert.match(wizard, /getOnboardingStatus\(\)/);
  assert.match(wizard, /readOnly value=\{status\.username\}/);
  assert.match(wizard, /completeOnboarding\(\{/);
  assert.match(wizard, /display_name: displayName\.trim\(\)/);
  assert.match(wizard, /credential: onboardingCredentialPayload\(draft\)/);
  assert.match(wizard, /maskSecret\(credential\.api_key, true\)/);
  assert.match(wizard, /setCredential\(\{ \.\.\.EMPTY_PERSONAL_CREDENTIAL \}\)/);
  assert.match(wizard, /completed\.status !== "COMPLETED"/);
  assert.match(wizard, /window\.location\.replace\("\/library"\)/);
  assert.doesNotMatch(implementation, /Docker|数据库|管理员|workspace_id|installation_mode/);
  assert.doesNotMatch(implementation, /window\.(?:localStorage|sessionStorage)|console\.|document\.cookie/);
});

test("administrator accepts only a canonical one-time registration URL", () => {
  const origin = "http://localhost:3000";
  assert.equal(isCanonicalRegistrationUrl(
    `${origin}/register#invite=${INVITE_TOKEN}`,
    origin,
  ), true);
  for (const value of [
    `${origin}/register?invite=${INVITE_TOKEN}`,
    `${origin}/register#invite=short`,
    `${origin}/register#invite=${INVITE_TOKEN}&role=OWNER`,
    `http://127.0.0.1:3000/register#invite=${INVITE_TOKEN}`,
    `${origin}/users#invite=${INVITE_TOKEN}`,
  ]) assert.equal(isCanonicalRegistrationUrl(value, origin), false, value);
});
