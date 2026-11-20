"""Generate explicit property and differential probes for approved container images."""
from __future__ import annotations

import json
from pathlib import Path
import re

_PYTHON = '''
import importlib, json
from hypothesis import given, settings, strategies as st
spec = json.loads(SPEC)
module = importlib.import_module(spec["module"])
target = getattr(module, spec["function"])
reference = getattr(importlib.import_module(spec.get("referenceModule", spec["module"])), spec.get("reference", spec["function"]))
kind = spec.get("strategy", "integers")
strategy = {"integers": st.integers(-1000, 1000), "text": st.text(max_size=40),
            "lists": st.lists(st.integers(-1000,1000), max_size=20)}[kind]
@settings(max_examples=spec.get("examples", 50), derandomize=True, database=None, deadline=None)
@given(strategy)
def check(value):
    actual = target(value)
    rule = spec["rule"]
    if rule == "idempotent":
        assert target(actual) == actual
    elif rule == "roundtrip":
        assert reference(actual) == value
    else:
        assert reference(value) == actual
check()
print("Octane property check passed")
'''
_NODE = '''
const fc = require("fast-check");
const {pathToFileURL} = require("node:url");
const path = require("node:path");
const spec = JSON.parse(SPEC);
(async () => {
  const load = async (file, name) => {
    const loaded = await import(pathToFileURL(path.resolve(file)).href);
    return loaded[name] ?? loaded.default?.[name];
  };
  const target = await load(spec.file, spec.function);
  const reference = await load(spec.referenceFile ?? spec.file, spec.reference ?? spec.function);
  const strategies = {integers: fc.integer({min: -1000, max: 1000}),
                      text: fc.string({maxLength:40}), lists: fc.array(fc.integer(), {maxLength:20})};
  await fc.assert(fc.asyncProperty(strategies[spec.strategy ?? "integers"], async (value) => {
    const actual = await target(value);
    if (spec.rule === "idempotent") {
      const repeated = await target(actual);
      return JSON.stringify(repeated) === JSON.stringify(actual);
    }
    const expected = await reference(spec.rule === "roundtrip" ? actual : value);
    return JSON.stringify(expected) === JSON.stringify(spec.rule === "roundtrip" ? value : actual);
  }), {numRuns: spec.examples ?? 50, seed: 73219});
  console.log("Octane property check passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
'''


def property_command(root: Path, specification):
    root = root.resolve()
    if not isinstance(specification, dict):
        raise ValueError("propertySpec must be an object")
    spec = dict(specification)
    if spec.get("rule") not in {"idempotent", "roundtrip", "differential"}:
        raise ValueError("property rule must be idempotent, roundtrip, or differential")
    if spec.get("rule") != "idempotent" and not spec.get("reference"):
        raise ValueError("roundtrip and differential checks require a reference function")
    if type(spec.get("examples", 50)) is not int or not 1 <= spec.get("examples", 50) <= 500:
        raise ValueError("property examples must be an integer from 1 to 500")
    if spec.get("strategy", "integers") not in {"integers", "text", "lists"}:
        raise ValueError("unsupported property input strategy")
    if not all(isinstance(spec.get(key, ""), str) and re.fullmatch(r"[A-Za-z_]\w*", spec.get(key, ""))
               for key in (("function", "reference") if "reference" in spec else ("function",))):
        raise ValueError("property function must be an exported function name")
    runtime = spec.get("runtime", "python")
    if runtime == "python":
        modules = [spec.get("module"), spec.get("referenceModule", spec.get("module"))]
        paths = []
        for module in modules:
            if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module):
                raise ValueError("property modules must be project-local dotted names")
            paths.append(module.replace(".", "/") + ".py")
        template = _PYTHON
        prefix = ["python", "-B", "-c"]
    elif runtime == "node":
        paths = [spec.get("file"), spec.get("referenceFile", spec.get("file"))]
        template = _NODE
        prefix = ["node", "-e"]
    else:
        raise ValueError("property runtime must be python or node")
    for relative in paths:
        if not isinstance(relative, str):
            raise ValueError("property probes need project-local source files")
        target = root / relative
        if Path(relative).is_absolute() or target.is_symlink() or not target.resolve().is_relative_to(root) or not target.is_file():
            raise ValueError("property probes need project-local source files")
    encoded = json.dumps(json.dumps(spec))
    return prefix + [template.replace("SPEC", encoded)]
