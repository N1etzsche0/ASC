import argparse
import os
import subprocess
import sys
import time

t_start = time.perf_counter()


def _format_class_name(name : str) -> str:
    if not name:
        raise ValueError("Class name cannot be empty")
    if name.startswith("L") and name.endswith(";") and "/" in name:
        return name
    name = name.replace(".", "/")
    if not name.startswith("L"):
        name = f"L{name}"
    if not name.endswith(";"):
        name = f"{name};"
    return name


def _normalize_class_query(name : str, fuzzy : bool) -> str:
    if name is None:
        return None
    if fuzzy:
        if "." in name and "/" not in name:
            return name.replace(".", "/")
        return name
    return _format_class_name(name)


def _build_member_find(key : str, clz : str, clz_fuzzy : bool, name : str) -> dict:
    if clz == "":
        clz = None
    if name == "":
        name = None
    if clz is None and name is None:
        raise ValueError(f"{key} query needs at least one of class or {key} name")
    if clz is None:
        return {key: {"class": None, key: name}}
    clz = _normalize_class_query(clz, clz_fuzzy)
    return {key: {"class": [clz, not clz_fuzzy], key: name}}


def _get_find_query(args) -> tuple:
    if args.find_type == "string":
        return "string", {"string": args.value}
    if args.find_type == "type":
        return "type", {"type": args.value}
    if args.find_type == "method":
        return "method", _build_member_find("method", args.class_name, args.fuzzy_class, args.name)
    return "field", _build_member_find("field", args.class_name, args.fuzzy_class, args.name)


def _is_jvm_input(path):
    # Keep the Android startup path free of JVM parser/backend imports.
    if not os.path.isdir(path) and os.path.splitext(path)[1].lower() not in {
        ".class", ".jar", ".war", ".ear", ".zip",
    }:
        return False
    from droidasc.jvm.application import is_jvm_input
    return is_jvm_input(path)


def _handle_jvm(args):
    if not _is_jvm_input(args.apk_path):
        if any(getattr(args, option, None) for option in (
            "json", "origins", "origin", "classpath", "cfr_jar",
        )):
            raise ValueError("JVM output/backend options require a class, JAR, WAR, EAR, ZIP or directory input")
        return False
    import json
    from droidasc.jvm.application import JvmApplication
    if args.threads <= 0:
        raise ValueError("Worker count must be greater than zero")
    paths = [args.apk_path] + args.classpath
    with JvmApplication(paths, java_release=args.java_release) as application:
        if args.command == "getclass":
            from droidasc.jvm.decompiler import decompile

            entry = application.select(args.dalvik_class, args.origin)
            source = decompile(application, entry, args.cfr_jar, args.java, args.timeout)
            text = (json.dumps({**entry.record(), "source": source}, ensure_ascii=True)
                    if args.json else source)
            if args.output:
                with open(args.output, "w", encoding="utf-8", newline="\n") as out:
                    out.write(text.rstrip("\n") + "\n")
            print(text.rstrip("\n"))
        else:
            output_fp = open(args.output, "w", encoding="utf-8", newline="\n") if args.output else None
            try:
                if args.command == "listclass":
                    records = (entry.record() for entry in application.classes(args.prefix))
                else:
                    value = args.value if args.find_type in ("string", "type") else args.name
                    records = application.findrefs(
                        args.find_type, value, getattr(args, "class_name", None),
                        getattr(args, "fuzzy_class", False), args.prefix,
                    )
                for record in records:
                    if args.json:
                        text = json.dumps(record, ensure_ascii=True)
                    elif args.command == "listclass":
                        text = record["class"]
                        if args.origins:
                            text += "\t" + record["origin"]
                    else:
                        target = json.dumps(record["target"], ensure_ascii=True)
                        text = (f'{record["origin"]}\t{record["class"]}->{record["method"]}'
                                f'{record["descriptor"]} @{record["offset"]:04x}\t'
                                f'{record["kind"]}: {target}')
                    if output_fp is not None:
                        output_fp.write(text + "\n")
                    if output_fp is None or args.command == "findrefs":
                        print(text)
            finally:
                if output_fp is not None:
                    output_fp.close()
    if args.debug:
        print(f"[DEBUG] JVM execution time: {time.perf_counter() - t_start:.3f}s", file=sys.stderr)
    return True


