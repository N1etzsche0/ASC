"""Static JSP and framework metadata. Evidence, never runtime dispatch proof."""
import re
from pathlib import Path
import xml.etree.ElementTree as ET


JSP_KINDS = {"jsp", "jspx", "jspf", "tag", "tagx"}
CLASS_PATTERN = r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+"


def decode_text(data):
    # JSPs commonly use an explicit legacy page encoding.
    match = re.search(br'(?:pageEncoding|encoding)\s*=\s*[\x22\x27]([\w-]+)', data[:4096], re.I)
    encodings = [match.group(1).decode("ascii")] if match else []
    encodings.extend(["utf-8-sig", "gb18030", "latin-1"])
    for encoding in encodings:
        try:
            return data.decode(encoding), encoding
        except (UnicodeError, LookupError):
            continue
    raise ValueError("Unable to decode source")


def attributes(text):
    return {match.group(1): match.group(3) for match in
            re.finditer(r'([\w:.-]+)\s*=\s*([\x22\x27])(.*?)\2', text, re.S)}


def line_number(text, offset):
    return text.count("\n", 0, offset) + 1


def mask_comments(text):
    pattern = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|<%--.*?--%>|<!--.*?-->|/\*.*?\*/|//[^\n]*'''
    return re.sub(pattern, lambda m: m.group() if m.group().startswith(('"', "'"))
                  else re.sub(r"[^\n]", " ", m.group()), text, flags=re.S)


