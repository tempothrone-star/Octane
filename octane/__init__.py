"""Octane project intelligence primitives."""

from importlib import import_module

__version__ = "0.2.1"

__all__ = ["ProjectIntelligence", "ProjectScanner", "InvestigationWorkflow", "PythonAdapter", "ExplorationProfile",
           "ExplorationScheduler", "Tentacle", "WorkspaceManager"]

# Keep the standard-library CLI importable on Python 3.9. Native tools load on demand.
_EXPORT_MODULES = {
    "ProjectIntelligence": "db",
    "ProjectScanner": "scanner",
    "InvestigationWorkflow": "investigation",
    "PythonAdapter": "exploration",
    "ExplorationProfile": "exploration",
    "ExplorationScheduler": "exploration",
    "Tentacle": "exploration",
    "WorkspaceManager": "workspace",
}


def __getattr__(name):
    if name not in _EXPORT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("." + _EXPORT_MODULES[name], __name__), name)
    globals()[name] = value
    return value