def _add_jvm_options(parser):
    parser.add_argument("--classpath", action="append", default=[], metavar="PATH",
                        help="JVM: add another archive or class directory (repeatable).")
    parser.add_argument("--java-release", type=int, default=17,
                        help="JVM: select multi-release JAR variants for this Java version (default: 17).")
    parser.add_argument("--json", action="store_true", help="JVM: emit JSON Lines for Agent/script use.")


def _handle_application(args):
    import json
    from droidasc.jvm.application import JvmApplication

    failed = False
    export_stats = {}
    with JvmApplication([args.input_path] + args.classpath, java_release=args.java_release,
                        tolerant=args.command != "listresources", include_resources=True) as application:
        if args.command == "export":
            from droidasc.jvm.exporter import export_sources
            records = export_sources(application, args.output, args.cfr_jar, args.java, args.timeout,
                                     args.threads, args.prefix, not args.no_resources, args.resume, args.limit,
                                     batch_size=args.batch_size, stats=export_stats)
            out = sys.stdout
            output_fp = None
        elif args.command == "analyze":
            from droidasc.jvm.analyzer import analyze
            records = analyze(application, args.kind, args.prefix, args.resource_prefix, args.limit, args.sources_only)
            output_fp = open(args.output, "w", encoding="utf-8", newline="\n") if args.output else None
            out = output_fp or sys.stdout
        else:
            records = (entry.record() for entry in application.resources(set(args.kind) if args.kind else None))
            output_fp = open(args.output, "w", encoding="utf-8", newline="\n") if args.output else None
            out = output_fp or sys.stdout
        counts = {}
        try:
            for record in records:
                status = record.get("status", record.get("kind", "resource"))
                counts[status] = counts.get(status, 0) + 1
                failed |= status in {"error", "analysis-error"}
                text = json.dumps(record, ensure_ascii=True)
                out.write(text + "\n")
        finally:
            if output_fp is not None:
                output_fp.close()
    if args.debug or args.command == "export":
        print(json.dumps({"summary": counts, "seconds": round(time.perf_counter() - t_start, 3),
                          **export_stats}), file=sys.stderr)
    if failed:
        # Partial work is kept with explicit failed records; success must not hide gaps.
        sys.exit(1)


def _handle_getclass(args):
    if _handle_jvm(args):
        return
    from droidasc.asc_client.apk_handler import ApkHandler

    dalvik_class = _format_class_name(args.dalvik_class)
    apk_handler = ApkHandler(args.apk_path, debug=args.debug, max_workers=args.threads)
    hit = apk_handler.get_class_dex(dalvik_class)
    if hit is None:
        raise ValueError(f"Class {dalvik_class} not found in APK.")

    dex_name, dex_buf = hit
    if args.debug:
        t_hit_end = time.perf_counter()

    from droidasc.asc_client.asc_handler import AscHandler
    source_code = AscHandler(args.debug).getclass(dex_buf, dalvik_class)
    t_end = time.perf_counter()

    if args.debug:
        print(f"[DEBUG] Hit DEX: {dex_name}")
        print(f"[DEBUG] APK Scan Time: {(t_hit_end - t_start) * 1000000:.2f} us")
        print(f"[DEBUG] Total Execution Time: {(t_end - t_start) * 1000000:.2f} us")
        print("-" * 50)

    if args.output:
        with open(args.output, "w", encoding="utf-8", errors="replace", newline="\n") as fp:
            fp.write(source_code)
            if not source_code.endswith("\n"):
                fp.write("\n")
    print(source_code)


def _handle_listclass(args):
    if _handle_jvm(args):
        return
    from droidasc.asc_client.apk_handler import ApkHandler

    names = ApkHandler(args.apk_path, debug=args.debug, max_workers=args.threads).list_classes(
        args.prefix
    )

    output_fp = (
        open(args.output, "w", encoding="utf-8", errors="replace", newline="\n")
        if args.output
        else None
    )
    out = output_fp if output_fp is not None else sys.stdout
    try:
        for start in range(0, len(names), 8192):
            text = "\n".join(names[start:start + 8192]) + "\n"
            out.write(text)
    finally:
        if output_fp is not None:
            output_fp.close()

    if args.debug:
        t_end = time.perf_counter()
        print(f"[DEBUG] Total Execution Time: {(t_end - t_start) * 1000000:.2f} us")


