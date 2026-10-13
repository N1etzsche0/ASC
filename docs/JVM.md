# JVM application CLI

ASC can inspect Java application bytecode alongside its existing Android backend.
The JVM backend accepts a `.class`, `.jar`, `.war`, `.ear`, `.zip`, or a directory
containing multiple archives and loose class files. It recursively discovers
archives, including `WEB-INF/lib`, `BOOT-INF/lib` and WARs inside EARs. Application
classes under `WEB-INF/classes` and `BOOT-INF/classes` use ordinary Java package
names. ZIP inputs containing DEX/APK entries retain the Android route.

## Setup

Use this checkout with `python main.py`, or install it with `pip install -e .`.
`listclass` and `findrefs` use Python's standard library: they require neither
Java nor a decompiler. `getclass` requires a Java executable and a CFR JAR.
Download CFR from its [official site](https://www.benf.org/other/cfr/), then set:

```sh
export ASC_CFR_JAR=/absolute/path/to/cfr.jar
```

Alternatively pass `--cfr-jar /absolute/path/to/cfr.jar` on each `getclass` call.
The CLI does not download or run the application being inspected. It launches
CFR in a separate JVM with a 512 MiB heap and a default 120-second timeout.
Use `--java /path/to/java` and `--timeout 300` when needed. CFR 0.152 is covered
by integration tests; newer class-file features depend on the configured CFR
version. Selecting `--java-release` does not change the decompiler's Java runtime.

## Find classes and their origins

```sh
python main.py listclass app.war --prefix com.example --origins
python main.py listclass /path/to/webapp --json -o classes.jsonl
python main.py listclass app.jar --classpath /path/to/libs --json
python main.py listclass Main.class --json
```

A directory is scanned recursively. `--classpath PATH` adds another archive,
class file or directory to the same application scope and can be repeated.
Inputs are deduplicated by resolved filesystem path; class definitions from
different archives are preserved. Class names accept `com.example.Main`,
`com/example/Main`, or `Lcom/example/Main;`. Quote descriptors containing `;`
and internal-class names containing `$` in the shell.

Plain class lists contain one JVM descriptor per definition. `--origins` adds
a tab-separated source path. `--json` emits one JSON object per line:

```json
{"class":"Lcom/example/Main;","origin":"/path/app.war!/WEB-INF/classes/com/example/Main.class","release":0}
```

For a nested dependency the origin includes the full archive chain, for example
`/path/app.war!/WEB-INF/lib/library.jar!/com/example/Helper.class`.
`--prefix` filters calling-class names by prefix, without a wildcard syntax.
For `listclass`, `-o` writes only to the selected file.

## Decompile one class

```sh
python main.py getclass app.war com.example.Main -o Main.java
python main.py getclass app.jar 'com.example.Main$Inner' --json
python main.py getclass Main.class com.example.Main --classpath /path/to/libs
```

Duplicate names cause an error rather than selecting an arbitrary definition.
Choose the exact `origin` returned by `listclass --json` or `--origins`:

```sh
python main.py getclass /path/to/webapp com.example.Main \
  --origin '/path/to/webapp/app.war!/WEB-INF/classes/com/example/Main.class'
```

Only the selected class is passed to CFR. ASC stages its locally available
constant-pool dependencies, including internal classes, into a temporary class
directory. It favours definitions in the same archive; dependencies that remain
ambiguous across other archives are omitted. For loose classes, supply a class
root or related files via `--classpath` to provide context. The closure is bounded
to 512 classes or 128 MiB; reaching a bound produces a warning on stderr and CFR
continues with the available context. Missing context can reduce source quality.
Temporary files are removed after the command.

JSON decompilation emits one object with `class`, `origin`, `release`, and `source`.
`getclass -o` also prints the result, matching the existing Android command.

## Search bytecode references

```sh
python main.py findrefs --json app.war string Authorization
python main.py findrefs --json /path/to/webapp type java.sql.Connection
python main.py findrefs --json app.war method execute --class java.sql.Statement
python main.py findrefs --json app.war field token --class com.example.Config
python main.py findrefs --json --prefix com.example app.war method echo
python main.py findrefs --classpath /path/to/libs app.jar method run --class Service --fuzzy-class
```

Options on the `findrefs` parent command (`--json`, `--classpath`, `--prefix`,
`--debug`, `--threads`, `--java-release`) must precede `string/type/method/field`.
Member-specific options (`--class`, `--fuzzy-class`, `-o`) follow that subcommand.
String, type and member-name matching is case-sensitive substring matching.
Member-owner matching is exact unless `--fuzzy-class` is supplied. A member query
can specify just an owner, just a name, or both.

Each JSON result represents a bytecode occurrence:

```json
{"origin":"/path/app.jar!/com/example/Main.class","class":"Lcom/example/Main;","method":"run","descriptor":"()V","offset":12,"kind":"method","target":"Ljava/sql/Statement;->execute(Ljava/lang/String;)Z"}
```

Offsets are numeric bytecode offsets in JSON and hexadecimal in plain output.
Method descriptors distinguish overloads. The target is a constant-pool symbol,
not a resolved runtime dispatch destination. `-o` also retains stdout output,
matching the Android reference command. Debug output goes to stderr for JVM
commands; nonzero exit status indicates failure. On a mid-scan parse failure,
previously emitted lines may exist and should not be treated as a complete scan.

The scanner follows instruction boundaries, including `tableswitch`,
`lookupswitch`, and `wide`. It resolves direct field/method/type/string operands,
method handles, `invokedynamic`, and dynamic-constant bootstrap dependencies.
Type results include owners and object types in descriptors of referenced members.
It does not report unused constant-pool entries as code references.

## Multi-release JARs

`--java-release 17` is the default. A JAR with `Multi-Release: true` in its main
manifest selects the highest eligible `META-INF/versions/N` class variant for
each class. Set `--java-release 8`, `11`, `21`, etc. to change that selection;
version directories in ordinary JARs are ignored. `release: 0` means a base
entry. This is variant selection, not full class-loader emulation.

## Performance and current scope

JAR/WAR class inventory reads ZIP directory metadata without inflating class
bodies. Nested archives must be decompressed into temporary disk storage to
inspect their own directories. Loose class files are parsed to recover their
actual names, even when filenames differ. Reference search opens each archive
group once and processes one class at a time; it does not build a global call
graph or decompile the whole application. `--prefix` avoids reading unrelated
class bodies. The JVM scan currently runs sequentially; `--threads` still
controls the Android backend.

Each class is limited to 32 MiB, each nested archive to 512 MiB, nested spool
storage to 2 GiB total, and archive nesting depth to 8. ZIP duplicate entry names
and unsafe class paths are rejected. Archive entries are inventoried by their
class path; an entry/class-name mismatch is reported when it is parsed.

The extended CLI supports bulk source export and static JSP/framework/reflection
evidence; see the commands below. It does not implement interprocedural data flow,
runtime reflection resolution, JSP compilation/execution, full framework dispatch
emulation, exception-table reference searches, or JVM GUI support. Arbitrary
obfuscators and every JVM language/compiler/version are not guaranteed. Parsing
is a structural reader, not a full JVM verifier.

## Bulk source export

```sh
python main.py export /path/to/webapp -o /path/to/export --threads 2 --java-release 8
python main.py export /path/to/webapp -o /path/to/export --threads 2 --java-release 8 --resume
python main.py export app.war -o /path/to/export --prefix com.example --limit 100 --no-resources
```

`export` emits JSON Lines progress on stdout and a summary on stderr. It writes
Java source to `sources/<container-hash>/<package>/<Class>.java`. Different input
containers keep separate paths, so duplicate definitions are preserved. Loose
classes in the same package root share a container; their dependency context
prefers that root. Raw `.java`, JSP/JSPX/JSPF and tag sources are copied to
`resources/<container-hash>/...` unless `--no-resources` is used. XML/properties
configuration and service-provider files are inventoried/analyzed rather than
copied by default. Source assets can themselves contain sensitive data: select
the intended source scope for export.

The destination must be outside directory inputs and initially empty. A populated
ASC export accepts `--resume`; arbitrary populated directories are rejected.
`manifest.jsonl` records each origin, input SHA-256, output SHA-256, backend/config
fingerprint, relative output path and success/error. Resume skips matching managed
outputs, regenerates changed outputs and retries failures. Input file size/mtime
changes invalidate dependent source results; class content and managed output
hashes are also checked. The hashes provide integrity checks, not a signed audit
trail. Files not owned by the manifest are not replaced. Output writes are atomic,
and an interrupted final manifest line can be recovered. Concurrent export
invocations must use separate destinations; a manifest is owned by one run.

`--threads` defaults to two concurrent CFR processes. Omit `--batch-size` to
select the size automatically, or supply a number in 1..256. Each group contains
classes from one source container and is split further at 32 MiB of selected
bytecode. Nested libraries and duplicate
containers remain separate. Only selected classes are passed to CFR; dependency
classes are shared context, not additional export records. Inner/anonymous class
definitions retain individual output paths. ZIP directories are reused through
worker-local caches of at most eight archives, and the dependency closure is
staged once per invocation (512 classes / 128 MiB). `--batch-size 1` uses the
individual-class path.

Automatic selection runs once after input discovery, using Linux `MemAvailable`
(reclaimable-memory estimate on older kernels) or available physical pages where
supported. It takes the smaller of host availability and remaining cgroup v1/v2
memory, including ancestor limits; v2 `memory.high` is also respected. It reserves
25% of that availability plus 512 MiB, then divides the remainder by `--threads`.
The result is a conservative scheduling budget, not a JVM heap setting:

| Budget per worker | Selected maximum classes per batch |
| --- | --- |
| At least 1 GiB | 64 |
| At least 768 MiB | 32 |
| At least 512 MiB | 16 |
| At least 384 MiB | 8 |
| Less than 384 MiB | 1 |

When memory information is unavailable, the fallback is 16. Automatic sizing is
capped at the measured size of 64; explicit settings can reach 256. The export
summary on stderr includes `batch_size`, `batch_size_mode` (`auto`/`explicit`) and,
for automatic mode, `available_memory_bytes` (null when unknown). New manifest
records also retain the selected size for their run. Stdout remains per-entry
JSON Lines. Sampling does not monitor subsequent memory pressure or enforce an
RSS limit; complex classes can use more memory than the scheduling estimate.

Each CFR process defaults to a 512 MiB Java heap, plus native JVM and Python
inventory/input memory. Batch caches can increase peak RSS compared with isolated
class invocations; reduce `--threads` or `--batch-size` when memory is constrained.
`getclass` and `export` accept `--heap-mb` (64..8192, default 512). Large generated
classes may require a larger heap, for example `--heap-mb 2048`; budget this per
worker. The same setting applies to batch invocations and isolated retries.
Changing the heap limit invalidates managed decompilation results on resume.
Tasks are submitted through a bounded queue. `--timeout` limits each CFR invocation,
including a batch or isolated retry. Missing output/context failures are retried
once per affected class. A batch timeout records errors without starting N new
per-class timeout attempts; resume with a smaller batch or longer timeout.
Each completed class is committed to the manifest before the rest of its group
can fail; JSON progress is emitted when a scheduled group completes.

The batch strategy, automatic policy version and explicit `--batch-size` are part
of the configuration fingerprint. Upgrading from the original per-class exporter,
switching automatic/manual mode or changing an explicit size regenerates managed
outputs on resume. Changes in available memory within automatic mode affect
remaining jobs while preserving completed results that pass the input, backend,
configuration and output-hash checks. Unmanaged-file protections still apply.
`--limit` bounds class definitions, while resources are selected separately by
the input scope or `--no-resources`. Malformed inputs and timeouts produce error
records and exit status 1 while retaining successful work. Each returned Java
source is limited to 32 MiB. Source quality depends on CFR and class context;
batching is not a guarantee of compilable or semantically equivalent source.

See [batch export measurements](JVM-BATCH-PERFORMANCE.md) for paired timings,
source-hash checks and memory tradeoffs on bounded real JAR inputs.
For module responsibilities and contributor guidance, see
[JVM implementation structure](JVM-ARCHITECTURE.md).

## JSP and source/configuration inventory

```sh
python main.py listresources app.war --kind jsp --kind tld --json
python main.py listresources app.jar --kind service --json
python main.py analyze /path/to/jsp --kind jsp --sources-only --json
python main.py analyze app.war --kind jsp --json
```

JSP analysis reports imports/includes/taglibs/page encoding, `jsp:useBean`, dynamic
includes/forwards, Java scriptlet/declaration/expression line ranges, EL identifiers,
and JSP/taglib deployment metadata. It preserves source files during export; JSP
is already source and does not require bytecode decompilation. Legacy encodings
are read from page/XML declarations when available.

Compiled JSP servlet classes are recognized through `_jspService`, JSP SourceFile
metadata, or JSP entries in SourceDebugExtension/SMAP. The report lists available
source file mappings and service methods. CFR exports generated servlet Java;
original JSP text, formatting and template structure cannot reliably be recovered
from compiled code. The tool does not run Jasper/Resin compilers or execute JSPs.
Lexical source analysis is best-effort, not a complete JSP/Java parser.

## Framework and reflection evidence

```sh
python main.py analyze app.war --kind framework --java-release 8 --json -o frameworks.jsonl
python main.py analyze app.jar --kind reflection --prefix com.example --json
python main.py analyze /path/to/sources --sources-only --resource-prefix WEB-INF --json
```

Framework metadata includes servlet/filter/listener definitions and mappings,
Resin-compatible deployment descriptors, Jersey package scan settings, Spring XML
beans/factories/references/component scanning, XFire service interfaces and
implementations, JSP/TLD metadata, `META-INF/services` providers, and class-like
configuration keys. Compiled class/method/field annotations cover Spring, JAX-RS,
servlet annotations and javax/jakarta injection annotations. Annotation retention
in both RuntimeVisibleAnnotations and RuntimeInvisibleAnnotations is supported.
Java source files report recognized annotation names as syntax evidence.

Complete XML documents use namespace-aware tag handling. Partial files may yield
complete supported blocks with `coverage: fragment`; unsupported/malformed pieces
remain explicit errors. External DOCTYPE declarations are discarded without
retrieval; internal DTD/entity expansion is rejected. Arbitrary bean property
values and unrelated configuration values are not included in reports.

Reflection analysis covers standard Class/ClassLoader lookups, reflected member
lookup/invocation, ServiceLoader, Proxy and MethodHandles lookup sites. Bytecode
literal tracking follows strings/class literals and reference local assignments
within basic blocks. Known literals produce `binding: literal` and caller bytecode
offset/source line; branches, unhandled transfers and unknown values remain
`binding: dynamic`. This does not infer overload parameter arrays, string-building
flows, custom-loader semantics, global aliases or interprocedural call graphs.
Java/JSP source fallbacks identify call-like syntax with weaker confidence;
arbitrary `invoke/newInstance` method names can be unrelated to reflection.

`definition_links` connects referenced class names to inventoried origins and
labels them `present`, `ambiguous`, or `external-or-missing`. These links do not
prove runtime loading, framework activation, reachability or a security finding.
Analysis emits JSON Lines by default (`--json` is accepted for consistency).
`--prefix`, `--resource-prefix`, `--limit`, and `--sources-only` bound the scope.
Output files use `-o`; errors are retained in the stream and result in exit status
1. Create the output parent directory before using `-o`.

Format references: [JVMS class-file format](https://docs.oracle.com/javase/specs/jvms/se25/html/jvms-4.html),
[JVMS instructions](https://docs.oracle.com/javase/specs/jvms/se25/html/jvms-6.html),
[CFR option definitions](https://github.com/leibnitz27/cfr/blob/master/src/org/benf/cfr/reader/util/getopt/OptionsImpl.java).
