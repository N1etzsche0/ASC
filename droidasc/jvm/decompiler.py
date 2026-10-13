"""Configurable CFR subprocess adapter. No backend download during queries."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import warnings

from .application import _safe_class_path


def _backend(cfr_jar, java, timeout):
    if timeout <= 0:
        raise ValueError("Decompiler timeout must be greater than zero")
    cfr_jar = cfr_jar or os.environ.get("ASC_CFR_JAR")
    if not cfr_jar:
        raise ValueError("CFR backend is required: use --cfr-jar /path/to/cfr.jar or set ASC_CFR_JAR")
    cfr_jar = Path(cfr_jar).expanduser().resolve()
    if not cfr_jar.is_file():
        raise ValueError(f"CFR JAR does not exist: {cfr_jar}")
    executable = shutil.which(java)
    if executable is None:
        raise ValueError(f"Java executable not found: {java}")
    return executable, cfr_jar


def _source(path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f"CFR source exceeds 32 MiB: {path.name}")
    return path.read_text(encoding="utf-8")


def validate_heap_mb(heap_mb):
    if not isinstance(heap_mb, int) or not 64 <= heap_mb <= 8192:
        raise ValueError("CFR heap must be an integer from 64 to 8192 MiB")


def decompile(application, entry, cfr_jar=None, java="java", timeout=120, heap_mb=512):
    validate_heap_mb(heap_mb)
    executable, cfr_jar = _backend(cfr_jar, java, timeout)
    with tempfile.TemporaryDirectory(prefix="asc-cfr-") as temp:
        context = Path(temp) / "classes"
        output = Path(temp) / "source"
        truncated = application.stage_context(entry, context)
        if truncated:
            warnings.warn("Decompiler context reached 512 classes or 128 MiB; some types may be unresolved", RuntimeWarning)
        target = context / _safe_class_path(entry.name)
        command = [executable, f"-Xmx{heap_mb}m", "-jar", str(cfr_jar), str(target),
                   "--extraclasspath", str(context), "--outputpath", str(output),
                   "--outputencoding", "UTF-8", "--silent", "true"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"CFR decompilation exceeded {timeout} seconds") from exc
        source = output / _safe_class_path(entry.name).with_suffix(".java")
        if result.returncode != 0 or not source.is_file():
            details = (result.stderr or result.stdout).strip()[-4000:]
            raise ValueError(f"CFR failed to decompile {entry.origin}: {details or 'no source produced'}")
        return _source(source)


class BatchTimeout(ValueError):
    """A timed-out group must not automatically multiply its timeout per class."""


def decompile_batch(application, entries, cfr_jar=None, java="java", timeout=120,
                    class_data=None, heap_mb=512):
    """Run selected classes in one CFR JVM; yield per-origin source or failure.

    Only explicitly selected roots are passed as arguments. Dependency context
    is shared within the group, not emitted as additional export records.
    """
    validate_heap_mb(heap_mb)
    entries = list(entries)
    if not entries:
        return
    executable, cfr_jar = _backend(cfr_jar, java, timeout)
    with tempfile.TemporaryDirectory(prefix="asc-cfr-batch-") as temp:
        context = Path(temp) / "classes"
        output = Path(temp) / "source"
        if application.stage_contexts(entries, context, class_data=class_data):
            warnings.warn("Decompiler context reached 512 classes or 128 MiB; some types may be unresolved", RuntimeWarning)
        command = [executable, f"-Xmx{heap_mb}m", "-jar", str(cfr_jar),
                   *[str(context / _safe_class_path(entry.name)) for entry in entries],
                   "--extraclasspath", str(context), "--outputpath", str(output),
                   "--outputencoding", "UTF-8", "--silent", "true",
                   "--skipbatchinnerclasses", "false"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise BatchTimeout(f"CFR batch of {len(entries)} classes exceeded {timeout} seconds; "
                               "retry with a smaller --batch-size or larger --timeout") from exc
        details = (result.stderr or result.stdout).strip()[-4000:]
        for entry in entries:
            source = output / _safe_class_path(entry.name).with_suffix(".java")
            if result.returncode != 0 or not source.is_file():
                yield entry, ValueError(f"CFR batch did not produce {entry.origin}: {details or 'no source produced'}")
            else:
                try:
                    yield entry, _source(source)
                except (ValueError, OSError, UnicodeError) as exc:
                    yield entry, exc
