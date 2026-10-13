# JVM implementation guide

The JVM workflow shares ASC's CLI with the Android backend. Runtime code lives
under `droidasc/jvm/`; command contracts live in [JVM.md](JVM.md). Tests and
benchmark runners live in `tests/`, while measurements and exported application
source stay in ignored artifact directories.

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `droidasc/cli.py` | Parse commands, select Android/JVM input handling, serialize records and report exit status |
| `jvm/application.py` | Discover inputs, preserve origins, select multi-release definitions, read classes/resources and stage dependency context |
| `jvm/classfile.py` | Parse class metadata, constant-pool values, instructions, annotations and bytecode references |
| `jvm/decompiler.py` | Invoke configured CFR for individual classes or batches; enforce timeout and source-size limits |
| `jvm/exporter.py` | Schedule bounded groups, write atomic outputs, maintain the manifest and validate resume candidates |
| `jvm/memory.py` | Read available host/container memory and choose the default batch size for the worker count |
| `jvm/analyzer.py` | Stream resource/class analysis records and attach inventoried definition links |
| `jvm/webanalysis.py` | Parse JSP, XML, source annotations, source reflection and compiled JSP mappings |
| `jvm/reflection.py` | Report reflection sites and locally recoverable literal bindings from bytecode |

Keep input ownership and archive access in `JvmApplication`, rather than adding
independent archive walkers to analyzers. Keep CFR subprocess details in the
decompiler adapter, and leave JSON serialization and process exits to the CLI.
Inventory and static analysis must continue to work without a Java runtime or
configured CFR backend.

## Discovery and selection

`JvmApplication` owns its temporary nested-archive spool and closes it on context
exit. An origin identifies the original path and full archive-member chain;
the same internal class name in two libraries remains two distinct definitions.
Multi-release selection applies the requested Java release before queries.
Archive listing uses directory metadata instead of parsing every class body;
loose class files are parsed to recover their internal names.

Bytecode queries read selected definitions on demand. Dependency staging starts
from selected roots, prefers definitions in their source container, and only
uses unambiguous application-wide matches as a fallback. Dependencies supply
context without becoming additional export records.

## Export lifecycle

1. Discover inputs and sample memory once. Automatic batch sizing reserves
   headroom and divides the remaining budget by worker count; an explicit size
   bypasses the memory probe.
2. Select class definitions by prefix and limit. Group consecutive definitions
   from one source container, splitting on the batch cap, duplicate internal names
   or selected-bytecode bound.
3. Read each group through a worker-local archive cache retained for the export.
   Share that reader with dependency staging and isolated retries. Compare managed
   output hashes and configuration fingerprints before invoking CFR.
4. Stage shared context for pending classes, invoke CFR, and check each expected
   source file. Missing outputs and context failures fall back to individual
   class attempts; a batch timeout records failures without multiplying timeouts.
5. Replace successful outputs atomically and append their manifest records under
   a lock. The CLI emits JSON Lines when each scheduled group completes and a
   JSON summary on stderr after export.

The executor has a bounded submission queue. Each worker keeps at most eight
archive handles, without sharing a handle with other workers. Reader contexts
close after the executor finishes, including failure and early generator closure.
Archive reuse changes scheduling/I/O only, so it does not alter the resume
configuration fingerprint. Output paths include a source
container hash, so duplicate definitions retain separate files. Resume preserves
completed results only after checking the input, backend/configuration and output
hashes. Changes in available memory under the same automatic policy affect
pending scheduling; changing explicit batch settings invalidates managed results.
Successful records remain available when other entries fail, and the CLI returns
status 1 for partial failures.

## Analysis lifecycle

`analyze()` combines resource evidence with class-file metadata. Source-only mode
returns after resource analysis. Otherwise, class-prefix and class-count filters
bound the bytecode pass. Definition links distinguish a unique definition,
duplicates and external/missing classes; they do not establish runtime dispatch
or reachability. Resource and class parsing failures remain explicit
`analysis-error` records.

Add framework formats or annotations to `webanalysis.py`, bytecode reflection
handling to `reflection.py`, and record coordination to `analyzer.py`. Preserve
origin and location evidence, mask comments in source analysis, and avoid
reporting unrelated configuration values. Dynamic bindings should stay explicit
when local literal recovery cannot resolve them.

## Validation and packaging

Generated JVM fixtures cover loose classes, nested archives, duplicate names,
multi-release selection, JSP/framework/reflection records and resume behavior.
Synthetic memory fixtures cover cgroup v1/v2 ancestors, namespaces and worker
budgets. Real CFR integration covers inner and anonymous classes and both CLI
batch-selection modes. See [test commands](../tests/TESTING.md).

`scripts/build_release.py` includes JVM runtime modules, README, documentation
and the testing guide in deterministic source ZIPs. Executable tests, fixture
archives, local backend JARs and exported application source are excluded.
The extracted-package integration test verifies Android and JVM commands outside
the checkout. Use a source checkout to run the regression and performance suites.

The CI matrix remains Python 3.11/3.12 with existing Android performance gates.
JVM export benchmarks are separate diagnostics: use identical inputs and explicit
batch settings, retain per-origin output/hash checks, and report memory alongside
time. See [batch measurements](JVM-BATCH-PERFORMANCE.md) and
[release conventions](RELEASING.md).
