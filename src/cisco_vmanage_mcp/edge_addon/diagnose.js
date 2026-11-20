import {collectSnapshot} from "./collect.js";
import {deleteChat, listChats, modelHistory, newChat, readChat, saveTurn} from "./chats.js";

const el = id => document.getElementById(id);
const spec = await (await fetch("reads.json")).json();
const {id: windowId} = await chrome.windows.getCurrent();
let session;
let mode;

function controls() {
  const idle = session?.connected && !session.busy && !!session.origin;
  el("refresh").disabled = !idle || !Number.isInteger(session?.tabId);
  el("device").disabled = !idle || !session?.ready;
  el("ask").disabled = el("device").disabled;
  el("new-chat").disabled = !idle;
  el("saved").disabled = !idle || !session?.choices.length;
  el("delete-chat").disabled = el("new-chat").disabled;
}

function resetChatControls() {
  el("thread").replaceChildren();
  el("saved").replaceChildren(new Option("New conversation", ""));
  el("earlier").hidden = true;
  el("chat-options").hidden = true;
  el("chat-options").open = false;
  el("delete-chat").textContent = "Delete this chat";
  el("cancel-delete").hidden = true;
}

function renderChat(s) {
  resetChatControls();
  for (const item of s.choices) {
    el("saved").add(new Option(item.title + " · " + new Date(item.updated).toLocaleString(), item.id));
  }
  const saved = !s.chat.detached && s.choices.some(item => item.id === s.chat.id);
  if (saved) el("saved").value = s.chat.id;
  else if (s.chat.turns.length) el("saved").options[0].text = "Unsaved conversation";
  el("chat-options").hidden = !saved;
  for (const {question, answer, captured} of s.chat.turns.slice(-s.visibleTurns)) {
    const article = document.createElement("article");
    const title = document.createElement("strong");
    title.textContent = question;
    const time = document.createElement("time");
    time.dateTime = captured;
    time.textContent = "Snapshot · " + new Date(captured).toLocaleString();
    article.append(title, time, document.createTextNode(answer));
    el("thread").append(article);
  }
  el("earlier").hidden = s.chat.turns.length <= s.visibleTurns;
}

function clearEvidence() {
  el("metrics").hidden = true;
  for (const id of ["total", "down", "time"]) el(id).textContent = "";
  resetChatControls();
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
    s.captured = snapshot.captured;
    if (!s.canSave) el("progress").textContent = "Chat history is available only in this panel for this session.";
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
    if (s === session && s.connected) {
      renderChat(s);
      controls();
    }
  }
}

async function bind(tabId, captureOnReady) {
  disconnect(session);
  const port = chrome.runtime.connectNative("org.sdwan.workstation");
  const s = {tabId, port, connected: true, busy: true, ready: false, captureOnReady,
    chat: newChat(), choices: [], visibleTurns: 20, canSave: false};
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
    if (!hello.chat_history) throw new Error("Install the current Windows kit and run Set up BlairScope.cmd to enable chat history.");
    if (!mode) {
      const tab = Number.isInteger(tabId) ? await chrome.tabs.get(tabId) : null;
      if (s !== session) return;
      const onLab = tab?.url && new URL(tab.url).origin === hello.origins.sandbox;
      mode = !hello.origins.work || (onLab && hello.origins.work !== hello.origins.sandbox) ? "sandbox" : "work";
    }
    const result = await request(s, {type: "select", mode});
    if (s !== session) return;
    s.origin = result.origin;
    s.scope = JSON.stringify([mode, s.origin]);
    if (s.origin && !chrome.extension.inIncognitoContext) {
      try {
        s.choices = await listChats(s.scope);
        s.canSave = true;
      } catch {
        if (s === session && s.connected) el("progress").textContent = "Local chat storage is unavailable.";
      }
    }
    if (s !== session || !s.connected) return;
    s.busy = false;
    showMode();
    renderChat(s);
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
function startChat(s) {
  s.chat = newChat();
  s.visibleTurns = 20;
  el("question").value = "";
  el("progress").textContent = "";
  renderChat(s);
  el("question").focus();
}
el("new-chat").addEventListener("click", () => startChat(session));
el("earlier").addEventListener("click", () => {
  session.visibleTurns += 20;
  renderChat(session);
});
el("saved").addEventListener("change", async () => {
  const s = session;
  const id = el("saved").value;
  if (!id) return startChat(s);
  s.busy = true;
  controls();
  try {
    const chat = await readChat(s.scope, id);
    if (s !== session || !s.connected) return;
    s.chat = chat;
    s.visibleTurns = 20;
    el("question").value = "";
    el("progress").textContent = "";
    renderChat(s);
  } catch (error) {
    if (s === session && s.connected) {
      renderChat(s);
      el("progress").textContent = error.message;
    }
  } finally {
    s.busy = false;
    if (s === session) controls();
  }
});
el("cancel-delete").addEventListener("click", () => renderChat(session));
el("delete-chat").addEventListener("click", async () => {
  const s = session;
  if (el("cancel-delete").hidden) {
    el("cancel-delete").hidden = false;
    el("delete-chat").textContent = "Confirm deletion";
    return;
  }
  s.busy = true;
  controls();
  try {
    await deleteChat(s.scope, s.chat);
    s.choices = await listChats(s.scope);
    if (s === session && s.connected) startChat(s);
  } catch (error) {
    if (s === session && s.connected) el("progress").textContent = error.message;
  } finally {
    s.busy = false;
    if (s === session) controls();
  }
});
document.querySelector("form").addEventListener("submit", async event => {
  event.preventDefault();
  const s = session;
  const question = el("question").value.trim();
  if (!s?.ready || s.busy || !question) return;
  s.busy = true;
  controls();
  el("progress").textContent = "Running Qwen on this laptop…";
  try {
    const reply = await request(s, {type: "ask", question, history: modelHistory(s.chat.turns, question)});
    if (s !== session || !s.connected) return;
    const turn = {question, answer: reply.answer, captured: s.captured};
    let notice = "This conversation is only in this panel.";
    if (s.canSave && !s.chat.detached) {
      try {
        await saveTurn(s.scope, s.chat, turn);
        const others = s.choices.filter(item => item.id !== s.chat.id);
        const title = (s.chat.turns[0]?.question || question).slice(0, 80);
        s.choices = [{id: s.chat.id, title, updated: Date.now()}, ...others];
        notice = "";
      } catch (error) {
        // Preserve the generated reply in this panel, without overwriting another window's edit.
        notice = "Reply was not saved. " + error.message;
        s.chat.detached = true;
      }
    }
    if (s !== session || !s.connected) return;
    s.chat.turns.push(turn);
    renderChat(s);
    el("question").value = "";
    el("progress").textContent = notice;
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