def _handle_getmanifest(args):
    from droidasc.asc_client.manifest_handler import get_manifest_xml

    xml = get_manifest_xml(args.apk_path, pretty=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8", errors="replace", newline="\n") as fp:
            fp.write(xml)
            if not xml.endswith("\n"):
                fp.write("\n")
    print(xml)

    if args.debug:
        t_end = time.perf_counter()
        print(f"[DEBUG] Total Execution Time: {(t_end - t_start) * 1000000:.2f} us")


def _handle_findrefs(args):
    if _handle_jvm(args):
        return
    from droidasc.asc_client.apk_handler import ApkHandler

    apk_handler = ApkHandler(args.apk_path, debug=args.debug, max_workers=args.threads)
    find_type, find = _get_find_query(args)

    output_fp = None
    if args.output:
        output_fp = open(args.output, "w", encoding="utf-8", errors="replace", newline="\n")

    out = sys.stdout
    for _dex_name, lines in apk_handler.for_each_findrefs(find_type, find):
        if not lines:
            continue
        text = "\n".join(lines) + "\n"
        out.write(text)
        if output_fp is not None:
            output_fp.write(text)

    if output_fp is not None:
        output_fp.close()

    if args.debug:
        t_end = time.perf_counter()
        print(f"[DEBUG] Total Execution Time: {(t_end - t_start) * 1000000:.2f} us")


def _run_gui(argv):
    parser = argparse.ArgumentParser(
        description="ASC GUI entry.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc app.apk --gui
  droidasc app.apk --gui --debug
  droidasc app.apk --gui --threads 16
""",
    )
    parser.add_argument("apk_path", help="Path to the input APK file.")
    parser.add_argument("--gui", action="store_true", help="Launch GUI.")
    parser.add_argument("--threads", "--thread", type=int, default=8, help="Worker count.")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode.")
    parser.add_argument("--gui-foreground", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if _is_jvm_input(args.apk_path):
        parser.error("JVM inputs currently support CLI commands: listclass, getclass and findrefs")

    if not args.debug and not args.gui_foreground:
        cmd = [
            sys.executable,
            "-m", "droidasc",
            args.apk_path,
            "--gui",
            "--gui-foreground",
            "--threads",
            str(args.threads),
        ]
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            start_new_session = True
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        return

    from droidasc.asc_client.gui.app import launch_gui
    launch_gui(args.apk_path, max_workers=args.threads, debug=args.debug)


def main():
    if len(sys.argv) == 1:
        _build_main_parser().print_help()
        return
    if "--gui" in sys.argv[1:]:
        _run_gui(sys.argv[1:])
        return

    parser = _build_main_parser()
    args = parser.parse_args()

    try:
        if args.command == "getclass":
            _handle_getclass(args)
        elif args.command == "listclass":
            _handle_listclass(args)
        elif args.command == "getmanifest":
            _handle_getmanifest(args)
        elif args.command == "findrefs":
            _handle_findrefs(args)
        elif args.command in {"export", "analyze", "listresources"}:
            _handle_application(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)


def _build_main_parser():
    parser = argparse.ArgumentParser(
        description="ASC on-demand Android and JVM decompilation/search.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc app.apk --gui
  droidasc getclass app.apk Lcom/poc/Main; -o Main.java
  droidasc getclass app.apk com.poc.Main --threads 16
  droidasc listclass app.apk -o classes.txt
  droidasc listclass app.apk --prefix com.poc
  droidasc getmanifest app.apk -o AndroidManifest.xml
  droidasc findrefs app.apk string token -o string_refs.txt
  droidasc findrefs app.apk type com.poc.Main
  droidasc findrefs app.apk method onCreate --class com.poc.Main
  droidasc findrefs app.apk method notify --class MainActivity --fuzzy-class -o method_refs.txt
  droidasc findrefs app.apk field apiKey -o field_refs.txt
  droidasc listclass application.war --origins --json
  droidasc listclass /path/to/webapp --prefix com.example
  droidasc getclass application.war com.example.Controller --cfr-jar /path/to/cfr.jar
  droidasc findrefs --json /path/to/webapp method execute --class java.sql.Statement
  droidasc export app.war -o /path/to/export --threads 2 --resume
  droidasc analyze /path/to/webapp --kind framework --json
  droidasc analyze /path/to/jsp --kind jsp --sources-only --json
""",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    getclass_parser = subparsers.add_parser(
        "getclass",
        help="Decompile a selected class from Android or JVM inputs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc getclass app.apk Lcom/poc/Main;
  droidasc getclass app.apk com.poc.Main -o Main.java
  droidasc getclass app.apk com.poc.Main --threads 16 --debug
""",
    )
    getclass_parser.add_argument("--debug", action="store_true", help="Enable debug profiling output.")
    getclass_parser.add_argument("--threads", "--thread", type=int, default=8, help="Worker thread count.")
    getclass_parser.add_argument("-o", "--output", help="Also write decompiled output to this file.")
    getclass_parser.add_argument("apk_path", help="Input APK/DEX, class, JAR/WAR/EAR/ZIP or application directory.")
    getclass_parser.add_argument("dalvik_class", help="Class name, e.g. com.poc.Main or Lcom/poc/Main;.")
    _add_jvm_options(getclass_parser)
    getclass_parser.add_argument("--origin", help="JVM: exact origin from listclass --origins; resolves duplicate classes.")
    getclass_parser.add_argument("--cfr-jar", help="JVM: path to CFR JAR (or set ASC_CFR_JAR).")
    getclass_parser.add_argument("--java", default="java", help="JVM: Java executable (default: java).")
    getclass_parser.add_argument("--timeout", type=float, default=120, help="JVM: decompiler timeout in seconds.")

    listclass_parser = subparsers.add_parser(
        "listclass",
        help="List Android/JVM classes; use --prefix to filter by package/class prefix.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc listclass app.apk
  droidasc listclass app.apk --prefix com.poc
  droidasc listclass app.apk -o classes.txt
  droidasc listclass app.apk --threads 16 --debug
""",
    )
    listclass_parser.add_argument("--debug", action="store_true", help="Enable debug profiling output.")
    listclass_parser.add_argument("--threads", "--thread", type=int, default=8, help="Worker thread count.")
    listclass_parser.add_argument(
        "--prefix",
        help="Only list classes with this package/class prefix (e.g., com.poc or Lcom/poc).",
    )
    listclass_parser.add_argument("-o", "--output", help="Write class names to this file instead of stdout.")
    listclass_parser.add_argument("apk_path", help="Input APK/DEX, class, JAR/WAR/EAR/ZIP or application directory.")
    _add_jvm_options(listclass_parser)
    listclass_parser.add_argument("--origins", action="store_true", help="JVM: include the archive/file origin of each class.")

    getmanifest_parser = subparsers.add_parser(
        "getmanifest",
        help="Decode AndroidManifest.xml from APK and print it as XML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc getmanifest app.apk
  droidasc getmanifest app.apk -o AndroidManifest.xml
""",
    )
    getmanifest_parser.add_argument("--debug", action="store_true", help="Enable debug profiling output.")
    getmanifest_parser.add_argument("-o", "--output", help="Also write decoded manifest to this file.")
    getmanifest_parser.add_argument("apk_path", help="Path to the input APK file.")

    findrefs_parser = subparsers.add_parser(
        "findrefs",
        help="Find bytecode references for string/type/method/field across Android/JVM inputs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc findrefs app.apk string token -o string_refs.txt
  droidasc findrefs app.apk type com.poc.Main
  droidasc findrefs app.apk method onCreate --class com.poc.Main
  droidasc findrefs app.apk method notify --class openclaw --fuzzy-class -o method_refs.txt
  droidasc findrefs app.apk field changeQuickRedirect -o field_refs.txt
""",
    )
    findrefs_parser.add_argument("--debug", action="store_true", help="Enable debug profiling output.")
    findrefs_parser.add_argument("--threads", "--thread", type=int, default=8, help="Worker thread count.")
    findrefs_parser.add_argument("apk_path", help="Input APK/DEX, class, JAR/WAR/EAR/ZIP or application directory.")
    _add_jvm_options(findrefs_parser)
    findrefs_parser.add_argument("--prefix", help="JVM: limit the scan to calling classes with this package/class prefix.")
    find_subparsers = findrefs_parser.add_subparsers(dest="find_type", required=True)

    string_parser = find_subparsers.add_parser(
        "string",
        help="Find references to a fuzzy string.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc findrefs app.apk string token
  droidasc findrefs app.apk string Authorization -o string_refs.txt
""",
    )
    string_parser.add_argument("-o", "--output", help="Also write reference search output to this file.")
    string_parser.add_argument("value", help="Fuzzy string pattern.")

    type_parser = find_subparsers.add_parser(
        "type",
        help="Find references to a fuzzy type descriptor/name.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc findrefs app.apk type com.poc.Main
  droidasc findrefs app.apk type Lcom/poc/Main; -o type_refs.txt
""",
    )
    type_parser.add_argument("-o", "--output", help="Also write reference search output to this file.")
    type_parser.add_argument("value", help="Fuzzy type pattern.")

    method_parser = find_subparsers.add_parser(
        "method",
        help="Find references to methods.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc findrefs app.apk method onCreate
  droidasc findrefs app.apk method notify --class com.poc.Main
  droidasc findrefs app.apk method notify --class poc --fuzzy-class -o method_refs.txt
""",
    )
    method_parser.add_argument("-o", "--output", help="Also write reference search output to this file.")
    method_parser.add_argument("name", nargs="?", default=None, help="Fuzzy method name.")
    method_parser.add_argument("--class", dest="class_name", default=None, help="Dalvik class or fuzzy class pattern.")
    method_parser.add_argument("--fuzzy-class", action="store_true", help="Treat --class as fuzzy match.")

    field_parser = find_subparsers.add_parser(
        "field",
        help="Find references to fields.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  droidasc findrefs app.apk field changeQuickRedirect
  droidasc findrefs app.apk field token --class com.poc.Main
  droidasc findrefs app.apk field token --class poc --fuzzy-class -o field_refs.txt
""",
    )
    field_parser.add_argument("-o", "--output", help="Also write reference search output to this file.")
    field_parser.add_argument("name", nargs="?", default=None, help="Fuzzy field name.")
    field_parser.add_argument("--class", dest="class_name", default=None, help="Dalvik class or fuzzy class pattern.")
    field_parser.add_argument("--fuzzy-class", action="store_true", help="Treat --class as fuzzy match.")

    export_parser = subparsers.add_parser("export", help="Bulk JVM source export with per-origin paths and a resumable manifest.")
    export_parser.add_argument("input_path", help="Class/archive/application or source directory.")
    export_parser.add_argument("-o", "--output", required=True, help="Output directory outside the input tree.")
    _add_jvm_options(export_parser)
    export_parser.add_argument("--threads", type=int, default=2, help="Concurrent CFR processes (default: 2).")
    export_parser.add_argument("--batch-size", type=int, default=None,
                               help="Classes per source-container batch, 1..256 (default: auto from available memory/threads, max 64; 1 disables batching).")
    export_parser.add_argument("--cfr-jar", help="Path to CFR JAR (or ASC_CFR_JAR).")
    export_parser.add_argument("--java", default="java", help="Java executable.")
    export_parser.add_argument("--timeout", type=float, default=120, help="CFR timeout per batch or isolated retry in seconds.")
    export_parser.add_argument("--prefix", help="Filter class names by prefix.")
    export_parser.add_argument("--limit", type=int, help="Export at most this many class definitions.")
    export_parser.add_argument("--no-resources", action="store_true", help="Do not copy Java/JSP/tag source assets.")
    export_parser.add_argument("--resume", action="store_true", help="Resume using hashes and the existing ASC manifest.")
    export_parser.add_argument("--debug", action="store_true")

    analyze_parser = subparsers.add_parser("analyze", help="Static JSP, framework metadata and reflection evidence (JSON Lines).")
    analyze_parser.add_argument("input_path", help="Class/archive/application or source input.")
    _add_jvm_options(analyze_parser)
    analyze_parser.add_argument("--kind", choices=("all", "jsp", "framework", "reflection"), default="all")
    analyze_parser.add_argument("--prefix", help="Filter compiled calling classes by prefix.")
    analyze_parser.add_argument("--resource-prefix", help="Filter source/configuration paths by prefix.")
    analyze_parser.add_argument("--limit", type=int, help="Analyze at most this many compiled classes.")
    analyze_parser.add_argument("--sources-only", action="store_true", help="Analyze source/configuration without scanning bytecode.")
    analyze_parser.add_argument("-o", "--output", help="Write JSON Lines to a file instead of stdout.")
    analyze_parser.add_argument("--debug", action="store_true")

    resources_parser = subparsers.add_parser("listresources", help="List Java/JSP/tag/configuration assets with archive origins (JSON Lines).")
    resources_parser.add_argument("input_path", help="Archive/application or source input.")
    _add_jvm_options(resources_parser)
    resources_parser.add_argument("--kind", action="append", choices=("java", "jsp", "jspx", "jspf", "tag", "tagx", "xml", "tld", "properties", "service"))
    resources_parser.add_argument("-o", "--output", help="Write JSON Lines to this file.")
    resources_parser.add_argument("--debug", action="store_true")
    return parser


if __name__ == "__main__":
    main()
