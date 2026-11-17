"""Python entry point mounted into an Octane Docker workspace."""
import json
import os
import runpy
import sys
from collections import Counter

_ROOT = "/workspace/"
_MAX_EVENTS = 50000
_MAX_ROWS = 150


def _location(frame):
    code = frame.f_code
    path = code.co_filename
    if not path.startswith(_ROOT) or ".." in path.split("/") or len(path) > 350:
        return None
    name = getattr(code, "co_qualname", code.co_name).replace(".<locals>.", ".")
    return path[len(_ROOT):], name[:200], code.co_firstlineno


def main():
    mode, target, *arguments = sys.argv[1:]
    sys.argv = [target, *arguments]
    sys.path[0] = "/workspace" if mode == "module" else os.path.dirname(os.path.abspath(target))
    calls, exceptions, entries = Counter(), Counter(), Counter()
    events = 0
    truncated = False

    def record(counter, key):
        nonlocal truncated
        if key in counter or sum(map(len, (calls, exceptions, entries))) < _MAX_ROWS:
            counter[key] += 1
        else:
            truncated = True

    def trace(frame, event, arg):
        nonlocal events, truncated
        location = _location(frame)
        if location is None:
            return None
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
            json.dump({
                "events": events, "truncated": truncated,
                "calls": [{"caller": symbol(source), "callee": symbol(dest), "call_line": line, "count": count}
                          for (source, dest, line), count in calls.items()],
                "entries": [{**symbol(location), "count": count} for location, count in entries.items()],
                "exceptions": [{"function": symbol(location), "line": line, "exception_type": kind, "count": count}
                               for (location, line, kind), count in exceptions.items()],
            }, output)


if __name__ == "__main__":
    main()
