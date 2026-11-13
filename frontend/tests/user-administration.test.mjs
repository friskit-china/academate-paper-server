import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { storedKeyPlaceholder } from "../app/credential-hint.ts";

test("MULTI_USER navigation resolves invitation and user permission from strict session status", async () => {
  const runtime = await readFile(new URL("../app/runtime-capabilities.ts", import.meta.url), "utf8");
  const navigation = await readFile(new URL("../app/capability-ui.tsx", import.meta.url), "utf8");
  const page = await readFile(new URL("../app/users/page.tsx", import.meta.url), "utf8");

  assert.match(runtime, /manifest\.capabilities\.user_management \|\| manifest\.capabilities\.invitations/);
  assert.match(runtime, /path: "\/users"/);
  assert.match(navigation, /status\.can_manage_users \|\| status\.can_manage_invitations/);
  assert.match(navigation, /item\.id !== "users" \|\| canManageAdministration/);
  assert.match(page, /多用户部署/);
  assert.match(page, /成员与邀请/);
});

test("administrator UI uses invitations as the only ordinary account creation flow", async () => {
  const panel = await readFile(
    new URL("../app/users/user-administration-panel.tsx", import.meta.url),
    "utf8",
  );
  const api = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");

  for (const copy of [
    "创建单次邀请码",
    "有效期",
    "备注（可选）",
    "现在复制完整注册链接",
    "邀请码记录",
    "创建时间",
    "到期时间",
    "兑换时间",
    "兑换用户",
    "撤销",
    "当前文库用户",
    "安装所有者",
    "停用",
  ]) assert.match(panel, new RegExp(copy.replace(/[()]/g, "\\$&")));

  assert.match(panel, /createInvitation\(\{/);
  assert.match(panel, /expires_in_seconds: expiresInSeconds/);
  assert.match(panel, /registration_url: registrationUrl/);
  assert.match(panel, /navigator\.clipboard\.writeText\(oneTimeRegistrationUrl\)/);
  assert.match(panel, /listInvitations\(true\)/);
  assert.match(panel, /revokeInvitation\(invitation\.id\)/);
  assert.match(panel, /disableManagedUser\(user\.id\)/);
  assert.doesNotMatch(panel, /createManagedUser|initialPassword|initial_password|type="password"/);
  assert.doesNotMatch(panel, /window\.(?:localStorage|sessionStorage)|console\.|document\.cookie/);

  for (const path of [
    "admin/invitations",
    "include_inactive=",
    "admin/invitations/${encodeURIComponent(invitationId)}/revoke",
    "admin/users/${encodeURIComponent(userId)}/disable",
  ]) assert.match(api, new RegExp(path.replace(/[${}()]/g, "\\$&")));
});

test("member session renders no management lists or mutation controls", async () => {
  const panel = await readFile(
    new URL("../app/users/user-administration-panel.tsx", import.meta.url),
    "utf8",
  );
  const statusCall = panel.indexOf("getWebSessionStatus()");
  const userCall = panel.indexOf("session.can_manage_users ? listManagedUsers()");
  const invitationCall = panel.indexOf("session.can_manage_invitations ? listInvitations(true)");
  assert.ok(statusCall >= 0 && userCall > statusCall && invitationCall > statusCall);
  assert.match(panel, /authorizationResolved && !canManageUsers && !canManageInvitations/);
  assert.match(panel, /当前账号不能查看或管理成员与邀请码/);
  assert.match(panel, /\{canManageInvitations \? \(/);
  assert.match(panel, /\{canManageUsers \? \(/);
});

test("settings describe user-scoped preferences and credentials", async () => {
  const settings = await readFile(new URL("../app/settings/page.tsx", import.meta.url), "utf8");
  const credential = await readFile(new URL("../app/llm-credential-form.tsx", import.meta.url), "utf8");
  assert.match(settings, /<h1>个人设置<\/h1>/);
  assert.match(settings, /个人翻译系统提示词/);
  assert.match(credential, /个人 LLM 凭据/);
  assert.doesNotMatch(`${settings}\n${credential}`, /Workspace settings|Global translation system prompt|Local owner LLM credential/);
});

test("saved API credential hints stay in the placeholder and replacement starts empty", async () => {
  assert.equal(storedKeyPlaceholder(null), undefined);
  assert.equal(storedKeyPlaceholder({ masked_suffix: "••••9AbC" }), "********9AbC");
  assert.equal(storedKeyPlaceholder({ masked_suffix: "••••" }), "********");
  assert.equal(storedKeyPlaceholder({ masked_suffix: null }), "********");
  assert.equal(storedKeyPlaceholder({ masked_suffix: "unexpected-unmasked-value" }), "********");

  const form = await readFile(new URL("../app/llm-credential-form.tsx", import.meta.url), "utf8");
  assert.match(form, /placeholder=\{keyPlaceholder\}/);
  assert.match(form, /value=\{value\.api_key\}/);
  assert.match(form, /keyPlaceholder=\{storedKeyPlaceholder\(metadata\)\}/);
  assert.match(form, /setDraft\(\(current\) => \(\{ \.\.\.current, api_key: "" \}\)\)/);
  const onboarding = await readFile(new URL("../app/onboarding/onboarding-wizard.tsx", import.meta.url), "utf8");
  assert.match(onboarding, /placeholder=\{storedKeyPlaceholder\(status\.default_credential\)\}/);
  assert.match(onboarding, /value=\{credential\.api_key\}/);
});
