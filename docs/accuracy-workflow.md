# Accurate diagnosis and final validation (0.3.0)

The engine verifies; the agent proposes. These additions keep model calls out of Octane's core and keep the same twelve OpenClaw/MCP tools. They implement an initial workflow across the six priorities in the November 20 enhancement proposal; the remaining research items are listed below. They do not make a score a proof that behavior is unintended.

## What was already available

SARIF import/export, sandboxed property probes (Hypothesis and fast-check), Git revision export and evaluation, mutation checks, Python/Node/Ruby/GDB traces, Speedscope profiles, and unchanged-snapshot scan reuse already existed. This release extends those capabilities rather than claiming them as new.

## Trace comparison

Run the same reproduction before and after a candidate with `pythonTrace: true`. Tracing covers the selected Python thread; child threads/processes need their own collector. Each runtime observation now contains an ordered prefix of up to 256 project-local call, line, return and exception events. It captures up to eight bounded primitive numeric/boolean/None values, including a primitive return value. It omits strings, containers, objects, and names suggesting credentials; it never invokes project `repr` or serialization hooks. Numeric data can still be sensitive under neutral names: enable tracing only for approved inputs.

```json
{"action":"analyze","projectId":"PROJECT_ID","analysisKind":"trace-diff","evidenceIds":["PASSING_TRACE_EVIDENCE","FAILING_TRACE_EVIDENCE"]}
```

Call `octane_investigation` with that payload. The result names the first observed event/value difference and records links to both input evidence records. Passing/failing order is explicit; the API does not infer test intent from an exit code. Different inputs, source line shifts, scheduling and instrumentation can cause divergence. A recorded prefix is not complete execution or a backward data-flow slice. Sampled or aggregated graphs are rejected for first-divergence analysis.

Other runtimes can import `ordered-trace` reports using the same event schema (up to 512 events) through `octane_evidence` action `import` or `reportFormat`. Built-in ordered value capture is Python-only; existing Node/Ruby/GDB/Speedscope call graphs remain available. The source path must be relative to the checkout.

## Per-test coverage and fault localization

Aggregate coverage cannot establish which failing test executed a line. Supply explicit per-test coverage as a `spectrum` report:

```json
{"test_spectra":[
  {"id":"negative","status":"failed","coverage":{"calc.py":[2,3]}},
  {"id":"positive","status":"passed","coverage":{"calc.py":[2,4]}}
]}
```

Capture it from an explicit sandbox command writing `OCTANE_REPORT_PATH`, or import it as unverified external evidence. An adapter may collect executed lines in any programming language; Octane does not synthesize per-test coverage from aggregate reports. Maximums: 2,000 test executions, 200,000 aggregate line entries and the existing 4 MB report bound.

Use `octane_investigation` action `analyze`, `analysisKind: "localize"`, and spectrum evidence IDs. Ochiai is `failed_covering / sqrt(total_failed * (failed_covering + passed_covering))`. Results include raw counts, test totals and exclusions. Mixed outcomes for repeated test IDs are classified as flaky; varying coverage is excluded as unstable. Skips do not count as passes. Both passing and failing tests are required for `available: true`. These are correlation scores, not calibrated confidence probabilities.

`analysisKind: "select-tests"` with `analysisOptions: {"changedLines":{"calc.py":[3]}}` produces `first` and `remainder` queues. Run the first queue for feedback, then the remainder and required preservation checks. Unknown changed lines, flaky results or unstable coverage fall back to the entire suite. Selection never authorizes skipping tests. Test IDs are data, not shell command fragments. The agent must map them to the project's normal test runner.

## Analyzer fusion and low-confidence patterns

`analysisKind: "fuse"` takes SARIF/report evidence IDs and optional `failingLines` / `changedLines` maps. Stable failing-test spectra and recorded trace-difference evidence automatically contribute the failing path; optional maps add explicit context. The result also retains the spectrum ranking and trace evidence IDs. Tool name/version, rule, source and evidence IDs survive import. Findings are deduplicated by content and ranked by explicit weights: failing path 4, changed path 2, same-line bonus 1 per signal. At most 10,000 findings may be fused. This describes proximity, not analyzer agreement or confirmed causation.

