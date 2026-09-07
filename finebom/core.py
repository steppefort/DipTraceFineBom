"""DipTrace FineBOM: read-only Schematic XML -> OTS template -> ODS.

Python 3.11+, standard library only. The DipTrace exchange file is never changed.
"""
from __future__ import annotations

import configparser
import copy
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
import time
from datetime import datetime
from dataclasses import dataclass, field
from collections import OrderedDict
from xml.dom import minidom, Node
import xml.etree.ElementTree as ET
import zipfile

from .page_labels import apply_page_labels

from . import __version__ as VERSION
BASE = Path(__file__).resolve().parents[1]
NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
}
STANDARD = ("RefDes", "Value", "Name", "Manufacturer", "Datasheet", "PartName", "BaseName")
DEFAULT_ORDER = "A,C,R,L,D,DA,DD,D*,VD,VT,*"
ODS_MIME = "application/vnd.oasis.opendocument.spreadsheet"


class BomError(Exception):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_xml(data: bytes):
    # XML supplied by CAD/template files must not contain external entities.
    upper = data.upper().replace(b"\x00", b"")
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise BomError("DTD and XML entities are not supported.")
    return ET.fromstring(data)


def csv_list(s):
    return [v.strip() for v in s.split(",") if v.strip()]


class Config:
    def __init__(self, path=None):
        self.path = Path(path or BASE / "finebom.ini").resolve()
        self.p = configparser.ConfigParser(interpolation=None)
        self.p.optionxform = str
        if not self.path.is_file():
            raise BomError(f"INI file not found: {self.path}")
        self.p.read(self.path, encoding="utf-8-sig")
        self.replacements = []
        for section in self.p.sections():
            if not section.lower().startswith("replace:"):
                continue
            if not self.boolean(section, "enabled", False):
                continue
            mode = self.get(section, "mode", "exact")
            if mode not in ("exact", "literal", "regex"):
                raise BomError(f"[{section}] unknown mode={mode}")
            pattern = self.get(section, "find", "")
            if not pattern:
                raise BomError(f"[{section}] find must not be empty")
            flags = re.I if self.boolean(section, "ignore_case", False) else 0
            compiled = re.compile(pattern if mode == "regex" else re.escape(pattern), flags)
            self.replacements.append((csv_list(self.get(section, "fields", "*")), mode,
                                      compiled, self.get(section, "replace", "")))
        self.order = csv_list(self.get("sort", "refdes_order", DEFAULT_ORDER))
        self.allowed = {s.casefold() for s in csv_list(self.get("bom", "part_types", "Normal,Tip"))}
        self.asm_true = {s.casefold() for s in csv_list(self.get("bom", "asm_true", "Y,YES,TRUE"))}

    def get(self, section, key, default=""):
        if not self.p.has_section(section):
            return default
        for k, value in self.p.items(section):
            if k.casefold() == key.casefold():
                return value
        return default

    def boolean(self, section, key, default=False):
        raw = self.get(section, key, str(default)).strip().casefold()
        if raw not in ("true", "yes", "y", "1", "false", "no", "n", "0"):
            raise BomError(f"[{section}] {key}: expected true/false, got {raw!r}")
        return raw in ("true", "yes", "y", "1")

    def transform(self, name, value):
        for fields, mode, pattern, replacement in self.replacements:
            if not any(fnmatch.fnmatchcase(name.casefold(), f.casefold()) for f in fields):
                continue
            if mode == "exact":
                if pattern.fullmatch(value):
                    value = replacement
            elif mode == "literal":
                value = pattern.sub(lambda _: replacement, value)
            else:
                value = pattern.sub(replacement, value)
        return value


def refdes_key(refdes, order):
    s = refdes.strip().upper()
    match = re.match(r"[^\d]+", s)
    prefix = match.group(0) if match else ""
    rank = next((i for i, token in enumerate(order)
                 if fnmatch.fnmatchcase(prefix, token.upper())), len(order))
    # Tagged pieces avoid comparisons between int and str; R2 precedes R10.
    natural = tuple((0, int(p)) if p.isdigit() else (1, p)
                    for p in re.split(r"(\d+)", s) if p)
    return rank, prefix, natural, s


@dataclass
class Component:
    refdes: str
    fields: dict[str, str]
    part_type: str
    source_ids: list[str]
    unit_conflicts: list[dict] = field(default_factory=list)


@dataclass
class Group:
    key: str
    components: list[Component]
    canonical: dict[str, str]
    conflicts: list[dict] = field(default_factory=list)


