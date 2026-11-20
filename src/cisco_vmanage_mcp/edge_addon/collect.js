// This function is serialized by Edge and runs only in the activated tab's isolated world.
export async function collectSnapshot(origin, spec, selected) {
  if (window.top !== window || location.protocol !== "https:" || location.origin !== origin) {
    throw new Error("Open the configured HTTPS Manager tab and click Diagnose there.");
  }
  const ipv4 = value => typeof value === "string" && value.split(".").length === 4 &&
    value.split(".").every(part => /^(0|[1-9]\d{0,2})$/.test(part) && Number(part) <= 255);
  const empty = state => ({state, rows: []});
  async function read(id, device) {
    const entry = spec[id];
    const url = new URL(entry.path, origin);
    if (device) url.searchParams.set("deviceId", device);
    try {
      const res = await fetch(url, {
        method: "GET", mode: "same-origin", credentials: "same-origin", redirect: "manual",
        cache: "no-store", referrerPolicy: "no-referrer", headers: {Accept: "application/json"},
        signal: AbortSignal.timeout(18000)
      });
      if (res.type === "opaqueredirect" || res.status === 401) return empty("sign-in required");
      if (res.status === 403) return empty("denied");
      if (!res.ok || !res.body) return empty("unavailable");
      const type = res.headers.get("content-type") || "";
      if (type.includes("text/html")) return empty("sign-in required");
      if (!type.includes("json")) return empty("unavailable");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let text = "", size = 0;
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 6 * 1024 * 1024) {
          await reader.cancel();
          return empty("response too large");
        }
        text += decoder.decode(value, {stream: true});
      }
      const payload = JSON.parse(text + decoder.decode());
      if (!Array.isArray(payload.data)) return empty("unavailable");
      const rows = [];
      for (const row of payload.data.slice(0, entry.limit)) {
        if (!row || typeof row !== "object" || Array.isArray(row)) return empty("unavailable");
        const safe = {};
        for (const field of entry.fields) {
          const value = row[field];
          if (Number.isFinite(value)) safe[field] = value;
          if (typeof value === "string" && value.length <= 128 && !/[\u0000-\u001f]/u.test(value)) safe[field] = value;
        }
        rows.push(safe);
      }
      const limited = payload.data.length > rows.length || payload.pageInfo?.hasMoreData === true;
      return {state: limited ? "partial" : "ok", rows};
    } catch {
      return empty("unavailable");
    }
  }
  const inventory = await read("inventory");
  const tables = {inventory};
  if (["ok", "partial"].includes(inventory.state)) {
    const ids = ["alarms", "counts"];
    if (selected) {
      if (!ipv4(selected) || !inventory.rows.some(row => row["system-ip"] === selected)) {
        throw new Error("The selected device is no longer in the visible inventory.");
      }
      ids.push("bfd", "control", "system");
    }
    const results = await Promise.all(ids.map(id => read(id, ["bfd","control","system"].includes(id) ? selected : null)));
    ids.forEach((id, index) => { tables[id] = results[index]; });
  }
  return {captured: new Date().toISOString(), selected: selected || null, tables};
}
