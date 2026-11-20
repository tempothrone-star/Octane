# Contributing to Octane

Open an issue describing the expected and actual behavior, or submit a focused pull request with a reproduction and the relevant checks. Follow SECURITY.md for vulnerability reports.

Use Python 3.11+ for package development and the full test suite. Node.js versions must satisfy package.json.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
npm ci --omit=peer
.venv/bin/ruff check octane tests
.venv/bin/python -m unittest discover -s tests -q
npm test
```

For Docker checks, pull the selected image locally and set OCTANE_TEST_DOCKER_IMAGE. Keep networking disabled in test containers. The checkout CLI also has a Python 3.9 compatibility job.

Keep checks identical between baseline and candidate runs. A proposal that changes tests needs allowTestChanges and a testChangesRationale, and candidate executions still use the original baseline checks.

Optional integration checks are documented in [docs/integrations.md](docs/integrations.md). CI installs pinned Python/TypeScript language servers and builds trusted Hypothesis/fast-check images before offline project checks. Set `OCTANE_LSP_TEST_TOOLS`, `OCTANE_TEST_PROPERTY_IMAGE`, and `OCTANE_TEST_NODE_PROPERTY_IMAGE` to run these locally. Cryptographic tests need `.[provenance]`.

Coverage uses subprocess instrumentation:

```sh
COVERAGE_FILE="$PWD/.coverage" COVERAGE_RCFILE="$PWD/.coveragerc" .venv/bin/coverage run -m unittest discover -s tests -q
.venv/bin/coverage combine
.venv/bin/coverage report --fail-under=69
.venv/bin/coverage report --include='octane/experiments.py' --fail-under=85
```

Use a GitHub no-reply email for new commits when you want to keep a personal address out of public history.

The optional parser uses the tested pair `tree-sitter-languages==1.10.2` and `tree-sitter==0.21.3`. The bundled loader calls `Language(path, name)` and `Parser.set_language`; runtime 0.22+ changes that API. Dependabot excludes those incompatible standalone runtime updates until the loader and grammar bundle are migrated together. Validate future parser changes with `pip install '.[ast]'` and the optional-parser suite; disabling that job or silently accepting heuristic fallback does not validate a parser upgrade.
