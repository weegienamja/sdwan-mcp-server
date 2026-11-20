import {collectSnapshot} from "./collect.js";

const el = id => document.getElementById(id);
const tabId = Number(location.hash.slice(1));
const spec = await (await fetch("reads.json")).json();
const port = chrome.runtime.connectNative("org.sdwan.workstation");
let manager, documentId, waiting, ready = false, busy = false, closed = false;

function controls() {
  el("ask").disabled = closed || busy || !ready;
  el("refresh").disabled = closed || busy || !manager;
  el("device").disabled = el("refresh").disabled;
}
port.onMessage.addListener(reply => {
  if (!waiting) return;
  const {resolve, reject} = waiting;
  waiting = null;
  if (reply.type === "error") reject(new Error(reply.error));
  else resolve(reply);
});
port.onDisconnect.addListener(() => {
  const reason = chrome.runtime.lastError?.message || "Workstation disconnected.";
  closed = true;
  controls();
  const error = new Error(reason + " Run Set up Edge on this laptop, then click Diagnose again.");
  el("state").textContent = error.message;
  waiting?.reject(error);
  waiting = null;
});
function host(message) {
  return new Promise((resolve, reject) => {
    if (closed || waiting) throw new Error("Workstation is unavailable.");
    waiting = {resolve, reject};
    port.postMessage(message);
  });
}
function appendTurn(question, answer) {
  const article = document.createElement("article");
  const title = document.createElement("strong");
  title.textContent = question;
  article.append(title, document.createElement("br"), document.createTextNode(answer));
  el("thread").append(article);
}
function clearView() {
  ready = false;
  el("thread").replaceChildren();
  el("sources").replaceChildren();
  el("progress").textContent = "";
}
async function capture() {
  busy = true;
  clearView();
  controls();
  el("state").textContent = "Reading Manager…";
  try {
    await host({type: "clear"});
    const tab = await chrome.tabs.get(tabId);
    if (!tab.url || new URL(tab.url).origin !== manager) {
      throw new Error("Sign in to the configured Manager tab, then click the extension's Diagnose action.");
    }
    const selected = el("device").value;
    const target = documentId ? {tabId, documentIds: [documentId]} : {tabId};
    const [frame] = await chrome.scripting.executeScript({
      target, world: "ISOLATED", func: collectSnapshot, args: [manager, spec, selected]
    });
    if (!frame?.result) throw new Error("The Manager page changed or the read was blocked. Click Diagnose again.");
    documentId = frame.documentId;
    const snapshot = frame.result;
    for (const [name, source] of Object.entries(snapshot.tables)) {
      const item = document.createElement("li");
      item.textContent = `GET ${spec[name].path} · ${source.state} · ${source.rows.length} rows`;
      el("sources").append(item);
    }
    const inv = snapshot.tables.inventory;
    if (Object.values(snapshot.tables).some(s => s.state === "sign-in required")) {
      throw new Error("Manager requires sign-in. Complete SSO in its tab and click Diagnose again.");
    }
    if (!["ok", "partial"].includes(inv.state)) throw new Error("Inventory " + inv.state + ". Check Cisco access.");
    await host({type: "capture", origin: manager, snapshot});
    el("device").replaceChildren(new Option("Estate overview", ""));
    for (const row of inv.rows) {
      if (row["system-ip"]) el("device").add(new Option(
        `${row["host-name"] || "Device"} · ${row["system-ip"]}`, row["system-ip"]
      ));
    }
    el("device").value = selected;
    const partial = Object.values(snapshot.tables).some(s => s.state !== "ok");
    el("state").textContent = `${inv.rows.length} inventory rows captured at ${new Date(snapshot.captured).toLocaleTimeString()}.` +
      (partial ? " Some sources are incomplete. See collection results." : "");
    ready = true;
  } catch (error) {
    el("state").textContent = error.message;
  } finally {
    busy = false;
    controls();
  }
}
el("refresh").addEventListener("click", capture);
el("device").addEventListener("change", capture);
document.querySelector("form").addEventListener("submit", async event => {
  event.preventDefault();
  const question = el("question").value.trim();
  if (!ready || busy || !question) return;
  busy = true;
  controls();
  el("progress").textContent = "Running Qwen on this laptop…";
  try {
    const reply = await host({type: "ask", question});
    appendTurn(question, reply.answer);
    el("question").value = "";
    el("progress").textContent = "";
  } catch (error) {
    el("progress").textContent = error.message;
  } finally {
    busy = false;
    controls();
  }
});
try {
  const hello = await host({type: "hello"});
  manager = hello.manager;
  el("manager").textContent = manager;
  await capture();
} catch (error) {
  el("state").textContent = error.message;
}
