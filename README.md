# FineBOM

English · [Українська](README_UA.md)

**Generate a formatted OpenDocument bill of materials from a DipTrace schematic.** FineBOM fills an `.ots` spreadsheet template with component fields and project variables, groups components by Value, and saves an `.ods` file in the project's `Docs` folder.

The default [`templates/BOM.ots`](templates/BOM.ots) is included. You can edit it in LibreOffice Calc to match your documentation style.

FineBOM is a Python plugin built directly on [DipTraceSchPluginAdapter](https://github.com/steppefort/DipTraceSchPluginAdapter). It also serves as a complete example of a plugin using the adapter's public context API. There is no separate FineBOM native launcher.

Version **0.1.1** · Python **3.11+** · Windows x64 launcher · MIT license.

## Version and adapter dependency

FineBOM **0.1.1** uses DipTraceSchPluginAdapter **0.1.1**, API **1**, pinned to
commit `02685a4e96f258a22215e9a8e40f0c73827acfdc` (release tag `v0.1.1`).
These are independent product versions.

`finebom/build_info.json` is the single source of the FineBOM version.
The Python package, ODS generator metadata, report and build tool read it.
The build passes this version to the upstream plugin generator and writes it
into `[plugin] version` in the installed `adapter.ini`. The display name is
**FineBOM 0.1.1**; `FineBOM.exe`, `plugin_id` and the captures path stay stable.

The adapter EXE and `.adapter` directory come unchanged from the pinned commit.
Windows EXE properties and `FineBOM.exe --version` identify the **adapter**.
`report.json` contains separate `plugin_version` and `adapter_version` fields.
The workflow rejects a mixture of plugin code and configuration versions.

To update an installed copy, replace the complete `FineBOM` folder and restart
DipTrace Schematic Capture. Preserve custom `finebom.ini` settings and custom
templates; use the new `adapter.ini` so its plugin version matches the code.

## Install and run

1. Obtain the built `FineBOM` folder, or build it from source as described below.
2. Copy the entire folder, including `.adapter`, into `Plugins\Schematic` under your DipTrace installation directory. A typical location is `C:\Program Files\DipTrace\Plugins\Schematic\FineBOM`.
3. Install Python 3.11 or later. No third-party Python packages are required. In `adapter.ini`, leave `executable = py.exe` if that launcher is available, or specify the absolute path to your `pythonw.exe`, without quotes.
4. Restart DipTrace Schematic Capture. Open a saved project, then select **Tools > Plugins > FineBOM 0.1.1**.
5. Check the project's `Docs` directory for the generated `.ods` file. Python exits automatically; no console or capture-folder window opens.

The default filename is `<projectname>v<revision>bom<datetime>.ods`, for example `Board01v2bom06.09.2026_21-20.ods`. Set `projectname` and `revision` in DipTrace's **Tools > Environment variables**, or provide explicit INI fallbacks. The colon in the filename timestamp is replaced by a hyphen for Windows compatibility. Repeated names get `_2`, `_3`, and so on; existing BOM files are preserved.

Set `[plugin] open_result = true` in `finebom.ini` to open the completed BOM in the application associated with `.ods` on Windows.

## Build from source

The source repository contains application code and the default template. The build tool obtains a **pinned commit** of DipTraceSchPluginAdapter and uses its own `tools/new_plugin.py` to create the installable folder.

In PowerShell, from your development directory:

```powershell
git clone https://github.com/steppefort/DipTraceFineBom.git
cd DipTraceFineBom
py -3 tools/build_plugin.py
```

If you downloaded a source archive, extract it and run the last command from its root instead. Git is still required to fetch the adapter dependency.

The command:

1. Reads `adapter-dependency.json`, fetches the specified upstream commit into `.cache`, and checks its commit and EXE checksum.
2. Calls the upstream generator with `--name FineBOM --mode job --plugin-version 0.1.1`.
3. Adds FineBOM's code, configuration, template, and documentation to the generated folder.
4. Sets the DipTrace menu name to **FineBOM 0.1.1**, preserves the upstream runtime, and records the dependency in `adapter.lock.json`.
5. Creates **`build/FineBOM`**. Copy that complete folder into DipTrace to install it.

No C compiler is needed: the adapter commit includes its built Windows EXE. Python is not embedded or installed by this command. The destination must not already exist. For another build, use a fresh destination:

```powershell
py -3 tools/build_plugin.py --dest build/FineBOM-next
```

For an offline build, provide a clean local checkout of the exact pinned adapter commit:

```powershell
py -3 tools/build_plugin.py --adapter-source D:\src\DipTraceSchPluginAdapter --dest build/FineBOM-offline
```

The same build works on Linux with `python3 tools/build_plugin.py`; the output still targets Windows. Native macOS launching is not supported by the pinned adapter.

To update the dependency, choose a reviewed upstream commit and update its `version`, `tag`, `commit`, and `exe_sha256` in `adapter-dependency.json`. Build a fresh package and run the tests. Dependency updates happen during development or packaging, not while DipTrace is running the plugin.

## How the integration works

| File or directory                                  | Responsibility                                                                                                                         |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| [`plugin.py`](plugin.py)                           | Public `main(ctx)` entry point; calls FineBOM's workflow and returns success. Exceptions propagate to the adapter.                     |
| [`finebom/workflow.py`](finebom/workflow.py)       | Reads the captured XML and selected environment values through `ctx`, chooses output paths, and writes diagnostics into `ctx.run_dir`. |
| [`finebom/core.py`](finebom/core.py)               | Parses schematic components, groups them, and fills the OpenDocument template.                                                         |
| [`finebom/output.py`](finebom/output.py)           | Builds valid filenames and publishes output without overwriting earlier BOMs.                                                          |
| [`finebom/page_labels.py`](finebom/page_labels.py) | Configures print headers and page-number fields.                                                                                       |
| `finebom.ini`                                      | BOM configuration and replacement rules.                                                                                               |
| `adapter.ini`                                      | Adapter API version, `job` mode, Python executable, and entry point.                                                                   |
| `adapter-dependency.json`                          | Upstream repository, pinned commit, API version, and EXE checksum used for builds.                                                     |
| `build/FineBOM/.adapter`                           | Unmodified runtime copied by the upstream generator; not a FineBOM implementation.                                                     |

FineBOM uses `ctx.read_xml()`, `ctx.environment(names)`, `ctx.project_dir`, `ctx.plugin_dir`, `ctx.run_dir`, `ctx.exchange_path`, and `ctx.log()`. It never calls `ctx.commit_xml()`. The generated manifest uses `ExpMode=All` and `ImpMode=None`.

The adapter owns capture directories, process creation, inherited environment selection, and execution status. FineBOM does not run the previous BOMJob command-line worker, load its `capture.json`, or implement its own launcher. All BOM logic is ordinary Python code.

For another plugin, keep this boundary: let the adapter supply the invocation context, implement the task in your own package, and return from `main(ctx)` when the task finishes. See the [adapter API](https://github.com/steppefort/DipTraceSchPluginAdapter/blob/main/docs/API.md) for UI and writeback behavior.

## Template rules

Create one header row containing `@RefDes`, `#Value`, and `$Quantity`. Other fields may appear in any order in the same row.

| Marker                                                  | Behavior                                                                                                                                          |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `@RefDes`                                               | Lists component references in one cell, separated by commas. Other `@` fields also concatenate values.                                            |
| `#Value`                                                | Defines the grouping value. Components with exactly equal original Value share one BOM row by default.                                            |
| Other `#` fields                                        | Use the first component's value. Differences in later components are ignored for that field and the conflicting RefDes is colored red.            |
| `$Quantity`                                             | Counts all included components in the group, including those with conflicting fields.                                                             |
| `<projectname>`, `<Company>`, and other `<name>` tokens | Replace the whole token with the corresponding environment value, without case sensitivity. Names are discovered from the template automatically. |
| `<datetime>`                                            | Uses the current local time; the default cell format is `YYYYMMDD HH:MM`. This token is not an environment variable.                              |

The marker prefixes are removed from the generated header; data begins below it. Put one or two empty styled prototype rows immediately below the header. With `template_rows = auto`, two distinct prototypes alternate across all output rows. Their fills and borders are retained. `template_rows = 1` or `2` selects an explicit count.

The default print behavior copies paper size, orientation, margins, and scale from the template; it leaves the top header empty and adds centered **Page N from M** fields at the bottom. Use `[print] header_footer = template` to retain the template's original headers and footers.

Merged cells in BOM prototype rows, formulas below the header, and named ranges affected by inserted rows are not supported. FineBOM reports these cases rather than producing a document with broken references.

## Component selection and order

By default, include components only if Part Type is **Normal** or **Tip** and the `ASM` field is **Y**, **YES**, or **TRUE**, ignoring case. Disabled components are excluded. Multiple units of the same component count once.

The default reference order is `A,C,R,L,D,DA,DD,D*,VD,VT,*`, with numeric ordering within each prefix (`C2` before `C10`). `D*` covers remaining prefixes starting with D; `*` places remaining prefixes in alphabetical order. Groups appear in the order of their first included component.

Replacement sections such as `[replace:01]` run in file order. Supported modes are `exact`, `literal`, and `regex`. `fields` accepts comma-separated field names, `*`, or `env:author` for template environment replacement. By default, replacements affect displayed/comparison values but do not merge different original Values. Set `group_after_replacements = true` only if transformed Values should define groups.

## Configuration and environment values



Edit the installed `finebom.ini`. Existing configurations use the same BOM, sort, replacement, and print keys as BOMJob 0.2.7, but the plugin configuration is now named `finebom.ini` and the optional project fallback is **`finebom.project.ini`**.

| Setting                         | Default and purpose                                                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `[plugin] template`             | `templates/BOM.ots`; relative to the INI directory.                                                                                   |
| `[plugin] output_dir`           | Empty: use the adapter's project directory. A relative override starts beside the INI.                                                |
| `[plugin] output_subdir`        | `Docs`; a relative folder without `..`. Empty saves directly in the base directory.                                                   |
| `[plugin] environment_file`     | Optional explicit INI file with an `[environment]` section.                                                                           |
| `[plugin] filename_pattern`     | `<projectname>v<revision>bom<datetime>.ods`.                                                                                          |
| `[format] timestamp_format`     | `%Y%m%d %H:%M` inside the spreadsheet.                                                                                                |
| `[environment_options] missing` | `keep` preserves missing template tokens and reports them; `error` prevents output. Missing filename variables always prevent output. |

Only completed `.ods` files remain in the output directory. Reports, component data, and logs are stored in the adapter's run directory. If a specified project directory is unavailable, generation fails without redirecting output. If ProjectDir is empty, output falls back to `Docs` under the run directory and this is logged.

Variable precedence, from highest to lowest:

1. Environment-variable containers in the XML, when present.
2. Values returned by the adapter's `ctx.environment()`.
3. The file specified by `[plugin] environment_file`.
4. `<project directory>/finebom.project.ini`, section `[environment]`.
5. `[environment]` in `finebom.ini`.

The adapter reads inherited process values, not persistent Windows system settings. Only requested names are captured. FineBOM does not create or edit variables in DipTrace. Old `environment.json` snapshots are not silently reused.

## Diagnostics and manual debugging

Each launch has a directory under `%LOCALAPPDATA%\DipTraceSchPluginAdapter\FineBOM\captures`. `latest.json` points to the most recent one. Set `capture_dir` in `adapter.ini` to change the location.

Check `status.json` for `ok` or `error`: the EXE can exit before the background job finishes. `plugin.log` records progress and failures; `worker.log` captures Python output. FineBOM adds `report.json` and `components.json`. Selected inherited variables are in `selected_environment.json`.

To debug from source, build the plugin and run the upstream host with a copied XML capture:

```powershell
$env:projectname = 'ExampleProject'
$env:revision = '1'
$env:Company = 'Example Company'
$env:author = 'Example Engineer'
$env:root_proj = 'Main'
py -3 build/FineBOM/.adapter/host.py --config build/FineBOM/adapter.ini --exchange D:\Test\exchange.xml
```

This creates a new adapter capture and executes the same `main(ctx)` entry point. It does not modify an open DipTrace schematic. Variables from the original process are not inherited by a later shell automatically; provide them explicitly as above or through the configured fallback INI. For Linux replay of a Windows capture, set `[plugin] output_dir` to an accessible local directory.

## Tests

Build the plugin first, then run:

```powershell
py -3 tools/build_plugin.py
py -3 -m unittest discover -s tests -v
```

If the build already exists, run only the test command. Set `FINEBOM_TEST_PLUGIN` to a different built folder if needed. Tests cover filtering, component units, grouping, conflict coloring, replacement rules, environment precedence, filenames, repeated output, formatting, and execution through the real adapter Python host. Fixtures are synthetic; private captured schematics are not distributed.

The tests can run on Linux and Windows. Testing the Windows EXE launched by DipTrace and visually checking the printed BOM still require a Windows DipTrace installation.

## License

See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md). The BOM engine is derived from BOMJob Python 0.2.7. The default `BOM.ots` is preserved from that plugin. DipTrace is a Novarm product; FineBOM is an independent community project.
