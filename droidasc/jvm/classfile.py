"""Small, bounded class-file reader and bytecode reference scanner.

This reads structure, not Java semantics: it does not load or execute classes.
Instruction boundaries follow JVMS chapter 6, including switch and wide.
"""
from dataclasses import dataclass, field
import re
import struct


class ClassFormatError(ValueError):
    pass


class Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.pos = 0

    def take(self, size):
        if size < 0 or size > len(self.data) - self.pos:
            raise ClassFormatError(f"Truncated class data at offset {self.pos}")
        start = self.pos
        self.pos += size
        return self.data[start:self.pos]

    def u1(self):
        return self.take(1)[0]

    def u2(self):
        return int.from_bytes(self.take(2), "big")

    def u4(self):
        return int.from_bytes(self.take(4), "big")


def decode_mutf8(data):
    try:
        value = bytes(data).replace(b"\xc0\x80", b"\0").decode("utf-8", "surrogatepass")
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    except UnicodeError as exc:
        raise ClassFormatError("Invalid modified UTF-8 constant") from exc


def internal_name(name):
    name = name.strip()
    if name.startswith("L") and name.endswith(";"):
        name = name[1:-1]
    if name.endswith(".class"):
        name = name[:-6]
    name = name.replace(".", "/")
    if not name:
        raise ValueError("Class name cannot be empty")
    return name


def descriptor_types(descriptor):
    return re.findall(r"L([^;]+);", descriptor)


@dataclass(frozen=True)
class Reference:
    kind: str
    target: str
    owner: str = ""
    name: str = ""
    descriptor: str = ""


@dataclass
class Method:
    name: str
    descriptor: str
    code: memoryview | None
    annotations: list = field(default_factory=list)
    lines: list = field(default_factory=list)


@dataclass
class Field:
    name: str
    descriptor: str
    annotations: list = field(default_factory=list)


# Every assigned opcode up through jsr_w; variable widths handled separately.
_WIDTHS = [1] * 202
for _op in (0x10, 0x12, *range(0x15, 0x1a), *range(0x36, 0x3b), 0xa9, 0xbc):
    _WIDTHS[_op] = 2
for _op in (0x11, 0x13, 0x14, 0x84, *range(0x99, 0xa9), *range(0xb2, 0xb9),
            0xbb, 0xbd, 0xc0, 0xc1, 0xc6, 0xc7):
    _WIDTHS[_op] = 3
_WIDTHS[0xc5] = 4
for _op in (0xb9, 0xba, 0xc8, 0xc9):
    _WIDTHS[_op] = 5
_CP_OPS = {0x12, 0x13, 0x14, *range(0xb2, 0xbb), 0xbb, 0xbd, 0xc0, 0xc1, 0xc5}


def instructions(code):
    pos = 0
    size = len(code)
    while pos < size:
        opcode = code[pos]
        if opcode >= len(_WIDTHS):
            raise ClassFormatError(f"Invalid opcode 0x{opcode:02x} at bytecode offset {pos}")
        width = _WIDTHS[opcode]
        if opcode in (0xaa, 0xab):
            base = (pos + 4) & ~3
            header = 12 if opcode == 0xaa else 8
            if base + header > size:
                raise ClassFormatError("Truncated switch instruction")
            if opcode == 0xaa:
                low, high = struct.unpack_from(">ii", code, base + 4)
                count = high - low + 1
                if count <= 0:
                    raise ClassFormatError("Invalid tableswitch range")
                width = base - pos + 12 + count * 4
            else:
                count = struct.unpack_from(">i", code, base + 4)[0]
                if count < 0:
                    raise ClassFormatError("Invalid lookupswitch count")
                width = base - pos + 8 + count * 8
        elif opcode == 0xc4:
            if pos + 1 >= size:
                raise ClassFormatError("Truncated wide instruction")
            extended = code[pos + 1]
            if extended not in {0x84, 0xa9, *range(0x15, 0x1a), *range(0x36, 0x3b)}:
                raise ClassFormatError("Invalid wide opcode")
            width = 6 if extended == 0x84 else 4
        if pos + width > size:
            raise ClassFormatError(f"Truncated instruction at bytecode offset {pos}")
        index = None
        if opcode in _CP_OPS:
            index = code[pos + 1] if opcode == 0x12 else int.from_bytes(code[pos + 1:pos + 3], "big")
        yield pos, index
        pos += width


