"""Expose the analyzer shared by Octane's tools and command-line interface."""


def analyzer():
    from .ledger import source, polygraph

    return source, polygraph
