"""JSP/framework/reflection and resumable export regression fixtures."""
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from droidasc.jvm.application import JvmApplication
from droidasc.jvm.analyzer import analyze
from droidasc.jvm.classfile import ClassFile
from droidasc.jvm.exporter import export_sources
from droidasc.jvm.webanalysis import jsp_records, source_reflection, source_annotations, xml_records, smap_records


ROOT = Path(__file__).resolve().parents[1]


def jar_bytes(entries):
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


class WebMetadataTests(unittest.TestCase):
    def test_jsp_directives_actions_blocks_el_and_comment_exclusion(self):
        text = '''<%-- <%@ include file="hidden.jsp" %> --%>
<%@ page import="sample.Target,java.util.Map" pageEncoding="UTF-8" %>
<%@ include file="layout.jspf" %>
<jsp:useBean id="target" class="sample.Target" scope="page"/>
<jsp:include page="view.jsp"/>
<%! int count = 0; %><% count++; %><%= count %>
${target.name} ${user['private-value']}
'''
        records = list(jsp_records(text, "index.jsp"))
        self.assertEqual(sum(r["kind"] == "jsp-java-block" for r in records), 3)
        directives = [r for r in records if r["kind"] == "jsp-directive"]
        self.assertEqual(len(directives), 2)
        self.assertEqual(directives[0]["line"], 2)
        self.assertEqual([r["symbols"] for r in records if r["kind"] == "jsp-el"], [["target.name"], ["user"]])
        self.assertNotIn("private-value", json.dumps(records))

    def test_source_reflection_literals_dynamic_comments_and_strings(self):
        text = '''// Class.forName("hidden.Comment");
String log = "Class.forName(\\\"hidden.String\\\")";
Class.forName("sample.Target");
Class.forName(name);
cls.getMethod("hello");
'''
        records = list(source_reflection(text, "Main.java"))
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["target"], "sample.Target")
        self.assertEqual(records[1]["binding"], "dynamic")
        self.assertEqual(records[2]["target"], "hello")

    def test_source_annotations_ignore_comments_and_strings(self):
        text = '// @Controller\nString label="@Service";\n@RestController class Example {}'
        records = list(source_annotations(text, "Example.java"))
        self.assertEqual([r["annotation"] for r in records], ["RestController"])

    def test_namespace_spring_refs_and_secret_values_are_not_reported(self):
        text = '''<beans xmlns="http://www.springframework.org/schema/beans"
                  xmlns:context="http://www.springframework.org/schema/context">
<bean id="service" class="sample.Target" factory-method="create">
  <property name="password" value="do-not-report-this"/>
  <property name="other" ref="dependency"/>
</bean><context:component-scan base-package="sample"/></beans>'''
        records = list(xml_records(text, "beans.xml"))
        bean = next(r for r in records if r["kind"] == "spring-bean")
        self.assertEqual(bean["references"], ["dependency"])
        self.assertEqual(bean["factory-method"], "create")
        self.assertNotIn("do-not-report-this", json.dumps(records))
        self.assertTrue(any(r["kind"] == "spring-scan" for r in records))

    def test_resin_jersey_fragment_and_xfire(self):
        fragment = '''</filter-mapping>
<servlet><servlet-name>rest</servlet-name><servlet-class>sample.Rest</servlet-class>
<init-param><param-name>com.sun.jersey.config.property.packages</param-name><param-value>sample;other</param-value></init-param>
</servlet><servlet-mapping><servlet-name>rest</servlet-name><url-pattern>/api/*</url-pattern></servlet-mapping>'''
        records = list(xml_records(fragment, "web.xml"))
        self.assertEqual(records[0]["coverage"], "fragment")
        mapping = next(r for r in records if r["kind"] == "web-mapping")
        self.assertEqual(mapping["class"], "sample.Rest")
        self.assertEqual(mapping["patterns"], ["/api/*"])
        self.assertEqual(next(r for r in records if r["kind"] == "framework-scan")["targets"], ["sample", "other"])
        xfire = list(xml_records('<services><service><name>example</name><serviceClass>sample.API</serviceClass><implementationClass>sample.Target</implementationClass></service></services>', "services.xml"))
        self.assertEqual(xfire[-1]["implementation"], "sample.Target")

    def test_external_dtd_ignored_without_fetch_internal_entity_rejected(self):
        records = list(xml_records('<!DOCTYPE web-app SYSTEM "https://invalid.example/never-fetch.dtd"><web-app/>', "web.xml"))
        self.assertTrue(records[0]["external_dtd_ignored"])
        with self.assertRaisesRegex(ValueError, "entity"):
            list(xml_records('<!DOCTYPE a [<!ENTITY e "boom">]><a>&e;</a>', "bad.xml"))

    def test_smap_and_generated_jsp_do_not_claim_original_source_recovery(self):
        class Metadata:
            name = "org/apache/jsp/index_jsp"
            source_file = "index_jsp.java"
            methods = []
            source_debug = "SMAP\nindex_jsp.java\nJSP\n*S JSP\n*F\n+ 1 index.jsp\n/views/index.jsp\n2 common.jspf\n*L\n1#1,20:1\n*E\n"
        record = list(smap_records(Metadata(), "index_jsp.class"))[0]
        self.assertEqual(len(record["smap_files"]), 2)
        self.assertFalse(record["original_jsp_recovered"])

    def test_resource_only_nested_archive_inventory_and_export(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            war = temp / "app.war"
            library = jar_bytes({"META-INF/services/sample.API": "sample.Target\n", "META-INF/tags/view.tag": "<%@ tag %>"})
            war.write_bytes(jar_bytes({"views/index.jsp": "<%@ include file='layout.jspf' %>",
                                      "WEB-INF/lib/library.jar": library}))
            with JvmApplication(war, include_resources=True) as app:
                self.assertEqual(len(app.resource_entries), 3)
                providers = [r for r in analyze(app, sources_only=True) if r["kind"] == "service-provider"]
                self.assertEqual(providers[0]["implementation"], "sample.Target")
                self.assertEqual(providers[0]["definition_links"][0]["resolution"], "external-or-missing")
                records = list(export_sources(app, temp / "out"))
                self.assertEqual(len(records), 2)  # JSP and tag sources only
                self.assertTrue(all(r["status"] == "ok" for r in records))

    def test_resource_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.war"
            path.write_bytes(jar_bytes({"../escape.jsp": "hidden"}))
            with self.assertRaisesRegex(ValueError, "Unsafe resource"):
                JvmApplication(path, include_resources=True)


@unittest.skipUnless(shutil.which("javac"), "JDK required for JVM web fixtures")
class JvmWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.classes = cls.root / "classes"
        sources = cls.root / "source"
        files = {
            "sample/Target.java": 'package sample; public class Target { public static String hello() { return "hello"; } }',
            "sample/Reflect.java": '''package sample;
public class Reflect {
 public Object literal() throws Exception { Class<?> c = Class.forName("sample.Target"); java.lang.reflect.Method m = c.getMethod("hello"); return m.invoke(null); }
 public Class<?> dynamic(String name) throws Exception { return Class.forName(name); }
 public Class<?> branch(boolean b) throws Exception { String n; if(b) n="sample.Target"; else n="other.Target"; return Class.forName(n); }
 public java.lang.reflect.Method classLiteral() throws Exception { return Target.class.getDeclaredMethod("hello"); }
 public java.util.ServiceLoader<Runnable> services() { return java.util.ServiceLoader.load(Runnable.class); }
}''',
            "org/springframework/web/bind/annotation/RequestMapping.java": '''package org.springframework.web.bind.annotation;
@java.lang.annotation.Retention(java.lang.annotation.RetentionPolicy.RUNTIME)
public @interface RequestMapping { String[] value(); }''',
            "javax/inject/Inject.java": '''package javax.inject;
@java.lang.annotation.Retention(java.lang.annotation.RetentionPolicy.CLASS) public @interface Inject {}''',
            "sample/Controller.java": '''package sample;
@org.springframework.web.bind.annotation.RequestMapping({"/base"})
public class Controller {
 @javax.inject.Inject public Target target;
 @org.springframework.web.bind.annotation.RequestMapping({"/view", "/other"}) public void view() {}
}''',
            "sample/index_jsp.java": 'package sample; public class index_jsp { public void _jspService() { } }',
        }
        for name, text in files.items():
            path = sources / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        result = subprocess.run([shutil.which("javac"), "--release", "8", "-g", "-d", str(cls.classes),
                                 *map(str, sources.rglob("*.java"))], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr)
        entries = {p.relative_to(cls.classes).as_posix(): p.read_bytes() for p in cls.classes.rglob("*.class")}
        entries["views/index.jsp"] = '<%@ page import="sample.Target" %><jsp:useBean id="target" class="sample.Target"/>'
        entries["WEB-INF/application.xml"] = '<beans><bean id="target" class="sample.Target"/></beans>'
        entries["META-INF/services/java.lang.Runnable"] = "sample.Target\n"
        cls.jar = cls.root / "app.jar"
        cls.jar.write_bytes(jar_bytes(entries))
        cls.duplicate = cls.root / "duplicate.jar"
        cls.duplicate.write_bytes(jar_bytes({"sample/Target.class": entries["sample/Target.class"]}))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)

    def test_literal_local_and_member_tracking_dynamic_control_flow_and_lines(self):
        with JvmApplication(self.jar, include_resources=True) as app:
            records = [r for r in analyze(app, "reflection") if r["kind"] == "reflection-bytecode"]
            literal = [r for r in records if r["method"] == "literal"]
            self.assertEqual(len(literal), 3)
            self.assertTrue(all(r["binding"] == "literal" and r["target_class"] == "Lsample/Target;" for r in literal))
            self.assertTrue(all(r["line"] is not None for r in literal))
            self.assertEqual(literal[-1]["target_member"], "hello")
            self.assertTrue(all(r["binding"] == "dynamic" for r in records if r["method"] in {"dynamic", "branch"}))
            self.assertEqual(next(r for r in records if r["method"] == "classLiteral")["target_member"], "hello")
            self.assertEqual(next(r for r in records if r["method"] == "services")["target_class"], "Ljava/lang/Runnable;")

    def test_class_method_field_annotations_and_jsp_servlet(self):
        with JvmApplication(self.jar, include_resources=True) as app:
            records = list(analyze(app))
            annotations = [r for r in records if r["kind"] == "framework-annotation"]
            self.assertEqual({r["target_kind"] for r in annotations}, {"class", "method", "field"})
            mapping = next(r for r in annotations if r["target_kind"] == "method")
            self.assertEqual(mapping["values"]["value"], ["/view", "/other"])
            self.assertTrue(any(r["kind"] == "jsp-compiled" for r in records))
            bean = next(r for r in records if r["kind"] == "spring-bean")
            self.assertEqual(bean["definition_links"][0]["resolution"], "present")

    def test_duplicate_definition_links_are_ambiguous(self):
        with JvmApplication([self.jar, self.duplicate], include_resources=True) as app:
            records = list(analyze(app, "framework", sources_only=True))
            bean = next(r for r in records if r["kind"] == "spring-bean")
            self.assertEqual(bean["definition_links"][0]["definition_count"], 2)
            self.assertEqual(bean["definition_links"][0]["resolution"], "ambiguous")

    def test_resume_hashes_duplicates_and_refusal_to_overwrite_unmanaged_files(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication([self.jar, self.duplicate], include_resources=True) as app:
            temp = Path(temp)
            output = temp / "out"
            with patch("droidasc.jvm.exporter.decompile", return_value="package sample; public class Target {}") as backend:
                first = list(export_sources(app, output, prefix="sample.Target", workers=2))
                self.assertEqual(backend.call_count, 2)
                self.assertEqual(len({r["output"] for r in first if "class" in r}), 2)
                self.assertTrue(all(r["status"] == "ok" for r in first))
                second = list(export_sources(app, output, prefix="sample.Target", workers=2, resume=True))
                self.assertTrue(all(r["resumed"] for r in second))
                self.assertEqual(backend.call_count, 2)
                (output / first[0]["output"]).write_text("tampered", encoding="utf-8")
                third = list(export_sources(app, output, prefix="sample.Target", resume=True))
                self.assertTrue(all(r["status"] == "ok" for r in third))
                self.assertTrue(any(not r["resumed"] for r in third))
            unmanaged = temp / "unmanaged"
            unmanaged.mkdir()
            (unmanaged / "keep.txt").write_text("keep")
            with self.assertRaisesRegex(ValueError, "not empty"):
                list(export_sources(app, unmanaged))
            self.assertEqual((unmanaged / "keep.txt").read_text(), "keep")

    def test_corrupt_final_manifest_line_can_resume_and_retry_failed_classes(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar, include_resources=True) as app:
            output = Path(temp) / "out"
            with patch("droidasc.jvm.exporter.decompile", side_effect=ValueError("fixture timeout")):
                first = list(export_sources(app, output, prefix="sample.Target", resources=False))
            self.assertEqual(first[0]["status"], "error")
            with (output / "manifest.jsonl").open("ab") as stream:
                stream.write(b'{"interrupted"')
            with patch("droidasc.jvm.exporter.decompile", return_value="class Target {}"):
                retried = list(export_sources(app, output, prefix="sample.Target", resources=False, resume=True))
            self.assertEqual(retried[0]["status"], "ok")
            for line in (output / "manifest.jsonl").read_text().splitlines():
                json.loads(line)

    def test_output_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar, include_resources=True) as app:
            temp = Path(temp)
            output = temp / "out"
            output.mkdir()
            (output / "manifest.jsonl").write_text("")
            (output / "sources").symlink_to(temp / "elsewhere", target_is_directory=True)
            with patch("droidasc.jvm.exporter.decompile", return_value="class Target {}"):
                records = list(export_sources(app, output, prefix="sample.Target", resources=False, resume=True))
            self.assertEqual(records[0]["status"], "error")
            self.assertIn("symlink", records[0]["error"])

    def test_partial_bad_class_does_not_hide_other_results(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.jar"
            target = (self.classes / "sample/Target.class").read_bytes()
            path.write_bytes(jar_bytes({"sample/Target.class": target, "sample/Bad.class": b"invalid"}))
            with JvmApplication(path, include_resources=True, tolerant=True) as app:
                records = list(analyze(app, "reflection"))
                self.assertEqual(sum(r["kind"] == "analysis-error" for r in records), 1)

    def test_cli_resources_and_source_only_analysis(self):
        result = self.cli("listresources", self.jar, "--kind", "jsp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["resource"], "views/index.jsp")
        result = self.cli("analyze", self.jar, "--kind", "jsp", "--sources-only", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(json.loads(line)["kind"] == "jsp-source" for line in result.stdout.splitlines()))

    @unittest.skipUnless(os.environ.get("ASC_TEST_CFR_JAR"), "Set ASC_TEST_CFR_JAR for bulk CFR integration")
    def test_real_cfr_bulk_export_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            args = ("export", self.jar, "--classpath", self.duplicate, "-o", output, "--prefix", "sample.Target",
                    "--cfr-jar", os.environ["ASC_TEST_CFR_JAR"], "--threads", "2")
            first = self.cli(*args)
            self.assertEqual(first.returncode, 0, first.stderr)
            records = [json.loads(line) for line in first.stdout.splitlines()]
            self.assertEqual(sum("class" in r for r in records), 2)
            self.assertTrue(all(r["status"] == "ok" for r in records))
            for record in records:
                self.assertTrue((output / record["output"]).is_file())
            second = self.cli(*args, "--resume")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(all(json.loads(line)["resumed"] for line in second.stdout.splitlines()))


if __name__ == "__main__":
    unittest.main()
