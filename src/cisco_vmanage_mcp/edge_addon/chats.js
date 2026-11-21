// IndexedDB belongs to this extension and browser profile. Crypto work happens
// outside write transactions so an awaited encryption cannot commit one early.
// This persistent ID also locates the encryption key for existing conversations.
const databaseName = "blairscope-chats";
const changedMessage = "This chat changed in another window. Open Saved chats again before continuing.";
const encoder = new TextEncoder();
const decoder = new TextDecoder();
let opening;

function result(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function committed(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = resolve;
    tx.onabort = () => reject(tx.error || new Error("The local chat could not be saved."));
  });
}

async function openStore() {
  const open = indexedDB.open(databaseName, 1);
  open.onupgradeneeded = () => {
    const db = open.result;
    db.createObjectStore("keys");
    db.createObjectStore("chats", {keyPath: ["scope", "id"]}).createIndex("scope", "scope");
    db.createObjectStore("turns", {keyPath: ["scope", "id", "number"]});
  };
  const db = await result(open);
  const old = await result(db.transaction("keys").objectStore("keys").get(1));
  if (old) return {db, key: old};
  const candidate = await crypto.subtle.generateKey({name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"]);
  const tx = db.transaction("keys", "readwrite");
  const done = committed(tx);
  const keys = tx.objectStore("keys");
  const key = await result(keys.get(1)) || candidate;
  if (key === candidate) keys.add(key, 1);
  await done;
  return {db, key};
}

function store() {
  opening ??= openStore().catch(error => { opening = null; throw error; });
  return opening;
}

async function seal(key, identity, value) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = await crypto.subtle.encrypt({name: "AES-GCM", iv, additionalData: encoder.encode(identity)},
    key, encoder.encode(JSON.stringify(value)));
  return {iv, data};
}

async function unseal(key, identity, {iv, data}) {
  const plain = await crypto.subtle.decrypt({name: "AES-GCM", iv, additionalData: encoder.encode(identity)}, key, data);
  return JSON.parse(decoder.decode(plain));
}

function turnRange(scope, id) {
  return IDBKeyRange.bound([scope, id, 0], [scope, id, Number.MAX_SAFE_INTEGER]);
}

export function newChat() {
  return {id: crypto.randomUUID(), turns: []};
}

export function modelHistory(turns, question) {
  if (turns.length <= 8) return turns;
  const recent = turns.slice(-6);
  const terms = new Set(question.toLowerCase().match(/[\p{L}\d_.-]{3,}/gu) || []);
  for (const common of ["the", "and", "with", "which", "what", "that", "was", "for", "from", "how", "are", "this"]) {
    terms.delete(common);
  }
  const ranked = turns.slice(0, -6).map((turn, index) => {
    const words = new Set((turn.question + " " + turn.answer).toLowerCase().match(/[\p{L}\d_.-]{3,}/gu));
    let score = 0;
    for (const term of terms) if (words.has(term)) score++;
    return {turn, index, score};
  });
  ranked.sort((a, b) => b.score - a.score || b.index - a.index);
  return ranked.slice(0, 2).sort((a, b) => a.index - b.index).map(hit => hit.turn).concat(recent);
}

export async function listChats(scope) {
  const {db, key} = await store();
  const rows = await result(db.transaction("chats").objectStore("chats").index("scope").getAll(scope));
  rows.sort((a, b) => b.updated - a.updated);
  return Promise.all(rows.map(async row => ({
    id: row.id, updated: row.updated, title: await unseal(key, JSON.stringify([scope, row.id]), row.title)
  })));
}

export async function readChat(scope, id) {
  const {db, key} = await store();
  const rows = await result(db.transaction("turns").objectStore("turns").getAll(turnRange(scope, id)));
  if (!rows.length) throw new Error("This chat has been deleted.");
  const turns = await Promise.all(rows.map(row => unseal(key, JSON.stringify([scope, id, row.number]), row)));
  return {id, turns};
}

export async function saveTurn(scope, chat, turn) {
  const {db, key} = await store();
  const id = chat.id;
  const number = chat.turns.length + 1;
  const encrypted = await seal(key, JSON.stringify([scope, id, number]), turn);
  const title = number === 1 ? await seal(key, JSON.stringify([scope, id]), turn.question.slice(0, 80)) : null;
  const tx = db.transaction(["chats", "turns"], "readwrite");
  const done = committed(tx);
  const chats = tx.objectStore("chats");
  const prev = await result(chats.get([scope, id]));
  if ((prev?.count ?? 0) !== number - 1) {
    await done;
    throw new Error(changedMessage);
  }
  chats.put({scope, id, updated: Date.now(), count: number, title: prev?.title ?? title});
  tx.objectStore("turns").add({scope, id, number, ...encrypted});
  await done;
}

export async function deleteChat(scope, chat) {
  const {db} = await store();
  const tx = db.transaction(["chats", "turns"], "readwrite");
  const done = committed(tx);
  const chats = tx.objectStore("chats");
  const row = await result(chats.get([scope, chat.id]));
  if (row && row.count !== chat.turns.length) {
    await done;
    throw new Error(changedMessage);
  }
  chats.delete([scope, chat.id]);
  tx.objectStore("turns").delete(turnRange(scope, chat.id));
  await done;
}
