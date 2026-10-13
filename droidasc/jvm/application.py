"""Archive-directory inventory with lazy class reads and bounded nested spooling."""
from dataclasses import dataclass
from collections import deque, OrderedDict
from contextlib import contextmanager
from itertools import groupby
from pathlib import Path
import tempfile
import zipfile

from .classfile import ClassFile, internal_name


ARCHIVES = {".jar", ".war", ".ear", ".zip"}
MAX_CLASS_BYTES = 32 * 1024 * 1024
MAX_NESTED_BYTES = 512 * 1024 * 1024
MAX_SPOOL_BYTES = 2 * 1024 * 1024 * 1024
RESOURCE_SUFFIXES = {".java", ".jsp", ".jspx", ".jspf", ".tag", ".tagx", ".xml", ".tld", ".properties"}
MAX_RESOURCE_BYTES = 16 * 1024 * 1024


def is_resource_name(name):
    return Path(name).suffix.lower() in RESOURCE_SUFFIXES or "/META-INF/services/" in "/" + name


def resource_kind(name):
    return "service" if "/META-INF/services/" in "/" + name else Path(name).suffix.lower().lstrip(".")


def is_jvm_input(path):
    path = Path(path)
    if path.is_dir() or path.suffix.lower() in ARCHIVES | {".class"}:
        # .zip may also be an Android bundle: keep the existing DEX route.
        if path.suffix.lower() == ".zip" and path.is_file():
            with zipfile.ZipFile(path) as archive:
                return not any(info.filename.endswith((".dex", ".apk")) for info in archive.infolist())
        return True
    return False


@dataclass(frozen=True)
class ClassEntry:
    name: str
    origin: str
    path: Path
    member: str | None = None
    release: int = 0

    def record(self):
        return {"class": f"L{self.name};", "origin": self.origin, "release": self.release}


@dataclass(frozen=True)
class ResourceEntry:
    name: str
    origin: str
    path: Path
    member: str | None = None

    def record(self):
        return {"resource": self.name, "origin": self.origin, "kind": resource_kind(self.name)}


def safe_relative_path(name):
    parts = name.split("/")
    if any(part in ("", ".", "..") or any(c in part for c in ("\\", ":", "\0")) for part in parts):
        raise ValueError(f"Unsafe resource path: {name!r}")
    return Path(*parts)


def entry_container(entry):
    if entry.member is not None:
        return entry.origin[:-(len(entry.member) + 2)]
    suffix = entry.name + ".class"
    path = entry.path.as_posix()
    return path[:-len(suffix)] if path.endswith(suffix) else entry.origin


def _safe_class_path(name):
    parts = name.split("/")
    if any(part in ("", ".", "..") or any(c in part for c in ("\\", ":", "\0")) for part in parts):
        raise ValueError(f"Unsafe class name: {name!r}")
    return Path(*parts).with_name(parts[-1] + ".class")


def _read_class(path, archive=None, member=None):
    if archive is None:
        with path.open("rb") as stream:
            data = stream.read(MAX_CLASS_BYTES + 1)
    else:
        info = archive.getinfo(member)
        if info.file_size > MAX_CLASS_BYTES:
            raise ValueError(f"Class exceeds {MAX_CLASS_BYTES} bytes: {member}")
        with archive.open(info) as stream:
            data = stream.read(MAX_CLASS_BYTES + 1)
    if len(data) > MAX_CLASS_BYTES:
        raise ValueError(f"Class exceeds {MAX_CLASS_BYTES} bytes: {member or path}")
    return data


