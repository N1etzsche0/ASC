"""Conservative basic-block literal tracking for Java reflection call sites."""
import re
from .classfile import instructions


def parameter_types(descriptor):
    return re.findall(r"\[*L[^;]+;|\[*[BCDFIJSZ]", descriptor[1:descriptor.index(")")])


def _class_literal(name):
    return bool(re.fullmatch(r"[A-Za-z_$][\w$]*(?:[./][A-Za-z_$][\w$]*)+", name))


def reflection_records(parsed, origin):
    for method in parsed.methods:
        if method.code is None:
            continue
        code = method.code
        decoded = list(instructions(code))
        targets = set()
        # Clear tracked values on every branch target; no path merging/invention.
        for pos, _index in decoded:
            op = code[pos]
            if 0x99 <= op <= 0xa8 or op in (0xc6, 0xc7):
                targets.add(pos + int.from_bytes(code[pos + 1:pos + 3], "big", signed=True))
            elif op in (0xc8, 0xc9):
                targets.add(pos + int.from_bytes(code[pos + 1:pos + 5], "big", signed=True))
            elif op in (0xaa, 0xab):
                # Switch successors may be nonadjacent. Flush all locals for the
                # remainder of this method rather than carrying path-specific literals.
                targets.update(p for p, _ in decoded if p > pos)
        stack = []
        locals_ = {}

        def pop():
            return stack.pop() if stack else None

        for pos, index in decoded:
            if pos in targets:
                stack.clear()
                locals_.clear()
            op = code[pos]
            if op == 0x00:
                continue
            if op in (0x12, 0x13, 0x14):
                tag, value = parsed.constant(index)
                stack.append(("string", parsed.utf8(value)) if tag == 8 else
                             (("class", parsed.utf8(value)) if tag == 7 else None))
            elif op == 0x01 or 0x02 <= op <= 0x11:
                stack.append(None)
            elif op == 0x19 or 0x2a <= op <= 0x2d:
                local = code[pos + 1] if op == 0x19 else op - 0x2a
                stack.append(locals_.get(local))
            elif op == 0x3a or 0x4b <= op <= 0x4e:
                local = code[pos + 1] if op == 0x3a else op - 0x4b
                locals_[local] = pop()
            elif op == 0x59:  # dup on reference/string values
                stack.append(stack[-1] if stack else None)
            elif op == 0x57:
                pop()
            elif op in (0xbb,):
                stack.append(None)
            elif op in (0xbc, 0xbd):
                pop()
                stack.append(None)
            elif op == 0xc5:
                for _ in range(code[pos + 3]):
                    pop()
                stack.append(None)
            elif 0x4f <= op <= 0x56:
                pop(); pop(); pop()
            elif op == 0xc0:  # checkcast does not change known identity
                continue
            elif op in (0xb2, 0xb4):
                if op == 0xb4:
                    pop()
                stack.append(None)
            elif op in (0xb3, 0xb5):
                pop()
                if op == 0xb5:
                    pop()
            elif op in (0xb6, 0xb7, 0xb8, 0xb9, 0xba):
                tag, value = parsed.constant(index)
                if tag in (10, 11):
                    owner = parsed.class_name(value[0])
                    name_index, desc_index = parsed.constant(value[1], {12})[1]
                elif tag == 18:
                    owner = "invokedynamic"
                    name_index, desc_index = parsed.constant(value[1], {12})[1]
                else:
                    stack.clear(); locals_.clear()
                    continue
                name, desc = parsed.utf8(name_index), parsed.utf8(desc_index)
                args = [pop() for _ in parameter_types(desc)][::-1]
                receiver = pop() if op not in (0xb8, 0xba) else None
                result = None
                recognized = (
                    owner == "java/lang/Class" and name in {"forName", "getMethod", "getDeclaredMethod", "getField", "getDeclaredField", "getConstructor", "getDeclaredConstructor", "newInstance"}
                    or (owner == "java/lang/ClassLoader" and name == "loadClass")
                    or (owner in {"java/lang/reflect/Method", "java/lang/reflect/Constructor", "java/lang/reflect/Field"}
                        and name in {"invoke", "newInstance", "get", "set", "getInt", "setInt", "getBoolean", "setBoolean"})
                    or (owner == "java/util/ServiceLoader" and name == "load")
                    or (owner == "java/lang/reflect/Proxy" and name == "newProxyInstance")
                    or (owner == "java/lang/invoke/MethodHandles$Lookup" and name.startswith("find"))
                )
                if recognized:
                    record = {"kind": "reflection-bytecode", "origin": origin,
                              "class": f"L{parsed.name};", "method": method.name,
                              "descriptor": method.descriptor, "offset": pos,
                              "line": parsed.line_at(method, pos), "api": f"{owner}.{name}",
                              "binding": "dynamic", "confidence": "call-site"}
                    if name in {"forName", "loadClass"} and args and args[0] and args[0][0] == "string" and _class_literal(args[0][1]):
                        target = args[0][1].replace(".", "/")
                        result = ("class", target)
                        record.update(binding="literal", target_class=f"L{target};", confidence="literal-dataflow")
                    elif owner == "java/lang/Class" and receiver and receiver[0] == "class":
                        target = receiver[1]
                        member = "<init>" if "Constructor" in name or name == "newInstance" else None
                        if args and args[0] and args[0][0] == "string" and re.fullmatch(r"[\w$]+", args[0][1]):
                            member = args[0][1]
                        record.update(target_class=f"L{target};")
                        if member:
                            record.update(binding="literal", target_member=member, confidence="literal-dataflow")
                            if name != "newInstance":
                                result = ("reflection", (target, member))
                    elif receiver and receiver[0] == "reflection":
                        target, member = receiver[1]
                        record.update(binding="literal", target_class=f"L{target};", target_member=member,
                                      confidence="literal-dataflow")
                    elif owner == "java/util/ServiceLoader" and args and args[0] and args[0][0] == "class":
                        record.update(binding="literal", target_class=f"L{args[0][1]};", confidence="literal-dataflow")
                    yield record
                if desc.rsplit(")", 1)[-1] != "V":
                    stack.append(result)
            else:
                # Unsupported arithmetic/control/stack effects kill evidence;
                # values must never survive an unknown transfer as known literals.
                stack.clear()
                locals_.clear()