class ClassFile:
    def __init__(self, data):
        reader = Reader(data)
        if bytes(reader.take(4)) != b"\xca\xfe\xba\xbe":
            raise ClassFormatError("Invalid JVM class magic")
        self.minor = reader.u2()
        self.major = reader.u2()
        self.cp = [None] * reader.u2()
        index = 1
        while index < len(self.cp):
            tag = reader.u1()
            if tag == 1:
                value = decode_mutf8(reader.take(reader.u2()))
            elif tag in (3, 4):
                value = bytes(reader.take(4))
            elif tag in (5, 6):
                value = bytes(reader.take(8))
            elif tag in (7, 8, 16, 19, 20):
                value = reader.u2()
            elif tag in (9, 10, 11, 12, 17, 18):
                value = (reader.u2(), reader.u2())
            elif tag == 15:
                value = (reader.u1(), reader.u2())
            else:
                raise ClassFormatError(f"Unknown constant-pool tag {tag}")
            self.cp[index] = (tag, value)
            index += 2 if tag in (5, 6) else 1
            if index > len(self.cp):
                raise ClassFormatError("Invalid double-slot constant")
        self.access = reader.u2()
        self.name = self.class_name(reader.u2())
        self.super_index = reader.u2()
        self.interfaces = [reader.u2() for _ in range(reader.u2())]
        self.fields = self._members(reader, methods=False)
        self.methods = self._members(reader, methods=True)
        self.bootstrap = []
        self.annotations = []
        self.source_file = None
        self.source_debug = None
        for name, content in self._attributes(reader):
            if name == "BootstrapMethods":
                sub = Reader(content)
                for _ in range(sub.u2()):
                    handle = sub.u2()
                    self.bootstrap.append([handle] + [sub.u2() for _ in range(sub.u2())])
                if sub.pos != len(sub.data):
                    raise ClassFormatError("Invalid BootstrapMethods attribute length")
            elif name in ("RuntimeVisibleAnnotations", "RuntimeInvisibleAnnotations"):
                self.annotations.extend(self._annotations(content))
            elif name == "SourceFile":
                sub = Reader(content)
                self.source_file = self.utf8(sub.u2())
            elif name == "SourceDebugExtension":
                self.source_debug = decode_mutf8(content)
        if reader.pos != len(reader.data):
            raise ClassFormatError("Trailing class data")

    def constant(self, index, tags=None):
        if not 0 < index < len(self.cp) or self.cp[index] is None:
            raise ClassFormatError(f"Invalid constant-pool index {index}")
        tag, value = self.cp[index]
        if tags is not None and tag not in tags:
            raise ClassFormatError(f"Unexpected constant-pool tag {tag} at index {index}")
        return tag, value

    def utf8(self, index):
        return self.constant(index, {1})[1]

    def class_name(self, index):
        return self.utf8(self.constant(index, {7})[1])

    def _attributes(self, reader):
        for _ in range(reader.u2()):
            name = self.utf8(reader.u2())
            yield name, reader.take(reader.u4())

    def _members(self, reader, methods):
        result = []
        for _ in range(reader.u2()):
            reader.u2()  # access flags
            name = self.utf8(reader.u2())
            descriptor = self.utf8(reader.u2())
            code = None
            annotations = []
            lines = []
            for attr, content in self._attributes(reader):
                if attr in ("RuntimeVisibleAnnotations", "RuntimeInvisibleAnnotations"):
                    annotations.extend(self._annotations(content))
                if methods and attr == "Code":
                    sub = Reader(content)
                    sub.take(4)  # max_stack and max_locals
                    length = sub.u4()
                    if not 0 < length < 65536:
                        raise ClassFormatError("Invalid Code length")
                    code = sub.take(length)
                    sub.take(sub.u2() * 8)  # exception table
                    for nested_name, nested_content in self._attributes(sub):
                        if nested_name == "LineNumberTable":
                            line_reader = Reader(nested_content)
                            lines.extend((line_reader.u2(), line_reader.u2())
                                         for _ in range(line_reader.u2()))
                    if sub.pos != len(sub.data):
                        raise ClassFormatError("Invalid Code attribute length")
            if methods:
                result.append(Method(name, descriptor, code, annotations, lines))
            else:
                result.append(Field(name, descriptor, annotations))
        return result

    def _annotation(self, reader, depth=0):
        if depth > 64:
            raise ClassFormatError("Annotation nesting exceeds 64")
        annotation = {"type": self.utf8(reader.u2()), "values": {}}
        for _ in range(reader.u2()):
            name = self.utf8(reader.u2())
            annotation["values"][name] = self._element_value(reader, depth + 1)
        return annotation

    def _element_value(self, reader, depth):
        if depth > 64:
            raise ClassFormatError("Annotation nesting exceeds 64")
        tag = chr(reader.u1())
        if tag in "sc":
            return self.utf8(reader.u2())
        if tag == "e":
            return {"enum": self.utf8(reader.u2()), "value": self.utf8(reader.u2())}
        if tag == "@":
            return self._annotation(reader, depth + 1)
        if tag == "[":
            return [self._element_value(reader, depth + 1) for _ in range(reader.u2())]
        if tag in "BCDFIJSZ":
            constant_tag, value = self.constant(reader.u2(), {3, 4, 5, 6})
            if constant_tag in (4, 6):
                return struct.unpack(">f" if constant_tag == 4 else ">d", value)[0]
            return int.from_bytes(value, "big", signed=True)
        raise ClassFormatError(f"Unknown annotation value tag {tag}")

    def _annotations(self, content):
        reader = Reader(content)
        result = [self._annotation(reader) for _ in range(reader.u2())]
        if reader.pos != len(reader.data):
            raise ClassFormatError("Invalid annotation attribute length")
        return result

    def line_at(self, method, offset):
        eligible = [(pc, line) for pc, line in method.lines if pc <= offset]
        return max(eligible)[1] if eligible else None

    def _types(self, descriptor):
        for name in descriptor_types(descriptor):
            yield Reference("type", f"L{name};", owner=name)

    def resolve(self, index, active=None):
        active = set() if active is None else active
        if index in active:
            return
        # Bounded traversal for dynamic-constant/bootstrap graphs.
        if len(active) >= 64:
            raise ClassFormatError("Constant dependency depth exceeds 64")
        active = active | {index}
        tag, value = self.constant(index)
        if tag == 8:
            yield Reference("string", self.utf8(value))
        elif tag == 7:
            name = self.utf8(value)
            if name.startswith("["):
                yield from self._types(name)
            else:
                yield Reference("type", f"L{name};", owner=name)
        elif tag in (9, 10, 11):
            owner_index, nt_index = value
            owner = self.class_name(owner_index)
            name_index, descriptor_index = self.constant(nt_index, {12})[1]
            name, descriptor = self.utf8(name_index), self.utf8(descriptor_index)
            kind = "field" if tag == 9 else "method"
            separator = ":" if kind == "field" else ""
            yield Reference(kind, f"L{owner};->{name}{separator}{descriptor}", owner, name, descriptor)
            yield from self.resolve(owner_index, active)
            yield from self._types(descriptor)
        elif tag == 15:
            yield from self.resolve(value[1], active)
        elif tag == 16:
            yield from self._types(self.utf8(value))
        elif tag in (17, 18):
            bootstrap_index, nt_index = value
            _, descriptor_index = self.constant(nt_index, {12})[1]
            yield from self._types(self.utf8(descriptor_index))
            if bootstrap_index >= len(self.bootstrap):
                raise ClassFormatError("Invalid bootstrap method index")
            for dependency in self.bootstrap[bootstrap_index]:
                yield from self.resolve(dependency, active)

    def references(self):
        for method in self.methods:
            if method.code is None:
                continue
            for offset, index in instructions(method.code):
                if index is not None:
                    for ref in dict.fromkeys(self.resolve(index)):
                        yield method, offset, ref

    def dependencies(self):
        """Constant-pool class names for selective decompiler context."""
        names = set()
        for entry in self.cp:
            if entry is None:
                continue
            tag, value = entry
            if tag == 7:
                name = self.utf8(value)
                names.update(descriptor_types(name) if name.startswith("[") else [name])
            elif tag == 1:
                names.update(descriptor_types(value))
        return names
