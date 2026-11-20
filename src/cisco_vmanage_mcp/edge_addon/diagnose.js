import {collectSnapshot} from "./collect.js";

const el = id => document.getElementById(id);
const spec = await (await fetch("reads.json")).json();
const {id: windowId} = await chrome.windows.getCurrent();
let session;
let mode;

function controls() {
  const idle = session?.connected && Number.isInteger(session.tabId) && !session.busy;
  el("refresh").disabled = !idle || !session?.origin;
  el("device").disabled = !idle || !session?.ready;
  el("ask").disabled = el("device").disabled;
}

function clearEvidence() {
  el("metrics").hidden = true;
  for (const id of ["total", "down", "time"]) el(id).textContent = "";
  el("thread").replaceChildren();
  el("sources").replaceChildren();
  el("device").replaceChildren(new Option("Estate overview", ""));
  el("progress").textContent = "";
}

function showMode() {
  const sandbox = mode === "sandbox";
  el("sandbox").checked = sandbox;
  el("lab").hidden = !sandbox;
  el("environment").textContent = sandbox ? "DevNet lab" : "Work estate";
  el("manager").textContent = session?.origin ?? "Connecting to Windows…";
  if (session?.origin === "") el("manager").textContent = "Work Manager is not configured.";
  const link = el("lab-manager");
  link.hidden = !sandbox || !session?.origin;
  if (link.hidden) link.removeAttribute("href");
  else link.href = session.origin;
}

function request(s, message) {
  return new Promise((resolve, reject) => {
    if (!s.connected || s.pending) throw new Error("The workstation is unavailable.");
    s.pending = {resolve, reject};
    s.port.postMessage(message);
  });
}

function disconnect(s) {
  if (!s) return;
  s.connected = false;
  s.pending?.reject(new Error("The selected tab or environment changed."));
  s.pending = null;
  s.port.disconnect();
}

function showSources(tables) {
  for (const [name, source] of Object.entries(tables)) {
    const item = document.createElement("li");
    item.textContent = `GET ${spec[name].path} · ${source.state} · ${source.rows.length} rows`;
    el("sources").append(item);
  }
}

async function capture(s) {
  if (s.busy || !s.connected || !s.origin || !Number.isInteger(s.tabId)) return;
  s.busy = true;
  s.ready = false;
  const selected = el("device").value;
  clearEvidence();
  controls();
  el("state").textContent = "Reading Manager…";
  try {
    await request(s, {type: "clear"});
    const tab = await chrome.tabs.get(s.tabId);
    if (s !== session || !s.connected) return;
    if (!tab.active || tab.windowId !== windowId || !tab.url || new URL(tab.url).origin !== s.origin) {
      throw new Error("Sign in to the Manager shown above, then click BlairScope in that tab’s toolbar.");
    }
    const target = s.documentId ? {tabId: s.tabId, documentIds: [s.documentId]} : {tabId: s.tabId};
    const [frame] = await chrome.scripting.executeScript({
      target, world: "ISOLATED", func: collectSnapshot, args: [s.origin, spec, selected]
    });
    if (s !== session || !s.connected) return;
    if (!frame?.result) throw new Error("The Manager page changed. Click BlairScope again.");
    s.documentId = frame.documentId;
    const snapshot = frame.result;
    showSources(snapshot.tables);
    const states = Object.values(snapshot.tables).map(source => source.state);
    if (states.includes("sign-in required")) throw new Error("Manager requires sign-in. Sign in and click BlairScope again.");
    const inv = snapshot.tables.inventory;
    if (!["ok", "partial"].includes(inv.state)) throw new Error("Inventory " + inv.state + ". Check Cisco access.");
    await request(s, {type: "capture", origin: s.origin, snapshot});
    if (s !== session) return;
    for (const row of inv.rows) {
      if (row["system-ip"]) el("device").add(new Option(
        `${row["host-name"] || "Device"} · ${row["system-ip"]}`, row["system-ip"]
      ));
    }
    el("device").value = selected;
    el("total").textContent = inv.rows.length;
    const statuses = inv.rows.map(row => String(row.reachability).toLowerCase());
    el("down").textContent = statuses.every(value => ["reachable", "unreachable"].includes(value))
      ? statuses.filter(value => value === "unreachable").length : "—";
    el("time").textContent = new Date(snapshot.captured).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    el("metrics").hidden = false;
    el("state").textContent = states.some(state => state !== "ok")
      ? "Partial snapshot. See collection results for missing data." : "Snapshot ready.";
    s.ready = true;
  } catch (error) {
    if (s === session && s.connected) el("state").textContent = error.message;
  } finally {
    s.busy = false;
    if (s === session) controls();
  }
}

