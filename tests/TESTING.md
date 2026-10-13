# Test commands

Use Python 3.11+ and install `requirements.txt` in a virtual environment.

```sh
python tests/run_tests.py --require-decompiler
python tests/benchmark_startup.py --samples 9
python tests/benchmark_reference.py --samples 9
```

The regression runner fails if Androguard is missing or any test is skipped in
strict mode. It covers index zero, empty/fallback instruction maps, string layout,
zeroed reconstructed DEX signatures/checksums, dummy imports, the MUTF-8 shim,
CLI stored/Deflate multidex operation, GUI data-store reuse, and reference searches
without site packages. `listclass` coverage includes local class-table
enumeration, stable DEX definition order, normalized package-prefix filtering,
stored/Deflate multidex APKs, DEX 041 containers, malformed indices, operation
without site packages, and a large-tail Deflate fixture. It does not open GUI
windows.

The synthetic startup gate measures `getclass --debug` in nine fresh interpreters.
Its median budgets are 100 ms internal time and 250 ms wall time on Linux CI.

`fixtures/reference-workload.zip` is the supplied archive, unchanged. Its SHA-256
is recorded in `fixtures/reference-baseline.json`. The reference runner extracts
its three files into a temporary directory and executes the original `test.py`
and `test_findrefs.py` with this checkout's core on `PYTHONPATH`.

- `test.py` must output `ClockFaceView` source. The median of nine script-reported
  times must be at most **0.0880 s**. Every measurement and the maximum are retained.
- Every `test_findrefs.py` run must match all **16 count metrics** exactly and
  emit all **20 timing metrics**. Median timing differences from the supplied
  baseline are reported; these individual times are not hard performance budgets.
- Both benchmark commands fail on process errors, missing output, or timeouts.

These are fresh interpreter measurements with warm filesystem caches, not cold
storage measurements or a guarantee on arbitrary hardware. CI runs both Python
3.11 and 3.12, including real-DEX checks. Reports and stdout/stderr are uploaded
from `artifacts/startup/` and `artifacts/reference/` even when a gate fails.

## Practical-regression comparison

```sh
python tests/run_tests.py --require-decompiler --suite unit
python tests/run_tests.py --require-decompiler --suite integration
python tests/benchmark_compare.py --baseline /path/to/base-checkout --samples 31
```

CI checks out the PR's exact base SHA. Direct pushes to `dev-0.1.0` compare
against the pre-push SHA, so the candidate cannot become its own baseline.
Other push/manual builds compare against `MG1937/ASC:dev-0.1.0`. A missing
baseline fails the job. The same interpreter,
runner, DEX, original scripts and query arguments exercise both revisions. On
Linux the comparison and its child processes use one fixed available CPU; both
sides use `PYTHONHASHSEED=0`. These controls are recorded in the report.

The comparison covers 20 module timings from `test_findrefs.py`, core decompilation
from `test.py`, and real-APK CLI `getclass` / `findrefs`: **23 metrics** in total.
Each workload records an explicit warmup pair, then 31 independent-process pairs
in alternating base/candidate order. Warmups are retained in raw logs but excluded
from statistics. Reference counts and decompiled/CLI outputs must match before
any performance result can pass.

Every metric remains in the report, but CI gates only the 11 aggregate user-visible
paths: module-level instruction, string, code, method, field, type, method-reference
and field-reference work; core decompilation; and CLI `getclass` / `findrefs`.
Fine-grained locator sub-metrics remain diagnostic only. A gated metric fails only
when it is both statistically significant in the one-sided exact paired sign test
(5% family-wise error budget with Bonferroni correction) and its paired median
slowdown is at least **3.0%**. Balanced noise, isolated scheduling outliers, and
statistically detectable sub-3% changes are reported but do not block CI. Passing
means no material statistically significant regression was observed in these
workloads, not proof of identical timing on every machine.

