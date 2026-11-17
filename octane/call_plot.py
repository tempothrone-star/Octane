"""Render bounded call graphs without treating source names as Mermaid syntax."""
import re


def call_plot(nodes, edges):
    ids = {key: f"n{index}" for index, key in enumerate(nodes)}
    lines = ["flowchart LR"]
    for key, node in nodes.items():
        name = node.get("function") or node.get("name") or "(anonymous)"
        path = node.get("path") or "unknown source"
        location = f"{path}:{node['line']}" if node.get("line") else path
        label = re.sub(r"[^A-Za-z0-9_./: -]", "_", f"{name} at {location}")[:500]
        lines.append(f'  {ids[key]}["{label}"]')
    for source, target, line in edges:
        mark = f"|line {line}|" if type(line) is int and line > 0 else ""
        lines.append(f"  {ids[source]} -->{mark} {ids[target]}")
    return "\n".join(lines)
