"""Capture bounded project-local stacks inside a Docker-isolated GDB session."""
import json
import os
from pathlib import Path


def run():
    import gdb

    workspace = Path("/workspace")
    symbols = json.loads(os.environ["OCTANE_NATIVE_SYMBOLS"])
    stacks = []
    hits = set()
    unavailable = []
    truncated = False

    class Probe(gdb.Breakpoint):
        def stop(self):
            nonlocal truncated
            if len(stacks) >= 12:
                truncated = True
                return False
            frame = gdb.newest_frame()
            stack = []
            for _ in range(12):
                if frame is None:
                    break
                location = frame.find_sal()
                item = None
                if location.symtab and location.line and frame.name():
                    path = Path(location.symtab.fullname()).resolve()
                    if path.is_relative_to(workspace) and path.is_file():
                        item = {"function": frame.name()[:160], "path": path.relative_to(workspace).as_posix(),
                                "line": location.line, "inline": frame.type() == gdb.INLINE_FRAME}
                stack.append(item)
                frame = frame.older()
            if frame is not None:
                truncated = True
            if any(stack):
                stacks.append(stack)
                hits.add(self.location)
            return False

    probes = {}
    for symbol in symbols:
        try:
            probes[symbol] = Probe(symbol, internal=True)
        except gdb.error:
            unavailable.append(symbol)
    try:
        gdb.execute("run")
    finally:
        unavailable.extend(symbol for symbol, probe in probes.items() if probe.pending and symbol not in hits)
        with open("/octane-trace-output", "w", encoding="utf-8") as stream:
            json.dump({"stacks": stacks, "hit_symbols": [symbol for symbol in symbols if symbol in hits],
                       "unavailable_symbols": unavailable, "truncated": truncated}, stream)


run()