The comparison's unit tests exercise small/large regressions, improvements,
identical timings, noise, outliers and incomplete/invalid measurements. The
original absolute startup and 0.0880 s gates remain mandatory. Paired timings,
base/candidate SHAs, p-values and raw logs are saved in `artifacts/comparison/`.

## JVM regression coverage

```sh
python -m unittest discover -s tests -p test_jvm.py -v
ASC_TEST_CFR_JAR=/path/to/cfr.jar python tests/run_tests.py --require-decompiler --require-jvm
```

The JVM fixtures require JDK 17+ (`javac`) and are generated in temporary
directories. They cover JAR/WAR/EAR/Boot recursion, multi-JAR duplicates, loose
class names, Unicode/MUTF-8, multi-release manifests, opcode boundaries,
switch/wide instructions, lambda method references, bytecode offsets, selective
dependency staging, JSON Lines, malformed input, and bounded temporary storage.
Inventory tests verify that JAR class bodies remain unread during listing.
`ASC_TEST_CFR_JAR` enables real CFR decompilation of a WAR class and its internal
class; `--require-jvm` makes missing JDK/backend or JVM skips fail the runner.
Android-only strict mode does not require optional JVM integration prerequisites.
CI fetches CFR 0.152 with a pinned SHA-256 and runs both backends without skips.
Release tests verify the JVM package and JSON inventory from an unpacked source
archive outside the checkout. No application classes are executed by these tests.

The fixtures also verify Java 8 and 11 bytecode. Run a separate synthetic scale
probe with `python tests/benchmark_jvm.py --output artifacts/jvm/scale.json`.
Its default corpus contains 200 JARs with 50 classes each (10,000 definitions,
including duplicate names across archives). It checks complete reference counts
and reports inventory/scan medians and process RSS; it is not a real-application
performance guarantee or a comparison against another decompiler.

`test_jvm_web.py` covers JSP imports/actions/EL and Java block ranges, XML namespaces,
Resin/Jersey fragment recovery, XFire, ignored external DTDs, rejected entities,
Spring/injection class/method/field annotations, source comments/string exclusions,
SMAP metadata, ServiceLoader registrations, literal/local reflection tracking and
conservative branch handling. Bulk export tests cover duplicate paths, resume
hashes, output edits, failed-job retries, truncated final manifests, unmanaged
destination protection and symlink rejection. Real CFR bulk export/resume is in
the integration suite and is required by `--require-jvm`.

`test_jvm_batch.py` covers shared dependency context and ZIP reuse, bounded
per-container groups, nested duplicate libraries, selected-byte splits, batch
configuration fingerprints, edited-output resume, missing-source isolation and
non-multiplying batch timeouts. Real CFR verifies outer/inner exports without
exporting unselected dependency classes. This integration test is also required
by `--require-jvm`.

`test_jvm_memory.py` uses synthetic host/cgroup files to cover availability,
ancestor usage, v1/v2 limits, private namespaces, unknown-memory fallback and
worker-dependent batch budgets without requiring a JDK. Batch tests also verify
that memory changes preserve completed exports on resume and that real CFR CLI
summaries report automatic and explicit selections.

Run a paired full-JAR export diagnostic with a previous ASC checkout (or omit
`--baseline` to compare current `--batch-size 1`):

```sh
python tests/benchmark_jvm_export.py /path/to/library.jar \
  --baseline /path/to/previous-checkout --cfr-jar /path/to/cfr.jar \
  --jadx /path/to/jadx/bin/jadx --samples 3 --batch-size 64 \
  --output artifacts/jvm/export-comparison
```

The output directory must be empty. One warmup is excluded and fresh processes
run in alternating order on a fixed CPU set where supported. The runner checks
ASC class-origin sets and records source-hash matches, commands, logs, wall-time
medians and sampled process-tree RSS (when optional `psutil` is installed).
JADX may merge inner classes into outer source files, so its source-file count
can differ from ASC's per-definition export. The diagnostic is not a CI gate or
proof of semantic equivalence/statistical significance.
