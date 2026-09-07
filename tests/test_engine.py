"""Behavioral tests using synthetic schematics."""
import copy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from xml.dom import minidom
import zipfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from finebom.core import (Config, Component, Template, BomError, generate, read_schematic,
                    refdes_key, group_components, cell_text, attr, NS, rows, walk_cells)


def fixture(path):
    root = ET.Element("Source", Type="DipTrace-Schematic", Version="4.3.0.0", Units="mm")
    lib = ET.SubElement(ET.SubElement(root, "Library", Type="DipTrace-ComponentLibrary"), "Components")
    for style, ptype, count in [("CAP", "Normal", 1), ("RES", "Normal", 1),
                                ("TIP", "Tip", 1), ("POWER", "Power", 1),
                                ("PORT", "Net Port", 1), ("IC", "Normal", 2)]:
        c = ET.SubElement(lib, "Component", ComponentStyle=style)
        for i in range(count):
            ET.SubElement(c, "Part", Id=str(i), PartType=ptype)
    schematic = ET.SubElement(root, "Schematic")
    env = ET.SubElement(schematic, "EnvironmentVariables")
    for name, value in {"projectname": "Example project — synthetic XML",
                        "author": "Example engineer", "root_proj": "Example root", "revision": "DEMO", "Company": "Example Company"}.items():
        item = ET.SubElement(env, "Variable")
        ET.SubElement(item, "Name").text = "<" + name + ">"
        ET.SubElement(item, "Value").text = value
    parts = ET.SubElement(schematic, "Components")

    def add(ref, value, style="CAP", asm="Y", manufacturer="Vendor A", description="Capacitor", unit=0, **extra):
        p = ET.SubElement(parts, "Part", Id=str(len(parts)), ComponentStyle=style, ComponentPart=str(unit))
        for name, val in {"RefDes": ref, "Value": value, "Manufacturer": manufacturer}.items():
            ET.SubElement(p, name).text = val
        af = ET.SubElement(p, "AddFields")
        fields = {"ASM": asm, "Description": description, "schval": "100 nF", "case": "SM0603"}
        fields.update(extra)
        for name, val in fields.items():
            if val is None:
                continue
            f = ET.SubElement(af, "AddField", Type="Text")
            ET.SubElement(f, "Name").text = name
            ET.SubElement(f, "Text").text = val
        return p
    # Deliberately out of RefDes order. Same Value, conflicting manufacturer/description.
    add("C10", "CAP-100N", asm="tRuE", manufacturer="Vendor B")
    add("C2", "CAP-100N", asm="yEs")
    add("C3", "CAP-100N", description="Different description")
    add("C4", "CAP-100N", asm="N")
    add("C5", "EXCLUDE-MISSING-ASM", asm=None)
    add("P1", "EXCLUDE-POWER", style="POWER")
    add("P2", "EXCLUDE-PORT", style="PORT")
    add("R10", "RES-10K", style="RES", description="Resistor", schval="10 kΩ")
    add("R2", "RES-10K", style="RES", asm=" Y ", description="Resistor", schval="10 kΩ")
    add("A1", "MODULE", style="TIP", description="Module", schval="Module")
    add("DA1", "IC-DUAL", style="IC", unit=1, description="IC", schval="Dual")
    add("DA1", "IC-DUAL", style="IC", unit=0, description="IC", schval="Dual")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return root


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.source = self.directory / "fixture.xml"
        fixture(self.source)
        self.cfg = Config(BASE / "finebom.ini")
        self.template = BASE / "templates/BOM.ots"

    def test_sort(self):
        refs = "X2 DQ10 VD1 A10 C10 DA10 DD1 D1 R1 L1 A2 DQ2 DA2 VT1 B1 AA1 C2".split()
        actual = sorted(refs, key=lambda r: refdes_key(r, self.cfg.order))
        self.assertEqual(actual, "A2 A10 C2 C10 R1 L1 D1 DA2 DA10 DD1 DQ2 DQ10 VD1 VT1 AA1 B1 X2".split())

    def test_filter_dedup_group_and_red_runs(self):
        out = self.directory / "BOM.ods"
        before = self.source.read_bytes()
        report = generate(self.source, self.template, out, self.cfg, datetime(2026, 9, 5, 21, 7, tzinfo=timezone.utc))
        self.assertEqual(report["component_count"], 7)
        self.assertEqual(report["bom_row_count"], 4)
        self.assertEqual(report["conflicting_component_count"], 2)
        self.assertEqual(report["groups"][1]["refdes"], ["C2", "C3", "C10"])
        self.assertEqual(report["groups"][1]["quantity"], 3)
        self.assertEqual(report["groups"][1]["fields"]["manufacturer"], "Vendor A")
        self.assertEqual(report["unresolved_environment"], [])
        self.assertEqual(self.source.read_bytes(), before)
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.infolist()[0].filename, "mimetype")
            self.assertEqual(z.infolist()[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(z.read("mimetype"), b"application/vnd.oasis.opendocument.spreadsheet")
            d = minidom.parseString(z.read("content.xml"))
        red = d.getElementsByTagNameNS(NS["text"], "span")
        self.assertEqual([r.firstChild.data for r in red if attr(r, "text", "style-name") == "FineBOMConflict"], ["C3", "C10"])
        xml = d.toxml()
        self.assertIn("20260905 21:07", " ".join(cell_text(c) for c in d.getElementsByTagNameNS(NS["table"], "table-cell")))
        self.assertIn("21:07", xml)
        self.assertNotIn("@RefDes", xml)
        self.assertNotIn("#Value", xml)
        self.assertIn('office:value="3"', xml)
        generated_styles = [s for s in d.getElementsByTagNameNS(NS["style"], "style")
                            if attr(s, "style", "name").startswith("FineBOMWrap")]
        self.assertTrue(any(s.getElementsByTagNameNS(NS["style"], "table-cell-properties")
                            and attr(s.getElementsByTagNameNS(NS["style"], "table-cell-properties")[0],
                                     "fo", "border") for s in generated_styles))
        table = d.getElementsByTagNameNS(NS["table"], "table")[0]
        self.assertLessEqual(sum(int(attr(r, "table", "number-rows-repeated", "1")) for r in rows(table)), 1048576)

    def test_field_names_case_insensitive(self):
        parts, _, _ = read_schematic(self.source, self.cfg)
        self.assertEqual(parts[0].fields["case"], "SM0603")

    def test_missing_env_preserved_and_reported(self):
        root = ET.parse(self.source)
        s = root.find("Schematic")
        s.remove(s.find("EnvironmentVariables"))
        root.write(self.source, encoding="utf-8")
        report = generate(self.source, self.template, self.directory / "BOM.ods", self.cfg)
        self.assertEqual(report["unresolved_environment"], ["author", "company", "projectname", "revision", "root_proj"])
        self.cfg.p.set("environment_options", "missing", "error")
        with self.assertRaises(BomError):
            generate(self.source, self.template, self.directory / "strict.ods", self.cfg)
        self.assertFalse((self.directory / "strict.ods").exists())

    def test_ini_environment_fallback_xml_precedence(self):
        self.cfg.p.set("environment", "projectname", "WRONG")
        self.cfg.p.set("environment", "custom", "123")
        _, env, metadata = read_schematic(self.source, self.cfg)
        self.assertNotEqual(env["projectname"], "WRONG")
        self.assertEqual(env["custom"], "123")
        self.assertEqual(metadata["environment_origins"]["custom"], "ini")

    def test_replacements_compare_after_transform_group_by_original(self):
        p = self.directory / "custom.ini"
        text = (BASE / "finebom.ini").read_text(encoding="utf-8")
        text += '\n[replace:test]\nenabled=true\nfields=Manufacturer\nmode=exact\nfind=Vendor B\nreplace=Vendor A\n'
        p.write_text(text, encoding="utf-8")
        cfg = Config(p)
        parts, _, _ = read_schematic(self.source, cfg)
        groups = group_components(parts, Template(self.template, cfg).fields, cfg)
        self.assertEqual([i["refdes"] for i in groups[1].conflicts], ["C3"])

    def test_unknown_type_fails(self):
        tree = ET.parse(self.source)
        tree.getroot().remove(tree.find("Library"))
        tree.write(self.source, encoding="utf-8")
        with self.assertRaises(BomError):
            read_schematic(self.source, self.cfg)

    def test_duplicate_refdes_fails(self):
        tree = ET.parse(self.source)
        parts = tree.find("Schematic/Components")
        parts.append(copy.deepcopy(parts[0]))
        tree.write(self.source, encoding="utf-8")
        with self.assertRaises(BomError):
            read_schematic(self.source, self.cfg)

    def test_native_dch_fails_clearly(self):
        self.source.write_bytes(b"\x07DTSCHEMbinary")
        with self.assertRaisesRegex(BomError, "binary"):
            read_schematic(self.source, self.cfg)

    def test_empty_asm_never_included(self):
        tree = ET.parse(self.source)
        for f in tree.findall(".//Schematic/Components/Part/AddFields/AddField"):
            if f.findtext("Name") == "ASM":
                f.find("Text").text = ""
        tree.write(self.source, encoding="utf-8")
        report = generate(self.source, self.template, self.directory / "empty.ods", self.cfg)
        self.assertEqual(report["component_count"], 0)
        self.assertEqual(report["bom_row_count"], 0)

    def test_output_cannot_replace_source(self):
        with self.assertRaises(BomError):
            generate(self.source, self.template, self.source, self.cfg)

    def test_permuted_columns(self):
        with zipfile.ZipFile(self.template) as z:
            files = {n: z.read(n) for n in z.namelist()}
        d = minidom.parseString(files["content.xml"])
        header = next(r for r in d.getElementsByTagNameNS(NS["table"], "table-row") if "@RefDes" in r.toxml())
        cc = [c for c in header.childNodes if c.nodeType == c.ELEMENT_NODE][:7]
        texts = [cell_text(c) for c in cc]
        from finebom.core import write_cell
        for c, text in zip(cc, reversed(texts)):
            write_cell(c, text)
        files["content.xml"] = d.toxml(encoding="utf-8")
        t = self.directory / "permuted.ots"
        with zipfile.ZipFile(t, "w") as z:
            for n, data in files.items():
                z.writestr(n, data)
        report = generate(self.source, t, self.directory / "permuted.ods", self.cfg)
        self.assertEqual(report["groups"][1]["quantity"], 3)


if __name__ == "__main__":
    unittest.main()
