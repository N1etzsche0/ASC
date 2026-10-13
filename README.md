# ASC: Java web application and Android decompilation

ASC provides a CLI for inspecting compiled Java applications and Android
artifacts. This fork adds recursive JAR/WAR discovery, source export and static
JSP, framework and reflection analysis to the original Android workflow.
Agents and scripts can query classes and bytecode references before choosing
which sources to decompile, or export an entire application with a resumable
manifest.

For JVM inputs, inventory and static analysis use Python without starting Java.
Source decompilation uses a configured CFR backend. ASC inspects application
files without launching the application.

| Input | Supported workflow |
| --- | --- |
| Android APK / ZIP containing DEX | Class listing, selective decompilation, bytecode references and APK manifest decoding |
| JVM class / JAR / WAR / EAR / ZIP | Class listing, selective decompilation, bytecode references, bulk export and static analysis |
| JVM application directory | Recursive discovery of loose classes, multiple archives and source/configuration assets |

Nested libraries in `WEB-INF/lib`, `BOOT-INF/lib` and EARs are discovered.
Duplicate class definitions retain their full archive origins. ZIPs containing
DEX/APK entries use the Android backend. The GUI supports Android inputs;
the JVM workflow uses the CLI.

## Install this fork

Install **this checkout** to use the fork's JVM features. The upstream PyPI
package may have a different feature set. Python 3.11 and 3.12 are the repository's
CI targets.

```sh
git clone --branch jvm-web-cli https://github.com/N1etzsche0/ASC.git
cd ASC
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
droidasc --help
```

`python main.py` uses the same CLI. For an extracted source release, install
`requirements.txt` and run `python main.py`; see [source releases](docs/RELEASING.md).

