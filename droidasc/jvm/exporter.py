"""Bounded, resumable source export with per-origin output and JSONL evidence."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Lock

from .application import _safe_class_path, safe_relative_path, entry_container
from .decompiler import decompile, decompile_batch, BatchTimeout
from .memory import select_batch_size

MAX_BATCH_BYTES = 32 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def container_id(entry):
    if entry.member is not None:
        origin = entry_container(entry)
    elif hasattr(entry, "name") and entry.path.suffix.lower() == ".class":
        origin = entry_container(entry)
    else:
        origin = entry.origin[:-(len(entry.name))] if entry.origin.endswith(entry.name) else entry.origin
    return digest(origin.encode())[:20]


def checked_output(root, relative):
    path = root / safe_relative_path(relative)
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"Export destination contains a symlink: {parent}")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Export path leaves destination directory")
    return path


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".asc-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def rewrite_manifest(path, records):
    # Stream large resume histories instead of materializing another full copy.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".asc-", delete=False,
                                     mode="w", encoding="utf-8", newline="\n") as stream:
        temporary = Path(stream.name)
        try:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=True) + "\n")
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_sources(application, output, cfr_jar=None, java="java", timeout=120,
                   workers=2, prefix=None, resources=True, resume=False, limit=None,
                   batch_size=None, stats=None):
    if limit is not None and limit <= 0:
        raise ValueError("Export limit must be positive")
    selection = select_batch_size(workers, batch_size)
    # Memory pressure changes scheduling, not the validity of completed sources.
    batch_config = batch_size if batch_size is not None else "auto-memory-v1"
    batch_size = selection["batch_size"]
    if stats is not None:
        stats.update(selection)
    if timeout <= 0:
        raise ValueError("Decompiler timeout must be greater than zero")
    root = Path(output).expanduser().resolve()
    for input_path in application.paths:
        if input_path.is_dir() and root.is_relative_to(input_path):
            raise ValueError("Export directory must be outside input directories")
    manifest = root / "manifest.jsonl"
    if root.exists() and not root.is_dir():
        raise ValueError("Export destination must be a directory")
    if root.is_dir() and any(root.iterdir()) and not (resume and manifest.is_file()):
        raise ValueError("Export destination is not empty; use --resume with an ASC manifest")
    previous = {}
    if resume and manifest.is_file():
        with manifest.open("rb") as history:
            for raw in history:
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeError):
                    if history.read(1):
                        raise ValueError("Export manifest is malformed")
                    # An interrupted final append is not a completed output record.
                    break
                if not isinstance(record, dict) or not isinstance(record.get("origin"), str):
                    raise ValueError("Export manifest contains an invalid record")
                previous[record["origin"]] = record
        # Rewrite valid history so a truncated final line cannot corrupt a new append.
        rewrite_manifest(manifest, previous.values())
    root.mkdir(parents=True, exist_ok=True)
    config = {"java_release": application.java_release, "java": java, "timeout": timeout,
              "export_backend": "cfr-batch-v1", "batch_size": batch_config}
    # Dependency changes also invalidate a resumed decompilation result.
    inventory = sorted(str(path) for path in application._seen if path.is_file())
    config["inputs"] = [(path, Path(path).stat().st_size, Path(path).stat().st_mtime_ns)
                        for path in inventory if not Path(path).is_relative_to(Path(application._temp.name))]
    import os
    backend = cfr_jar or os.environ.get("ASC_CFR_JAR")
    if backend:
        config["backend"] = digest(Path(backend).expanduser().read_bytes())
    config_hash = digest(json.dumps(config, sort_keys=True).encode())

    log_lock = Lock()

    def commit(record):
        # Persist each completed class before the rest of its group can fail.
        with log_lock:
            log.write(json.dumps(record, ensure_ascii=True) + "\n")
            log.flush()

    def prepare(entry, is_resource, read):
        record = entry.record()
        record["config_hash"] = config_hash
        record["batch_size"] = batch_size
        try:
            data = application.read_resource(entry) if is_resource else read(entry)
            record["input_sha256"] = digest(data)
            relative = ("resources/" + container_id(entry) + "/" + entry.name if is_resource else
                        "sources/" + container_id(entry) + "/" + _safe_class_path(entry.name).with_suffix(".java").as_posix())
            record["output"] = relative
            target = checked_output(root, relative)
            old = previous.get(entry.origin)
            if (old and old.get("status") == "ok" and old.get("output") == relative
                    and old.get("input_sha256") == record["input_sha256"]
                    and old.get("config_hash") == config_hash and target.is_file()
                    and digest(target.read_bytes()) == old.get("output_sha256")):
                return {**old, "status": "ok", "resumed": True}, None, None
            if target.exists() and not (old and old.get("output") == relative):
                raise ValueError("Refusing to replace an output not owned by this manifest")
            return record, data, target
        except Exception as exc:
            record.update(status="error", error=str(exc))
            return record, None, None

    def task(entries, is_resource):
        records = []

        def publish(record):
            commit(record)
            records.append(record)

        def finish(item, result):
            entry, record, data, target = item
            try:
                if isinstance(result, Exception):
                    raise result
                data = result if is_resource else result.encode("utf-8")
                atomic_write(target, data)
                record.update(status="ok", output_sha256=digest(data), resumed=False)
            except Exception as exc:
                record.update(status="error", error=str(exc))
            publish(record)

        def individual(item):
            try:
                source = decompile(application, item[0], cfr_jar, java, timeout)
            except Exception as exc:
                source = exc
            finish(item, source)

        def flush(items):
            if len(items) == 1:
                individual(items[0])
                return
            pending = {item[0].origin: item for item in items}
            try:
                iterator = iter(decompile_batch(application, [item[0] for item in items],
                                               cfr_jar, java, timeout,
                                               class_data={item[0].origin: item[2] for item in items}))
            except BatchTimeout as exc:
                for item in items:
                    finish(item, exc)
                return
            except Exception:
                for item in items:
                    individual(item)
                return
            try:
                while pending:
                    try:
                        entry, source = next(iterator)
                    except BatchTimeout as exc:
                        # Do not turn one group timeout into N additional timeouts.
                        for item in pending.values():
                            finish(item, exc)
                        break
                    except Exception:
                        # A broken shared context/backend must not hide good classes.
                        for item in pending.values():
                            individual(item)
                        break
                    item = pending.pop(entry.origin)
                    if isinstance(source, Exception):
                        individual(item)
                    else:
                        finish(item, source)
            finally:
                getattr(iterator, "close", lambda: None)()

        prepared = []
        prepared_bytes = 0
        with application.class_reader() as read:
            for entry in entries:
                record, data, target = prepare(entry, is_resource, read)
                if target is None:
                    publish(record)
                elif is_resource:
                    finish((entry, record, data, target), data)
                else:
                    if prepared and prepared_bytes + len(data) > MAX_BATCH_BYTES:
                        flush(prepared)
                        prepared = []
                        prepared_bytes = 0
                    prepared.append((entry, record, data, target))
                    prepared_bytes += len(data)
        if prepared:
            flush(prepared)
        return records

    def jobs():
        count = 0
        group = []
        names = set()
        container = None
        for entry in application.classes(prefix):
            if limit is not None and count >= limit:
                break
            current = entry_container(entry)
            if group and (current != container or len(group) >= batch_size or entry.name in names):
                yield group, False
                group = []
                names = set()
            group.append(entry)
            names.add(entry.name)
            container = current
            count += 1
        if group:
            yield group, False
        if resources:
            # Source assets are copied exactly; deployment configs are analyzed,
            # not silently copied into source export directories.
            for entry in application.resources({"java", "jsp", "jspx", "jspf", "tag", "tagx"}):
                yield [entry], True

    with manifest.open("a", encoding="utf-8", newline="\n") as log:
        for error in application.errors:
            commit(error)
            yield error
        with ThreadPoolExecutor(max_workers=workers) as executor:
            iterator = iter(jobs())
            pending = set()
            exhausted = False
            while pending or not exhausted:
                while not exhausted and len(pending) < workers * 2:
                    try:
                        entries, is_resource = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    pending.add(executor.submit(task, entries, is_resource))
                if not pending:
                    break
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    yield from future.result()
