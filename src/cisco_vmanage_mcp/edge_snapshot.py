"""Validate and summarize a bounded browser capture without a Manager connection."""

import json
import math
import re
from collections import Counter
from datetime import datetime
from importlib.resources import files
from ipaddress import IPv4Address

READS = json.loads(files("cisco_vmanage_mcp").joinpath("edge_addon/reads.json").read_text("utf-8"))
DETAILS = {"bfd", "control", "system"}
ESTATE = {"inventory", "counts", "alarms"}
AVAILABLE = {"ok", "partial"}
STATES = AVAILABLE | {"denied", "unavailable", "sign-in required", "response too large"}


def _safe_row(row, fields):
    if not isinstance(row, dict) or row.keys() - set(fields):
        return False
    return all(
        (isinstance(v, str) and len(v) <= 128 and all(c.isprintable() for c in v))
        or (isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) <= 1e20 and math.isfinite(v))
        for v in row.values()
    )


def validate_capture(data: dict) -> dict:
    if not isinstance(data, dict) or data.keys() != {"captured", "selected", "tables"}:
        raise ValueError("Invalid snapshot.")
    stamp = data["captured"]
    if not isinstance(stamp, str) or len(stamp) > 40 or datetime.fromisoformat(stamp).tzinfo is None:
        raise ValueError("Missing capture time.")
    selected = data["selected"]
    if selected is not None and (not isinstance(selected, str) or str(IPv4Address(selected)) != selected):
        raise ValueError("Invalid selected system IP.")
    tables = data["tables"]
    if not isinstance(tables, dict) or tables.keys() != (ESTATE | DETAILS if selected else ESTATE):
        raise ValueError("Incomplete set of snapshot sources.")
    for name, table in tables.items():
        if not isinstance(table, dict) or table.keys() != {"state", "rows"}:
            raise ValueError("Invalid source.")
        state, rows = table["state"], table["rows"]
        spec = READS[name]
        if not isinstance(state, str) or state not in STATES or not isinstance(rows, list):
            raise ValueError("Invalid source status.")
        if state == "sign-in required":
            raise ValueError("Sign in to Manager again and click Diagnose.")
        if (len(rows) > spec["limit"] or (rows and state not in AVAILABLE)
                or not all(_safe_row(row, spec["fields"]) for row in rows)):
            raise ValueError("Invalid evidence fields.")
    inventory = tables["inventory"]
    if inventory["state"] not in AVAILABLE:
        raise ValueError("Inventory could not be read under this Cisco session.")
    if selected and not any(row.get("system-ip") == selected for row in inventory["rows"]):
        raise ValueError("Selected device is outside this inventory.")
    return data


def question_evidence(capture: dict, question: str) -> dict:
    tables = capture["tables"]
    devices = tables["inventory"]["rows"]
    site = re.search(r"\bsite(?:\s++id)?[\s:#-]*+([0-9]++)\b", question, re.I)
    if site:
        devices = [d for d in devices if str(d.get("site-id")) == site[1]]
    target = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", question)
    if target:
        devices = [d for d in devices if d.get("system-ip") == target[0]]
    if not (site or target):
        words = set(re.findall(r"[\w.-]+", question.casefold()))
        named = [d for d in devices if str(d.get("host-name", "")).casefold() in words]
        if named:
            devices = named
    matched_ips = {d["system-ip"] for d in devices if d.get("system-ip")}
    counts = Counter(str(device.get("reachability", "unknown")).lower() for device in devices)
    alarms = tables["alarms"]["rows"]
    if site or target or len(devices) < len(tables["inventory"]["rows"]):
        alarms = [a for a in alarms if a.get("system-ip", a.get("system_ip")) in matched_ips or
                  (site and not target and str(a.get("site-id", a.get("site_id"))) == site[1])]
    if "unreachable" in question.lower():
        devices = [d for d in devices if str(d.get("reachability", "")).lower() == "unreachable"]
    if "critical" in question.lower():
        alarms = [a for a in alarms if str(a.get("severity", "")).lower() == "critical"]
    evidence = {
        "capture_time": capture["captured"],
        "coverage": {READS[name]["path"]: table["state"] for name, table in tables.items()},
        "matching_device_count_in_capture": len(devices), "reachability_in_scope": dict(counts),
        "matching_alarm_count_in_capture": len(alarms) if tables["alarms"]["state"] in AVAILABLE else None,
        "fabric_alarm_counts": tables["counts"]["rows"] if tables["counts"]["state"] in AVAILABLE else None,
        "device_sample": devices[:16], "alarm_sample": alarms[:8],
    }
    if capture["selected"] in matched_ips:
        evidence["device_detail"] = {
            "system_ip": capture["selected"],
            **{name: {"rows_in_capture": len(tables[name]["rows"]) if tables[name]["state"] in AVAILABLE else None,
                      "sample": tables[name]["rows"][:8]}
               for name in sorted(DETAILS)},
        }
    return evidence