JVM inventory, bytecode reference search and static analysis require no Java
runtime or decompiler. JVM `getclass` and class export require Java and a locally
configured [CFR JAR](https://www.benf.org/other/cfr/). CFR 0.152 is covered by
integration tests.

```sh
export ASC_CFR_JAR=/absolute/path/to/cfr.jar
```

You can also pass `--cfr-jar /absolute/path/to/cfr.jar`. ASC does not download a
backend automatically. It invokes CFR without running the inspected application.
See [JVM setup and limits](docs/JVM.md#setup) for Java, timeout and dependency
context options.

## Quick start

Use an archive or a directory containing application classes and dependencies:

```sh
droidasc listclass /path/to/application --json -o classes.jsonl
droidasc getclass /path/to/application com.example.Controller -o Controller.java
droidasc export /path/to/application --threads 2 -o /path/to/export
droidasc export /path/to/application --threads 2 -o /path/to/export --resume
droidasc analyze /path/to/application --kind framework --json -o frameworks.jsonl
```

Use a new export directory outside the application directory. Class inventory
preserves each definition's archive origin; the export manifest tracks success,
failure and source hashes. Create parent directories for report files before
passing `-o`. The commands below describe selection, memory and analysis options.

## CLI commands

| Command | Purpose |
| --- | --- |
| `listclass` | List Android/JVM classes; filter packages and report JVM origins |
| `getclass` | Decompile one selected Android/JVM class |
| `findrefs` | Search bytecode string, type, method and field references |
| `getmanifest` | Decode an APK's AndroidManifest.xml |
| `export` | Export JVM source with separate origin paths and a resumable manifest |
| `listresources` | Inventory Java, JSP, tag, XML, properties and service-provider assets |
| `analyze` | Report static JSP, framework and reflection evidence as JSON Lines |

### Inspect a Java web application

```sh
droidasc listclass application.war --prefix com.example --origins --json
droidasc listclass /path/to/webapp --classpath /path/to/libs --json -o classes.jsonl
droidasc getclass application.war com.example.Controller -o Controller.java
droidasc findrefs --json /path/to/webapp method execute --class java.sql.Statement
droidasc listresources application.war --kind jsp --kind tld --json
```

For duplicate class names, pass the exact origin from `listclass` to
`getclass --origin`. Quote shell arguments containing `$` or `;`.
For `findrefs`, parent options such as `--json` and `--prefix` go before
`string/type/method/field`; member options such as `--class` follow it.

### Export source in batches

Choose an initially empty output directory outside the input directory.

```sh
droidasc export /path/to/webapp -o /path/to/export --threads 2 --java-release 8
droidasc export /path/to/webapp -o /path/to/export --threads 2 --java-release 8 --resume
droidasc export application.war -o /path/to/selected-export --prefix com.example --limit 100 --no-resources
```

Classes are written under `sources/<container-hash>/<package>/`; separate
containers preserve duplicate definitions. Existing Java/JSP/tag sources are
copied under `resources/<container-hash>/`. Configuration files can be inventoried
and analyzed; export does not copy them by default.

`manifest.jsonl` records origins, input/output hashes, backend configuration,
output paths and status. `--resume` skips matching outputs, regenerates changed
managed outputs and retries failed jobs. Unmanaged files are not overwritten.
The default is two concurrent CFR processes, each with a 512 MiB Java heap limit.
`getclass` and `export` accept `--heap-mb 64..8192`; large generated classes may
need `--heap-mb 2048`. Budget the heap per worker; changing it invalidates managed
results on resume.
`--no-enum-switch` can reduce analysis cost for large generated enum switch tables
by retaining lowered mapping expressions; changing it also invalidates resume.
Classes from the same source container share a JVM invocation. Batch size is
selected automatically from available memory, remaining Linux cgroup limits and
worker count, choosing 1, 8, 16, 32 or 64. `--batch-size N` overrides this choice
(1..256); each batch has a 32 MiB selected-input bound. The JSON summary on stderr
includes the selected size and mode. Shared dependencies are staged once per
invocation. Each worker reuses up to eight archive handles across batches and
dependency staging; caches close when export finishes.
`--batch-size 1` uses individual invocations. Missing batch
outputs are retried individually; a timed-out batch records failures for resume.
Batch settings enter the resume fingerprint, so upgrading the strategy, switching
automatic/manual mode or changing an explicit size regenerates managed results.
Memory fluctuations in automatic mode preserve completed, hash-verified outputs.
`--limit` bounds classes; resources
are selected separately or disabled with `--no-resources`. Use separate
destinations for concurrent export runs. See [bulk export](docs/JVM.md#bulk-source-export).

For a large generated-code JAR, select an explicit heap and worker count:

```sh
droidasc export application.jar --threads 8 --heap-mb 2048 --no-enum-switch \
  --no-resources -o /path/to/large-export
```

This profile was measured on the public inputs below. Heap limits apply to each
worker; `--no-enum-switch` trades reconstructed enum switches for lowered mapping
expressions. Tune the worker count to the available memory. Use the same options
when resuming, and use `getclass` to retry an exact class with different settings.

### Analyze JSP, frameworks and reflection

```sh
droidasc analyze /path/to/jsp --kind jsp --sources-only --json -o jsp.jsonl
droidasc analyze application.war --kind framework --java-release 8 --json -o frameworks.jsonl
droidasc analyze application.war --kind reflection --prefix com.example --json -o reflection.jsonl
```

JSP analysis reports directives, actions, Java block locations and EL identifiers.
Compiled servlet metadata can identify JSP source mappings; decompiled servlet
Java cannot reliably reconstruct the original JSP template.

Framework analysis covers servlet/filter/listener mappings, Resin-compatible
descriptors, Spring XML and annotations, Jersey scanning, XFire services,
JSP/TLD metadata and `META-INF/services`. Reflection analysis reports standard
lookup/invocation sites with literal or dynamic bindings. Class definition links
record present, ambiguous or external/missing definitions. These are static
evidence, not proof of runtime activation or reachability.

### Agent and script output

JVM `listclass`, `getclass` and `findrefs` support `--json`. `export`, `analyze`
and `listresources` emit JSON Lines by default. Class records retain origins;
reference records include caller methods, descriptors and bytecode offsets.
Export progress goes to stdout and its summary goes to stderr.

```sh
droidasc export application.jar --no-resources -o /path/to/export \
  > progress.jsonl 2> export.log
```

Stdout contains per-entry records. Stderr may include context/backend warnings
before the final JSON summary; scripts should read the last line for that summary.

Check the process exit status as well as the output. Export and analysis retain
successful records when individual inputs fail, emit explicit error records and
return status 1. Create output parent directories before using `-o`.
`listclass`, `listresources` and `analyze` send `-o` results only to the file;
`getclass` and `findrefs` also retain stdout output. See [JVM CLI documentation](docs/JVM.md)
for command-specific schemas and behavior.

### Android examples

```sh
droidasc listclass app.apk --prefix com.poc -o classes.txt
droidasc getclass app.apk 'Lcom/poc/Main;' -o Main.java
droidasc getmanifest app.apk -o AndroidManifest.xml
droidasc findrefs app.apk method onCreate --class com.poc.Main
droidasc app.apk --gui
```

Android class lists use Dalvik descriptors in definition order. The GUI
additionally requires Tk. Standalone DEX is supported by the core API; the
current Android CLI expects an APK/ZIP container.

## Validation and scope

Local validation on 2026-10-13, using Linux, OpenJDK 25.0.4 and CFR 0.152.
The recorded regression and Android performance gates cover implementation
commit `ea05ff1`, including export reader reuse, heap-setting and source-quality checks.
These are local checks, separate from GitHub Actions results.

### Regression and performance results

| Check | Python 3.11.16 | Python 3.12.14 | Acceptance |
| --- | ---: | ---: | --- |
| Strict regression, including real CFR | 137 passed, no skips | 137 passed, no skips | All tests pass |
| Paired Android performance comparison | Passed | Passed | No material regression in 11 gated metrics |
| Cold-process startup: internal median | 33.08 ms | 32.69 ms | At most 100 ms |
| Cold-process startup: wall-time median | 64.70 ms | 63.52 ms | At most 250 ms |
| Supplied DEX decompilation median | 50.40 ms | 48.50 ms | At most 88 ms |
| Supplied DEX reference counts | All 16 metrics matched | All 16 metrics matched | Exact counts on every sample |

The comparison used upstream development commit `e94ea6f` as the baseline,
31 alternating process pairs per workload after warmup, and CPU 23, separate
from the large-JAR workload's CPU 0..7.
It records 23 timing metrics and gates 11 aggregate paths. Startup and supplied
DEX checks each used nine fresh-process samples. These tests measure Android
behavior; they do not establish JVM export speed.

Regression coverage includes nested JAR/WAR/EAR inputs, duplicate origins,
bytecode queries, real CFR inner/anonymous classes, export/resume, automatic
memory sizing, JSP/framework/reflection evidence, and Android/JVM commands from
an extracted source package. See [test commands and gate definitions](tests/TESTING.md).

### JVM batch export results

The following full-JAR measurements use CFR 0.152, `--threads 2` and fixed
`--batch-size 64`. Each result is the median of three runs after warmup; the
baseline is the pre-batching exporter on identical inputs.

| Public dependency | Class definitions | Before batching | Batch export | Speedup | Matching source hashes |
| --- | ---: | ---: | ---: | ---: | ---: |
| `slf4j-api.jar` | 24 | 5.374 s | 0.887 s | 6.06× | 24/24 on all runs |
| `commons-lang.jar` | 124 | 36.743 s | 4.365 s | 8.42× | 124/124 on all runs |

Median process-tree peak RSS increased from 180.2 to 232.3 MiB for SLF4J and
from 230.2 to 707.9 MiB for Commons Lang. With batch size 16, Commons Lang took
8.024 s and 445.4 MiB, with all 124 source hashes still matching. These checks
confirm output consistency; source quality depends on CFR and dependencies. See
[batch export measurements](docs/JVM-BATCH-PERFORMANCE.md) for sampling limits,
JADX comparison and reproduction commands.

### Large public JAR results

Two public Maven Central JARs were measured on Linux with Python 3.14.6,
OpenJDK 25.0.4 and CFR 0.152. Export used eight workers, a 2 GiB heap per CFR
process, `--no-enum-switch`, `--no-resources`, and automatic batch size 64.
The Hive export covers `5f5ee68`; the larger AWS export covers `ea05ff1` with
archive readers shared across batches and dependency staging.

| Public JAR | Size | Selected class definitions | Inventory wall time | Inventory peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Apache Hive `hive-exec:4.0.1` | 79.54 MiB | 38,481 | 0.598 s | 62.80 MiB |
| AWS `aws-java-sdk-bundle:1.12.780` | 370.54 MiB | 173,906 | 2.659 s | 231.98 MiB |

| JAR | Operation | Wall time | Sampled process-tree peak RSS | Result |
| --- | --- | ---: | ---: | --- |
| Hive | Full export | 601.49 s | 5,872.12 MiB | 38,480 outputs; one heap failure, exit 1 |
| Hive | Resume | 106.94 s | 2,645.62 MiB | 38,480 reused; one failed retry, exit 1 |
| AWS Bundle | Full export | 1,217.22 s | 5,212.05 MiB | 173,906 outputs; no class errors, exit 0 |
| AWS Bundle | Resume | 83.38 s | 1,628.44 MiB | All 173,906 outputs reused, exit 0 |

Both exports passed manifest/output hash checks with zero mismatches. The Hive
parser that failed at 2 GiB produced a separate source file through exact
`getclass` selection at 8 GiB in 43.41 s; that recovery leaves the original export
manifest and exit status intact. All 12,416 common AWS sources from the interrupted
pre-cache trial matched the new export's source hashes.

Source quality was checked separately from output-file coverage:

| Batch outputs | Files with failure markers | CFR failed-method comments / failure stubs | Files with structuring warnings / warning count |
| --- | ---: | ---: | ---: |
| Hive | 27 | 32 / 32 | 43 / 60 |
| AWS Bundle | 8 | 10 / 10 | 6 / 8 |

The separately recovered Hive parser adds one structuring warning. Comments and
stubs can refer to the same method; marker totals are not distinct-method counts.
Compilability and semantic equivalence were not tested. Each operation was
measured once; these results do not establish a large-JAR speedup over another
tool. Public downloads, checksums, raw-evidence locations and reproduction commands
are in the [Hive validation](docs/JVM-LARGE-JAR-VALIDATION.md) and
[AWS Bundle validation](docs/JVM-AWS-BUNDLE-VALIDATION.md) reports.

Multi-release JAR selection defaults to `--java-release 17`; use the same
selection when resuming an export. Nested archives require bounded temporary
disk storage. JVM reference scans are sequential; `export --threads` controls
CFR concurrency. Decompilation quality depends on CFR and available dependencies.
Static analysis does not implement a global call graph, interprocedural data flow,
runtime reflection resolution or JSP execution. Arbitrary obfuscation and every
compiler/class-file version are not guaranteed. The Android benchmarks below
do not measure JVM performance.

## Testing and releases

The JVM test fixtures require JDK 17+ (`javac`) and a CFR JAR. Run the repository
runner with both integration backends required:

```sh
ASC_TEST_CFR_JAR=/absolute/path/to/cfr.jar python tests/run_tests.py --require-decompiler --require-jvm
```

See [testing and performance gates](tests/TESTING.md) and
[JVM implementation structure](docs/JVM-ARCHITECTURE.md) for contributor guidance,
and [release conventions](docs/RELEASING.md) for packaging. Releases require Python 3.11/3.12 CI,
integration tests and performance gates; the local result above does not replace
those checks. Retain the upstream [Apache-2.0 license](LICENSE) and acknowledgements.

## Upstream Android design

The original Droid ASC presentation,
[R8 Compiler Optimization as a DeCompiler Primitive](https://blackhat.com/europe/arsenal/schedule/index.html#droid-asc-r8-compiler-optimization-as-a-decompiler-primitive-54834),
describes metadata queries without full preprocessing, direct DEX bytecode
reference search and reconstruction of a minimal DEX for selected decompilation.
The upstream image and demonstration below describe Android workloads; the JVM
measurements in this README are separate local tests of this fork.

### Android benchmark
![Benchmark](./docs/benchmark_all_en.png)

https://github.com/user-attachments/assets/4c4a6813-8561-490c-a573-ef113da861b6

## Sponsors

| Sponsor | Amount |
| --- | ---: |
| [HTTChina](https://github.com/HTTChina) | 5000￥ |
| [c01ns](https://github.com/c01ns) | 11.9￥ |
| [gguowang](https://github.com/gguowang) AND [cococount](https://github.com/cococount) | 2￥ |
| [Ylarod](https://github.com/Ylarod) | 50.11￥ |

Thank you to all sponsors for your support!

ASC is built with the hope that it can genuinely help everyone improve their reverse engineering workflow — from speeding up the tedious parts to making the impossible feel routine.

## Acknowledgements

Thanks to the [Androguard](https://github.com/androguard/androguard) project
for providing the Android reverse-engineering and DEX analysis foundation used
by Droid ASC.
