"use strict";

const get = (id) => document.getElementById(id);
const methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"];
let operations = [];
let schemas = {};
let mode = "operations";

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function addSection(parent, title, value) {
  if (value == null) return;
  parent.append(element("h2", title), element("pre", JSON.stringify(value, null, 2)));
}

function referencedSchemas(value, names = new Set()) {
  if (value && typeof value === "object") {
    if (typeof value.$ref === "string" && value.$ref.startsWith("#/components/schemas/")) {
      const name = value.$ref.slice("#/components/schemas/".length).replace(/~1/g, "/").replace(/~0/g, "~");
      if (Object.hasOwn(schemas, name)) names.add(name);
    }
    Object.values(value).forEach((child) => referencedSchemas(child, names));
  }
  return names;
}

function addReferences(parent, value) {
  const names = [...referencedSchemas(value)].sort();
  if (names.length === 0) return;
  const links = element("div", "");
  links.className = "refs";
  for (const name of names) {
    const link = element("a", name);
    link.href = "#schema=" + encodeURIComponent(name);
    links.append(link);
  }
  parent.append(element("h2", "相关数据结构"), links);
}

function entry(summary, fill, open = false) {
  const details = element("details", "");
  details.append(summary);
  function populate() {
    if (details.open && details.children.length === 1) fill(details);
  }
  details.addEventListener("toggle", populate);
  details.open = open;
  if (open) populate();
  return details;
}

function showOperation({ path, method, operation, parameters, security }) {
  const summary = element("summary", "");
  const verb = element("span", method);
  verb.className = "verb";
  summary.append(verb, element("code", path));
  return entry(summary, (details) => {
    if (operation.summary) details.append(element("strong", operation.summary));
    if (operation.description) details.append(element("p", operation.description));
    if (operation.operationId) details.append(element("p", operation.operationId));
    if (Array.isArray(security)) {
      const auth = security.length ? security.map((choice) => Object.keys(choice).join(" + ") || "匿名").join(" / ") : "匿名";
      details.append(element("p", "认证方式 · " + auth));
    }
    if (parameters.length) addSection(details, "参数", parameters);
    addSection(details, "请求体", operation.requestBody);
    addSection(details, "响应", operation.responses);
    addReferences(details, [parameters, operation.requestBody, operation.responses]);
  });
}

function selectedSchema() {
  if (!location.hash.startsWith("#schema=")) return null;
  try {
    const name = decodeURIComponent(location.hash.slice("#schema=".length));
    return Object.hasOwn(schemas, name) ? name : null;
  } catch { return null; }
}

function render() {
  get("operations").setAttribute("aria-pressed", String(mode === "operations"));
  get("schemas").setAttribute("aria-pressed", String(mode === "schemas"));
  get("method-filter").hidden = mode !== "operations";
  const query = get("search").value.trim().toLowerCase();
  const result = document.createDocumentFragment();
  if (mode === "operations") {
    for (const item of operations) {
      if ((!get("method").value || item.method === get("method").value) && item.terms.includes(query)) {
        result.append(showOperation(item));
      }
    }
  } else {
    for (const [name, schema] of Object.entries(schemas)) {
      if (![name, schema.title, schema.description].join(" ").toLowerCase().includes(query)) continue;
      result.append(entry(element("summary", name), (details) => {
        addSection(details, "Schema", schema);
        addReferences(details, schema);
      }, name === selectedSchema()));
    }
  }
  const count = result.childElementCount;
  get("results").replaceChildren(result);
  get("status").textContent = count
    ? (mode === "operations" ? count + " 个接口" : count + " 个数据结构")
    : "没有找到匹配项";
}

function showMode(next) {
  history.replaceState(null, "", location.pathname + location.search);
  mode = next;
  get("search").value = "";
  render();
}

function followHash() {
  const name = selectedSchema();
  if (name !== null) {
    mode = "schemas";
    get("search").value = name;
  }
  render();
}

async function load() {
  try {
    const response = await fetch("/api/v2/openapi.json", {
      credentials: "same-origin", mode: "same-origin", redirect: "error",
      cache: "no-store", headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      get("status").replaceChildren("请先登录此 Server，再重新打开本页。 ", Object.assign(element("a", "登录"), { href: "/login" }));
      return;
    }
    if (!response.ok) throw new Error("API unavailable");
    const contract = await response.json();
    if (!contract.paths || !contract.info) throw new Error("Invalid contract");
    schemas = contract.components?.schemas || {};
    for (const [path, item] of Object.entries(contract.paths)) {
      for (const method of methods) {
        const operation = item[method.toLowerCase()];
        if (!operation) continue;
        const parameters = [...(item.parameters || []), ...(operation.parameters || [])];
        const terms = [method, path, operation.operationId, operation.summary, operation.description, ...(operation.tags || [])]
          .join(" ").toLowerCase();
        operations.push({ method, path, operation, parameters, security: operation.security ?? contract.security, terms });
      }
    }
    for (const method of methods.filter((value) => operations.some((op) => op.method === value))) {
      get("method").append(new Option(method, method));
    }
    const server = contract.info["x-server-version"];
    get("version").textContent = [server && "Server " + server, "API " + contract.info.version].filter(Boolean).join(" · ");
    for (const id of ["operations", "schemas", "search", "method"]) get(id).disabled = false;
    get("operations").addEventListener("click", () => showMode("operations"));
    get("schemas").addEventListener("click", () => showMode("schemas"));
    get("search").addEventListener("input", render);
    get("method").addEventListener("change", render);
    window.addEventListener("hashchange", followHash);
    followHash();
  } catch {
    get("status").textContent = "无法读取此 Server 的接口定义，请确认 Web 连接和登录状态后刷新。";
  }
}

load();
