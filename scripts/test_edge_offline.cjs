// Run against an extracted Windows kit, an installed Edge and an existing local GGUF.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const https = require("node:https");
const os = require("node:os");
const path = require("node:path");
const {spawnSync} = require("node:child_process");
const {chromium} = require(process.env.PLAYWRIGHT_CORE || "playwright-core");
const [kitArg, modelArg] = process.argv.slice(2);
if (!kitArg || !modelArg) throw new Error("Supply the extracted kit and local GGUF paths.");
const kit = path.resolve(kitArg), model = path.resolve(modelArg);
const key = "HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts\\org.sdwan.workstation";
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
async function main() {
  assert.notEqual(run("reg.exe", ["query", key]).status, 0, "An existing workstation registration is in use.");
  const cert = run(python, ["-c", certScript, root]);
  assert.equal(cert.status, 0, cert.stderr);
  const calls = [];
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
        reachability:"unreachable","device-type":"vedge",password:"fixture-field-must-not-leave"}],
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
    const setup = run(python, ["-m", "cisco_vmanage_mcp.edge_host", "--setup", "--manager", origin, "--model", model]);
    assert.equal(setup.status, 0, setup.stderr);
    const edge = path.join(process.env["ProgramFiles(x86)"], "Microsoft", "Edge", "Application", "msedge.exe");
    context = await chromium.launchPersistentContext(path.join(root, "profile"), {
      executablePath: edge, headless: true, ignoreHTTPSErrors: true, env,
      ignoreDefaultArgs: ["--disable-extensions"],
      args: ["--enable-unsafe-extension-debugging", "--disable-background-networking", "--no-proxy-server",
        "--load-extension=" + path.join(kit, "Edge extension"),
        "--disable-extensions-except=" + path.join(kit, "Edge extension"),
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"]
    });
    await context.route(/^https?:\/\//, route => {
      const url = new URL(route.request().url());
      return url.origin === origin ? route.continue() : route.abort();
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
    const [chat] = await Promise.all([
      context.waitForEvent("page", {timeout: 15000}),
      browser.send("Extensions.triggerAction", {id: extensionId, targetId: target.targetId})
    ]);
    await chat.waitForURL(/diagnose\.html/);
    await chat.waitForLoadState();
    chat.on("pageerror", error => console.error(error.message));
    try { await chat.waitForFunction(() => document.querySelector("#ask")?.disabled === false, null, {timeout: 20000}); }
    catch (error) { throw new Error((await chat.locator("body").innerText()) + " / " + error.message); }
    assert.match(await chat.locator("#sources").textContent(), /alarms · denied/);
    assert.equal(calls.length, 3);
    await chat.locator("#device").selectOption("192.0.2.14");
    await chat.waitForFunction(() => document.querySelector("#sources")?.textContent.includes("bfd/sessions"));
    assert.equal(calls.length, 9);
    assert.equal(calls.filter(c => c[2] === "?deviceId=192.0.2.14").length, 3);
    await chat.locator("#question").fill("Which router is unreachable at site 100? Give its hostname.");
    await chat.locator("#ask").click();
    await chat.waitForFunction(() => document.querySelector("#thread article") !== null, null, {timeout: 270000});
    const answer = await chat.locator("#thread").innerText();
    assert.match(answer, /edge-fixture/i);
    assert.doesNotMatch(answer, /fixture-field-must-not-leave/);
    assert.equal(calls.length, 9, "Qwen chat made another Manager request");
    denyInventory = true;
    await chat.locator("#refresh").click();
    await chat.getByText(/Inventory denied/).waitFor();
    assert.equal(await chat.locator("#ask").isDisabled(), true);
    denyInventory = false;
    expired = true;
    await chat.locator("#refresh").click();
    await chat.getByText(/Manager requires sign-in/).waitFor();
    assert.equal(await chat.locator("#ask").isDisabled(), true);
    assert.equal(calls.length, 11);
    assert.ok(calls.every(c => c[0] === "GET" && c[3]));
    console.log("Packaged Edge action, native host and local Qwen passed. Eleven fixture GETs; zero Manager reads during chat.");
  } finally {
    if (context) await context.close();
    server.close();
    const registered = run("reg.exe", ["query", key, "/ve"]);
    if (registered.stdout?.includes(path.join(local, "SD-WAN Workstation", "edge-host.json"))) {
      assert.equal(run("reg.exe", ["delete", key, "/f"]).status, 0);
    }
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;}).finally(() => {
  if (path.dirname(root) === path.resolve(os.tmpdir()) && path.basename(root).startsWith("sdwan-edge-test-")) {
    fs.rmSync(root, {recursive:true, force:true, maxRetries:4, retryDelay:200});
  }
});
