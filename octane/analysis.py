"""Load the shared portable analyzer without shadowing an installed octance module."""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

_LOCK = threading.RLock()


def analyzer():
    with _LOCK:
        name = "_octane_source_analysis"
        if name not in sys.modules:
            root = Path(__file__).resolve().parents[1] / "octance/scripts/octance"
            spec = importlib.util.spec_from_file_location(name, root / "__init__.py",
                                                         submodule_search_locations=[str(root)])
            if spec is None or spec.loader is None:
                raise RuntimeError("Octane's bundled source analyzer is missing; reinstall the complete package")
            package = importlib.util.module_from_spec(spec)
            sys.modules[name] = package
            spec.loader.exec_module(package)
        return (importlib.import_module(name + ".source"), importlib.import_module(name + ".polygraph"))