Run analyzers only through approved workspace images, with local rules/dependencies and networking disabled. For example, an image containing Ruff can run `ruff check --output-format sarif --output /octane-report-output .` with `reportFormat: "sarif"`. A prepared Semgrep image can use `semgrep scan --config /path/to/reviewed-local-rules.yml --sarif --output /octane-report-output .`. No analyzer is installed or executed on the host by diagnosis. Tools without native SARIF need an explicit reviewed converter; Octane does not pretend every listed analyzer emits SARIF. Regression fixtures exercise separate Ruff and Semgrep reports; they do not establish that those binaries were executed by that test.

`analysisKind: "patterns"` needs `analysisOptions: {"path":"calc.py","language":"python"}`. Engine-owned Tree-sitter queries recognize Python mutable defaults and swallowed exceptions, and empty catch blocks in JavaScript, TypeScript and Java. Python has a standard-library AST fallback. Unsupported grammars, parser errors and dependency incompatibility are explicit. Each match creates a proposed hypothesis with heuristic confidence 0.2, linked to the hashed source observation. It never changes source or marks a hypothesis supported. Intentional exception suppression and mutable caches are possible.

The pattern cache is bounded to 32 source-content entries in a process (256 KB each). Changed bytes miss the cache. The bridge starts a process per request, so this is not advertised as a persistent cross-request cache. Existing scans reuse unchanged revisions; persistent per-file graph caching and parallel candidate execution remain future work.

## Withheld final checks

The operator may set `holdoutPolicy` to an absolute JSON file outside **every** allowed project root and workspace root, in both OpenClaw and MCP configuration:

```json
{
  "projectRoot":"/absolute/project",
  "suiteRoot":"/absolute/operator-private/oracle",
  "command":["python","-B","/octane-holdout/check.py"]
}
```

The suite is operator-authored and must not overlap accessible roots. It is limited to 2 MB and 1,000 filesystem entries, contains no symlinks and is frozen before execution. Import project modules from `/workspace`; the oracle is mounted read-only at `/octane-holdout`. Use a command that exits nonzero when assertions fail. Provision all dependencies in the candidate's approved image.

When configured, **every bridge proposal** reruns this suite against the current passing candidate with the same immutable image, networking disabled, read-only source, baseline test overlays, and a 60-second timeout. Failure, cleanup failure or changed bytes rejects the proposal. Tool arguments cannot select, replace or disable the policy. Private command arguments (including inline oracle code) and output are removed **before** writing either execution rows or evidence. Only hashes, exit status and summary evidence are exposed. The proposal payload includes final-validation metadata, so its existing provenance payload digest binds that summary; raw oracle content is never included.

A standalone operator can run:

```sh
python3 -m octane native holdout PROJECT_ID WORKSPACE_ID CANDIDATE_RUN_ID \
  --policy /absolute/operator-private/policy.json \
  --database /absolute/state/pil.sqlite3 --workspace-root /absolute/state/workspaces
```

Standalone validation is not a substitute for configuring a mandatory bridge policy. Without a configured policy, ordinary proposals retain their existing focused/preservation gate and make no hidden-test claim. This boundary assumes the proposing agent uses Octane's allowed tools. Agents with unrestricted host shell access can read operator files; runtime code can see its mounted oracle. It is not a hostile-agent enclave or a guarantee against all overfitting.

## Compare surviving candidates

`analysisKind: "rank-patches"` accepts current recorded patch-proposal evidence IDs. It rejects arbitrary report payloads, changed/destroyed candidates and mixed images. It ranks gate-qualified candidates by masking warnings then diff size and returns `review_required: true`. Warnings identify added broad catches, skip calls and constant returns; those can be deliberate. Independent inputs and review must settle the warning. Mutation coverage, fresh analyzer comparisons and performance do not earn fabricated bonuses when unmeasured.

## Optimization decision

