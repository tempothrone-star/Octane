"""Keep the reviewed orchestration functions at Radon complexity 30 or below."""
from pathlib import Path
from radon.visitors import ComplexityVisitor

TARGETS = {
    'octane/exploration.py': {'PythonAdapter.scan'},
    'octane/workspace.py': {'WorkspaceManager._docker_run'},
    'octane/ledger/cli.py': {'_run'},
    'octane/focus.py': {'repair_focus'},
    'octane/scanner.py': {'ProjectScanner.scan'},
}
root = Path(__file__).resolve().parents[1]
violations = []
for relative, selected in TARGETS.items():
    visitor = ComplexityVisitor.from_code((root/relative).read_text())
    blocks = {block.name:block.complexity for block in visitor.functions}
    for cls in visitor.classes:
        blocks.update({cls.name+'.'+method.name:method.complexity for method in cls.methods})
    for name in sorted(selected):
        score = blocks.get(name)
        print(f'{relative}:{name}: {score}')
        if score is None or score > 30:
            violations.append(name)
if violations:
    raise SystemExit('Complexity exceeds the reviewed limit: '+', '.join(violations))
