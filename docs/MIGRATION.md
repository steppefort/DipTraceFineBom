# Move from BOMJob Python to FineBOM

English · [Українська](MIGRATION_UA.md) · [README](../README.md)

Install FineBOM in a separate plugin folder for the first test. It appears as
**FineBOM 0.1.1** in DipTrace. The existing BOMJob installation can remain available
while you compare output.

| BOMJob Python | FineBOM |
| --- | --- |
| `BOMJobAdapter.exe` and `adapter/` | Upstream `FineBOM.exe` and `.adapter`, supplied by the build |
| `bomjob.py` command-line launcher | `plugin.py` with `main(ctx)`, calling `finebom.workflow.run(ctx)` |
| `bomjob.ini` | `finebom.ini` |
| `bomjob.project.ini` | `finebom.project.ini` |
| Legacy `adapter.ini` with `mode=run`, `exe`, and `script` | Adapter API 1 configuration with `mode=job`, `executable`, and `entry` |
| Separate environment reader and `environment.json` replay | `ctx.environment()`; explicit environment or INI values for manual replay |
| Diagnostics under the old BOMJob capture tree | The shared adapter's FineBOM run directory |

Copy your BOM settings and replacement sections into `finebom.ini`. Do not copy
the old `adapter.ini`: its format belongs to the previous launcher. FineBOM's
complete default configuration is included; there is no need to merge old
launcher settings or run `configure_quiet.py`.

The shipped `BOM.ots` is unchanged from the supplied BOMJob package. Replace it
with your own template if needed and update `[plugin] template`. Relative paths
in `finebom.ini` start beside that file.

The original component selection, grouping, quantity calculation, red conflict
references, alternating prototype rows, borders, and page setup are retained.
The output still goes to `Docs` by default and uses the project-based filename.
FineBOM style names and conflict annotations use the new name and English text.

FineBOM does not load old environment snapshots automatically. For a manual
replay, set the process variables explicitly or create an INI with an
`[environment]` section and specify it as `[plugin] environment_file`. This keeps
replay input explicit and uses the same adapter API as normal execution.

The new launcher can return before BOM generation finishes. Check the current
run's `status.json` for `ok` or `error`, and `report.json` for the actual output
path. The schematic is never committed or changed by FineBOM.
