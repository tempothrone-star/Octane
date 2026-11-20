"""Normalize externally generated test, coverage, finding, and telemetry reports."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

MAX_REPORT_BYTES = 4_000_000
MAX_RECORDS = 10_000
FORMATS = {"junit", "pytest", "go-test", "jest", "coverage", "go-coverage", "sarif", "sentry", "otel",
           "mutmut", "stryker", "pit", "github"}


def _source(value, root):
    if not isinstance(value, str) or not value:
        return None
    value = value.split("::", 1)[0]
    parsed = urlsplit(value)
    if parsed.scheme == "file":
        value = unquote(parsed.path)
    elif parsed.scheme:
        return None
    candidate = Path(value)
    if value.startswith("/workspace/"):
        candidate = root / value.removeprefix("/workspace/")
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _xml(raw):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("XML reports must not declare entities or a document type")
    return ET.fromstring(raw)


def _junit(raw, root):
    tests = []
    for item in _xml(raw).iter("testcase"):
        failure = next((child for child in item if child.tag in {"failure", "error"}), None)
        status = "failed" if failure is not None else ("skipped" if item.find("skipped") is not None else "passed")
        tests.append({"id": item.get("classname", "") + "." + item.get("name", ""), "status": status,
                      "path": _source(item.get("file"), root),
                      "message": ((failure.get("message", "") or failure.text or "")[:2000]
                                  if failure is not None else "")})
    return {"tests": tests}


def _pytest(data, root):
    return {"tests": [{"id": item["nodeid"], "status": item["outcome"],
                       "path": _source(item["nodeid"], root),
                       "message": str(item.get("call", {}).get("longrepr", ""))[:2000]}
                      for item in data.get("tests", [])]}


def _go_test(raw, root):
    tests = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("Action") in {"pass", "fail", "skip"}:
            tests.append({"id": item.get("Package", "") + ":" + item.get("Test", "<package>"),
                          "status": {"pass": "passed", "fail": "failed", "skip": "skipped"}[item["Action"]],
                          "path": None, "package": item.get("Package")})
    return {"tests": tests}


def _jest(data, root):
    return {"tests": [{"id": assertion.get("fullName", assertion.get("title", "")),
                       "status": assertion.get("status", "unknown"), "path": _source(item.get("name"), root),
                       "message": "\n".join(assertion.get("failureMessages", []))[:2000]}
                      for item in data.get("testResults", []) for assertion in item.get("assertionResults", [])]}


def _lines(values):
    if not isinstance(values, list) or len(values) > 100_000 or any(type(line) is not int or line < 1 for line in values):
        raise ValueError("coverage lines must be bounded positive integer lists")
    return sorted(set(values))


def _coverage(data, root):
    files = {}
    for filename, item in data.get("files", {}).items():
        path = _source(filename, root)
        if path is not None:
            files[path] = {"covered": _lines(item.get("executed_lines", [])),
                           "missing": _lines(item.get("missing_lines", []))}
    return {"coverage": files}


def _go_coverage(raw, root):
    files = {}
    expanded = 0
    for row in raw.decode().splitlines():
        if row.startswith("mode:") or not row.strip():
            continue
        location, _, count = row.rsplit(None, 2)
        filename, span = location.rsplit(":", 1)
        start, end = (int(position.split(".", 1)[0]) for position in span.split(","))
        if not 1 <= start <= end or end - start > 100_000:
            raise ValueError("invalid Go coverage range")
        expanded += end - start + 1
        if expanded > 200_000:
            raise ValueError("Go coverage exceeds the aggregate line budget")
        path = _source(filename, root)
        if path is not None:
            item = files.setdefault(path, {"covered": set(), "missing": set()})
            item["covered" if int(count) > 0 else "missing"].update(range(start, end + 1))
    return {"coverage": {name: {"covered": sorted(item["covered"]),
                                "missing": sorted(item["missing"] - item["covered"])} for name, item in files.items()}}


def _sarif(data, root):
    findings = []
    for run in data.get("runs", []):
        for item in run.get("results", []):
            locations = item.get("locations", [])
            location = locations[0].get("physicalLocation", {}) if locations else {}
            findings.append({"rule": item.get("ruleId"), "level": item.get("level", "warning"),
                             "message": item.get("message", {}).get("text", "")[:2000],
                             "path": _source(location.get("artifactLocation", {}).get("uri"), root),
                             "line": location.get("region", {}).get("startLine")})
    return {"findings": findings}


def _sentry(data, root):
    records = []
    events = data if isinstance(data, list) else [data]
    for event in events:
        for value in event.get("exception", {}).get("values", []):
            frames = value.get("stacktrace", {}).get("frames", [])
            records.append({"event_id": event.get("event_id"), "exception_type": value.get("type"),
                            "frames": [{"path": _source(frame.get("filename"), root), "line": frame.get("lineno"),
                                        "function": frame.get("function")} for frame in frames]})
    return {"telemetry": records}


def _otel(data, root):
    records = []
    for resource in data.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                attributes = {item["key"]: item.get("value", {}) for item in span.get("attributes", [])}
                path = attributes.get("code.filepath", attributes.get("code.file.path", {})).get("stringValue")
                records.append({"trace_id": span.get("traceId"), "span_id": span.get("spanId"),
                                "name": span.get("name"), "status": span.get("status", {}).get("code"),
                                "path": _source(path, root)})
    return {"telemetry": records}


def _mutation(data, format_name):
    if format_name == "stryker":
        statuses = [item.get("status", "").lower() for file in data.get("files", {}).values()
                    for item in file.get("mutants", [])]
    elif format_name == "pit":
        statuses = [item.get("status", "").lower() for item in data.iter("mutation")]
    else:
        items = data.get("mutants", [])
        items = items.values() if isinstance(items, dict) else items
        statuses = [(item.get("status", "") if isinstance(item, dict) else str(item)).lower() for item in items]
    statuses = [status.replace("_", "").replace("-", "") for status in statuses]
    killed = sum(status in {"killed", "detected"} for status in statuses)
    survived = sum(status in {"survived", "surviving", "nocoverage", "uncovered", "notcovered"} for status in statuses)
    inconclusive = len(statuses) - killed - survived
    tested = killed + survived
    return {"mutation": {"killed": killed, "survived": survived, "inconclusive": inconclusive, "total": len(statuses),
                         "status": "inconclusive" if inconclusive else ("completed" if tested else "unavailable"),
                         "score": killed / tested if tested and not inconclusive else None,
                         "basis": "Imported mutation report; execution provenance must be linked separately"}}


def parse_report(raw: bytes, format_name: str, root: Path):
    if format_name not in FORMATS:
        raise ValueError("unsupported report format")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("report exceeds the 4 MB limit")
    root = root.resolve()
    try:
        if format_name == "junit":
            result = _junit(raw, root)
        elif format_name == "go-test":
            result = _go_test(raw, root)
        elif format_name == "go-coverage":
            result = _go_coverage(raw, root)
        elif format_name == "pit":
            result = _mutation(_xml(raw), format_name)
        else:
            data = json.loads(raw)
            if format_name in {"mutmut", "stryker"}:
                result = _mutation(data, format_name)
            elif format_name == "github":
                result = {"issue": {key: data.get(key) for key in ("number", "title", "body", "html_url", "state")}}
            else:
                result = {"pytest": _pytest, "jest": _jest, "coverage": _coverage, "sarif": _sarif,
                          "sentry": _sentry, "otel": _otel}[format_name](data, root)
    except (KeyError, TypeError, AttributeError, ET.ParseError, json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError("malformed " + format_name + " report") from exc
    if any(isinstance(value, (dict, list)) and len(value) > MAX_RECORDS for value in result.values()):
        raise ValueError("report exceeds the record limit")
    return {"schema": "octane.report.v1", "format": format_name, "sha256": hashlib.sha256(raw).hexdigest(), **result}


def read_report(root: Path, relative: str, format_name: str):
    path = root / relative
    if Path(relative).is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("report path must stay within the project")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    return parse_report(raw, format_name, root)


def import_report(intelligence, project_id, agent_id, relative, format_name):
    project = intelligence.get_project(project_id)
    report = read_report(Path(project["root"]), relative, format_name)
    return intelligence.submit_evidence(project_id, "structured_report", report,
                                         "Imported " + format_name + " report", agent_id,
                                         project["revision"], relative, verification_status="unverified")