async function bind(tabId, captureOnReady) {
  disconnect(session);
  const port = chrome.runtime.connectNative("org.sdwan.workstation");
  const s = {tabId, port, connected: true, busy: true, ready: false, captureOnReady};
  session = s;
  clearEvidence();
  el("question").value = "";
  controls();
  showMode();
  el("state").textContent = "Connecting to the workstation…";
  port.onMessage.addListener(reply => {
    const pending = s.pending;
    s.pending = null;
    if (reply.type === "error") pending?.reject(new Error(reply.error));
    else pending?.resolve(reply);
  });
  port.onDisconnect.addListener(() => {
    const reason = chrome.runtime.lastError?.message || "Windows disconnected.";
    s.connected = false;
    s.pending?.reject(new Error(reason));
    if (s !== session) return;
    clearEvidence();
    el("manager").textContent = "Windows host unavailable";
    el("state").textContent = "Run Set up BlairScope.cmd from the extracted kit, then click BlairScope again. " + reason;
    controls();
  });
  try {
    const hello = await request(s, {type: "hello"});
    if (s !== session) return;
    if (!hello.origins) throw new Error("Install the current Windows kit and run Set up BlairScope.cmd.");
    if (!mode) {
      const tab = Number.isInteger(tabId) ? await chrome.tabs.get(tabId) : null;
      if (s !== session) return;
      const onLab = tab?.url && new URL(tab.url).origin === hello.origins.sandbox;
      mode = !hello.origins.work || (onLab && hello.origins.work !== hello.origins.sandbox) ? "sandbox" : "work";
    }
    const result = await request(s, {type: "select", mode});
    if (s !== session) return;
    s.origin = result.origin;
    s.busy = false;
    showMode();
    el("state").textContent = s.origin
      ? "Click BlairScope in the signed-in Manager tab to capture a snapshot."
      : "Run Set up BlairScope.cmd to add your work Manager, or switch on Sandbox mode.";
    controls();
    if (s.captureOnReady) await capture(s);
  } catch (error) {
    if (s === session && s.connected) el("state").textContent = error.message;
  }
}

el("sandbox").addEventListener("change", () => {
  mode = el("sandbox").checked ? "sandbox" : "work";
  bind(session?.tabId, true);
});
el("refresh").addEventListener("click", () => capture(session));
el("device").addEventListener("change", () => capture(session));
document.querySelector("form").addEventListener("submit", async event => {
  event.preventDefault();
  const s = session;
  const question = el("question").value.trim();
  if (!s?.ready || s.busy || !question) return;
  s.busy = true;
  controls();
  el("progress").textContent = "Running Qwen on this laptop…";
  try {
    const reply = await request(s, {type: "ask", question});
    if (s !== session) return;
    const article = document.createElement("article");
    const title = document.createElement("strong");
    title.textContent = question;
    article.append(title, document.createElement("br"), document.createTextNode(reply.answer));
    el("thread").append(article);
    el("question").value = "";
    el("progress").textContent = "";
  } catch (error) {
    if (s === session && s.connected) el("progress").textContent = error.message;
  } finally {
    s.busy = false;
    if (s === session) controls();
  }
});

chrome.tabs.onActivated.addListener(info => {
  if (info.windowId === windowId && info.tabId !== session?.tabId) bind(info.tabId, false);
});
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.status === "loading" && tabId === session?.tabId) bind(tabId, false);
});
chrome.tabs.onRemoved.addListener(tabId => {
  if (tabId === session?.tabId) bind(null, false);
});
chrome.runtime.onMessage.addListener((message, sender) => {
  if (sender.id !== chrome.runtime.id || message.type !== "diagnose" || message.windowId !== windowId) return;
  if (session?.tabId !== message.tabId || !session.connected) bind(message.tabId, true);
  else if (session.busy) session.captureOnReady = true;
  else capture(session);
});
window.addEventListener("pagehide", () => disconnect(session), {once: true});
const [active] = await chrome.tabs.query({windowId, active: true});
if (!session) bind(active?.id, true);
