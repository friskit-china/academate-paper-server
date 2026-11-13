import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("Personal Server first-run page presents the Chinese step-by-step installer", async () => {
  const page = await readFile(new URL("../app/setup/page.tsx", import.meta.url), "utf8");
  const form = await readFile(
    new URL("../app/setup/setup-owner-form.tsx", import.meta.url),
    "utf8",
  );
  const wizardState = await readFile(
    new URL("../app/setup/setup-wizard-state.ts", import.meta.url),
    "utf8",
  );

  assert.match(page, /loadRuntimeCapabilities\(\)/);
  assert.match(page, /loadSetupStatus\(\)/);
  assert.match(page, /setup_state === "READY"/);
  assert.match(page, /redirect\("\/library"\)/);
  assert.match(page, /Server 首次配置/);
  assert.match(page, /设置登录密码和工作区/);
  assert.match(page, /<SetupOwnerForm initialStatus=\{status\}/);
  assert.match(page, /export const dynamic = "force-dynamic"/);

  for (const copy of [
    "欢迎使用 AcadeMate Paper",
    "设置登录密码",
    "设置个人工作区",
    "请检查后再保存",
    "保存配置并开始使用",
    "上一步",
    "下一步",
  ]) {
    assert.match(`${form}\n${wizardState}`, new RegExp(copy.replace(/[()]/g, "\\$&")));
  }

  assert.match(form, /aria-label="配置步骤"/);
  assert.match(form, /aria-current=\{index === step \? "step" : undefined\}/);
  assert.match(form, /步骤 \{step \+ 1\} \/ \{SETUP_STEPS\.length\}/);
  assert.doesNotMatch(form, /MULTI_USER|installation-mode/);
  assert.doesNotMatch(form, /setUsername|setDisplayName|autoComplete="username"/);
  assert.match(form, /再次输入密码/);
  assert.match(form, /passwordConfirmation/);
  assert.match(form, /workspaceName/);
  assert.match(form, /现在配置 LLM 凭据（可选）/);
  assert.match(form, /locale="zh-CN"/);
});

test("READY setup and product-home links converge on the authenticated library", async () => {
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  const home = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  const setup = await readFile(new URL("../app/setup/page.tsx", import.meta.url), "utf8");

  assert.match(layout, /setup_state === "READY" \? "\/library" : "\/setup"/);
  assert.match(home, /redirect\("\/library"\)/);
  assert.match(setup, /redirect\("\/library"\)/);
});

test("wizard keeps all mutations behind one final confirmation and masks secrets", async () => {
  const form = await readFile(
    new URL("../app/setup/setup-owner-form.tsx", import.meta.url),
    "utf8",
  );
  const api = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");

  assert.equal(form.match(/setupLocalOwner\(/g)?.length, 1);
  assert.match(form, /if \(step !== REVIEW_STEP\)/);
  assert.match(form, /type="submit"/);
  assert.match(form, /expected_state_version: initialStatus\.state_version/);
  assert.match(form, /installation_mode: "SINGLE_USER"/);
  assert.match(form, /workspace_name: workspaceName\.trim\(\)/);
  assert.match(form, /submitInFlight\.current/);
  assert.match(form, /maskSecret\(password\)/);
  assert.match(form, /maskSecret\(bootstrapToken\)/);
  assert.match(form, /maskSecret\(llmCredential\.api_key, true\)/);
  assert.match(form, /setBootstrapToken\(""\)/);
  assert.match(form, /setPassword\(""\)/);
  assert.match(form, /api_key: ""/);
  assert.doesNotMatch(form, /localStorage|sessionStorage|document\.cookie/);

  assert.match(api, /installation_mode: InstallationMode/);
  assert.match(api, /workspace_name: string/);
  assert.match(api, /expected_state_version: number/);
});

test("wizard consumes a fragment or accepts manual proof and waits for READY", async () => {
  const form = await readFile(
    new URL("../app/setup/setup-owner-form.tsx", import.meta.url),
    "utf8",
  );

  assert.match(form, /consumeSetupTokenFragment\(window\.location, window\.history\)/);
  assert.match(form, /name="bootstrap-token"/);
  assert.match(form, /onChange=\{\(event\) => \{/);
  assert.match(form, /getSetupStatus\(\)/);
  assert.match(form, /status\.setup_state === "READY"/);
  assert.match(form, /window\.location\.assign\("\/login\?setup=complete"\)/);
  assert.match(form, /progressState === "CONFIGURING"/);
  assert.match(form, /progressState === "VERIFYING_RUNTIME"/);
  assert.match(form, /请不要重复提交配置/);
  assert.match(form, /首次配置链接/);
  assert.doesNotMatch(form, /result\.toLowerCase/);
  assert.doesNotMatch(form, /MacTeX|Ghostscript|\.pkg|程序坞|启动器/);
});
