"""Streaming static application metadata analysis with explicit coverage errors."""
from pathlib import Path
import re
from .classfile import ClassFile
from .reflection import reflection_records
from .webanalysis import (JSP_KINDS, annotation_records, decode_text, jsp_records,
                          smap_records, source_annotations, source_reflection, xml_records, CLASS_PATTERN)


def link_definitions(record, application):
    # These links identify inventoried definitions, never callability/reachability.
    classes = []
    for key in ("class", "target_class", "implementation", "interface"):
        value = record.get(key)
        if isinstance(value, str) and value:
            classes.append(value)
    if record.get("api") in {"forName", "loadClass"} and record.get("binding") == "literal":
        classes.append(record["target"])
    if record.get("kind") == "jsp-action" and record.get("attributes", {}).get("class"):
        classes.append(record["attributes"]["class"])
    links = []
    for name in dict.fromkeys(classes):
        normalized = name[1:-1] if name.startswith("L") and name.endswith(";") else name.replace(".", "/")
        entries = application.by_name.get(normalized, [])
        links.append({"class": f"L{normalized};", "definition_count": len(entries),
                      "resolution": "present" if len(entries) == 1 else ("ambiguous" if entries else "external-or-missing"),
                      "origins": [entry.origin for entry in entries]})
    if links:
        record["definition_links"] = links
    return record


def analyze(application, kind="all", prefix=None, resource_prefix=None, limit=None,
            sources_only=False):
    if kind not in {"all", "jsp", "framework", "reflection"}:
        raise ValueError(f"Unknown analysis kind: {kind}")
    if limit is not None and limit <= 0:
        raise ValueError("Analysis class limit must be positive")
    for error in application.errors:
        yield {"kind": "analysis-error", **error}
    for entry in application.resources():
        if resource_prefix and not entry.name.startswith(resource_prefix):
            continue
        suffix = Path(entry.name).suffix.lower().lstrip(".")
        service_provider = "/META-INF/services/" in "/" + entry.name
        if suffix not in JSP_KINDS | {"java", "xml", "tld", "properties"} and not service_provider:
            continue
        try:
            text, encoding = decode_text(application.read_resource(entry))
            if suffix in JSP_KINDS and kind in {"all", "jsp"}:
                yield {"kind": "jsp-source", "origin": entry.origin, "resource": entry.name,
                       "encoding": encoding, "lines": len(text.splitlines()), "confidence": "source"}
                for record in jsp_records(text, entry.origin):
                    yield link_definitions(record, application)
            if suffix in {"xml", "tld"} and kind in {"all", "framework", "jsp"}:
                for record in xml_records(text, entry.origin):
                    if kind != "jsp" or record["kind"] in {"jsp-config", "xml-coverage"}:
                        yield link_definitions(record, application)
            if suffix in JSP_KINDS | {"java"} and kind in {"all", "reflection"}:
                for record in source_reflection(text, entry.origin):
                    yield link_definitions(record, application)
            if suffix == "java" and kind in {"all", "framework"}:
                yield from source_annotations(text, entry.origin)
            if service_provider and kind in {"all", "framework", "reflection"}:
                for number, line in enumerate(text.splitlines(), 1):
                    provider = line.split("#", 1)[0].strip()
                    if re.fullmatch(CLASS_PATTERN, provider):
                        yield link_definitions({"kind": "service-provider", "origin": entry.origin, "line": number,
                                                "interface": entry.name.rsplit("/", 1)[-1], "implementation": provider,
                                                "confidence": "configuration"}, application)
            if suffix == "properties" and kind in {"all", "framework", "reflection"}:
                for number, line in enumerate(text.splitlines(), 1):
                    match = re.match(r"\s*([\w.-]*(?:class|Class|provider|implementation))\s*[=:]\s*(" + CLASS_PATTERN + r")\s*$", line)
                    if match and not line.lstrip().startswith(("#", "!")):
                        yield link_definitions({"kind": "configured-class", "origin": entry.origin, "line": number,
                                                "setting": match.group(1), "class": match.group(2),
                                                "confidence": "configuration"}, application)
        except (ValueError, OSError) as exc:
            yield {"kind": "analysis-error", "origin": entry.origin, "error": str(exc)}
    if sources_only:
        return
    for number, entry in enumerate(application.classes(prefix)):
        if limit is not None and number >= limit:
            break
        try:
            parsed = ClassFile(application.read(entry))
            if parsed.name != entry.name:
                raise ValueError("Class entry/name mismatch")
            if kind in {"all", "jsp"}:
                yield from smap_records(parsed, entry.origin)
            if kind in {"all", "framework"}:
                for record in annotation_records(parsed, entry.origin):
                    yield link_definitions(record, application)
            if kind in {"all", "reflection"}:
                for record in reflection_records(parsed, entry.origin):
                    yield link_definitions(record, application)
        except (ValueError, OSError) as exc:
            yield {"kind": "analysis-error", "origin": entry.origin, "class": f"L{entry.name};", "error": str(exc)}