def fields_from(node):
    result = {}
    if node is None:
        return result
    for name in STANDARD:
        child = node.find(name)
        if child is not None:
            result[name.casefold()] = child.text or ""
    for item in node.findall("./AddFields/AddField"):
        name = (item.findtext("Name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in {n.casefold() for n in STANDARD}:
            raise BomError(f"Custom field {name!r} conflicts with a standard field.")
        value = item.findtext("Text", "")
        if key in result and result[key] != value:
            raise BomError(f"Conflicting values for field {name!r} in one component.")
        result[key] = value
    return result


def project_directory(raw, required=False):
    """Interpret an absolute ProjectDir on this OS; never reinterpret D:\\ as a relative path."""
    raw = raw.strip()
    if not raw:
        return None
    path = Path(raw)
    reason = ""
    if os.name != "nt" and PureWindowsPath(raw).drive:
        reason = "ProjectDir is a Windows path and is not usable on this OS"
    elif not path.is_absolute():
        reason = "ProjectDir must be an absolute path"
    if reason:
        if required:
            raise BomError(f"{reason}: {raw}. Set [plugin] output_dir.")
        return None
    return path


def environment_from(root, config, environment_file=None, diagnostics=None, source=None, required_names=(), inherited=None):
    """Read explicit fallbacks, inherited DipTrace variables and any XML containers."""
    env, origins = {}, {}
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update({"files": [], "xml_containers": [], "xml_keys": []})
    prefix = config.get("environment_options", "prefix", "<")
    suffix = config.get("environment_options", "suffix", ">")

    def clean(name):
        name = name.strip()
        pairs = [(prefix, suffix), ("<", ">"), ("%", "%")]
        for left, right in pairs:
            if left and right and name.startswith(left) and name.endswith(right):
                return name[len(left):-len(right)].strip().casefold()
        return name.casefold()

    def add_ini(parser, origin, path):
        keys = set()
        if parser.has_section("environment"):
            for name, value in parser.items("environment"):
                key = clean(name)
                if not key:
                    raise BomError(f"Empty variable name in {path}")
                if key in keys:
                    raise BomError(f"Duplicate variable name {name!r} in {path}")
                keys.add(key)
                env[key], origins[key] = value, origin
        diagnostics["files"].append({"path": str(path), "origin": origin,
                                     "status": "loaded", "keys": sorted(keys)})

    def read_ini(path, origin, required):
        if not path.is_file():
            if required:
                raise BomError(f"Environment file not found: {path}")
            diagnostics["files"].append({"path": str(path), "origin": origin,
                                         "status": "not_found", "keys": []})
            return
        data = path.read_bytes()
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(data.decode(encoding), source=str(path))
        if not parser.has_section("environment"):
            raise BomError(f"Environment file has no [environment] section: {path}")
        add_ini(parser, origin, path)

    # Fallback precedence: plugin INI < project INI < explicitly supplied INI.
    # Actual XML variables, when present, remain authoritative.
    add_ini(config.p, "ini", config.path)
    raw_project = root.findtext("./Schematic/Settings/ProjectDir", "")
    project = project_directory(raw_project)
    if project is not None:
        read_ini(project / "finebom.project.ini", "project_ini", required=False)
    else:
        diagnostics["project_ini_search"] = {
            "status": "skipped", "project_dir": raw_project,
            "reason": "ProjectDir is empty or is not an absolute path usable on this OS"}
    if environment_file is not None:
        read_ini(Path(environment_file).resolve(), "environment_file", required=True)
    selected = inherited or {}
    env.update(selected)
    origins.update({key: "adapter" for key in selected})
    diagnostics["adapter_keys"] = sorted(selected)
    tags = {"environmentvariables", "envvariables", "envvars", "uservariables"}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].casefold() not in tags:
            continue
        diagnostics["xml_containers"].append(node.tag)
        for item in node.iter():
            if item is node:
                continue
            name = item.get("Name") or item.get("name") or item.findtext("Name")
            if name is None:
                continue
            value = item.get("Value")
            if value is None:
                value = item.findtext("Value")
            if value is None:
                value = item.findtext("Text")
            if value is None:
                value = item.findtext("Folder")
            if value is None:
                continue
            key = clean(name)
            if origins.get(key) == "xml" and env[key] != value:
                raise BomError(f"Conflicting values for environment variable {name!r} in XML.")
            env[key], origins[key] = value, "xml"
    diagnostics["xml_keys"] = sorted(key for key in env if origins[key] == "xml")
    return env, origins


def read_schematic(path, config, environment_file=None, *, required_names=(), xml_bytes=None, inherited=None):
    started = time.perf_counter()
    data = xml_bytes if xml_bytes is not None else Path(path).read_bytes()
    if data.startswith(b"\x07DTSCHEM"):
        raise BomError("This is a binary .dch file. Open it in DipTrace and run Tools > Plugins > FineBOM. "
                       "For a standalone run, export DipTrace XML.")
    root = safe_xml(data)
    if root.tag != "Source" or root.get("Type") != "DipTrace-Schematic":
        raise BomError("Expected Source Type=\"DipTrace-Schematic\".")
    schematic = root.find("Schematic")
    if schematic is None:
        raise BomError("XML has no /Source/Schematic.")
    library = {}
    for c in root.findall("./Library/Components/Component"):
        key = c.get("ComponentStyle")
        if key:
            if key in library:
                raise BomError(f"Duplicate ComponentStyle={key}")
            library[key] = c
    instances = OrderedDict()
    skipped = []
    for part in schematic.findall("./Components/Part"):
        pid = part.get("Id", "?")
        if part.get("Enabled", "Y").casefold() == "n":
            skipped.append({"id": pid, "reason": "disabled"})
            continue
        style = part.get("ComponentStyle", "")
        c = library.get(style)
        units = list(c.findall("Part")) if c is not None else []
        index = part.get("ComponentPart", "0")
        unit = next((u for u in units if u.get("Id", "0") == index), None)
        first = next((u for u in units if u.get("Id", "0") == "0"), units[0] if units else None)
        ptype = part.get("PartType") or (unit.get("PartType") if unit is not None else None)
        if ptype is None and first is not None:
            ptype = first.get("PartType")
        if ptype is None:
            raise BomError(f"Part Id={pid}: cannot resolve Part Type through ComponentStyle={style!r}. "
                           "Full XML with the component library is required.")
        f = fields_from(first)
        # The placed part is authoritative, including explicitly empty fields.
        f.update(fields_from(part))
        ref = f.get("refdes", "").strip()
        if ptype.casefold() not in config.allowed:
            skipped.append({"id": pid, "refdes": ref, "reason": "part_type", "value": ptype})
            continue
        if not ref:
            raise BomError(f"Part Id={pid}: RefDes is empty.")
        try:
            unit_number = int(index)
        except ValueError:
            raise BomError(f"Part Id={pid}: ComponentPart is not a number.")
        instances.setdefault(ref.casefold(), []).append((unit_number, pid, style, ptype, f))
    components = []
    asm_key = config.get("bom", "asm_field", "ASM").casefold()
    for ref, units in instances.items():
        units.sort(key=lambda u: (u[0], u[1]))
        if len({u[2] for u in units}) > 1 or len({u[0] for u in units}) != len(units):
            raise BomError(f"RefDes {ref}: duplicate reference for different components or duplicate units.")
        _, _, _, ptype, f = units[0]
        asm = f.get(asm_key, "").strip().casefold()
        if asm not in config.asm_true:
            skipped.append({"refdes": f["refdes"], "reason": "asm", "value": f.get(asm_key, "")})
            continue
        conflicts = []
        for _, pid, _, _, other in units[1:]:
            for key, value in other.items():
                if key in ("partname", "refdes"):
                    continue
                if key in f and value != f[key]:
                    conflicts.append({"field": key, "kept": f[key], "ignored": value, "part_id": pid})
        components.append(Component(f["refdes"].strip(), dict(f), ptype,
                                    [u[1] for u in units], conflicts))
    components.sort(key=lambda c: refdes_key(c.refdes, config.order))
    environment_sources = {}
    env, origins = environment_from(root, config, environment_file, environment_sources, source=path, required_names=required_names, inherited=inherited)
    metadata = {"source_sha256": hashlib.sha256(data).hexdigest(),
                "root_sections": [child.tag for child in root],
                "xml_version": root.get("Version", ""), "skipped": skipped,
                "source_part_count": len(schematic.findall("./Components/Part")),
                "project_dir": schematic.findtext("./Settings/ProjectDir", ""),
                "environment_origins": origins,
                "environment_sources": environment_sources,
                "schematic_sections": [child.tag for child in schematic]}
    metadata["read_schematic_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return components, env, metadata


def group_components(components, fields, config):
    result = OrderedDict()
    singleton = [name.casefold() for _, prefix, name in fields if prefix == "#"]
    for c in components:
        values = {name: config.transform(name, value) for name, value in c.fields.items()}
        key = c.fields.get("value", "")
        if config.boolean("bom", "group_after_replacements", False):
            key = values.get("value", "")
        if key not in result:
            result[key] = Group(key, [], values)
        group = result[key]
        different = [{"field": name, "kept": group.canonical.get(name, ""),
                      "ignored": values.get(name, "")}
                     for name in singleton if values.get(name, "") != group.canonical.get(name, "")]
        different.extend(x for x in c.unit_conflicts if x["field"] in singleton)
        if different:
            group.conflicts.append({"refdes": c.refdes, "differences": different})
        group.components.append(c)
    return list(result.values())


def elements(node, ns=None, local=None):
    return [c for c in node.childNodes if c.nodeType == Node.ELEMENT_NODE
            and (ns is None or c.namespaceURI == NS[ns]) and (local is None or c.localName == local)]


def attr(node, ns, name, default=""):
    return node.getAttributeNS(NS[ns], name) if node.hasAttributeNS(NS[ns], name) else default


def set_attr(node, ns, name, value):
    node.setAttributeNS(NS[ns], f"{ns}:{name}", str(value))


def remove_attr(node, ns, name):
    if node.hasAttributeNS(NS[ns], name):
        node.removeAttributeNS(NS[ns], name)


def make(doc, ns, local, attributes=None):
    node = doc.createElementNS(NS[ns], f"{ns}:{local}")
    for key, val in (attributes or {}).items():
        p, n = key.split(":")
        set_attr(node, p, n, val)
    return node


def node_text(node):
    if node.nodeType == Node.TEXT_NODE:
        return node.data
    if node.nodeType != Node.ELEMENT_NODE:
        return ""
    if node.namespaceURI == NS["text"]:
        if node.localName == "s":
            return " " * int(attr(node, "text", "c", "1"))
        if node.localName == "tab":
            return "\t"
        if node.localName == "line-break":
            return "\n"
    return "".join(node_text(c) for c in node.childNodes)


def cell_text(cell):
    return "\n".join(node_text(p) for p in elements(cell, "text", "p"))


def cells(row):
    return [c for c in elements(row, "table") if c.localName in ("table-cell", "covered-table-cell")]


def rows(table):
    result = []
    for child in elements(table, "table"):
        if child.localName == "table-row":
            result.append(child)
        elif child.localName in ("table-header-rows", "table-rows", "table-row-group"):
            result.extend(rows(child))
    return result


def walk_cells(row):
    index = 0
    for cell in cells(row):
        repeat = int(attr(cell, "table", "number-columns-repeated", "1"))
        yield index, repeat, cell
        index += repeat


def cell_at(row, index):
    for start, count, cell in list(walk_cells(row)):
        if start <= index < start + count:
            if count == 1:
                return cell
            before, after = index - start, start + count - index - 1
            for n in (before, 1, after):
                if not n:
                    continue
                clone = cell.cloneNode(True)
                remove_attr(clone, "table", "number-columns-repeated")
                if n > 1:
                    set_attr(clone, "table", "number-columns-repeated", n)
                row.insertBefore(clone, cell)
            row.removeChild(cell)
            # The target is now an individual cell, re-resolve without expansion.
            return cell_at(row, index)
    # Short rows in compact ODF are valid: materialize only the requested cells.
    total = sum(n for _, n, _ in walk_cells(row))
    for _ in range(total, index + 1):
        row.appendChild(make(row.ownerDocument, "table", "table-cell"))
    return cells(row)[-1]


def clear_value(cell):
    for child in list(cell.childNodes):
        if child.nodeType == Node.ELEMENT_NODE and child.namespaceURI == NS["text"] and child.localName == "p":
            cell.removeChild(child)
    for ns, name in (("office", "value"), ("office", "value-type"), ("office", "string-value"),
                     ("office", "date-value"), ("office", "boolean-value"), ("table", "formula")):
        remove_attr(cell, ns, name)
    if cell.hasAttribute("calcext:value-type"):
        cell.removeAttribute("calcext:value-type")


def append_text(node, text):
    # Explicit ODF whitespace preserves repeated spaces, tabs and line breaks.
    doc = node.ownerDocument
    for token in re.split(r"( +|\t|\n)", text):
        if not token:
            continue
        if token.startswith(" "):
            node.appendChild(make(doc, "text", "s", {"text:c": len(token)}))
        elif token == "\t":
            node.appendChild(make(doc, "text", "tab"))
        elif token == "\n":
            node.appendChild(make(doc, "text", "line-break"))
        else:
            node.appendChild(doc.createTextNode(token))


def write_cell(cell, value, spans=None):
    clear_value(cell)
    if isinstance(value, int):
        set_attr(cell, "office", "value-type", "float")
        set_attr(cell, "office", "value", value)
    else:
        set_attr(cell, "office", "value-type", "string")
    paragraph = make(cell.ownerDocument, "text", "p")
    cell.appendChild(paragraph)
    if spans is None:
        append_text(paragraph, str(value))
    else:
        for text, red in spans:
            if red:
                span = make(cell.ownerDocument, "text", "span", {"text:style-name": "FineBOMConflict"})
                paragraph.appendChild(span)
                append_text(span, text)
            else:
                append_text(paragraph, text)


def template_environment_names(path):
    """Discover tokens before reading inherited variables; handle rich-text splits.

    Use the same cell paragraphs and node_text() as Template.render().
    Never infer variable names from component headers or substituted values.
    """
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise BomError("Template contains duplicate ZIP entry names.")
        if sum(i.file_size for i in archive.infolist()) > 128 * 1024 * 1024:
            raise BomError("Uncompressed template exceeds 128 MiB.")
        if "content.xml" not in archive.namelist():
            raise BomError("Not an OTS/ODS file: content.xml is missing.")
        data = archive.read("content.xml")
    safe_xml(data)
    doc = minidom.parseString(data)
    try:
        names = set()
        for cell in doc.getElementsByTagNameNS(NS["table"], "table-cell"):
            for paragraph in elements(cell, "text", "p"):
                names.update(n.strip().casefold() for n in re.findall(r"<([^<>]+)>", node_text(paragraph)) if n.strip())
        names.discard("datetime")
        return names
    finally:
        doc.unlink()


class Template:
    def __init__(self, path, config):
        self.path, self.config = Path(path), config
        with zipfile.ZipFile(path) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise BomError("Template contains duplicate ZIP entry names.")
            if sum(i.file_size for i in archive.infolist()) > 128 * 1024 * 1024:
                raise BomError("Uncompressed template exceeds 128 MiB.")
            self.files = {n: archive.read(n) for n in archive.namelist()}
        if "content.xml" not in self.files:
            raise BomError("Not an OTS/ODS file: content.xml is missing.")
        safe_xml(self.files["content.xml"])
        self.doc = minidom.parseString(self.files["content.xml"])
        for key, uri in NS.items():
            self.doc.documentElement.setAttribute(f"xmlns:{key}", uri)
        self.styles = self.doc.getElementsByTagNameNS(NS["office"], "automatic-styles")[0]
        candidates = []
        for table in self.doc.getElementsByTagNameNS(NS["table"], "table"):
            rownum = 0
            for row in rows(table):
                repeated = int(attr(row, "table", "number-rows-repeated", "1"))
                fields = []
                for index, count, cell in walk_cells(row):
                    value = cell_text(cell).strip()
                    if value and value[0] in "@#$":
                        if count != 1 or not value[1:].strip():
                            raise BomError("Invalid template header field.")
                        fields.append((index, value[0], value[1:].strip()))
                if fields:
                    if repeated != 1:
                        raise BomError("The header row must not be repeated.")
                    candidates.append((table, row, rownum, fields))
                rownum += repeated
        if len(candidates) != 1:
            raise BomError(f"Expected one row with @RefDes, #Value, $Quantity; found {len(candidates)}. "
                           "Check the template header prefixes.")
        self.table, self.header, self.header_index, self.fields = candidates[0]
        seen = set()
        for _, prefix, name in self.fields:
            key = name.casefold()
            if key in seen:
                raise BomError(f"Duplicate header field: {name}")
            seen.add(key)
            if prefix == "$" and key != "quantity":
                raise BomError(f"Sum field ${name} is not supported; use $Quantity.")
        for key, prefix in (("refdes", "@"), ("value", "#"), ("quantity", "$")):
            if not any(p == prefix and n.casefold() == key for _, p, n in self.fields):
                raise BomError(f"The header requires {prefix}{key}.")
        self.max_col = max(c for c, _, _ in self.fields)
        rr = rows(self.table)
        pos = rr.index(self.header)
        if pos + 1 >= len(rr):
            raise BomError("An empty styled prototype row is required below the header.")
        self.seed = rr[pos + 1]
        # Insertion requires reference rewriting for formulas/named ranges. Refuse
        # these templates until that feature exists, rather than silently corrupting them.
        if any(attr(c, "table", "formula") for r in rr[pos+1:] for c in cells(r)):
            raise BomError("Formulas below the header are not supported: inserting rows changes references.")
        if self.doc.getElementsByTagNameNS(NS["table"], "named-range"):
            raise BomError("Named ranges are not supported when inserting BOM rows.")
        self.seeds = [self.seed]
        mode = config.get("format", "template_rows", "auto").strip().lower()
        if mode not in ("auto", "1", "2"):
            raise BomError("template_rows must be auto, 1 or 2.")
        second = rr[pos + 2] if pos + 2 < len(rr) else None

        def blank(row):
            return not any(cell_text(c) or attr(c, "table", "formula") for c in cells(row))

        def signature(row):
            return (attr(row, "table", "style-name"),
                    tuple((i, count, attr(c, "table", "style-name")) for i, count, c in walk_cells(row)))

        # A separately styled blank row is a second prototype. The compressed
        # unformatted tail of a Calc sheet is not part of the colour cycle.
        distinct = (second is not None and blank(second) and
                    any(i <= self.max_col and attr(c, "table", "style-name") for i, _, c in walk_cells(second)) and
                    signature(second) != signature(self.seed))
        if mode == "2" or (mode == "auto" and distinct):
            if int(attr(self.seed, "table", "number-rows-repeated", "1")) > 1:
                if mode == "2":
                    raise BomError("template_rows=2 requires two separate prototype rows.")
            elif second is None:
                raise BomError("Two prototype rows are required below the header.")
            else:
                self.seeds.append(second)
        for seed in self.seeds:
            if seed.parentNode is not self.header.parentNode:
                raise BomError("Header and prototype rows must share the same row container.")
            if not blank(seed):
                raise BomError("Prototype rows below the header must be empty.")
            for _, _, c in walk_cells(seed):
                if c.localName == "covered-table-cell" or attr(c, "table", "number-columns-spanned"):
                    raise BomError("Merged cells are not supported in BOM prototype rows.")

    def add_style(self, name, family, parent="", properties=None):
        if any(attr(s, "style", "name") == name for s in elements(self.styles, "style", "style")):
            raise BomError(f"Template already contains reserved style {name}")
        item = make(self.doc, "style", "style", {"style:name": name, "style:family": family})
        if parent:
            set_attr(item, "style", "parent-style-name", parent)
        for kind, attributes in (properties or {}).items():
            item.appendChild(make(self.doc, "style", kind, attributes))
        self.styles.appendChild(item)

    def wrap_style(self, cell, cache):
        old = attr(cell, "table", "style-name")
        if not old:
            row = cell.parentNode
            column = next((i for i, _, c in walk_cells(row) if c is cell), 0)
            table = row
            while table.parentNode and not (table.namespaceURI == NS["table"] and table.localName == "table"):
                table = table.parentNode
            offset = 0
            for c in elements(table, "table", "table-column"):
                count = int(attr(c, "table", "number-columns-repeated", "1"))
                if offset <= column < offset + count:
                    old = attr(c, "table", "default-cell-style-name")
                    break
                offset += count
        if old not in cache:
            name = f"FineBOMWrap{len(cache)}"
            base = next((s for s in elements(self.styles, "style", "style")
                         if attr(s, "style", "name") == old), None)
            if base is not None:
                # ODF automatic styles cannot be parents. Copy their properties.
                cloned = base.cloneNode(True)
                set_attr(cloned, "style", "name", name)
                properties = elements(cloned, "style", "table-cell-properties")
                if properties:
                    properties = properties[0]
                else:
                    properties = make(self.doc, "style", "table-cell-properties")
                    cloned.insertBefore(properties, cloned.firstChild)
                set_attr(properties, "fo", "wrap-option", "wrap")
                self.styles.appendChild(cloned)
            else:
                self.add_style(name, "table-cell", old or "Default",
                               {"table-cell-properties": {"fo:wrap-option": "wrap"}})
            cache[old] = name
        set_attr(cell, "table", "style-name", cache[old])

    def render(self, groups, env, now, metadata):
        config = self.config
        self.add_style("FineBOMConflict", "text", properties={"text-properties": {"fo:color": "#ff0000"}})
        wrap_cache, missing = {}, set()
        stamp = now.strftime(config.get("format", "timestamp_format", "%Y%m%d %H:%M"))

        def replace(match):
            name = match.group(1).strip().casefold()
            if name == "datetime":
                return stamp
            if name not in env:
                missing.add(name)
                return match.group(0)
            return config.transform("env:" + name, env[name])

        # Replace within paragraphs, preserving unrelated paragraphs and cell style.
        # A token split across rich-text runs is resolved as a single paragraph.
        for cell in self.doc.getElementsByTagNameNS(NS["table"], "table-cell"):
            changed = False
            for paragraph in list(elements(cell, "text", "p")):
                old = node_text(paragraph)
                if not re.search(r"<([^<>]+)>", old):
                    continue
                new = re.sub(r"<([^<>]+)>", replace, old)
                if new != old:
                    for child in list(paragraph.childNodes):
                        paragraph.removeChild(child)
                    append_text(paragraph, new)
                    changed = True
            if changed:
                self.wrap_style(cell, wrap_cache)
        if missing and config.get("environment_options", "missing", "keep") == "error":
            raise BomError("Environment variables not found: " + ", ".join(sorted(missing)))
        metadata["unresolved_environment"] = sorted(missing)
        for col, _, name in self.fields:
            write_cell(cell_at(self.header, col), name)
        parent = self.seed.parentNode
        metadata["template_row_count"] = len(self.seeds)
        for index, group in enumerate(groups):
            row = self.seeds[index % len(self.seeds)].cloneNode(True)
            remove_attr(row, "table", "number-rows-repeated")
            # Attach before resolving inherited column styles in wrap_style().
            parent.insertBefore(row, self.seed)
            red = {x["refdes"] for x in group.conflicts}
            for col, prefix, name in self.fields:
                cell = cell_at(row, col)
                self.wrap_style(cell, wrap_cache)
                key = name.casefold()
                if prefix == "$":
                    write_cell(cell, len(group.components))
                elif prefix == "#":
                    write_cell(cell, group.canonical.get(key, ""))
                else:
                    spans = []
                    separator = config.get("format", "refdes_separator", ", ").replace("\\s", " ")
                    for i, component in enumerate(group.components):
                        if i:
                            spans.append((separator, False))
                        value = component.refdes if key == "refdes" else config.transform(key, component.fields.get(key, ""))
                        spans.append((value, key == "refdes" and component.refdes in red))
                    write_cell(cell, "", spans=spans)
                    if key == "refdes" and group.conflicts:
                        annotation = make(self.doc, "office", "annotation")
                        paragraph = make(self.doc, "text", "p")
                        text = "\n".join(f"{item['refdes']}: {d['field']}: kept {d['kept']!r}; "
                                         f"ignored {d['ignored']!r}"
                                         for item in group.conflicts for d in item["differences"])
                        append_text(paragraph, text)
                        annotation.appendChild(paragraph)
                        cell.appendChild(annotation)
        # Consume both prototypes, so no unused coloured row follows the BOM.
        for seed in self.seeds:
            seed_count = int(attr(seed, "table", "number-rows-repeated", "1"))
            if seed_count > 1:
                set_attr(seed, "table", "number-rows-repeated", seed_count-1)
            else:
                parent.removeChild(seed)
        # Inserting at the top of a full-size Calc sheet must not exceed 1,048,576 rows.
        excess = max(0, sum(int(attr(r, "table", "number-rows-repeated", "1"))
                            for r in rows(self.table)) - 1048576)
        for row in reversed(rows(self.table)):
            if not excess:
                break
            if any(cell_text(c) or attr(c, "table", "formula") for c in cells(row)):
                raise BomError("BOM exceeds the sheet size; the last row contains data.")
            count = int(attr(row, "table", "number-rows-repeated", "1"))
            take = min(count, excess)
            if take == count:
                row.parentNode.removeChild(row)
            else:
                set_attr(row, "table", "number-rows-repeated", count-take)
            excess -= take
        last_row = self.header_index + 1 + max(len(groups), 1)
        row_number = 0
        for r in rows(self.table):
            count = int(attr(r, "table", "number-rows-repeated", "1"))
            if any(cell_text(c) for c in cells(r)):
                last_row = max(last_row, row_number + count)
            row_number += count
        sheet = attr(self.table, "table", "name").replace("'", "''")
        colname, number = "", self.max_col + 1
        while number:
            number, remainder = divmod(number-1, 26)
            colname = chr(65+remainder) + colname
        if config.boolean("print", "set_bom_print_range", True):
            set_attr(self.table, "table", "print-ranges", f"'{sheet}'.A1:'{sheet}'.{colname}{last_row}")
        self.files["content.xml"] = self.doc.toxml(encoding="utf-8")
        # Preserve page layout even with legacy landscape=true
        # in an older installed INI. Overrides require a new explicit opt-in.
        page_setup = config.get("print", "page_setup", "template").strip().lower()
        if page_setup not in ("template", "override"):
            raise BomError("page_setup must be template or override.")
        if "styles.xml" in self.files and page_setup == "override":
            doc = minidom.parseString(self.files["styles.xml"])
            for props in doc.getElementsByTagNameNS(NS["style"], "page-layout-properties"):
                if config.boolean("print", "landscape", True):
                    width, height = attr(props, "fo", "page-width"), attr(props, "fo", "page-height")
                    if not width or not height:
                        set_attr(props, "fo", "page-width", "297mm")
                        set_attr(props, "fo", "page-height", "210mm")
                    elif attr(props, "style", "print-orientation") != "landscape":
                        set_attr(props, "fo", "page-width", height)
                        set_attr(props, "fo", "page-height", width)
                    set_attr(props, "style", "print-orientation", "landscape")
                if config.boolean("print", "fit_to_width", True):
                    remove_attr(props, "style", "scale-to")
                    remove_attr(props, "style", "scale-to-pages")
                    set_attr(props, "style", "scale-to-X", "1")
                    set_attr(props, "style", "scale-to-Y", "0")
            self.files["styles.xml"] = doc.toxml(encoding="utf-8")
        labels = config.get("print", "header_footer", "bom").strip().lower()
        if labels not in ("bom", "template"):
            raise BomError("header_footer must be bom or template.")
        if labels == "bom" and "styles.xml" in self.files:
            safe_xml(self.files["styles.xml"])
            self.files["styles.xml"] = apply_page_labels(self.files["styles.xml"])
        self.files["mimetype"] = ODS_MIME.encode("ascii")
        manifest = minidom.parseString(self.files["META-INF/manifest.xml"])
        for entry in list(manifest.getElementsByTagNameNS(NS["manifest"], "file-entry")):
            path = attr(entry, "manifest", "full-path")
            if path == "/":
                set_attr(entry, "manifest", "media-type", ODS_MIME)
            if path.startswith("Thumbnails/") or "signatures" in path:
                entry.parentNode.removeChild(entry)
        self.files["META-INF/manifest.xml"] = manifest.toxml(encoding="utf-8")
        for name in list(self.files):
            if name.startswith("Thumbnails/") or "signatures" in name:
                del self.files[name]
        if "meta.xml" in self.files:
            doc = minidom.parseString(self.files["meta.xml"])
            generators = doc.getElementsByTagNameNS(NS["meta"], "generator")
            if generators:
                for child in list(generators[0].childNodes):
                    generators[0].removeChild(child)
                generators[0].appendChild(doc.createTextNode(f"DipTrace FineBOM {VERSION}"))
            self.files["meta.xml"] = doc.toxml(encoding="utf-8")

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".finebom-", suffix=".ods", dir=path.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w") as archive:
                archive.writestr("mimetype", self.files["mimetype"], compress_type=zipfile.ZIP_STORED)
                for name, data in self.files.items():
                    if name != "mimetype":
                        archive.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
            with zipfile.ZipFile(tmp) as archive:
                if archive.testzip():
                    raise BomError("Output ZIP is corrupt.")
                safe_xml(archive.read("content.xml"))
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)


def generate(source, template, output, config, now=None, progress=None, *, schematic_data=None):
    started = time.perf_counter()
    last = started
    timings = {}

    def checkpoint(name):
        nonlocal last
        tick = time.perf_counter()
        timings[name] = round((tick - last) * 1000, 3)
        last = tick
        if progress is not None:
            progress(name, timings[name])

    source, template, output = Path(source).resolve(), Path(template).resolve(), Path(output).resolve()
    if output in (source, template):
        raise BomError("Output must not overwrite the schematic or template.")
    if output.suffix.lower() != ".ods":
        raise BomError("Output must have the .ods extension.")
    if schematic_data is None:
        components, env, metadata = read_schematic(source, config, required_names=template_environment_names(template))
        checkpoint("read_schematic")
    else:
        # The plugin reads ProjectDir before choosing the destination. Reuse that
        # same snapshot, so XML/environment values cannot change between reads.
        components, env, original_metadata = schematic_data
        metadata = copy.deepcopy(original_metadata)
        if sha256(source) != metadata["source_sha256"]:
            raise BomError("Source XML changed after it was read.")
        timings["read_schematic"] = metadata["read_schematic_ms"]
        if progress is not None:
            progress("read_schematic", timings["read_schematic"])
        last = time.perf_counter()
    document = Template(template, config)
    checkpoint("read_template")
    groups = group_components(components, document.fields, config)
    checkpoint("group_components")
    stamp = now or datetime.now().astimezone()
    metadata.update({"plugin_version": VERSION, "generated_at": stamp.isoformat(timespec="minutes"),
                     "template_sha256": sha256(template), "component_count": len(components),
                     "bom_row_count": len(groups), "environment_values": env,
                     "empty_value_refdes": [c.refdes for c in components if not c.fields.get("value", "")],
                     "missing_fields": {name: [c.refdes for c in components if name.casefold() not in c.fields]
                                        for _, prefix, name in document.fields if prefix != "$"}})
    document.render(groups, env, stamp, metadata)
    checkpoint("fill_template")
    metadata["groups"] = [{"value": g.key, "refdes": [c.refdes for c in g.components],
                           "quantity": len(g.components), "fields": g.canonical, "conflicts": g.conflicts}
                          for g in groups]
    metadata["conflicting_component_count"] = sum(len(g.conflicts) for g in groups)
    if sha256(source) != metadata["source_sha256"]:
        raise BomError("Source XML changed during generation.")
    document.save(output)
    checkpoint("write_ods")
    metadata["timings_ms"] = timings
    metadata["total_ms"] = round((time.perf_counter() - started) * 1000 +
                                  (timings["read_schematic"] if schematic_data is not None else 0), 3)
    metadata["components"] = [
        {"refdes": c.refdes, "part_type": c.part_type, "source_ids": c.source_ids,
         "fields": c.fields, "unit_conflicts": c.unit_conflicts} for c in components
    ]
    return metadata