class JvmApplication:
    def __init__(self, paths, java_release=17, tolerant=False, include_resources=False):
        if java_release < 8:
            raise ValueError("Java release must be at least 8")
        if isinstance(paths, (str, Path)):
            paths = [paths]
        self.paths = [Path(path).expanduser().resolve() for path in paths]
        self.java_release = java_release
        self.entries = []
        self.resource_entries = []
        self.errors = []
        self.tolerant = tolerant
        self.include_resources = include_resources
        self._temp = tempfile.TemporaryDirectory(prefix="asc-jvm-")
        self._spool_bytes = 0
        self._spool_count = 0
        self._seen = set()
        try:
            for path in self.paths:
                self._discover_checked(path)
            self.by_name = {}
            for entry in self.entries:
                self.by_name.setdefault(entry.name, []).append(entry)
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._temp.cleanup()

    def _discover_checked(self, path, resource_name=None):
        try:
            self._discover(path, resource_name)
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
            if not self.tolerant:
                raise
            self.errors.append({"origin": str(path), "error": str(exc), "status": "error"})

    def _discover(self, path, resource_name=None):
        if not path.exists():
            raise ValueError(f"Input does not exist: {path}")
        if path in self._seen:
            return
        self._seen.add(path)
        if path.is_dir():
            # No symlink traversal; stable output for Agent callers.
            for child in sorted(path.rglob("*")):
                relative = child.relative_to(path).as_posix()
                relevant = child.suffix.lower() in ARCHIVES | {".class"} or (self.include_resources and is_resource_name(relative))
                if child.is_file() and not child.is_symlink() and relevant:
                    self._discover_checked(child.resolve(), child.relative_to(path).as_posix())
        elif path.suffix.lower() == ".class":
            parsed = ClassFile(_read_class(path))
            self.entries.append(ClassEntry(parsed.name, str(path), path))
        elif path.suffix.lower() in ARCHIVES:
            self._archive(path, str(path), depth=0)
        elif self.include_resources and is_resource_name(resource_name or path.as_posix()):
            name = resource_name or path.name
            safe_relative_path(name)
            self.resource_entries.append(ResourceEntry(name, str(path), path))
        else:
            raise ValueError(f"Unsupported JVM input: {path}")

    def _archive(self, path, origin, depth):
        if depth > 8:
            raise ValueError(f"Nested archive depth exceeds 8: {origin}")
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            relevant = [i for i in infos if not i.is_dir() and
                        (i.filename.endswith(".class") or Path(i.filename).suffix.lower() in ARCHIVES
                         or (self.include_resources and is_resource_name(i.filename)))]
            names = [i.filename for i in relevant]
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate archive entry names: {origin}")
            multi_release = False
            if "META-INF/MANIFEST.MF" in archive.namelist():
                with archive.open("META-INF/MANIFEST.MF") as stream:
                    manifest = stream.read(1024 * 1024 + 1)
                if len(manifest) > 1024 * 1024:
                    raise ValueError(f"Manifest exceeds 1 MiB: {origin}")
                manifest = manifest.replace(b"\r\n", b"\n").replace(b"\n ", b"")
                main_section = manifest.split(b"\n\n", 1)[0]
                multi_release = any(line.lower().strip() == b"multi-release: true"
                                    for line in main_section.splitlines())
            selected = {}
            nested = []
            for info in relevant:
                name = info.filename
                if self.include_resources and is_resource_name(name):
                    safe_relative_path(name)
                    self.resource_entries.append(ResourceEntry(name, origin + "!/" + name, path, name))
                    continue
                if not name.endswith(".class"):
                    nested.append(info)
                    continue
                release = 0
                if name.startswith("META-INF/versions/"):
                    parts = name.split("/", 3)
                    if not multi_release or len(parts) != 4 or not parts[2].isdigit():
                        continue
                    release = int(parts[2])
                    if release < 9 or release > self.java_release:
                        continue
                    name = parts[3]
                elif name.startswith("META-INF/"):
                    continue
                for prefix in ("WEB-INF/classes/", "BOOT-INF/classes/"):
                    if name.startswith(prefix):
                        name = name[len(prefix):]
                        break
                name = name[:-6]
                _safe_class_path(name)
                entry = ClassEntry(name, origin + "!/" + info.filename, path, info.filename, release)
                previous = selected.get(name)
                if previous is None or release > previous.release:
                    selected[name] = entry
                elif release == previous.release:
                    raise ValueError(f"Duplicate class paths: {previous.origin}, {entry.origin}")
            self.entries.extend(selected.values())
            for info in nested:
                if info.file_size > MAX_NESTED_BYTES or self._spool_bytes + info.file_size > MAX_SPOOL_BYTES:
                    raise ValueError(f"Nested archive spool limit exceeded: {origin}!/{info.filename}")
                self._spool_count += 1
                destination = Path(self._temp.name) / f"archive-{self._spool_count}.zip"
                written = 0
                with archive.open(info) as src, destination.open("wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_NESTED_BYTES or self._spool_bytes + written > MAX_SPOOL_BYTES:
                            raise ValueError("Nested archive spool limit exceeded")
                        out.write(chunk)
                self._spool_bytes += written
                self._archive(destination, origin + "!/" + info.filename, depth + 1)

    def classes(self, prefix=None):
        if prefix is not None:
            if not prefix.strip():
                raise ValueError("Class prefix cannot be empty")
            prefix = internal_name(prefix)
        for entry in self.entries:
            if prefix is None or entry.name.startswith(prefix):
                yield entry

    def select(self, name, origin=None):
        candidates = self.by_name.get(internal_name(name), [])
        if origin is not None:
            candidates = [entry for entry in candidates if entry.origin == origin]
        if not candidates:
            raise ValueError(f"Class {name} not found" + (f" at {origin}" if origin else ""))
        if len(candidates) > 1:
            origins = "\n".join(entry.origin for entry in candidates)
            raise ValueError(f"Ambiguous class {name}; use --origin with a value from listclass --origins:\n{origins}")
        return candidates[0]

    def read(self, entry):
        if entry.member is None:
            return _read_class(entry.path)
        with zipfile.ZipFile(entry.path) as archive:
            return _read_class(entry.path, archive, entry.member)

    @contextmanager
    def class_reader(self):
        """Reuse a bounded set of ZIP directories within one worker/context."""
        archives = OrderedDict()

        def read(entry):
            if entry.member is None:
                return _read_class(entry.path)
            archive = archives.pop(entry.path, None)
            if archive is None:
                if len(archives) >= 8:
                    archives.popitem(last=False)[1].close()
                archive = zipfile.ZipFile(entry.path)
            archives[entry.path] = archive
            return _read_class(entry.path, archive, entry.member)

        try:
            yield read
        finally:
            for archive in archives.values():
                archive.close()

    def resources(self, kinds=None):
        for entry in self.resource_entries:
            if kinds is None or resource_kind(entry.name) in kinds:
                yield entry

    def read_resource(self, entry):
        if entry.member is None:
            with entry.path.open("rb") as stream:
                data = stream.read(MAX_RESOURCE_BYTES + 1)
        else:
            with zipfile.ZipFile(entry.path) as archive:
                if archive.getinfo(entry.member).file_size > MAX_RESOURCE_BYTES:
                    raise ValueError(f"Resource exceeds 16 MiB: {entry.origin}")
                with archive.open(entry.member) as stream:
                    data = stream.read(MAX_RESOURCE_BYTES + 1)
        if len(data) > MAX_RESOURCE_BYTES:
            raise ValueError(f"Resource exceeds 16 MiB: {entry.origin}")
        return data

    def parsed_classes(self, prefix=None):
        for path, group in groupby(self.classes(prefix), key=lambda entry: entry.path):
            group = list(group)
            if group[0].member is None:
                for entry in group:
                    yield entry, ClassFile(self.read(entry))
            else:
                with zipfile.ZipFile(path) as archive:
                    for entry in group:
                        try:
                            parsed = ClassFile(_read_class(path, archive, entry.member))
                            if parsed.name != entry.name:
                                raise ValueError(f"Entry name {entry.name} differs from class name {parsed.name}")
                            yield entry, parsed
                        except ValueError as exc:
                            raise ValueError(f"{entry.origin}: {exc}") from exc

    def findrefs(self, kind, value=None, owner=None, fuzzy_owner=False, prefix=None):
        if kind not in {"string", "type", "method", "field"}:
            raise ValueError(f"Unknown reference kind: {kind}")
        if kind in {"method", "field"} and not (value or owner):
            raise ValueError(f"{kind} query needs at least one of class or {kind} name")
        if kind in {"string", "type"} and value is None:
            raise ValueError(f"{kind} query needs a value")
        if owner:
            owner = internal_name(owner)
        if kind == "type":
            value = internal_name(value)
        for entry, parsed in self.parsed_classes(prefix):
            try:
                for method, offset, ref in parsed.references():
                    if ref.kind != kind:
                        continue
                    if kind == "string":
                        matched = value in ref.target
                    elif kind == "type":
                        matched = value in ref.owner
                    else:
                        matched = (not value or value in ref.name) and (
                            not owner or (owner in ref.owner if fuzzy_owner else owner == ref.owner))
                    if matched:
                        yield {"origin": entry.origin, "class": f"L{parsed.name};",
                               "method": method.name, "descriptor": method.descriptor,
                               "offset": offset, "kind": kind, "target": ref.target}
            except ValueError as exc:
                raise ValueError(f"{entry.origin}: {exc}") from exc

    def stage_context(self, entry, destination, max_classes=512):
        """Stage a bounded dependency closure, favouring the selected class container.

        The context lets CFR resolve related classes without expanding every JAR.
        Ambiguous dependencies outside that container are omitted.
        """
        return self.stage_contexts([entry], destination, max_classes)

    def stage_contexts(self, entries, destination, max_classes=512, class_data=None):
        """Stage selected roots once, then their shared bounded dependencies.

        Required roots must fit the context; dependencies may be truncated.
        Different definitions of a name must be processed in separate contexts.
        """
        entries = list(entries)
        if len({entry.name for entry in entries}) != len(entries):
            raise ValueError("Duplicate class names require separate decompiler contexts")
        if len(entries) > max_classes:
            raise ValueError("Selected classes exceed the decompiler context limit")
        destination = Path(destination)
        pending = deque(entries)
        queued = {entry.name for entry in entries}
        required = {entry.origin for entry in entries}
        staged = set()
        staged_bytes = 0
        with self.class_reader() as read:
            while pending:
                current = pending.popleft()
                if current.name in staged:
                    continue
                if len(staged) >= max_classes:
                    return True
                data = class_data.get(current.origin) if class_data is not None else None
                if data is None:
                    data = read(current)
                if staged_bytes + len(data) > 128 * 1024 * 1024:
                    if current.origin in required:
                        raise ValueError("Selected classes exceed the 128 MiB context limit")
                    return True
                parsed = ClassFile(data)
                if parsed.name != current.name:
                    raise ValueError(f"Class entry/name mismatch: {current.origin}")
                target = destination / _safe_class_path(current.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                staged.add(current.name)
                staged_bytes += len(data)
                # InnerClasses entries are part of the constant-pool dependencies.
                for name in sorted(parsed.dependencies() - queued):
                    candidates = self.by_name.get(name, [])
                    local = [candidate for candidate in candidates
                             if entry_container(candidate) == entry_container(current)]
                    if len(local) == 1:
                        pending.append(local[0])
                        queued.add(name)
                    elif len(candidates) == 1:
                        pending.append(candidates[0])
                        queued.add(name)
        return False