Existing Speedscope integration can observe hot paths in approved profiler images. To assess a proposed optimization, record 8–50 independent before/after timing pairs on fixed snapshots and the same immutable image/command/trace settings, plus separate paired preservation runs. Alternate execution order, discard warmup runs and avoid concurrent load where possible. Every run ID must be distinct. `octane_workspace` action `benchmark` accepts:

```json
{"action":"benchmark","projectId":"PROJECT_ID",
 "baselineRunIds":["EIGHT_OR_MORE_BEFORE_RUN_IDS"],
 "candidateRunIds":["MATCHING_AFTER_RUN_IDS"],
 "preservationPairs":[["BEFORE_PRESERVATION_RUN","AFTER_PRESERVATION_RUN"]],
 "minimumImprovement":0.05}
```

The placeholder arrays above must be expanded into actual run IDs. The native `benchmark` CLI exposes the same comparison with `--before-runs`, `--after-runs`, `--preservation-pairs-json`, `--database` and `--workspace-root`.

Acceptance requires complete equal outputs and baseline test integrity in each pair, passing unchanged executions, and the lower end of an exact binomial order-statistic median interval above the requested fractional reduction (default 5%). It reports the interval's actual confidence (at least 95%), mean and standard deviation for both groups. Independent paired samples are assumed; correlated load can invalidate that assumption. Container startup is included. The gate records a decision, not permission to apply an unreviewed patch or proof of general performance.

## Reproducible measurements

```sh
OCTANE_TEST_DOCKER_IMAGE=python:3.12-slim python3 -m unittest tests.test_accuracy_benchmark -v
```

The Docker CI job runs this harness. Source, labels and expected outputs are committed in `tests/accuracy_cases.py`; no external dataset is downloaded. One local run on November 20, 2026 produced:

| Check | Result |
| --- | --- |
| Eight isolated seeded Python defects | Buggy line top-1 and top-5 in 8/8 cases |
| Eight repaired controls | No ranked failing line in 8/8 cases |
| Ranking after spectra available | About 0.09–0.14 ms per case |
| 61-test simulated suite, time to first failure inside the container | 189 ms original order; 3.1 ms impacted-first |
| Hard-coded visible-test fix | Visible test passed; withheld input rejected it; general correction passed |
| Paired sum-loop optimization, 8 timing pairs | Accepted; median reduction 72.2%; interval 66.7–73.7% at 99.22% confidence |
| Mean duration and standard deviation | Before 0.973 ± 0.038 s; after 0.277 ± 0.019 s |

These are seeded mechanics checks. Many cases share a simple branch shape. The negative-control rate is not a general analyzer false-positive estimate; analysis time excludes spectrum collection; latency uses deliberate sleeps; the sum-loop result is not a project-wide optimization claim. CI checks localization thresholds and holdout rejection; timing variance can legitimately make the optimization gate decline.

The existing local public-history/SWE-bench-style adapter now reports file-level top-5 alongside top-1/MRR and time to the first produced hypothesis. Setup/execution errors count as localization misses in the labeled denominator. The [Blinker replay](evaluation.md) remains a one-case public check. Broader Defects4J/BugsInPy/SWE-bench accuracy, hidden-fix success rates, and representative false-positive rates remain unmeasured; no external benchmark dataset or license is bundled.

## Further research

Mutation-based localization, budgeted delta debugging, semantic backward slicing, deterministic repair-template generation, accepted-fix retrieval, automatic churn/coverage risk scores, ASan/TSan build orchestration, Atheris/Go fuzz adapters, N+1/resource-lifetime/nullability pattern families, persistent graph caches and parallel candidate validation are not delivered by this release. Existing mutation/property/revision tools provide foundations, but those names do not imply complete implementations.

## Primary references

- [Python tracing events and line tracing](https://docs.python.org/3.13/library/sys.html#sys.settrace).
- [OASIS SARIF 2.1.0 plus Errata 01](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html).
- [Abreu's spectrum-based fault-localization research](https://repository.tudelft.nl/record/uuid%3A78aa2510-acff-4acb-85ec-15852aa08e5c).
- [NIST Technical Note 2119](https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2119.pdf), quantile intervals using binomial order statistics.
