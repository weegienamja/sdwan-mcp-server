import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {collectSnapshot} from "../src/cisco_vmanage_mcp/edge_addon/collect.js";

const spec = JSON.parse(await readFile(new URL("../src/cisco_vmanage_mcp/edge_addon/reads.json", import.meta.url)));
const origin = "https://fixture.invalid";
globalThis.window = {get top() { return this; }};
globalThis.location = new URL(origin);
function json(data) { return Response.json({data}); }

test("only approved fields leave the tab and every request is a fixed same-origin GET", async t => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push(url);
    assert.equal(options.method, "GET");
    assert.equal(options.mode, "same-origin");
    assert.equal(options.redirect, "manual");
    assert.deepEqual(options.headers, {Accept: "application/json"});
    assert.equal(url.origin, origin);
    return json([{"host-name":"west","system-ip":"192.0.2.1", cookie:"private",
      "running-config":"private", nested:{secret:"private"}}]);
  });
  const capture = await collectSnapshot(origin, spec, "192.0.2.1");
  assert.deepEqual(calls.map(url => url.pathname).sort(), Object.values(spec).map(s => s.path).sort());
  assert.doesNotMatch(JSON.stringify(capture), /private|cookie|running-config/);
  assert.deepEqual(capture.tables.inventory.rows, [{"host-name":"west","system-ip":"192.0.2.1"}]);
});
test("a wrong origin or an unavailable inventory never leads to device reads", async t => {
  const stub = t.mock.method(globalThis, "fetch", async () => new Response("", {status:403}));
  await assert.rejects(collectSnapshot("https://other.invalid", spec, ""), /configured/);
  assert.equal(stub.mock.callCount(), 0);
  const snapshot = await collectSnapshot(origin, spec, "");
  assert.deepEqual(snapshot.tables, {inventory:{state:"denied",rows:[]}});
  assert.equal(stub.mock.callCount(), 1);
});
test("sign-in HTML and malformed data yield no evidence", async t => {
  const stub = t.mock.method(globalThis, "fetch", async () => new Response("login", {headers:{"Content-Type":"text/html"}}));
  assert.equal((await collectSnapshot(origin,spec,"")).tables.inventory.state, "sign-in required");
  stub.mock.mockImplementation(async () => Response.json({unexpected:[]}));
  assert.equal((await collectSnapshot(origin,spec,"")).tables.inventory.state, "unavailable");
});
test("an unlisted device cannot trigger detail reads", async t => {
  const stub = t.mock.method(globalThis, "fetch", async () => json([{"system-ip":"192.0.2.1"}]));
  await assert.rejects(collectSnapshot(origin, spec, "192.0.2.254"), /visible/);
  assert.equal(stub.mock.callCount(), 1);
});
test("oversized and paginated sources are labelled", async t => {
  const stub = t.mock.method(globalThis, "fetch", async () => Response.json({data:[], pageInfo:{hasMoreData:true}}));
  assert.equal((await collectSnapshot(origin,spec,"")).tables.inventory.state, "partial");
  stub.mock.mockImplementation(async () => new Response("a".repeat(7 * 1024 * 1024), {headers:{"Content-Type":"application/json"}}));
  assert.equal((await collectSnapshot(origin,spec,"")).tables.inventory.state, "response too large");
});
