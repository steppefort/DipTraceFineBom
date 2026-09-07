"""BOM workflow using the public DipTraceSchPluginAdapter context API."""
from datetime import datetime
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
import time

from .core import BomError, Config, generate, read_schematic, template_environment_names
from .output import bom_filename, publish_bom
from . import __version__


def requested_names(config, template):
    names = set(template_environment_names(template))
    names.update(n.strip() for n in config.get(
        "environment_options", "process_names", "root_proj,projectname,revision,author").split(",") if n.strip())
    names.update(re.findall(r"<([^<>]+)>", config.get(
        "plugin", "filename_pattern", "<projectname>v<revision>bom<datetime>.ods")))
    normalized = set()
    for name in names:
        name = name.strip()
        for left, right in (("<", ">"), ("%", "%")):
            if name.startswith(left) and name.endswith(right):
                name = name[1:-1].strip()
                break
        if name.casefold() != "datetime":
            normalized.add(name.casefold())
    return sorted(normalized)


def output_directory(ctx, config, metadata):
    raw = config.get("plugin", "output_dir", "").strip()
    if raw:
        directory = Path(os.path.expandvars(raw)).expanduser()
        if os.name != "nt" and PureWindowsPath(raw).drive:
            raise BomError("output_dir is a Windows path; set a path usable on this OS.")
        if not directory.is_absolute():
            directory = config.path.parent / directory
        origin = "config:plugin.output_dir"
    elif metadata["project_dir"].strip():
        # On POSIX, Path would otherwise reinterpret a Windows drive as relative.
        if os.name != "nt" and PureWindowsPath(metadata["project_dir"]).drive:
            raise BomError("ProjectDir is a Windows path; set [plugin] output_dir for this OS.")
        directory = ctx.project_dir
        if directory is None or not directory.is_dir():
            raise BomError(f"Project directory is unavailable: {directory}. Set [plugin] output_dir.")
        origin = "adapter:project_dir"
    else:
        directory = ctx.run_dir
        origin = "adapter:run_dir (ProjectDir is empty)"
        ctx.log("ProjectDir is empty; the BOM will be saved under this run directory.")
    subdir = config.get("plugin", "output_subdir", "Docs").strip()
    relative = PureWindowsPath(subdir)
    if relative.drive or relative.root or ".." in relative.parts:
        raise BomError("output_subdir must be relative without '..', for example Docs or Docs/BOM.")
    return directory.joinpath(*relative.parts).resolve(), origin


def run(ctx, *, now=None):
    """Generate one ODS; diagnostics stay in ctx.run_dir. Never commit schematic XML."""
    if ctx.mode != "job":
        raise BomError("FineBOM requires adapter mode=job.")
    if ctx.plugin_version != __version__:
        raise BomError(f"FineBOM code version {__version__} differs from adapter.ini version "
                       f"{ctx.plugin_version!r}. Reinstall the complete FineBOM folder.")
    ctx.log(f"FineBOM {__version__}; DipTraceSchPluginAdapter {ctx.adapter_version}")
    started = time.perf_counter()
    config = Config(ctx.plugin_dir / "finebom.ini")
    template = (config.path.parent / config.get("plugin", "template", "templates/BOM.ots")).resolve()
    names = requested_names(config, template)
    inherited = ctx.environment(names) if config.boolean("environment_options", "process_environment", True) else {}
    source = ctx.exchange_path
    captured = ctx.read_xml()
    environment_file = config.get("plugin", "environment_file", "").strip()
    environment_file = config.path.parent / environment_file if environment_file else None
    schematic = read_schematic(source, config, environment_file,
                               xml_bytes=captured, inherited=inherited)
    directory, origin = output_directory(ctx, config, schematic[2])
    now = now or datetime.now().astimezone()
    output = directory / bom_filename(config, schematic[1], now)
    if output in (source.resolve(), template):
        raise BomError("Output must not overwrite the captured XML or template.")
    directory.mkdir(parents=True, exist_ok=True)
    ctx.log(f"FineBOM: {len(schematic[0])} components; template: {template}")
    fd, staging_name = tempfile.mkstemp(prefix=".finebom-", suffix=".ods", dir=directory)
    os.close(fd)
    staging = Path(staging_name)
    try:
        report = generate(source, template, staging, config, now=now, schematic_data=schematic,
                          progress=lambda name, ms: ctx.log(f"{name}: {ms:.1f} ms"))
        ctx.read_xml()  # Verify capture integrity before publishing the ODS.
        output = publish_bom(staging, output, automatic=True)
    finally:
        staging.unlink(missing_ok=True)
    report.update({"plugin": "FineBOM", "source_path": str(source), "output_path": str(output),
                   "adapter_version": ctx.adapter_version,
                   "output_origin": origin, "diagnostics_directory": str(ctx.run_dir),
                   "requested_environment_names": names,
                   "worker_total_ms": round((time.perf_counter() - started) * 1000, 3)})
    for name, value in (("report.json", report), ("components.json", report["components"])):
        (ctx.run_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ctx.log(f"BOM: {output}; rows: {report['bom_row_count']}; conflicts: {report['conflicting_component_count']}")
    if report["unresolved_environment"]:
        ctx.log("Unresolved template variables: " + ", ".join(report["unresolved_environment"]))
    if config.boolean("plugin", "open_result", False):
        if os.name == "nt":
            try:
                os.startfile(str(output))
            except OSError as error:
                ctx.log(f"BOM saved, but the viewer could not be opened: {error}")
        else:
            ctx.log("open_result is supported on Windows; open the ODS manually on this OS.")
    return report
