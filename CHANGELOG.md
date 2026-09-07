# Changelog

English · [Українська](CHANGELOG_UA.md)

## 0.1.1

- Use published DipTraceSchPluginAdapter 0.1.1 through its upstream generator.
- Pin the release commit and verify adapter version, API and EXE checksum.
- Display FineBOM 0.1.1 in the DipTrace menu; retain the stable plugin ID and EXE name.
- Read the plugin version from finebom/build_info.json.
- Record both product versions in diagnostics; reject mixed plugin code/configuration versions.
- Use py.exe by default, preserving the working Python launcher setting.

BOM grouping, formatting and the default BOM.ots template are unchanged.

## 0.1.0

Initial FineBOM migration from BOMJob 0.2.7 to the shared adapter API.
