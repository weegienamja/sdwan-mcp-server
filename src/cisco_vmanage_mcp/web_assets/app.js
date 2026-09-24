const byId = (id) => document.getElementById(id);
const API_BASE = "/api/v1";
const pinCandidates = new Map();
const topologyCandidates = new Map();
const relationshipCandidates = new Map();

const state = {
  overview: null,
  topology: null,
  actions: null,
  snapshots: [],
  assuranceLoaded: false,
  changePlans: [],
  changesLoaded: false,
  connectors: [],
  workflows: [],
  collaboration: null,
  audienceView: null,
  actors: [],
  agents: [],
  integrationsLoaded: false,
  devices: [],
  selectedContext: null,
  metadata: null,
  assistantMode: "evidence",
  busy: false,
  currentInvestigation: null,
  investigations: [],
  inventoryLoaded: false,
  abortController: null,
  setup: null,
  setupLoaded: false,
  setupQualification: null,
  setupTestReady: false,
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function replaceStateItem(collectionName, item, render) {
  state[collectionName] = state[collectionName].map((existing) => (
    existing.id === item.id ? item : existing
  ));
  render(state[collectionName]);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 204) {
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return null;
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`Unexpected response from ${url}`);
  }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function requestEventStream(url, options, onEvent) {
  const response = await fetch(url, {
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  if (!response.body) throw new Error("Streaming is unavailable in this browser");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let dataLines = [];

  async function consumeLine(line) {
    if (line === "") {
      if (dataLines.length) {
        await onEvent(JSON.parse(dataLines.join("\n")));
        dataLines = [];
      }
      return;
    }
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let newline = buffer.indexOf("\n");
      while (newline !== -1) {
        const line = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        await consumeLine(line);
        newline = buffer.indexOf("\n");
      }
      if (done) break;
    }
    if (buffer) await consumeLine(buffer.replace(/\r$/, ""));
    await consumeLine("");
  } finally {
    reader.releaseLock();
  }
}

function announceError(message) {
  const announcer = byId("error-announcer");
  announcer.textContent = "";
  requestAnimationFrame(() => { announcer.textContent = message; });
}

function stablePinId(type, value) {
  const normalized = String(value).toLowerCase().replace(/[^a-z0-9.-]+/g, "-").replace(/^-|-$/g, "");
  return `${type}:${(normalized || "item").slice(0, 120)}`;
}

function isPinned(pinId) {
  return Boolean(state.currentInvestigation?.pins?.some((pin) => pin.id === pinId));
}

function makePinButton(pin) {
  pinCandidates.set(pin.id, pin);
  const button = element("button", "pin-button", isPinned(pin.id) ? "Pinned" : "Pin");
  button.type = "button";
  button.dataset.pinId = pin.id;
  button.disabled = isPinned(pin.id);
  button.setAttribute("aria-label", `Pin ${pin.label} to this investigation`);
  return button;
}

function updatePinButtons() {
  document.querySelectorAll("[data-pin-id]").forEach((button) => {
    button.disabled = isPinned(button.dataset.pinId);
    button.textContent = button.disabled ? "Pinned" : "Pin";
  });
}

function setConnection(connected, error = "") {
  const label = byId("connection-state");
  label.classList.toggle("connected", connected);
  label.classList.toggle("disconnected", !connected);
  label.lastChild.textContent = connected ? " Connected" : " Offline";
  label.title = error || (connected ? "vManage client is ready" : "vManage is unavailable");
  label.setAttribute("aria-label", `vManage connection status: ${connected ? "connected" : "offline"}`);
}

function setSetupMessage(message, status = "") {
  const output = byId("setup-message");
  output.className = `setup-message ${status}`.trim();
  output.textContent = message;
}

function setSetupProgress(step, status) {
  const item = document.querySelector(`[data-setup-progress="${step}"]`);
  item.classList.remove("complete", "active", "attention");
  if (status) item.classList.add(status);
}

function setSetupFormEditable(editable) {
  byId("setup-form").querySelectorAll("input, button").forEach((control) => {
    control.disabled = !editable;
  });
  byId("setup-qualify-button").disabled = !state.setup?.complete;
  if (editable) byId("setup-save-button").disabled = !state.setupTestReady;
}

function setupFormValues() {
  return {
    host: byId("setup-host").value.trim(),
    port: Number(byId("setup-port").value),
    username: byId("setup-username").value.trim(),
    password: byId("setup-password").value,
    verify_ssl: byId("setup-verify-ssl").checked,
    ca_bundle: byId("setup-ca-bundle").value.trim() || null,
  };
}

function countList(title, values = {}) {
  const section = element("section", "");
  section.append(element("h3", "", title));
  const list = element("ul", "");
  const entries = Object.entries(values);
  if (!entries.length) {
    list.append(element("li", "", "No values reported"));
  } else {
    entries.forEach(([label, count]) => list.append(element("li", "", `${label} · ${count}`)));
  }
  section.append(list);
  return section;
}

function renderSetupQualification(report) {
  state.setupQualification = report;
  const container = byId("setup-qualification");
  container.replaceChildren();
  const available = (report.capabilities || []).filter((item) => item.status === "available").length;
  const summary = element("dl", "setup-result-summary");
  [
    ["Readiness", report.ready ? "Ready" : "Not ready"],
    ["Manager", report.manager],
    ["Devices", String(report.device_count)],
    ["Capabilities", `${available}/${(report.capabilities || []).length} available`],
  ].forEach(([label, value]) => {
    const item = element("div", "");
    item.append(element("dt", "", label), element("dd", "", value));
    summary.append(item);
  });
  container.append(summary);

  const profiles = element("div", "setup-profile-grid");
  profiles.append(
    countList("Software versions", report.software_versions),
    countList("Device families", report.device_models),
    countList("Device roles", report.device_types),
  );
  container.append(profiles);

  const tableWrap = element("div", "setup-capability-table");
  const table = element("table", "");
  const caption = element("caption", "", "Read-only vManage capability qualification");
  const head = element("thead", "");
  const headingRow = element("tr", "");
  ["Capability", "Endpoint", "Required", "Status", "Rows", "Time", "Notes"].forEach((label) => {
    headingRow.append(element("th", "", label));
  });
  head.append(headingRow);
  const body = element("tbody", "");
  (report.capabilities || []).forEach((capability) => {
    const row = element("tr", "");
    const statusCell = element("td", "");
    statusCell.append(element("span", `setup-capability-state ${capability.status}`, capability.status));
    [
      element("td", "", capability.id),
      element("td", "", capability.endpoint),
      element("td", "", capability.required ? "Yes" : "No"),
      statusCell,
      element("td", "", capability.row_count === null ? "—" : String(capability.row_count)),
      element("td", "", `${Math.round(capability.duration_ms || 0)}ms`),
      element("td", "", capability.reason || "—"),
    ].forEach((cell) => row.append(cell));
    body.append(row);
  });
  table.append(caption, head, body);
  tableWrap.append(table);
  container.append(tableWrap);

  if ((report.warnings || []).length) {
    const warnings = element("ul", "setup-warning-list");
    report.warnings.forEach((warning) => warnings.append(element("li", "", warning)));
    container.append(warnings);
  }
  byId("setup-download-report").disabled = false;
  setSetupProgress("qualification", report.ready ? "complete" : "attention");
}

function downloadSetupQualification() {
  if (!state.setupQualification) return;
  const blob = new Blob(
    [`${JSON.stringify(state.setupQualification, null, 2)}\n`],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `cisco-sdwan-qualification-${new Date().toISOString().slice(0, 10)}.json`;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function copySetupInstallCommand() {
  const command = byId("setup-install-command").textContent;
  try {
    await navigator.clipboard.writeText(command);
    setSetupMessage("Installer command copied. Run it in the operator workstation terminal.", "success");
  } catch {
    setSetupMessage(`Copy and run this command: ${command}`, "error");
  }
}

function renderSetupStatus(setup) {
  state.setup = setup;
  state.setupLoaded = true;
  const connection = setup.connection;
  byId("setup-mode").textContent = setup.mode === "local" ? "Local guided setup" : "Enterprise deployment-managed";
  byId("setup-role").textContent = setup.identity.role;
  byId("setup-tenant").textContent = setup.identity.tenant;
  byId("setup-storage").textContent = connection.config_file;

  const overall = byId("setup-overall-state");
  overall.className = `setup-state ${setup.complete ? "complete" : "attention"}`;
  overall.textContent = setup.complete ? "Connected" : "Action needed";
  setSetupProgress("deployment", "complete");
  setSetupProgress("connection", setup.complete ? "complete" : "active");
  if (!state.setupQualification) setSetupProgress("qualification", setup.complete ? "active" : "");
  setSetupProgress("integrations", "complete");

  const notice = byId("setup-deployment-notice");
  notice.textContent = setup.editable
    ? "Local owner mode: validated settings are written to an owner-only file on this machine. You can safely retest or replace the connection here."
    : "Enterprise mode: connection and identity settings are controlled by the deployment platform. Use this screen to verify status; change secrets through the approved secret manager.";

  const form = byId("setup-form");
  if (!form.dataset.dirty) {
    byId("setup-host").value = connection.host || "";
    byId("setup-port").value = connection.port || 443;
    byId("setup-verify-ssl").checked = connection.verify_ssl;
  }
  setSetupFormEditable(setup.editable);
  if (!setup.editable) {
    setSetupMessage("Connection changes are disabled here because this deployment is managed externally.");
  } else if (setup.complete) {
    setSetupMessage("vManage is connected. Test any changed values before saving them.", "success");
  } else {
    const guidance = connection.credentials_configured
      ? "The saved connection could not start. Check the fields below and test again."
      : "Credentials are not configured. Enter a read-only account below, then test the connection.";
    const detail = connection.error ? ` Technical detail: ${connection.error}` : "";
    setSetupMessage(`${guidance}${detail}`, connection.error ? "error" : "");
  }

  const integrations = byId("setup-integration-list");
  integrations.replaceChildren();
  setup.integrations.forEach((integration) => {
    const item = element("article", `setup-integration-item ${integration.state}`);
    item.append(
      element("h3", "", integration.name),
      element("p", "", (integration.capabilities || []).join(" · ") || "No capabilities reported"),
      element("span", "", integration.optional ? `${integration.state} · optional` : integration.state),
    );
    if ((integration.configuration_keys || []).length) {
      item.append(
        element("p", "configuration-keys", `Configure: ${integration.configuration_keys.join(" + ")}`),
      );
    }
    integrations.append(item);
  });
  byId("setup-ai-status").textContent = setup.browser_ai.configured
    ? `AI explanations are configured with ${setup.browser_ai.provider}. Evidence mode remains available.`
    : `AI explanations are optional. Configure ${setup.browser_ai.configuration_keys.join(" and ")} later if approved; deterministic evidence mode is fully available.`;
  byId("setup-open-canvas").disabled = !setup.complete;
  byId("setup-finish-title").textContent = setup.complete ? "Core setup is complete" : "Setup is not complete";
  byId("setup-finish-detail").textContent = setup.complete
    ? "The full Operations Canvas is ready. Optional integrations can be configured later."
    : "Connect and qualify vManage to open the operations workspace.";
}

async function loadSetup() {
  renderSetupStatus(await requestJson(`${API_BASE}/setup`));
}

async function testSetupConnection() {
  const form = byId("setup-form");
  if (!form.reportValidity()) return;
  const button = byId("setup-test-button");
  button.disabled = true;
  byId("setup-save-button").disabled = true;
  setSetupMessage("Testing authentication and all read-only capabilities…", "testing");
  try {
    const result = await requestJson(`${API_BASE}/setup/test`, {
      method: "POST",
      body: JSON.stringify(setupFormValues()),
    });
    renderSetupQualification(result.qualification);
    if (result.ready) {
      state.setupTestReady = true;
      byId("setup-save-button").disabled = false;
      setSetupMessage("Connection passed. Review the report, then save and connect.", "success");
    } else {
      setSetupMessage("Authentication succeeded, but required capabilities are unavailable. Review the report before continuing.", "error");
    }
  } catch (error) {
    state.setupTestReady = false;
    setSetupProgress("qualification", "attention");
    setSetupMessage(error.message, "error");
  } finally {
    button.disabled = !state.setup?.editable;
  }
}

async function qualifyCurrentSetupConnection() {
  const button = byId("setup-qualify-button");
  button.disabled = true;
  setSetupMessage("Qualifying the connected environment across all read-only APIs…", "testing");
  try {
    const result = await requestJson(`${API_BASE}/setup/qualify`, { method: "POST" });
    renderSetupQualification(result.qualification);
    setSetupMessage(
      result.ready
        ? "Current environment is ready. The full report is shown below."
        : "Current environment needs attention. Review the report below.",
      result.ready ? "success" : "error",
    );
  } catch (error) {
    setSetupMessage(error.message, "error");
  } finally {
    button.disabled = !state.setup?.complete;
  }
}

async function applySetupConnection() {
  const form = byId("setup-form");
  if (!form.reportValidity() || !state.setupTestReady) return;
  const button = byId("setup-save-button");
  button.disabled = true;
  setSetupMessage("Saving the private configuration and connecting the canvas…", "testing");
  try {
    const result = await requestJson(`${API_BASE}/setup/apply`, {
      method: "POST",
      body: JSON.stringify(setupFormValues()),
    });
    renderSetupQualification(result.qualification);
    byId("setup-password").value = "";
    form.dataset.dirty = "";
    state.setupTestReady = false;
    state.metadata = await requestJson(`${API_BASE}/meta`);
    setConnection(true);
    await loadSetup();
    await initializeInvestigations();
    await loadOverview();
    byId("setup-save-button").disabled = true;
    setSetupMessage("Connection saved. The Operations Canvas is ready.", "success");
  } catch (error) {
    setSetupMessage(error.message, "error");
  }
}

function renderSources(sources = []) {
  const list = byId("source-list");
  list.replaceChildren();
  if (!sources.length) {
    list.append(element("li", "empty-state", "No source calls yet"));
    return;
  }
  sources.forEach((source) => {
    const item = element("li", source.state === "failed" ? "failed" : "");
    item.append(element("i", ""));
    item.append(element("span", "", source.label));
    item.append(element("time", "", source.duration_ms === undefined ? "" : `${Math.round(source.duration_ms)}ms`));
    item.append(makePinButton({
      id: stablePinId("source", source.label),
      type: "source",
      label: source.label,
      data: {
        state: source.state,
        duration_ms: source.duration_ms,
        detail: source.detail || "",
      },
    }));
    if (source.detail) item.title = source.detail;
    list.append(item);
  });
}

function renderRootCauses(causes = []) {
  const list = byId("root-causes");
  list.replaceChildren();
  if (!causes.length) {
    list.append(element("li", "empty-state", "No active hypotheses"));
    return;
  }
  causes.forEach((cause) => {
    const item = element("li", "");
    const title = element("strong", "", cause.title || cause);
    item.append(title);
    if (cause.confidence) item.append(element("span", "", ` · ${cause.confidence} confidence`));
    item.append(makePinButton({
      id: stablePinId("hypothesis", `${cause.rank || 0}-${cause.title || cause}`),
      type: "hypothesis",
      label: cause.title || cause,
      data: {
        rank: cause.rank || null,
        confidence: cause.confidence || "unknown",
        evidence: cause.evidence || [],
        checks: cause.checks || [],
      },
    }));
    list.append(item);
  });
}

function renderOverview(overview) {
  state.overview = overview;
  byId("health-value").textContent = overview.health;
  byId("health-light").className = overview.health;
  byId("health-light").setAttribute("aria-label", `Fabric health: ${overview.health}`);
  byId("device-count").textContent = overview.devices.total;
  byId("unreachable-count").textContent = overview.devices.unreachable;
  byId("critical-count").textContent = overview.alarms.Critical || 0;
  byId("impact-scope").textContent = overview.impact.scope;

  const impact = byId("impact-panel");
  impact.replaceChildren(
    element("span", "", "Impact"),
    element("strong", "", overview.impact.scope),
    element("p", "", overview.impact.summary || "No affected scope identified."),
  );
  renderRootCauses(overview.root_causes);
  renderSources(overview.sources);
  const freshness = byId("evidence-freshness");
  const updated = new Date(overview.updated_at);
  freshness.dateTime = overview.updated_at;
  freshness.textContent = Number.isNaN(updated.getTime())
    ? "Evidence time unavailable"
    : `Evidence as of ${updated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
}

function addMessage(role, text, status = "") {
  const conversation = byId("conversation");
  const shouldStickToBottom = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 48;
  const message = element("article", `message ${role}-message ${status}`.trim());
  if (status === "critical") {
    message.setAttribute("role", "alert");
    message.setAttribute("aria-live", "assertive");
  }
  message.append(element("div", "message-author", role === "user" ? "You" : "Operations assistant"));
  message.append(element("p", "", text));
  conversation.append(message);
  if (shouldStickToBottom) conversation.scrollTop = conversation.scrollHeight;
  return message;
}

function addErrorMessage(text, retryPrompt = "") {
  announceError(text);
  const message = addMessage("assistant", text, "critical");
  if (retryPrompt) {
    const retry = element("button", "button button-quiet retry-button", "Try again");
    retry.type = "button";
    retry.dataset.retryPrompt = retryPrompt;
    message.append(retry);
  }
  return message;
}

function renderDetails(message, result) {
  const data = result.data || {};
  const details = element("div", "result-details");
  const values = [];

  if (result.kind === "devices") {
    values.push(["Matching devices", data.count]);
    renderInventory(data.items || []);
  } else if (result.kind === "alarms") {
    values.push(["Matching alarms", data.count]);
    const critical = (data.items || []).filter((item) => String(item.severity).toLowerCase() === "critical").length;
    values.push(["Critical", critical]);
  } else if (result.kind === "diagnosis" && data.device) {
    values.push(["Device", data.device.hostname]);
    values.push(["Site", data.device.site_id]);
    values.push(["BFD sessions", data.device.bfd_sessions]);
    values.push(["Control connections", data.device.control_connections]);
  } else if (result.kind === "change-readiness") {
    values.push(["Recommendation", String(result.status).toUpperCase()]);
    values.push(["Fabric health", data.health]);
  } else if (result.kind === "health") {
    values.push(["Impact", data.impact?.scope || "unknown"]);
    values.push(["Unreachable", data.devices?.unreachable ?? 0]);
  }

  if (!values.length) return;
  const description = element("dl", "");
  values.forEach(([label, value]) => {
    const group = element("div", "");
    group.append(element("dt", "", String(label)), element("dd", "", String(value)));
    description.append(group);
  });
  details.append(description);
  if (result.kind === "alarms" && data.items?.length) {
    const list = element("ul", "result-list");
    data.items.forEach((alarm) => {
      const item = element("li", "");
      const label = `${alarm.severity}: ${alarm.type} on ${alarm.hostname}`;
      item.append(element("span", "", label));
      item.append(makePinButton({
        id: stablePinId("alarm", alarm.id),
        type: "alarm",
        label,
        data: alarm,
      }));
      list.append(item);
    });
    details.append(list);
  }
  if (result.kind === "diagnosis" && data.device) {
    details.append(makePinButton({
      id: stablePinId("device", data.device.system_ip),
      type: "device",
      label: data.device.hostname,
      data: {
        hostname: data.device.hostname,
        system_ip: data.device.system_ip,
        site_id: data.device.site_id,
        model: data.device.model,
        health: data.device.health,
        reachable: data.device.reachable,
      },
    }));
  }
  if (result.deterministic_answer) {
    const baseline = element("div", "deterministic-baseline");
    baseline.append(
      element("strong", "", "Deterministic baseline"),
      document.createTextNode(result.deterministic_answer),
    );
    details.append(baseline);
  }
  message.append(details);
}

function renderInventory(devices = null) {
  if (devices) state.devices = devices;
  const siteId = state.selectedContext?.siteId;
  const visibleDevices = siteId
    ? state.devices.filter((device) => String(device.site_id) === String(siteId))
    : state.devices;
  const context = byId("inventory-context");
  context.hidden = !siteId;
  context.querySelector("span").textContent = siteId ? `Showing site ${siteId}` : "";
  const body = byId("inventory-body");
  body.replaceChildren();
  if (!visibleDevices.length) {
    const row = element("tr", "");
    const cell = element("td", "empty-state", siteId ? `No devices found at site ${siteId}` : "No device rows loaded");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
    return;
  }
  visibleDevices.forEach((device) => {
    const row = element("tr", "");
    const deviceButton = element("button", "link-button", device.hostname);
    deviceButton.type = "button";
    deviceButton.dataset.diagnoseIp = device.system_ip;
    row.append(
      element("td", ""),
      element("td", "", device.system_ip),
      element("td", "", device.type),
      element("td", "", device.site_id),
    );
    row.firstChild.append(deviceButton);
    const stateCell = element("td", "");
    const stateLabel = element("span", `state-cell ${device.reachability}`);
    stateLabel.append(element("i", ""), document.createTextNode(device.reachability));
    stateCell.append(stateLabel);
    row.append(stateCell);
    const evidenceCell = element("td", "");
    evidenceCell.append(makePinButton({
      id: stablePinId("device", device.system_ip),
      type: "device",
      label: device.hostname,
      data: device,
    }));
    row.append(evidenceCell);
    body.append(row);
  });
  state.inventoryLoaded = true;
  if (!document.querySelector('[data-view-panel="inventory"]').classList.contains("hidden")) {
    requestAnimationFrame(() => byId("inventory-table").focus());
  }
}

function statusRank(status) {
  return { unknown: 0, healthy: 1, degraded: 2, critical: 3 }[status] || 0;
}

function worseStatus(first, second) {
  return statusRank(first) >= statusRank(second) ? first : second;
}

function formatFreshness(value) {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime())
    ? "Evidence time unavailable"
    : `Evidence as of ${timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
}

function topologyLane(title, description, items) {
  const lane = element("section", "topology-lane");
  const heading = element("header", "");
  heading.append(element("h2", "", title), element("p", "", description));
  const grid = element("div", "topology-node-grid");
  if (!items.length) grid.append(element("p", "empty-state", "No current evidence"));
  items.forEach((item) => {
    topologyCandidates.set(item.id, item);
    const button = element("button", `topology-node ${item.status}`);
    button.type = "button";
    button.dataset.topologyNode = item.id;
    button.setAttribute("aria-label", `${item.label}, ${item.status}`);
    button.append(
      element("span", "", item.kind),
      element("strong", "", item.label),
      element("span", "", item.meta || item.status),
    );
    grid.append(button);
  });
  lane.append(heading, grid);
  return lane;
}

function renderTopologyContext(item) {
  const panel = byId("topology-context");
  panel.replaceChildren();
  if (!item) {
    panel.append(
      element("span", "eyebrow", "Selection"),
      element("h2", "", "No object selected"),
      element("p", "", "Select a controller, transport, site, or relationship to inspect its evidence."),
    );
    return;
  }
  panel.append(
    element("span", "eyebrow", item.kind === "relationship" ? "Relationship" : item.kind),
    element("h2", "", item.label),
    element("p", "", `Current state: ${item.status}`),
  );
  const description = element("dl", "");
  const values = item.kind === "relationship"
    ? [["From", item.source], ["To", item.target], ["Type", item.relationshipKind]]
    : [
        ["System IP", item.system_ip],
        ["Site", item.site_id],
        ["Device type", item.device_type],
        ["Transport", item.color],
        ["Members", item.memberCount],
      ];
  values.filter(([, value]) => value !== null && value !== undefined).forEach(([label, value]) => {
    const group = element("div", "");
    group.append(element("dt", "", label), element("dd", "", String(value)));
    description.append(group);
  });
  panel.append(description, element("h3", "", "Evidence"));
  const evidence = element("ul", "");
  (item.evidence || []).forEach((source) => evidence.append(element("li", "", source)));
  panel.append(evidence);

  const actions = element("div", "context-actions");
  if (item.site_id) {
    const inventory = element("button", "button button-quiet", "View site inventory");
    inventory.type = "button";
    inventory.dataset.viewSite = item.site_id;
    actions.append(inventory);
  }
  if (item.system_ip) {
    const investigate = element("button", "button button-primary", "Investigate device");
    investigate.type = "button";
    investigate.dataset.diagnoseIp = item.system_ip;
    actions.append(investigate);
  }
  if (state.metadata?.manager_url) {
    const manager = element("a", "button button-quiet", "Open vManage");
    manager.href = state.metadata.manager_url;
    manager.target = "_blank";
    manager.rel = "noopener noreferrer";
    actions.append(manager);
  }
  if (actions.children.length) panel.append(actions);
}

function renderTopology(topology) {
  state.topology = topology;
  topologyCandidates.clear();
  relationshipCandidates.clear();
  const summaryValues = [
    topology.summary.sites,
    topology.summary.edges,
    topology.summary.tlocs,
    topology.summary.tunnels,
  ];
  byId("topology-summary").querySelectorAll("dd").forEach((value, index) => {
    value.textContent = summaryValues[index];
  });
  const freshness = byId("topology-freshness");
  freshness.dateTime = topology.generated_at;
  freshness.textContent = formatFreshness(topology.generated_at);

  const deviceNodes = topology.nodes.filter((node) => ["controller", "edge"].includes(node.kind));
  const controllers = deviceNodes
    .filter((node) => node.kind === "controller")
    .map((node) => ({ ...node, meta: node.device_type }));
  const sites = topology.nodes
    .filter((node) => node.kind === "site")
    .map((node) => ({
      ...node,
      memberCount: deviceNodes.filter((device) => device.site_id === node.site_id).length,
      meta: `${deviceNodes.filter((device) => device.site_id === node.site_id).length} devices`,
    }));
  const transportsByColor = new Map();
  topology.nodes.filter((node) => node.kind === "tloc").forEach((node) => {
    const key = node.color || "unknown";
    const current = transportsByColor.get(key) || {
      id: `transport:${key}`,
      kind: "transport",
      label: key,
      color: key,
      status: "unknown",
      memberCount: 0,
      evidence: [],
    };
    current.status = worseStatus(current.status, node.status);
    current.memberCount += 1;
    current.meta = `${current.memberCount} TLOCs`;
    current.evidence = [...new Set([...current.evidence, ...node.evidence])];
    transportsByColor.set(key, current);
  });

  const map = byId("topology-map");
  map.replaceChildren(
    topologyLane("Controllers", "Management and control plane", controllers),
    topologyLane("Transports", "TLOC colors and tunnel state", [...transportsByColor.values()]),
    topologyLane("Sites", "Aggregated branch fault domains", sites),
  );
  const relationshipSection = element("section", "topology-relationships");
  relationshipSection.append(element("h2", "", "Observed relationships"));
  const relationshipList = element("div", "relationship-list");
  const visibleRelationships = topology.edges.filter((edge) => ["bfd", "control"].includes(edge.kind));
  if (!visibleRelationships.length) relationshipList.append(element("p", "empty-state", "No tunnel or control relationships collected"));
  visibleRelationships.slice(0, 100).forEach((edge) => {
    const item = {
      ...edge,
      id: edge.id,
      kind: "relationship",
      relationshipKind: edge.kind,
    };
    relationshipCandidates.set(edge.id, item);
    const button = element("button", edge.status, `${edge.kind.toUpperCase()} · ${edge.label}`);
    button.type = "button";
    button.dataset.topologyEdge = edge.id;
    relationshipList.append(button);
  });
  relationshipSection.append(relationshipList);
  map.append(relationshipSection);
  const firstIssue = [...sites, ...controllers].find((node) => node.status === "critical");
  renderTopologyContext(firstIssue || sites[0] || controllers[0] || null);
}

function selectTopologyNode(nodeId) {
  document.querySelectorAll("[data-topology-node]").forEach((button) => {
    button.classList.toggle("selected", button.dataset.topologyNode === nodeId);
  });
  renderTopologyContext(topologyCandidates.get(nodeId));
}

function selectTopologyEdge(edgeId) {
  document.querySelectorAll("[data-topology-node]").forEach((button) => button.classList.remove("selected"));
  renderTopologyContext(relationshipCandidates.get(edgeId));
}

async function loadTopology() {
  const map = byId("topology-map");
  map.setAttribute("aria-busy", "true");
  try {
    renderTopology(await requestJson(`${API_BASE}/topology`));
  } catch (error) {
    map.replaceChildren(element("p", "empty-state", error.message));
    announceError(error.message);
  } finally {
    map.setAttribute("aria-busy", "false");
  }
}

function renderActions(inbox) {
  state.actions = inbox;
  const priorities = ["P1", "P2", "P3", "P4"];
  byId("actions-summary").querySelectorAll("dd").forEach((value, index) => {
    value.textContent = inbox.summary[priorities[index]] || 0;
  });
  const freshness = byId("actions-freshness");
  freshness.dateTime = inbox.generated_at;
  freshness.textContent = formatFreshness(inbox.generated_at);
  const list = byId("actions-list");
  list.replaceChildren();
  if (!inbox.items.length) {
    list.append(element("p", "empty-state", "No current alarm or event actions"));
    return;
  }
  inbox.items.forEach((action) => {
    const item = element("article", "action-item");
    item.append(element("span", `priority-badge ${action.priority}`, action.priority));
    const body = element("div", "");
    body.append(
      element("h2", "", action.title),
      element("p", "", `${action.alarm_count} alarms · ${action.event_count} events · site ${action.site_id}`),
    );
    const evidence = element("ul", "action-evidence");
    action.evidence.slice(0, 5).forEach((record, index) => {
      const row = element("li", "");
      const label = `${record.severity}: ${record.label} on ${record.hostname}`;
      row.append(element("span", "", label));
      row.append(makePinButton({
        id: stablePinId(record.kind === "alarm" ? "alarm" : "source", `${action.id}-${index}-${record.label}`),
        type: record.kind === "alarm" ? "alarm" : "source",
        label,
        data: record,
      }));
      evidence.append(row);
    });
    body.append(evidence);
    const controls = element("div", "context-actions");
    if (action.system_ips.length) {
      const investigate = element("button", "button button-primary", "Investigate");
      investigate.type = "button";
      investigate.dataset.diagnoseIp = action.system_ips[0];
      controls.append(investigate);
    }
    const site = element("button", "button button-quiet", "View site");
    site.type = "button";
    site.dataset.viewSite = action.site_id;
    controls.append(site);
    item.append(body, controls);
    list.append(item);
  });
  updatePinButtons();
}

async function loadActions() {
  const list = byId("actions-list");
  list.setAttribute("aria-busy", "true");
  try {
    renderActions(await requestJson(`${API_BASE}/actions`));
  } catch (error) {
    list.replaceChildren(element("p", "empty-state", error.message));
    announceError(error.message);
  } finally {
    list.setAttribute("aria-busy", "false");
  }
}

async function showInventoryForSite(siteId) {
  state.selectedContext = { siteId };
  if (state.inventoryLoaded) renderInventory();
  await switchView("inventory");
}

async function investigateDevice(systemIp) {
  state.selectedContext = { systemIp };
  await switchView("investigate");
  await ask(`Diagnose ${systemIp}`);
}

function snapshotLabel(snapshot) {
  const captured = new Date(snapshot.captured_at);
  const time = Number.isNaN(captured.getTime()) ? snapshot.captured_at : captured.toLocaleString();
  return `${time} · ${snapshot.health}${snapshot.partial ? " · partial" : ""}`;
}

function renderSnapshotHistory() {
  const body = byId("snapshot-history");
  body.replaceChildren();
  if (!state.snapshots.length) {
    const row = element("tr", "");
    const cell = element("td", "empty-state", "No snapshots captured");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
    return;
  }
  state.snapshots.forEach((snapshot) => {
    const row = element("tr", "");
    const captured = new Date(snapshot.captured_at);
    row.append(
      element("td", "", Number.isNaN(captured.getTime()) ? snapshot.captured_at : captured.toLocaleString()),
      element("td", snapshot.partial ? "partial-label" : "", snapshot.partial ? `${snapshot.health} · partial` : snapshot.health),
      element("td", "", String(snapshot.device_count)),
      element("td", "", String(snapshot.critical_alarms)),
      element("td", snapshot.delayed_source_count ? "partial-label" : "", String(snapshot.delayed_source_count || 0)),
    );
    const controls = element("td", "");
    const remove = element("button", "pin-button", "Delete");
    remove.type = "button";
    remove.dataset.deleteSnapshot = snapshot.id;
    remove.setAttribute("aria-label", `Delete snapshot from ${captured.toLocaleString()}`);
    controls.append(remove);
    row.append(controls);
    body.append(row);
  });
}

function renderSnapshotSelectors() {
  const before = byId("before-snapshot");
  const after = byId("after-snapshot");
  before.replaceChildren();
  after.replaceChildren();
  state.snapshots.forEach((snapshot) => {
    const beforeOption = element("option", "", snapshotLabel(snapshot));
    beforeOption.value = snapshot.id;
    const afterOption = beforeOption.cloneNode(true);
    before.append(beforeOption);
    after.append(afterOption);
  });
  if (state.snapshots.length > 1) before.value = state.snapshots[1].id;
  if (state.snapshots.length) after.value = state.snapshots[0].id;
  const disabled = state.snapshots.length < 2;
  before.disabled = disabled;
  after.disabled = disabled;
  byId("compare-snapshots-button").disabled = disabled;
}

function renderComparison(comparison) {
  const panel = byId("comparison-result");
  panel.replaceChildren();
  const verdict = element("div", `comparison-verdict ${comparison.verdict}`);
  verdict.append(element("i", ""), element("strong", "", comparison.verdict));
  panel.append(verdict);
  const deltas = element("dl", "delta-grid");
  const values = [
    ["Device changes", comparison.device_changes.length],
    ["Critical alarms", comparison.alarm_delta.Critical || 0],
    ["BFD down", comparison.bfd_down_delta],
    ["Control down", comparison.control_down_delta],
  ];
  values.forEach(([label, value]) => {
    const group = element("div", "");
    group.append(element("dt", "", label), element("dd", "", Number(value) > 0 ? `+${value}` : String(value)));
    deltas.append(group);
  });
  panel.append(deltas);
  if (comparison.configuration_changes.length) {
    panel.append(element("p", "", `${comparison.configuration_changes.length} configuration hash changes detected.`));
  }
}

function renderTrends(trends) {
  const container = byId("trend-series");
  container.replaceChildren();
  if (!trends.series.length) {
    container.append(element("p", "empty-state", "Capture snapshots to build fault-domain trends."));
    return;
  }
  trends.series.forEach((series) => {
    const row = element("section", "trend-row");
    row.append(element("h3", "", series.fault_domain));
    const bars = element("div", "trend-bars");
    bars.setAttribute("role", "img");
    bars.setAttribute("aria-label", `${series.fault_domain} availability across ${series.points.length} snapshots`);
    series.points.slice(-30).forEach((point) => {
      const bar = element("i", point.availability_percent === null ? "unknown" : "");
      const availability = point.availability_percent;
      bar.style.setProperty("--bar-height", `${Math.max(3, availability || 0)}%`);
      bar.title = availability === null ? "Availability unknown" : `${availability}% available`;
      bars.append(bar);
    });
    const latest = series.points[series.points.length - 1];
    const latestSummary = element("div", "trend-latest");
    latestSummary.append(
      element("strong", "", latest.availability_percent === null ? "Unknown" : `${latest.availability_percent}% available`),
      document.createTextNode(latest.latency_ms === null ? "Latency unavailable" : `${latest.latency_ms}ms latency`),
    );
    row.append(bars, latestSummary);
    container.append(row);
  });
}

async function loadAssurance() {
  const [history, trends] = await Promise.all([
    requestJson(`${API_BASE}/snapshots`),
    requestJson(`${API_BASE}/trends?days=30`),
  ]);
  state.snapshots = history.items || [];
  if (state.changesLoaded) renderChangeSnapshotSelectors();
  state.assuranceLoaded = true;
  renderSnapshotSelectors();
  renderSnapshotHistory();
  renderTrends(trends);
}

async function captureSnapshot() {
  const button = byId("capture-snapshot-button");
  button.disabled = true;
  button.textContent = "Capturing…";
  try {
    const includeHashes = byId("include-configuration-hashes").checked;
    await requestJson(
      `${API_BASE}/snapshots?include_configuration_hashes=${includeHashes}`,
      { method: "POST" },
    );
    await loadAssurance();
  } catch (error) {
    addErrorMessage(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Capture snapshot";
  }
}

async function compareSelectedSnapshots() {
  const before = byId("before-snapshot").value;
  const after = byId("after-snapshot").value;
  if (!before || !after || before === after) {
    announceError("Choose two different snapshots to compare");
    return;
  }
  try {
    const comparison = await requestJson(
      `${API_BASE}/comparisons?before=${encodeURIComponent(before)}&after=${encodeURIComponent(after)}`,
    );
    renderComparison(comparison);
  } catch (error) {
    addErrorMessage(error.message);
  }
}

async function deleteSnapshot(snapshotId) {
  if (!confirm("Delete this local snapshot?")) return;
  await requestJson(`${API_BASE}/snapshots/${encodeURIComponent(snapshotId)}`, { method: "DELETE" });
  await loadAssurance();
}

function parseTargets(value) {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

function renderChangeSnapshotSelectors() {
  const pre = byId("change-pre-snapshot");
  const post = byId("change-post-snapshot");
  pre.replaceChildren();
  post.replaceChildren();
  state.snapshots.forEach((snapshot) => {
    const option = element("option", "", snapshotLabel(snapshot));
    option.value = snapshot.id;
    pre.append(option);
    post.append(option.cloneNode(true));
  });
  if (state.snapshots.length) {
    pre.value = state.snapshots[0].id;
    post.value = state.snapshots[0].id;
  }
  pre.disabled = !state.snapshots.length;
  post.disabled = !state.snapshots.length;
  byId("change-plan-form").querySelector('button[type="submit"]').disabled = !state.snapshots.length;
}

function renderChangePlans(plans) {
  state.changePlans = plans;
  const list = byId("change-plan-list");
  list.replaceChildren();
  if (!plans.length) {
    list.append(element("p", "empty-state", "No change plans."));
    return;
  }
  plans.forEach((plan) => {
    const item = element("article", "change-plan-item");
    item.append(element("span", `change-plan-status ${plan.status}`, plan.status));
    const body = element("div", "");
    body.append(
      element("h2", "", plan.intent),
      element("p", "", `${plan.operation} · ${plan.targets.length} targets · ${plan.canary_targets.length} canaries`),
      element("p", "plan-hash", `Hash ${plan.plan_hash}`),
    );
    if (plan.verification) {
      body.append(element(
        "p",
        plan.verification.rollback_recommended ? "critical-text" : "",
        plan.verification.rollback_recommended
          ? `Rollback recommended: ${plan.verification.rollback_strategy}`
          : `Verification ${plan.verification.comparison.verdict}`,
      ));
    }
    const controls = element("div", "context-actions");
    if (plan.status === "planned") {
      const approve = element("button", "button button-primary", "Approve plan");
      approve.type = "button";
      approve.dataset.approvePlan = plan.id;
      approve.dataset.planHash = plan.plan_hash;
      controls.append(approve);
    }
    if (plan.status === "approved") {
      const verify = element("button", "button button-primary", "Verify");
      verify.type = "button";
      verify.dataset.verifyPlan = plan.id;
      controls.append(verify);
    }
    if (["planned", "approved"].includes(plan.status)) {
      const cancel = element("button", "button button-quiet", "Cancel");
      cancel.type = "button";
      cancel.dataset.cancelPlan = plan.id;
      controls.append(cancel);
    }
    const apply = element("button", "button button-quiet disabled-execution", "Apply unavailable");
    apply.type = "button";
    apply.disabled = true;
    apply.title = "This build contains no write-capable executor";
    controls.append(apply);
    item.append(body, controls);
    list.append(item);
  });
}

async function loadChanges() {
  const [plans, snapshots] = await Promise.all([
    requestJson(`${API_BASE}/change-plans`),
    requestJson(`${API_BASE}/snapshots`),
  ]);
  state.snapshots = snapshots.items || [];
  renderChangeSnapshotSelectors();
  renderChangePlans(plans.items || []);
  state.changesLoaded = true;
}

async function createChangePlan() {
  const operation = byId("change-operation").value;
  const plan = await requestJson(`${API_BASE}/change-plans`, {
    method: "POST",
    body: JSON.stringify({
      operation,
      intent: byId("change-intent").value.trim(),
      target_id: byId("change-target-id").value.trim(),
      targets: parseTargets(byId("change-targets").value),
      canary_targets: parseTargets(byId("change-canaries").value),
      pre_snapshot_id: byId("change-pre-snapshot").value,
      rollback_strategy: byId("change-rollback").value.trim(),
    }),
  });
  state.changePlans = [plan, ...state.changePlans];
  renderChangePlans(state.changePlans);
}

async function approveChangePlan(planId, planHash) {
  const plan = await requestJson(`${API_BASE}/change-plans/${encodeURIComponent(planId)}/approve`, {
    method: "POST",
    body: JSON.stringify({ expected_hash: planHash }),
  });
  replaceStateItem("changePlans", plan, renderChangePlans);
}

async function verifyChangePlan(planId) {
  const postSnapshotId = byId("change-post-snapshot").value;
  if (!postSnapshotId) throw new Error("Capture a post-change snapshot before verification");
  const plan = await requestJson(`${API_BASE}/change-plans/${encodeURIComponent(planId)}/verify`, {
    method: "POST",
    body: JSON.stringify({ post_snapshot_id: postSnapshotId }),
  });
  replaceStateItem("changePlans", plan, renderChangePlans);
}

async function cancelChangePlan(planId) {
  const plan = await requestJson(`${API_BASE}/change-plans/${encodeURIComponent(planId)}/cancel`, {
    method: "POST",
  });
  replaceStateItem("changePlans", plan, renderChangePlans);
}

function renderConnectors(connectors) {
  state.connectors = connectors;
  const list = byId("connector-list");
  list.replaceChildren();
  connectors.forEach((connector) => {
    const item = element("article", `connector-item ${connector.state}`);
    item.append(
      element("h3", "", connector.display_name),
      element("p", "", connector.provenance),
      element("span", "", `${connector.state} · ${connector.data_classification}`),
    );
    if (connector.handoff_url) {
      const handoff = element("a", "link-button", "Open specialist view");
      handoff.href = connector.handoff_url;
      handoff.target = "_blank";
      handoff.rel = "noopener noreferrer";
      item.append(handoff);
    }
    list.append(item);
  });
  const allowed = byId("agent-connectors");
  allowed.replaceChildren();
  connectors.forEach((connector) => {
    const option = element("option", "", connector.display_name);
    option.value = connector.id;
    option.disabled = !connector.enabled;
    allowed.append(option);
  });
}

function renderConnectorEvidence(collection) {
  const container = byId("connector-evidence");
  container.replaceChildren();
  if (!collection.items.length) {
    const sourceSummary = collection.sources.length
      ? collection.sources.map((source) => `${source.connector_id}: ${source.state}`).join(" · ")
      : "No connector supports this evidence type";
    container.append(element("p", "empty-state", sourceSummary));
    return;
  }
  collection.items.forEach((record) => {
    const item = element("div", "external-evidence-item");
    item.append(
      element("small", "", record.connector_id),
      element("strong", "", `${record.severity}: ${record.title}`),
      makePinButton({
        id: stablePinId("source", `${record.connector_id}-${record.id}`),
        type: "source",
        label: `${record.connector_id}: ${record.title}`,
        data: record,
      }),
    );
    container.append(item);
  });
  updatePinButtons();
}

function workflowPreviewText(workflow) {
  const preview = workflow.preview || {};
  return preview.short_description || preview.markdown || preview.text || workflow.destination;
}

function renderWorkflows(workflows) {
  state.workflows = workflows;
  const list = byId("workflow-list");
  list.replaceChildren();
  if (!workflows.length) {
    list.append(element("p", "empty-state", "No workflow drafts."));
    return;
  }
  workflows.forEach((workflow) => {
    const item = element("article", "workflow-item");
    item.append(element("span", `workflow-status ${workflow.status}`, workflow.status));
    const body = element("div", "");
    body.append(
      element("h3", "", `${workflow.destination} · ${workflow.target || "target not set"}`),
      element("p", "", workflowPreviewText(workflow)),
    );
    const controls = element("div", "context-actions");
    if (workflow.status === "draft") {
      const approve = element("button", "button button-primary", "Approve draft");
      approve.type = "button";
      approve.dataset.approveWorkflow = workflow.id;
      approve.dataset.contentHash = workflow.content_hash;
      const cancel = element("button", "button button-quiet", "Cancel");
      cancel.type = "button";
      cancel.dataset.cancelWorkflow = workflow.id;
      controls.append(approve, cancel);
    }
    item.append(body, controls);
    list.append(item);
  });
}

function renderAgents(agents) {
  state.agents = agents;
  const list = byId("agent-list");
  list.replaceChildren();
  if (!agents.length) {
    list.append(element("p", "empty-state", "No agents registered."));
    return;
  }
  agents.forEach((agent) => {
    const item = element("article", "agent-item");
    item.append(
      element("h3", "", agent.display_name),
      element("p", "", `${agent.id} · owner ${agent.human_owner_id}`),
      element("p", "", `${agent.maximum_data_classification} · ${(agent.allowed_connectors || []).join(", ") || "no connectors"}`),
    );
    list.append(item);
  });
}

async function loadIntegrations() {
  const [connectorPayload, workflowPayload, agentPayload] = await Promise.all([
    requestJson(`${API_BASE}/connectors`),
    requestJson(`${API_BASE}/workflows`),
    requestJson(`${API_BASE}/agents`),
  ]);
  renderConnectors(connectorPayload.items || []);
  renderWorkflows(workflowPayload.items || []);
  renderAgents(agentPayload.items || []);
  state.integrationsLoaded = true;
}

async function collectConnectorEvidence() {
  const button = byId("collect-connector-button");
  button.disabled = true;
  try {
    const siteId = byId("connector-site").value.trim();
    const context = siteId ? { site_id: siteId } : {};
    const collection = await requestJson(`${API_BASE}/connectors/collect`, {
      method: "POST",
      body: JSON.stringify({
        capability: byId("connector-capability").value,
        context,
      }),
    });
    renderConnectorEvidence(collection);
  } catch (error) {
    addErrorMessage(error.message);
  } finally {
    button.disabled = false;
  }
}

async function createWorkflowDraft() {
  if (!state.currentInvestigation) {
    announceError("Create an investigation before drafting a workflow");
    return;
  }
  try {
    const workflow = await requestJson(`${API_BASE}/workflows/drafts`, {
      method: "POST",
      body: JSON.stringify({
        investigation_id: state.currentInvestigation.id,
        destination: byId("workflow-destination").value,
        target: byId("workflow-target").value.trim() || null,
      }),
    });
    state.workflows = [workflow, ...state.workflows.filter((item) => item.id !== workflow.id)];
    renderWorkflows(state.workflows);
  } catch (error) {
    addErrorMessage(error.message);
  }
}

async function approveWorkflowDraft(workflowId, contentHash) {
  const workflow = await requestJson(`${API_BASE}/workflows/${encodeURIComponent(workflowId)}/approve`, {
    method: "POST",
    body: JSON.stringify({ actor_id: "local-operator", expected_hash: contentHash }),
  });
  replaceStateItem("workflows", workflow, renderWorkflows);
}

async function cancelWorkflowDraft(workflowId) {
  const workflow = await requestJson(`${API_BASE}/workflows/${encodeURIComponent(workflowId)}/cancel`, {
    method: "POST",
    body: JSON.stringify({ actor_id: "local-operator" }),
  });
  replaceStateItem("workflows", workflow, renderWorkflows);
}

async function registerAgent() {
  const connectors = [...byId("agent-connectors").selectedOptions].map((option) => option.value);
  const actor = state.metadata?.actor;
  const agent = await requestJson(`${API_BASE}/agents`, {
    method: "POST",
    body: JSON.stringify({
      id: byId("agent-id").value.trim(),
      display_name: byId("agent-name").value.trim(),
      human_owner_id: actor?.id || "local-owner",
      allowed_connectors: connectors,
      maximum_data_classification: byId("agent-classification").value,
    }),
  });
  state.agents = [...state.agents.filter((item) => item.id !== agent.id), agent];
  renderAgents(state.agents);
  byId("agent-form").reset();
}

function applyAssistantResult(result) {
  const message = addMessage("assistant", result.answer, result.status);
  renderDetails(message, result);
  renderSources(result.evidence || []);
  if (result.data?.root_causes) renderRootCauses(result.data.root_causes);
  if (result.data?.impact) {
    const impact = result.data.impact;
    const panel = byId("impact-panel");
    panel.replaceChildren(
      element("span", "", "Impact"),
      element("strong", "", impact.scope || result.status),
      element("p", "", impact.summary || result.answer),
    );
  }

  const promptRow = document.querySelector(".prompt-row");
  (result.suggestions || []).slice(0, 4).forEach((suggestion) => {
    if ([...promptRow.children].some((button) => button.dataset.prompt === suggestion)) return;
    const button = element("button", "", suggestion);
    button.type = "button";
    button.dataset.prompt = suggestion;
    promptRow.append(button);
  });
  updatePinButtons();
}

function renderPins() {
  const list = byId("pinned-list");
  list.replaceChildren();
  const pins = state.currentInvestigation?.pins || [];
  if (!pins.length) {
    list.append(element("li", "empty-state", "Nothing pinned yet"));
    updatePinButtons();
    return;
  }
  pins.forEach((pin) => {
    const item = element("li", "");
    const label = element("span", "", pin.label);
    label.append(element("small", "", pin.type));
    const remove = element("button", "pin-button", "Remove");
    remove.type = "button";
    remove.dataset.unpinId = pin.id;
    remove.setAttribute("aria-label", `Remove ${pin.label} from this investigation`);
    item.append(label, remove);
    list.append(item);
  });
  updatePinButtons();
}

function renderConversationHistory(messages = []) {
  const conversation = byId("conversation");
  conversation.replaceChildren();
  if (!messages.length) {
    addMessage(
      "assistant",
      "I can investigate health, devices, alarms, and individual system IPs. Every conclusion stays linked to its vManage evidence.",
    );
    return;
  }
  messages.forEach((message) => addMessage(message.role === "user" ? "user" : "assistant", message.content));
  conversation.scrollTop = conversation.scrollHeight;
}

function investigationSummary(investigation) {
  return {
    id: investigation.id,
    title: investigation.title,
    created_at: investigation.created_at,
    updated_at: investigation.updated_at,
    message_count: investigation.messages?.length || 0,
    pin_count: investigation.pins?.length || 0,
  };
}

function updateInvestigationSelector() {
  const select = byId("investigation-select");
  select.replaceChildren();
  state.investigations.forEach((investigation) => {
    const option = element("option", "", investigation.title);
    option.value = investigation.id;
    select.append(option);
  });
  select.disabled = !state.investigations.length;
  if (state.currentInvestigation) select.value = state.currentInvestigation.id;
}

function updateExportLink() {
  const link = byId("export-button");
  if (!state.currentInvestigation) {
    link.href = "#";
    link.setAttribute("aria-disabled", "true");
    return;
  }
  const format = byId("export-format").value;
  link.href = `${API_BASE}/investigations/${encodeURIComponent(state.currentInvestigation.id)}/export?format=${encodeURIComponent(format)}`;
  link.removeAttribute("aria-disabled");
}

function setCurrentInvestigation(investigation, renderHistory = true) {
  state.currentInvestigation = investigation;
  const summary = investigationSummary(investigation);
  state.investigations = [summary, ...state.investigations.filter((item) => item.id !== summary.id)];
  updateInvestigationSelector();
  updateExportLink();
  renderPins();
  if (renderHistory) renderConversationHistory(investigation.messages || []);
  loadCollaboration().catch((error) => announceError(error.message));
}

function renderCollaboration(record) {
  state.collaboration = record;
  const owner = byId("collaboration-owner");
  owner.replaceChildren();
  state.actors.filter((actor) => actor.role !== "agent").forEach((actor) => {
    const option = element("option", "", actor.display_name);
    option.value = actor.id;
    owner.append(option);
  });
  owner.value = record.owner_id;
  owner.disabled = owner.options.length < 2;
  const hypotheses = byId("collaboration-hypotheses");
  hypotheses.replaceChildren();
  if (!record.hypotheses.length) {
    hypotheses.append(element("li", "empty-state", "No user hypotheses"));
  } else {
    record.hypotheses.forEach((hypothesis) => {
      const item = element("li", "");
      item.append(element("span", "", hypothesis.title));
      const confidence = element("select", "");
      confidence.dataset.hypothesisId = hypothesis.id;
      confidence.setAttribute("aria-label", `Confidence for ${hypothesis.title}`);
      ["low", "medium", "high"].forEach((level) => {
        const option = element("option", "", level);
        option.value = level;
        confidence.append(option);
      });
      confidence.value = hypothesis.confidence;
      item.append(confidence);
      hypotheses.append(item);
    });
  }
  const timeline = byId("collaboration-timeline");
  timeline.replaceChildren();
  record.events.slice(-30).forEach((event) => {
    const item = element("li", event.stale ? "stale" : "");
    item.append(element("span", "", event.body));
    const created = new Date(event.created_at);
    item.append(element("time", "", `${event.actor_id} · ${created.toLocaleString()}${event.stale ? " · stale" : ""}`));
    timeline.append(item);
  });
}

function renderAudienceView(view) {
  state.audienceView = view;
  const summary = byId("audience-summary");
  summary.replaceChildren(
    element("strong", "", `${view.audience.toUpperCase()} · ${view.status}`),
    document.createTextNode(view.summary),
    element("small", "", `${view.message_count} messages · ${view.pin_count} evidence pins`),
  );
}

async function loadAudienceView() {
  if (!state.currentInvestigation) return;
  const audience = byId("audience-view").value;
  const view = await requestJson(
    `${API_BASE}/investigations/${encodeURIComponent(state.currentInvestigation.id)}/view?audience=${encodeURIComponent(audience)}`,
  );
  renderAudienceView(view);
}

async function loadCollaboration() {
  if (!state.currentInvestigation) return;
  const [actors, collaboration, audienceView] = await Promise.all([
    requestJson(`${API_BASE}/actors`),
    requestJson(`${API_BASE}/collaborations/${encodeURIComponent(state.currentInvestigation.id)}`),
    requestJson(`${API_BASE}/investigations/${encodeURIComponent(state.currentInvestigation.id)}/view?audience=${encodeURIComponent(byId("audience-view").value)}`),
  ]);
  state.actors = actors.items || [];
  renderCollaboration(collaboration);
  renderAudienceView(audienceView);
}

async function addCollaborationComment(body) {
  if (!state.currentInvestigation || !state.collaboration) return;
  const collaboration = await requestJson(
    `${API_BASE}/collaborations/${encodeURIComponent(state.currentInvestigation.id)}/comments`,
    {
      method: "POST",
      body: JSON.stringify({ body, expected_revision: state.collaboration.revision }),
    },
  );
  renderCollaboration(collaboration);
  await loadAudienceView();
}

async function assignCollaborationOwner(ownerId) {
  if (!state.currentInvestigation || !state.collaboration) return;
  const collaboration = await requestJson(
    `${API_BASE}/collaborations/${encodeURIComponent(state.currentInvestigation.id)}/owner`,
    {
      method: "POST",
      body: JSON.stringify({ owner_id: ownerId, expected_revision: state.collaboration.revision }),
    },
  );
  renderCollaboration(collaboration);
  await loadAudienceView();
}

async function addCollaborationHypothesis(title, confidence) {
  if (!state.currentInvestigation || !state.collaboration) return;
  const collaboration = await requestJson(
    `${API_BASE}/collaborations/${encodeURIComponent(state.currentInvestigation.id)}/hypotheses`,
    {
      method: "POST",
      body: JSON.stringify({
        title,
        confidence,
        expected_revision: state.collaboration.revision,
      }),
    },
  );
  renderCollaboration(collaboration);
  await loadAudienceView();
}

async function updateCollaborationHypothesis(hypothesisId, confidence) {
  if (!state.currentInvestigation || !state.collaboration) return;
  const collaboration = await requestJson(
    `${API_BASE}/collaborations/${encodeURIComponent(state.currentInvestigation.id)}/hypotheses/${encodeURIComponent(hypothesisId)}`,
    {
      method: "PATCH",
      body: JSON.stringify({
        confidence,
        expected_revision: state.collaboration.revision,
      }),
    },
  );
  renderCollaboration(collaboration);
  await loadAudienceView();
}

async function createInvestigation() {
  const title = `Investigation ${new Date().toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
  const investigation = await requestJson(`${API_BASE}/investigations`, {
    method: "POST",
    body: JSON.stringify({ title }),
  });
  setCurrentInvestigation(investigation);
  return investigation;
}

async function loadInvestigation(investigationId, renderHistory = true) {
  const investigation = await requestJson(`${API_BASE}/investigations/${encodeURIComponent(investigationId)}`);
  setCurrentInvestigation(investigation, renderHistory);
}

async function initializeInvestigations() {
  const payload = await requestJson(`${API_BASE}/investigations`);
  state.investigations = payload.items || [];
  if (state.investigations.length) await loadInvestigation(state.investigations[0].id);
  else await createInvestigation();
}

async function pinEvidence(pin) {
  if (!state.currentInvestigation) return;
  const investigation = await requestJson(
    `${API_BASE}/investigations/${encodeURIComponent(state.currentInvestigation.id)}/pins`,
    { method: "POST", body: JSON.stringify(pin) },
  );
  setCurrentInvestigation(investigation, false);
}

async function unpinEvidence(pinId) {
  if (!state.currentInvestigation) return;
  await requestJson(
    `${API_BASE}/investigations/${encodeURIComponent(state.currentInvestigation.id)}/pins/${encodeURIComponent(pinId)}`,
    { method: "DELETE" },
  );
  state.currentInvestigation.pins = state.currentInvestigation.pins.filter((pin) => pin.id !== pinId);
  setCurrentInvestigation(state.currentInvestigation, false);
}

function setBusy(busy) {
  state.busy = busy;
  const form = byId("assistant-form");
  form.setAttribute("aria-busy", String(busy));
  form.querySelector('button[type="submit"]').disabled = busy;
  byId("cancel-button").hidden = !busy;
}

async function ask(message) {
  if (state.busy) return;
  if (!state.currentInvestigation) {
    try {
      await createInvestigation();
    } catch (error) {
      addErrorMessage(error.message, message);
      return;
    }
  }
  setBusy(true);
  state.abortController = new AbortController();
  addMessage("user", message);
  const pending = addMessage("assistant", "Gathering current evidence…", "loading");
  let completed = false;
  try {
    await requestEventStream("/api/v1/chat/stream", {
      method: "POST",
      body: JSON.stringify({
        message,
        investigation_id: state.currentInvestigation.id,
        assistant_mode: state.assistantMode,
      }),
      signal: state.abortController.signal,
    }, async (event) => {
      const progress = {
        accepted: "Request accepted…",
        collecting: "Collecting current evidence…",
        correlating: "Correlating health and impact…",
        explaining: "Explaining normalized evidence…",
      };
      if (progress[event.phase]) pending.querySelector("p").textContent = progress[event.phase];
      if (event.phase === "error") throw new Error(event.error || "Evidence collection failed");
      if (event.phase === "completed") {
        completed = true;
        pending.remove();
        applyAssistantResult(event.result);
      }
    });
    if (!completed) throw new Error("The evidence stream ended before completion");
    await loadInvestigation(state.currentInvestigation.id, false);
  } catch (error) {
    pending.remove();
    if (error.name === "AbortError") {
      addMessage("assistant", "Investigation cancelled. No network changes were made.");
    } else {
      addErrorMessage(error.message, message);
    }
  } finally {
    state.abortController = null;
    setBusy(false);
    const investigate = document.querySelector('[data-view-panel="investigate"]');
    if (!investigate.classList.contains("hidden")) byId("assistant-input").focus();
  }
}

async function loadOverview() {
  const refresh = byId("refresh-button");
  refresh.disabled = true;
  refresh.textContent = "Refreshing…";
  refresh.setAttribute("aria-label", "Refreshing fabric evidence");
  try {
    const overview = await requestJson(`${API_BASE}/overview`);
    renderOverview(overview);
    setConnection(true);
  } catch (error) {
    setConnection(false, error.message);
    byId("health-value").textContent = "Unavailable";
    byId("health-light").className = "critical";
    byId("health-light").setAttribute("aria-label", "Fabric health: unavailable");
    byId("evidence-freshness").textContent = "Evidence unavailable";
    addErrorMessage(error.message);
  } finally {
    refresh.disabled = false;
    refresh.textContent = "Refresh evidence";
    refresh.setAttribute("aria-label", "Refresh fabric evidence");
  }
}

async function switchView(view) {
  document.querySelector(".status-band").classList.toggle("hidden", view === "setup");
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.viewPanel !== view);
  });
  document.querySelectorAll(".rail-item").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (window.location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);

  if (view === "setup" && !state.setupLoaded) {
    try {
      await loadSetup();
    } catch (error) {
      setSetupMessage(error.message, "error");
    }
  }
  if (view === "topology" && !state.topology) await loadTopology();
  if (view === "actions" && !state.actions) await loadActions();
  if (view === "integrations" && !state.integrationsLoaded) {
    try {
      await loadIntegrations();
    } catch (error) {
      addErrorMessage(error.message);
    }
  }
  if (view === "changes" && !state.changesLoaded) {
    try {
      await loadChanges();
    } catch (error) {
      addErrorMessage(error.message);
    }
  }
  if (view === "assurance" && !state.assuranceLoaded) {
    try {
      await loadAssurance();
    } catch (error) {
      addErrorMessage(error.message);
    }
  }
  if (view === "inventory" && !state.inventoryLoaded) await ask("Show devices");
  if (view === "inventory" && state.inventoryLoaded) byId("inventory-table").focus();
}

async function initialize() {
  try {
    const metadata = await requestJson(`${API_BASE}/meta`);
    state.metadata = metadata;
    const generative = byId("assistant-mode-generative");
    generative.disabled = !(metadata.assistant_modes || []).includes("generative");
    generative.title = generative.disabled
      ? "Configure BROWSER_AI_PROVIDER and a server-side provider key to enable"
      : `Explain with ${metadata.browser_ai_provider}`;
    byId("version-label").textContent = `v${metadata.version}`;
    setConnection(metadata.connected, metadata.error || "");
  } catch (error) {
    setConnection(false, error.message);
  }
  if (state.metadata?.setup_required) {
    await switchView("setup");
    return;
  }
  if (window.location.hash === "#setup") await switchView("setup");
  try {
    await initializeInvestigations();
  } catch (error) {
    announceError(`Investigation history is unavailable: ${error.message}`);
  }
  await loadOverview();
}

document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) return;
  const topologyNode = event.target.closest("[data-topology-node]");
  if (topologyNode) {
    selectTopologyNode(topologyNode.dataset.topologyNode);
    return;
  }
  const topologyEdge = event.target.closest("[data-topology-edge]");
  if (topologyEdge) {
    selectTopologyEdge(topologyEdge.dataset.topologyEdge);
    return;
  }
  const site = event.target.closest("[data-view-site]");
  if (site) {
    showInventoryForSite(site.dataset.viewSite);
    return;
  }
  const diagnosis = event.target.closest("[data-diagnose-ip]");
  if (diagnosis) {
    investigateDevice(diagnosis.dataset.diagnoseIp);
    return;
  }
  const snapshot = event.target.closest("[data-delete-snapshot]");
  if (snapshot) {
    deleteSnapshot(snapshot.dataset.deleteSnapshot).catch((error) => addErrorMessage(error.message));
    return;
  }
  const approveWorkflow = event.target.closest("[data-approve-workflow]");
  if (approveWorkflow) {
    approveWorkflowDraft(approveWorkflow.dataset.approveWorkflow, approveWorkflow.dataset.contentHash)
      .catch((error) => addErrorMessage(error.message));
    return;
  }
  const cancelWorkflow = event.target.closest("[data-cancel-workflow]");
  if (cancelWorkflow) {
    cancelWorkflowDraft(cancelWorkflow.dataset.cancelWorkflow)
      .catch((error) => addErrorMessage(error.message));
    return;
  }
  const approvePlan = event.target.closest("[data-approve-plan]");
  if (approvePlan) {
    approveChangePlan(approvePlan.dataset.approvePlan, approvePlan.dataset.planHash)
      .catch((error) => addErrorMessage(error.message));
    return;
  }
  const verifyPlan = event.target.closest("[data-verify-plan]");
  if (verifyPlan) {
    verifyChangePlan(verifyPlan.dataset.verifyPlan).catch((error) => addErrorMessage(error.message));
    return;
  }
  const cancelPlan = event.target.closest("[data-cancel-plan]");
  if (cancelPlan) {
    cancelChangePlan(cancelPlan.dataset.cancelPlan).catch((error) => addErrorMessage(error.message));
    return;
  }
  const pin = event.target.closest("[data-pin-id]");
  if (pin) {
    const candidate = pinCandidates.get(pin.dataset.pinId);
    if (candidate) pinEvidence(candidate).catch((error) => addErrorMessage(error.message));
    return;
  }
  const unpin = event.target.closest("[data-unpin-id]");
  if (unpin) {
    unpinEvidence(unpin.dataset.unpinId).catch((error) => addErrorMessage(error.message));
    return;
  }
  const retry = event.target.closest("[data-retry-prompt]");
  if (retry) {
    ask(retry.dataset.retryPrompt);
    return;
  }
  const prompt = event.target.closest("[data-prompt]");
  if (prompt) ask(prompt.dataset.prompt);
  const view = event.target.closest("[data-view]");
  if (view) switchView(view.dataset.view);
});

document.addEventListener("change", (event) => {
  if (!(event.target instanceof Element)) return;
  const confidence = event.target.closest("[data-hypothesis-id]");
  if (confidence) {
    updateCollaborationHypothesis(confidence.dataset.hypothesisId, confidence.value)
      .catch((error) => addErrorMessage(error.message));
  }
});

byId("assistant-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = byId("assistant-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  ask(message);
});

byId("setup-test-button").addEventListener("click", () => {
  testSetupConnection().catch((error) => setSetupMessage(error.message, "error"));
});
byId("setup-qualify-button").addEventListener("click", () => {
  qualifyCurrentSetupConnection().catch((error) => setSetupMessage(error.message, "error"));
});
byId("setup-form").addEventListener("submit", (event) => {
  event.preventDefault();
  applySetupConnection().catch((error) => setSetupMessage(error.message, "error"));
});
byId("setup-form").addEventListener("input", () => {
  byId("setup-form").dataset.dirty = "true";
  state.setupTestReady = false;
  byId("setup-save-button").disabled = true;
  setSetupProgress("qualification", "active");
  setSetupMessage("Connection details changed. Test them again before saving.");
});
byId("setup-open-canvas").addEventListener("click", () => switchView("investigate"));
byId("setup-download-report").addEventListener("click", downloadSetupQualification);
byId("setup-copy-command").addEventListener("click", copySetupInstallCommand);

byId("collaboration-comment").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = byId("collaboration-comment-body");
  const body = input.value.trim();
  if (!body) return;
  addCollaborationComment(body)
    .then(() => { input.value = ""; })
    .catch((error) => addErrorMessage(error.message));
});

byId("hypothesis-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = byId("hypothesis-title");
  const title = input.value.trim();
  if (!title) return;
  addCollaborationHypothesis(title, byId("hypothesis-confidence").value)
    .then(() => { input.value = ""; })
    .catch((error) => addErrorMessage(error.message));
});

byId("collaboration-owner").addEventListener("change", (event) => {
  assignCollaborationOwner(event.target.value).catch((error) => addErrorMessage(error.message));
});
byId("audience-view").addEventListener("change", () => {
  loadAudienceView().catch((error) => addErrorMessage(error.message));
});

byId("assistant-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    byId("assistant-form").requestSubmit();
  }
});

byId("assistant-input").addEventListener("input", (event) => {
  if (event.target.value.length > 2000) event.target.value = event.target.value.slice(0, 2000);
});

document.querySelectorAll('input[name="assistant-mode"]').forEach((control) => {
  control.addEventListener("change", (event) => {
    if (event.target.checked) state.assistantMode = event.target.value;
  });
});

byId("refresh-button").addEventListener("click", loadOverview);
byId("capture-snapshot-button").addEventListener("click", captureSnapshot);
byId("compare-snapshots-button").addEventListener("click", compareSelectedSnapshots);
byId("collect-connector-button").addEventListener("click", collectConnectorEvidence);
byId("create-workflow-button").addEventListener("click", createWorkflowDraft);
byId("agent-form").addEventListener("submit", (event) => {
  event.preventDefault();
  registerAgent().catch((error) => addErrorMessage(error.message));
});
byId("change-plan-form").addEventListener("submit", (event) => {
  event.preventDefault();
  createChangePlan().catch((error) => addErrorMessage(error.message));
});
byId("clear-inventory-filter").addEventListener("click", () => {
  state.selectedContext = null;
  renderInventory();
});
byId("cancel-button").addEventListener("click", () => state.abortController?.abort());
byId("new-investigation-button").addEventListener("click", () => {
  createInvestigation().catch((error) => addErrorMessage(error.message));
});
byId("investigation-select").addEventListener("change", (event) => {
  loadInvestigation(event.target.value).catch((error) => addErrorMessage(error.message));
});
byId("export-format").addEventListener("change", updateExportLink);

initialize();
