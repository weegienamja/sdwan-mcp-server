// Run with an extracted Windows kit and local GGUF. BLAIRSCOPE_TEST_BROWSER
// can point to Chrome for Testing in place of the installed Edge.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const https = require("node:https");
const os = require("node:os");
const {randomUUID} = require("node:crypto");
const path = require("node:path");
const {spawnSync} = require("node:child_process");
const {chromium} = require(process.env.PLAYWRIGHT_CORE || "playwright-core");
const [kitArg, modelArg] = process.argv.slice(2);
if (!kitArg || !modelArg) throw new Error("Supply the extracted kit and local GGUF paths.");
const kit = path.resolve(kitArg), model = path.resolve(modelArg);
const keys = [String.raw`Microsoft\Edge`, String.raw`Google\Chrome`].map(browser =>
  String.raw`HKCU\Software\${browser}\NativeMessagingHosts\org.sdwan.workstation`
);
const root = fs.mkdtempSync(path.join(os.tmpdir(), "sdwan-edge-test-"));
const local = path.join(root, "local");
const env = {...process.env, LOCALAPPDATA: local, PYTHONUTF8: "1"};
const python = path.join(kit, "python", "python.exe");
const run = (cmd, args) => spawnSync(cmd, args, {env, encoding: "utf8", windowsHide: true});
const certScript = `
import sys
from pathlib import Path
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
p=Path(sys.argv[1])
k=rsa.generate_private_key(public_exponent=65537,key_size=2048)
n=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"Offline Manager")])
now=datetime.now(UTC)
c=(x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key())
   .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(hours=1))
   .not_valid_after(now+timedelta(days=1)).add_extension(
       x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]),False).sign(k,hashes.SHA256()))
(p/"cert.pem").write_bytes(c.public_bytes(serialization.Encoding.PEM))
(p/"key.pem").write_bytes(k.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
`;
// DevTools exposes the native sidebar as an extension target outside the tab list.
async function panelDriver(profile, extensionId) {
  const port = fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0];
  let panel;
  for (let attempt = 0; attempt < 100 && !panel; attempt++) {
    const targets = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
    panel = targets.find(t => t.url === `chrome-extension://${extensionId}/diagnose.html`);
    if (!panel) await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(panel, "The toolbar did not open the native panel.");
  const socket = new WebSocket(panel.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {socket.onopen = resolve; socket.onerror = reject;});
  let sequence = 0;
  const pending = new Map();
  socket.onmessage = event => {
    const reply = JSON.parse(event.data);
    if (pending.has(reply.id)) {
      pending.get(reply.id)(reply);
      pending.delete(reply.id);
    }
  };
  async function command(method, params) {
    const id = ++sequence;
    const response = new Promise(resolve => pending.set(id, resolve));
    socket.send(JSON.stringify({id, method, params}));
    const reply = await response;
    assert.ok(!reply.error, JSON.stringify(reply.error));
    return reply.result;
  }
  async function evaluate(expression) {
    const result = await command("Runtime.evaluate", {expression, returnByValue:true, userGesture:true});
    assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async function wait(expression, timeout = 20000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      if (await evaluate(expression)) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(expression + " timed out. " + await evaluate("document.body.innerText"));
  }
  return {evaluate, wait, command, close: () => socket.close()};
}
async function main() {
  for (const key of keys) assert.notEqual(run("reg.exe", ["query", key]).status, 0, "An existing workstation registration is in use.");
  const cert = run(python, ["-c", certScript, root]);
  assert.equal(cert.status, 0, cert.stderr);
  const calls = [];
  const omittedCanary = randomUUID();
  let expired = false, denyInventory = false;
  const server = https.createServer({
    key: fs.readFileSync(path.join(root, "key.pem")), cert: fs.readFileSync(path.join(root, "cert.pem"))
  }, (req, res) => {
    const url = new URL(req.url, "https://127.0.0.1");
    if (url.pathname === "/") {
      res.setHeader("content-type", "text/html");
      return res.end('<a href="/fixture-session">Sign in to the fixture</a>');
    }
    if (url.pathname === "/fixture-session") {
      res.setHeader("set-cookie", "fixture=operator; HttpOnly; Secure; SameSite=Strict; Path=/");
      return res.end("Synthetic Cisco session");
    }
    if (!url.pathname.startsWith("/dataservice/")) { res.writeHead(404); return res.end(); }
    calls.push([req.method, url.pathname, url.search, req.headers.cookie === "fixture=operator"]);
    if (expired || !req.headers.cookie) { res.writeHead(302, {Location: "/"}); return res.end(); }
    if (url.pathname.endsWith("/alarms") || denyInventory) { res.writeHead(403); return res.end(); }
    res.setHeader("content-type", "application/json");
    const tables = {
      "/dataservice/device": [{"host-name":"edge-fixture","system-ip":"192.0.2.14","site-id":100,
        reachability:"unreachable","device-type":"vedge",password:omittedCanary}],
      "/dataservice/alarms/count": [{severity:"Critical",count:1}],
      "/dataservice/device/bfd/sessions": [{state:"down","system-ip":"192.0.2.3"}],
      "/dataservice/device/control/connections": [], "/dataservice/device/system/status": [{min5_avg:8}]
    };
    if (!(url.pathname in tables)) {res.writeHead(404); return res.end();}
    res.end(JSON.stringify({data: tables[url.pathname]}));
  });
  let context;
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    const origin = "https://127.0.0.1:" + server.address().port;
    const sandbox = origin.replace("127.0.0.1", "localhost");
    const setup = run(python, ["-m", "cisco_vmanage_mcp.edge_host", "--setup", "--manager", origin, "--sandbox-manager", sandbox, "--model", model]);
    assert.equal(setup.status, 0, setup.stderr);
    const edge = path.join(process.env["ProgramFiles(x86)"], "Microsoft", "Edge", "Application", "msedge.exe");
    context = await chromium.launchPersistentContext(path.join(root, "profile"), {
      executablePath: process.env.BLAIRSCOPE_TEST_BROWSER || edge, headless: true, ignoreHTTPSErrors: true, env,
      ignoreDefaultArgs: ["--disable-extensions"],
      args: ["--remote-debugging-port=0", "--enable-unsafe-extension-debugging", "--window-size=1280,1100",
        "--disable-background-networking", "--no-proxy-server",
        "--load-extension=" + path.join(kit, "Edge extension"),
        "--disable-extensions-except=" + path.join(kit, "Edge extension"),
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"]
    });
    await context.route(/^https?:\/\//, route => {
      const url = new URL(route.request().url());
      return [origin, sandbox].includes(url.origin) ? route.continue() : route.abort();
    });
    const tab = context.pages()[0];
    await tab.goto(origin);
    await tab.getByText("Sign in to the fixture").click();
    const browser = await context.browser().newBrowserCDPSession();
    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    const extensionId = new URL(worker.url()).host;
    assert.equal(extensionId, "beapjnblokiomlppfhbidaaabofbolci");
    const targets = await browser.send("Target.getTargets", {filter: [{type: "tab"}, {exclude: true}]});
    const target = targets.targetInfos.find(t => t.url === tab.url()) || (
      targets.targetInfos.length === 1 ? targets.targetInfos[0] : null
    );
    assert.ok(target, "Could not identify the isolated fixture tab.");
    const trigger = () => browser.send("Extensions.triggerAction", {id: extensionId, targetId: target.targetId});
    await trigger();
    const chat = await panelDriver(path.join(root, "profile"), extensionId);
    const read = chat.evaluate;
    const wait = chat.wait;
    await wait("document.querySelector('#ask')?.disabled === false");
    assert.equal(context.pages().filter(p => p.url().includes("diagnose.html")).length, 0, "Diagnostics opened a tab");
    assert.match(await read("document.querySelector('#sources').textContent"), /alarms · denied/);
    assert.equal(calls.length, 3);
    await read("const byId = id => document.getElementById(id); byId('device').value='192.0.2.14'; byId('device').dispatchEvent(new Event('change'))");
    await wait("!byId('ask').disabled && byId('sources').textContent.includes('bfd/sessions')");
    assert.equal(calls.length, 9);
    assert.equal(calls.filter(c => c[2] === "?deviceId=192.0.2.14").length, 3);
    await read("byId('question').value='Which router is unreachable at site 100? Give its hostname.'; byId('ask').click()");
    await wait("!!document.querySelector('#thread article')", 270000);
    const answer = await read("byId('thread').innerText");
    assert.match(answer, /edge-fixture/i);
    assert.ok(!answer.includes(omittedCanary));
    assert.equal(calls.length, 9, "Qwen chat made another Manager request");
    denyInventory = true;
    await read("byId('refresh').click()");
    await wait("byId('state').textContent.includes('Inventory denied')");
    assert.ok(await read("byId('ask').disabled"));
    denyInventory = false;
    expired = true;
    await read("byId('refresh').click()");
    await wait("byId('state').textContent.includes('Manager requires sign-in')");
    assert.equal(calls.length, 11);
    expired = false;
    await read("byId('sandbox').click()");
    await wait("byId('manager').textContent.includes('localhost') && byId('state').textContent.includes('Sign in')");
    assert.ok(await read("byId('ask').disabled && !byId('thread').textContent && !byId('sources').textContent"));
    assert.equal(await read("document.querySelector('#lab a').href"), "https://developer.cisco.com/sandbox/");
    assert.equal(calls.length, 11, "Sandbox mode read the work tab");
    const tabFilter = {filter: [{type: "tab"}, {exclude: true}]};
    const beforeLab = await browser.send("Target.getTargets", tabFilter);
    const [labTab] = await Promise.all([
      context.waitForEvent("page"), read("byId('lab-manager').click()")
    ]);
    await labTab.getByText("Sign in to the fixture").click();
    const labTargets = await browser.send("Target.getTargets", tabFilter);
    const labTarget = labTargets.targetInfos.find(info => !beforeLab.targetInfos.some(old => old.targetId === info.targetId));
    assert.ok(labTarget, "The lab sign-in link did not open a new browser tab");
    await browser.send("Extensions.triggerAction", {id: extensionId, targetId: labTarget.targetId});
    await wait("!byId('ask').disabled && byId('manager').textContent.includes('localhost')");
    assert.equal(calls.length, 14);
    if (process.env.BLAIRSCOPE_SCREENSHOT) {
      const {data} = await chat.command("Page.captureScreenshot", {format:"png", captureBeyondViewport:true,
        clip: await read("({x:0, y:0, width:innerWidth, height:document.documentElement.scrollHeight, scale:1})")});
      fs.writeFileSync(process.env.BLAIRSCOPE_SCREENSHOT, Buffer.from(data, "base64"));
    }
    await read("byId('sandbox').click()");
    await wait("byId('manager').textContent.includes('127.0.0.1') && byId('state').textContent.includes('Sign in')");
    assert.equal(calls.length, 14, "Work mode read the sandbox tab");
    await tab.bringToFront();
    await trigger();
    await wait("!byId('ask').disabled && byId('manager').textContent.includes('127.0.0.1')");
    await labTab.bringToFront();
    await wait("byId('ask').disabled && !byId('sources').textContent && !byId('total').textContent");
    assert.equal(calls.length, 17, "Tab switch collected data without a toolbar action");
    assert.ok(calls.every(c => c[0] === "GET" && c[3]));
    chat.close();
    console.log("Native sidebar, local GGUF and two isolated Manager tabs passed. Seventeen GETs, zero during chat.");
  } finally {
    if (context) await context.close();
    server.close();
    for (const key of keys) {
      const registered = run("reg.exe", ["query", key, "/ve"]);
      if (registered.stdout?.includes(path.join(local, "SD-WAN Workstation", "edge-host.json"))) {
        assert.equal(run("reg.exe", ["delete", key, "/f"]).status, 0);
      }
    }
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;}).finally(() => {
  if (path.dirname(root) === path.resolve(os.tmpdir()) && path.basename(root).startsWith("sdwan-edge-test-")) {
    fs.rmSync(root, {recursive:true, force:true, maxRetries:4, retryDelay:200});
  }
});
