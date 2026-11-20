"""Python entry point mounted into an Octane Docker workspace."""
import json
import os
import math
import re
import runpy
import sys
from collections import Counter

_ROOT = "/workspace/"
_MAX_EVENTS = 50000
_MAX_ROWS = 150
_MAX_TIMELINE = 256
_PRIVATE = re.compile(r"secret|token|password|credential|authorization|cookie|key", re.I)


def _numeric_values(frame):
    # Do not call repr(), descriptors, or user-defined serializers on project values.
    values = {}
    for name, value in list(frame.f_locals.items())[:64]:
        if not isinstance(name, str) or not name.isprintable() or _PRIVATE.search(name) or len(name) > 80:
            continue
        if value is None or type(value) is bool:
            values[name] = value
        elif type(value) is int and value.bit_length() <= 53:
            values[name] = value
        elif type(value) is float and math.isfinite(value) and abs(value) <= 2 ** 53:
            values[name] = value
        if len(values) >= 8:
            break
    return values



def _safe_scalar(value):
    if value is None or type(value) is bool:
        return True
    return type(value) in (int, float) and abs(value) <= 2 ** 53 and math.isfinite(value)


def _location(frame):
    code = frame.f_code
    path = code.co_filename
    if not path.startswith(_ROOT) or ".." in path.split("/") or len(path) > 350:
        return None
    name = getattr(code, "co_qualname", code.co_name).replace(".<locals>.", ".")
    return path[len(_ROOT):], name[:200], code.co_firstlineno


def _write_trace(output, payload):
    encoded = json.dumps(payload)
    while len(encoded.encode('utf-8')) > 390000:
        payload['truncated'] = True
        if payload['timeline']:
            payload['timeline_truncated'] = True
            payload['timeline'] = payload['timeline'][:len(payload['timeline']) // 2]
        else:
            name = max(('calls', 'entries', 'exceptions'), key=lambda key: len(payload[key]))
            payload[name] = payload[name][:len(payload[name]) // 2]
        encoded = json.dumps(payload)
    output.write(encoded)


def main():
    mode, target, *arguments = sys.argv[1:]
    sys.argv = [target, *arguments]
    sys.path[0] = "/workspace" if mode == "module" else os.path.dirname(os.path.abspath(target))
    calls, exceptions, entries = Counter(), Counter(), Counter()
    events = 0
    timeline = []
    timeline_truncated = False
    truncated = False

    def record(counter, key):
        nonlocal truncated
        if key in counter or sum(map(len, (calls, exceptions, entries))) < _MAX_ROWS:
            counter[key] += 1
        else:
            truncated = True

    def trace(frame, event, arg):
        nonlocal events, truncated, timeline_truncated
        location = _location(frame)
        if location is None:
            return None
        frame.f_trace_lines = True
        if event in {"call", "line", "return", "exception"}:
            if len(timeline) < _MAX_TIMELINE:
                values = _numeric_values(frame)
                if event == "return" and _safe_scalar(arg):
                    values = {**dict(list(values.items())[:7]), "<return>": arg}
                timeline.append({"path": location[0], "function": location[1], "line": max(1, frame.f_lineno),
                                 "event": event, "values": values})
            else:
                timeline_truncated = True
                frame.f_trace_lines = False
        if event in {"call", "exception"}:
            if events >= _MAX_EVENTS:
                truncated = True
                sys.settrace(None)
                return None
            events += 1
            if event == "call":
                caller = _location(frame.f_back) if frame.f_back else None
                if caller:
                    record(calls, (caller, location, frame.f_back.f_lineno))
                else:
                    record(entries, location)
            else:
                record(exceptions, (location, frame.f_lineno, arg[0].__name__[:120]))
        return trace

    def symbol(location):
        return dict(zip(("path", "function", "definition_line"), location))

    with open("/octane-trace-output", "w", encoding="utf-8") as output:
        sys.settrace(trace)
        try:
            if mode == "module":
                runpy.run_module(target, run_name="__main__", alter_sys=True)
            else:
                runpy.run_path(os.path.abspath(target), run_name="__main__")
        finally:
            sys.settrace(None)
            _write_trace(output, {
                "events": events, "truncated": truncated,
                "timeline": timeline, "timeline_truncated": timeline_truncated,
                "calls": [{"caller": symbol(source), "callee": symbol(dest), "call_line": line, "count": count}
                          for (source, dest, line), count in calls.items()],
                "entries": [{**symbol(location), "count": count} for location, count in entries.items()],
                "exceptions": [{"function": symbol(location), "line": line, "exception_type": kind, "count": count}
                               for (location, line, kind), count in exceptions.items()],
            })


if __name__ == "__main__":
    main()