def mask_strings(text):
    return re.sub(r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',
                  lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def jsp_records(text, origin):
    text = re.sub(r"<%--.*?--%>|<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.S)
    for match in re.finditer(r"<%@\s*(\w+)\b(.*?)%>", text, re.S):
        directive = match.group(1)
        attrs = attributes(match.group(2))
        kept = {key: value for key, value in attrs.items() if key in {
            "import", "file", "pageEncoding", "contentType", "prefix", "uri", "tagdir", "extends"}}
        yield {"kind": "jsp-directive", "origin": origin, "line": line_number(text, match.start()),
               "directive": directive, "attributes": kept, "confidence": "syntax"}
    for match in re.finditer(r"<jsp:(useBean|include|forward|directive\.\w+|scriptlet|declaration|expression)\b([^>]*)(?:/?>)", text, re.S):
        attrs = attributes(match.group(2))
        kept = {key: value for key, value in attrs.items() if key in {
            "id", "class", "type", "scope", "page", "file", "import", "name", "uri", "prefix"}}
        yield {"kind": "jsp-action", "origin": origin, "line": line_number(text, match.start()),
               "action": match.group(1), "attributes": kept, "confidence": "syntax"}
    for match in re.finditer(r"<%(?!@|--)([!=]?)(.*?)%>", text, re.S):
        yield {"kind": "jsp-java-block", "origin": origin, "line": line_number(text, match.start()),
               "end_line": line_number(text, match.end()),
               "block": {"!": "declaration", "=": "expression", "": "scriptlet"}[match.group(1)],
               "confidence": "syntax"}
    for match in re.finditer(r"[$#]\{([^{}]*)\}", text):
        # Emit identifiers, never arbitrary expression text/string literals.
        expression = re.sub(r'([\x22\x27]).*?\1', "", match.group(1))
        names = sorted(set(re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", expression)))
        yield {"kind": "jsp-el", "origin": origin, "line": line_number(text, match.start()),
               "symbols": names, "confidence": "syntax"}


def _local(tag):
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def xml_records(text, origin):
    # Legacy descriptors use external DOCTYPE declarations. Strip these without
    # resolving them; internal subsets/entities remain unsupported.
    if re.search(r"<!\s*ENTITY\b|<!\s*DOCTYPE[^>]*\[", text, re.I):
        raise ValueError("Internal DTD/entity declarations are not supported")
    dtd_ignored = bool(re.search(r"<!\s*DOCTYPE\b", text, re.I))
    text = re.sub(r"<!\s*DOCTYPE[^>]*>", "", text, flags=re.I)
    coverage = "document"
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Extract complete metadata blocks from partial deployment descriptors.
        coverage = "fragment"
        fragments = [match.group() for match in re.finditer(
            r"<(servlet|filter|servlet-mapping|filter-mapping|bean|service|taglib|listener|jsp-property-group)\b[^>]*>.*?</\1\s*>", text, re.S)]
        root = ET.Element("fragment")
        for fragment in fragments:
            try:
                root.append(ET.fromstring(fragment))
            except ET.ParseError:
                continue
        if not len(root):
            raise ValueError("Malformed XML; no complete supported metadata blocks")
    yield {"kind": "xml-coverage", "origin": origin, "coverage": coverage, "confidence": "syntax", "external_dtd_ignored": dtd_ignored}
    definitions = {}
    for element in root.iter():
        tag = _local(element.tag)
        children = {}
        for child in element:
            key = _local(child.tag)
            children.setdefault(key, []).append((child.text or "").strip())
        def first(key):
            return next(iter(children.get(key, [])), "")
        if tag in {"servlet", "filter", "listener"}:
            cls = first(tag + "-class")
            if cls:
                name = first(tag + "-name")
                definitions[(tag, name)] = cls
                yield {"kind": "web-component", "origin": origin, "component": tag,
                       "name": name, "class": cls, "confidence": "configuration", "coverage": coverage}
            for parameter in element:
                if _local(parameter.tag) != "init-param":
                    continue
                pairs = {_local(child.tag): (child.text or "").strip() for child in parameter}
                key = pairs.get("param-name", "")
                if key in {"com.sun.jersey.config.property.packages", "jersey.config.server.provider.packages",
                           "javax.ws.rs.Application", "jakarta.ws.rs.Application", "contextConfigLocation"}:
                    yield {"kind": "framework-scan", "origin": origin, "setting": key,
                           "targets": re.split(r"[;,\s]+", pairs.get("param-value", "")),
                           "confidence": "configuration", "coverage": coverage}
        elif tag == "bean":
            record = {"kind": "spring-bean", "origin": origin, "confidence": "configuration", "coverage": coverage}
            record.update({key: element.attrib[key] for key in ("id", "name", "class", "factory-bean", "factory-method", "parent", "scope") if key in element.attrib})
            refs = []
            for child in element.iter():
                refs.extend(child.attrib[key] for key in ("ref", "bean", "local", "parent") if key in child.attrib)
            record["references"] = sorted(set(refs))
            yield record
        elif tag in {"component-scan", "annotation-config"}:
            yield {"kind": "spring-scan", "origin": origin, "base_package": element.attrib.get("base-package", ""),
                   "confidence": "configuration", "coverage": coverage}
        elif tag == "service" and (first("serviceClass") or first("implementationClass")):
            yield {"kind": "xfire-service", "origin": origin, "name": first("name"),
                   "interface": first("serviceClass"), "implementation": first("implementationClass"),
                   "confidence": "configuration", "coverage": coverage}
        elif tag in {"tag", "tag-file", "taglib", "jsp-property-group"}:
            yield {"kind": "jsp-config", "origin": origin, "element": tag,
                   "metadata": {key: value for key, value in children.items() if key in {
                       "name", "tag-class", "tei-class", "path", "taglib-uri", "taglib-location",
                       "url-pattern", "include-prelude", "include-coda"}}, "confidence": "configuration"}
    for element in root.iter():
        tag = _local(element.tag)
        if tag not in {"servlet-mapping", "filter-mapping"}:
            continue
        component = tag[:-8]
        names = [(child.text or "").strip() for child in element if _local(child.tag) == component + "-name"]
        patterns = [(child.text or "").strip() for child in element if _local(child.tag) == "url-pattern"]
        for name in names:
            yield {"kind": "web-mapping", "origin": origin, "component": component, "name": name,
                   "patterns": patterns, "class": definitions.get((component, name)),
                   "confidence": "configuration", "coverage": coverage}


def source_reflection(text, origin):
    clean = mask_comments(text)
    code = mask_strings(clean)
    # Source regex evidence is deliberately weaker than bytecode literal tracking.
    api_pattern = r"(?P<receiver>[\w.$]+)\s*\.\s*(?P<api>forName|loadClass|getMethod|getDeclaredMethod|getField|getDeclaredField|getConstructor|getDeclaredConstructor|newInstance|invoke)\s*\("
    for match in re.finditer(api_pattern, code):
        api, receiver = match.group("api"), match.group("receiver")
        if api in {"invoke", "newInstance"} and receiver not in {"Class", "java.lang.Class"}:
            # Arbitrary methods can have these names: retain explicit uncertainty.
            confidence = "name-only"
        else:
            confidence = "source-syntax"
        tail = clean[match.end():match.end() + 1024]
        literal = re.match(r'\s*"([\w.$]+)"', tail)
        record = {"kind": "reflection-source", "origin": origin, "line": line_number(clean, match.start()),
                  "api": api, "receiver": receiver, "confidence": confidence, "binding": "dynamic"}
        if literal and ((api in {"forName", "loadClass"} and re.fullmatch(CLASS_PATTERN, literal.group(1)))
                        or api in {"getMethod", "getDeclaredMethod", "getField", "getDeclaredField"}):
            record.update(binding="literal", target=literal.group(1))
        yield record


def source_annotations(text, origin):
    text = mask_strings(mask_comments(text))
    names = {"RequestMapping", "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping",
             "Path", "GET", "POST", "PUT", "DELETE", "Produces", "Consumes", "WebServlet", "WebFilter",
             "Controller", "RestController", "Component", "Service", "Repository", "Configuration", "Bean",
             "Autowired", "Inject", "Named", "Resource", "Qualifier", "ComponentScan"}
    for match in re.finditer(r"@([\w.]+)(?:\s*\(([^\n]*?)\))?", text):
        if match.group(1).rsplit(".", 1)[-1] not in names:
            continue
        yield {"kind": "framework-source-annotation", "origin": origin,
               "line": line_number(text, match.start()), "annotation": match.group(1),
               "confidence": "source-syntax"}


def smap_records(parsed, origin):
    debug = parsed.source_debug or ""
    files = []
    if debug.startswith("SMAP\n") or debug.startswith("SMAP\r\n"):
        lines = debug.splitlines()
        stratum = None
        index = 3
        while index < len(lines):
            line = lines[index]
            if line.startswith("*S "):
                stratum = line[3:]
            elif line == "*F":
                index += 1
                while index < len(lines) and not lines[index].startswith("*"):
                    parts = lines[index].split(" ", 2)
                    if parts[0] == "+" and len(parts) == 3 and index + 1 < len(lines):
                        files.append({"id": parts[1], "name": parts[2], "path": lines[index + 1], "stratum": stratum})
                        index += 2
                    elif len(parts) >= 2:
                        files.append({"id": parts[0], "name": " ".join(parts[1:]), "stratum": stratum})
                        index += 1
                    else:
                        index += 1
                continue
            index += 1
    jsp_files = [file for file in files if Path(file["name"]).suffix.lower().lstrip(".") in JSP_KINDS]
    if jsp_files or (parsed.source_file and parsed.source_file.endswith((".jsp", ".jspx"))) or any(m.name == "_jspService" for m in parsed.methods):
        yield {"kind": "jsp-compiled", "origin": origin, "class": f"L{parsed.name};",
               "source_file": parsed.source_file, "smap_files": jsp_files,
               "service_methods": [m.name + m.descriptor for m in parsed.methods if m.name in {"_jspService", "_jspInit", "_jspDestroy"}],
               "confidence": "class-metadata", "original_jsp_recovered": False}


def annotation_records(parsed, origin):
    targets = [("class", parsed.name, parsed.annotations)]
    targets += [("method", m.name + m.descriptor, m.annotations) for m in parsed.methods]
    targets += [("field", f.name + ":" + f.descriptor, f.annotations) for f in parsed.fields]
    for target_kind, target, annotations in targets:
        for annotation in annotations:
            annotation_type = annotation["type"]
            if not annotation_type.startswith(("Lorg/springframework/", "Ljavax/ws/rs/", "Ljakarta/ws/rs/",
                                               "Ljavax/servlet/annotation/", "Ljakarta/servlet/annotation/",
                                               "Ljavax/inject/", "Ljakarta/inject/", "Ljavax/annotation/", "Ljakarta/annotation/")):
                continue
            values = {key: value for key, value in annotation["values"].items() if key in {
                "value", "path", "name", "urlPatterns", "basePackages", "basePackageClasses", "method", "lookup", "mappedName"}}
            yield {"kind": "framework-annotation", "origin": origin, "class": f"L{parsed.name};",
                   "target_kind": target_kind, "target": target, "annotation": annotation_type,
                   "values": values, "confidence": "class-metadata"}
