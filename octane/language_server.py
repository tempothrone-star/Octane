"""Bounded LSP call-hierarchy queries through operator-configured local servers."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit


class LanguageServer:
    def __init__(self, command, root: Path, timeout=45):
        if (not isinstance(command, list) or not command or len(command) > 32
                or any(not isinstance(value, str) or not value or "\0" in value for value in command)):
            raise ValueError("language server commands must be explicit non-empty argument arrays")
        self.deadline = time.monotonic() + timeout
        self.process = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        self.messages = queue.Queue(maxsize=256)
        self.identifier = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            total = 0
            while self.process.poll() is None:
                headers = {}
                while True:
                    line = self.process.stdout.readline(8193)
                    if not line or len(line) > 8192:
                        raise ValueError("language server closed or sent an invalid header")
                    if line in (b"\r\n", b"\n"):
                        break
                    name, value = line.decode("ascii").split(":", 1)
                    headers[name.lower()] = value.strip()
                size = int(headers["content-length"])
                total += size
                if not 0 < size <= 4_000_000 or total > 32_000_000:
                    raise ValueError("language server response exceeds the analysis budget")
                raw = self.process.stdout.read(size)
                if len(raw) != size:
                    raise ValueError("language server response is incomplete")
                self.messages.put(json.loads(raw), timeout=1)
        except (OSError, ValueError, KeyError, queue.Full) as exc:
            try:
                self.messages.put(exc, timeout=1)
            except queue.Full:
                return

    def send(self, method, params=None, identifier=None):
        value = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            value["params"] = params
        if identifier is not None:
            value["id"] = identifier
        self._write(value)

    def _write(self, value):
        data = json.dumps(value, separators=(",", ":")).encode()
        self.process.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.identifier += 1
        identifier = self.identifier
        self.send(method, params, identifier)
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("language server exceeded its analysis budget")
            try:
                value = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("language server exceeded its analysis budget") from exc
            if isinstance(value, Exception):
                raise value
            if "method" in value:
                if "id" in value:
                    method_name = value["method"]
                    if method_name == "workspace/configuration":
                        result = [{} for _ in value.get("params", {}).get("items", [])]
                    elif method_name == "workspace/applyEdit":
                        result = {"applied": False, "failureReason": "Octane uses read-only LSP analysis"}
                    else:
                        result = None
                    self._write({"jsonrpc": "2.0", "id": value["id"], "result": result})
                continue
            if value.get("id") != identifier:
                continue
            if value.get("error"):
                raise RuntimeError("language server rejected " + method + ": " + str(value["error"].get("message", "")))
            return value.get("result")

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process.stdin.close()
        self.reader.join(timeout=2)
        self.process.stdout.close()


def _definitions(items):
    for item in items or []:
        if item.get("kind") in {5, 6, 9, 12, 13}:
            yield item
        yield from _definitions(item.get("children", []))


def _node(item, language, root, revision, known):
    parsed = urlsplit(item["uri"])
    if parsed.scheme != "file":
        return None
    path = Path(unquote(parsed.path)).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return None
    position = item.get("selectionRange", item["range"])["start"]
    line = position["line"] + 1
    name = item["name"]
    matching = [node for node in known if node["path"] == relative and node["line"] == line
                and (node["name"] == name or node["name"].endswith("." + name))]
    if len(matching) == 1:
        return matching[0]
    return {"node_key": f"lsp:{language}:{relative}:{line}:{position['character']}:{name}",
            "name": name, "path": relative, "line": line, "kind": "function", "language": language,
            "revision": revision, "metadata": {"basis": "LSP call hierarchy", "owner": "native-scanner",
                                                "provider": language}}


def semantic_calls(root, files, contents, revision, known, servers, limit=50):
    nodes, edges, coverage = {}, {}, {}
    for language, definition in (servers or {}).items():
        if language not in {"python", "typescript"}:
            raise ValueError("languageServers supports python and typescript")
        selected = [item for item in files if item["language"] in
                    ({"python"} if language == "python" else {"typescript", "javascript"})]
        if not selected:
            continue
        status = {"provider": language, "files": 0, "symbols": 0, "calls": 0, "truncated": False,
                  "analyzed_callers": []}
        coverage[language] = status
        server = None
        try:
            command = definition.get("command") if isinstance(definition, dict) else definition
            options = {"disableAutomaticTypingAcquisition": True}
            if isinstance(definition, dict):
                options.update(definition.get("initializationOptions", {}))
            server = LanguageServer(command, root)
            server.request("initialize", {"processId": None, "rootUri": root.as_uri(),
                "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                "capabilities": {"textDocument": {"callHierarchy": {"dynamicRegistration": False},
                                                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}}},
                "initializationOptions": options})
            server.send("initialized", {})
            for item in selected[:64]:
                language_id = "python" if language == "python" else item["language"]
                server.send("textDocument/didOpen", {"textDocument": {
                    "uri": (root / item["path"]).as_uri(), "languageId": language_id, "version": 1,
                    "text": contents[item["path"]].decode("utf-8", "replace")}})
            examined = 0
            for item in selected[:64]:
                uri = (root / item["path"]).as_uri()
                outlines = server.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
                status["files"] += 1
                for symbol in _definitions(outlines):
                    if examined >= limit:
                        status["truncated"] = True
                        break
                    examined += 1
                    position = symbol.get("selectionRange", symbol.get("range",
                                          symbol.get("location", {}).get("range")))["start"]
                    prepared = server.request("textDocument/prepareCallHierarchy",
                                              {"textDocument": {"uri": uri}, "position": position})
                    for call_item in prepared or []:
                        caller = _node(call_item, language, root, revision, known)
                        if caller is None:
                            continue
                        nodes[caller["node_key"]] = caller
                        outgoing = server.request("callHierarchy/outgoingCalls", {"item": call_item})
                        status["analyzed_callers"].append(caller["node_key"])
                        for call in outgoing or []:
                            callee = _node(call["to"], language, root, revision, known)
                            if callee is None:
                                continue
                            nodes[callee["node_key"]] = callee
                            ranges = call.get("fromRanges", [])
                            line = ranges[0]["start"]["line"] + 1 if ranges else caller["line"]
                            key = (caller["node_key"], callee["node_key"], line)
                            edges[key] = {"source": key[0], "target": key[1], "kind": "calls", "confidence": 0.95,
                                          "metadata": {"basis": "LSP call hierarchy", "provider": language, "line": line}}
                if examined >= limit:
                    break
            status.update(symbols=sum(node["language"] == language for node in nodes.values()),
                          calls=sum(edge["metadata"]["provider"] == language for edge in edges.values()),
                          truncated=status["truncated"] or len(selected) > 64)
        except (OSError, ValueError, RuntimeError, TimeoutError, TypeError, KeyError) as exc:
            status["fallback"] = type(exc).__name__
            status["basis"] = "Static analyzer fallback retained"
        finally:
            if server is not None:
                server.close()
    return list(nodes.values()), list(edges.values()), coverage
