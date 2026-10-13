"""Bounded immutable dependency metadata, keyed by class name and content hash."""
from collections import OrderedDict
import hashlib
from threading import Lock

from .classfile import ClassFile


class ContextMetadataCache:
    def __init__(self, max_entries=4096, max_bytes=16 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.size = 0
        self.lock = Lock()
        self.hits = 0
        self.misses = 0

    def dependencies(self, entry, data):
        # Hash the current bytes: edited classes and duplicate origins cannot
        # reuse metadata from another definition with the same internal name.
        key = (entry.name, hashlib.sha256(data).digest())
        with self.lock:
            cached = self.entries.pop(key, None)
            if cached is not None:
                self.entries[key] = cached
                self.hits += 1
                return cached[0]
            self.misses += 1
        parsed = ClassFile(data)
        if parsed.name != entry.name:
            raise ValueError(f"Class entry/name mismatch: {entry.origin}")
        dependencies = tuple(sorted(parsed.dependencies()))
        # Conservative charge for strings, tuple, hash/key and dictionary nodes;
        # also cap count. Never retain class bodies or mutable parsed objects.
        cost = 512 + len(entry.name) * 4 + sum(128 + len(name) * 4 for name in dependencies)
        if cost <= self.max_bytes and self.max_entries > 0:
            with self.lock:
                old = self.entries.pop(key, None)
                if old is not None:
                    self.size -= old[1]
                while self.entries and (len(self.entries) >= self.max_entries or self.size + cost > self.max_bytes):
                    self.size -= self.entries.popitem(last=False)[1][1]
                self.entries[key] = (dependencies, cost)
                self.size += cost
        return dependencies

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.size = 0
