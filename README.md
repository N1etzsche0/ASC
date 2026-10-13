# Droid ASC: On-demand Android and JVM decompilation

This fork extends ASC's Android workflow to large Java web applications. The
CLI lists classes, searches bytecode references, decompiles selected classes,
exports source in batches, and reports static JSP, framework and reflection
evidence. JSON Lines output supports Agents and scripts.

| Input | Supported workflow |
| --- | --- |
| Android APK / DEX | Class listing, selective decompilation, bytecode references and APK manifest decoding |
| JVM class / JAR / WAR / EAR / ZIP | Class listing, selective decompilation, bytecode references, bulk export and static analysis |
| JVM application directory | Recursive discovery of loose classes, multiple archives and source/configuration assets |

Nested libraries in `WEB-INF/lib`, `BOOT-INF/lib` and EARs are discovered.
Duplicate class definitions retain their full archive origins. ZIPs containing
DEX/APK entries use the Android backend. The GUI supports Android inputs;
the JVM workflow uses the CLI.

## Install

Install **this checkout** to use the fork's JVM features. The upstream PyPI
package may have a different feature set. Python 3.11 and 3.12 are the repository's
CI targets.

```sh
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
Classes from the same source container share a JVM invocation. Batch size is
selected automatically from available memory, remaining Linux cgroup limits and
worker count, choosing 1, 8, 16, 32 or 64. `--batch-size N` overrides this choice
(1..256); each batch has a 32 MiB selected-input bound. The JSON summary on stderr
includes the selected size and mode. Shared dependencies are staged once.
`--batch-size 1` uses individual invocations. Missing batch
outputs are retried individually; a timed-out batch records failures for resume.
Batch settings enter the resume fingerprint, so upgrading the strategy, switching
automatic/manual mode or changing an explicit size regenerates managed results.
Memory fluctuations in automatic mode preserve completed, hash-verified outputs.
`--limit` bounds classes; resources
are selected separately or disabled with `--no-resources`. Use separate
destinations for concurrent export runs. See [bulk export](docs/JVM.md#bulk-source-export).

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

Local validation on 2026-10-13:

| Check | Result |
| --- | --- |
| Strict local regression suite, including real CFR | 130 tests passed on each of Python 3.11.16 and 3.12.14 |
| ecology9 recursive JVM inventory | 331,447 class definitions; no discovery errors |
| ecology9 JSP/configuration analysis | 97 JSPs and 15 parseable XMLs; six incomplete XML fragments remain explicit errors |
| ecology9 CFR bulk export and resume | Five small interfaces exported successfully; all five skipped on resume |

The ecology9 run did not decompile all 331,447 definitions. See
[compatibility validation](docs/ECOLOGY9-VALIDATION.md) for corpus details,
reproduction commands and coverage gaps. Validation artifacts and exported
application source remain outside the submitted files.

Multi-release JAR selection defaults to `--java-release 17`; use the same
selection when resuming an export. Nested archives require bounded temporary
disk storage. JVM reference scans are sequential; `export --threads` controls
CFR concurrency. Decompilation quality depends on CFR and available dependencies.
Static analysis does not implement a global call graph, interprocedural data flow,
runtime reflection resolution or JSP execution. Arbitrary obfuscation and every
compiler/class-file version are not guaranteed. The Android benchmarks below
do not measure JVM performance.

The bounded CFR batch exporter reduced local full-JAR export times by about
6–8 times in two dependency-library samples, with increased peak memory. See
[batch export measurements](docs/JVM-BATCH-PERFORMANCE.md) for matching-source
checks, memory tradeoffs and reproduction commands.

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

The original Droid ASC presentation, **R8 Compiler Optimization as a DeCompiler Primitive**:
https://blackhat.com/europe/arsenal/schedule/index.html#droid-asc-r8-compiler-optimization-as-a-decompiler-primitive-54834

When decompiling massive Android APKs, the standard procedure is to wait. We wait for tools to eat gigabytes of RAM, fully inflate the artifacts, and spend tens of minutes building heavy global indexes and cross references... All of this is just to guarantee fast code searches later, but here is the contradiction. A compiled artifact is already highly structured, modern decompilers never utilize this, they waste massive amounts of time and memory reconstructing a bloated database of code relationships over already structured data. This engineering approach defies common sense. When I can directly extract any code relationship from the APK in milliseconds, does this preprocessing still hold any value?

Instead of forcing decompilers into heavy preprocessing, we choose to query the compiled artifact directly as a database. We built a stateless, zero-overhead engine that extracts and searches code on demand in milliseconds. In this briefing, we will explore the underlying engineering required to bypass traditional bottlenecks. We will demonstrate how to abandon full inflate by probing directly within the Deflate bitstream, building dense Huffman lookup tables to extract core metadata without touching irrelevant data blocks. Furthermore, we will explain optimization details of the R8 compiler, especially how deterministic constant relocation and instruction deduplication leave behind highly concentrated physical layouts, we weaponize this compiler behavior to execute lightning-fast cross DEX code searches. To map these raw bytecode offsets back to methods, we engineered an O(1) instruction locating primitive, achieving constant-time method resolution without building heavy mapping tables. Finally, upon hitting a target, Droid ASC extracts only the specific bytecodes and its dependencies, dynamically reconstructing a minimal and self consistent DEX entirely in memory for instant decompilation.

We will demonstrate this architecture live against a 352MB commercial APK. Droid ASC executes global cross reference searches in 1.79 seconds and decompiles target classes in 177 milliseconds using only 141MB of RAM. By treating the artifact as a read only database and operating with zero preprocessing, we return the decompiler to its core essence. It is no longer a bloated indexing tool, but a lightning fast, on demand decompilation engine that fundamentally redefines how we analyze compiled code.

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
