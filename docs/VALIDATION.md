# Validation of FineBOM 0.1.1

English · [Українська](VALIDATION_UA.md) · [README](../README.md)

The Linux validation suite completed successfully: 19 tests, including an
integration run through the actual DipTraceSchPluginAdapter Python host.
The adapter dependency is pinned to commit
`02685a4e96f258a22215e9a8e40f0c73827acfdc`.

The integration test verifies the FineBOM manifest, hashes of every vendored
adapter file, inherited variable selection, one ODS per invocation, separate
diagnostics, unchanged input XML, and propagation of worker failures to the
adapter's `status.json`.

BOM engine tests cover reference ordering, assembly filters, multi-unit
components, exact-Value grouping, conflicting fields, replacements, missing
variables, and invalid input. Output checks cover filename collisions, Company
substitution, timestamp text, alternating rows, borders, and retained page-layout
properties, with a blank top header and live page number/count fields below.

For FineBOM 0.1.0, a separate comparison against the supplied BOMJob Python 0.2.7 engine used the
same synthetic schematic, configuration, timestamp, and default OTS template.
Both outputs contain 7 components in 4 groups, with 2 conflicting references.
Their OpenDocument archive contents match after accounting for the renamed
generator/version, generated style identifiers, and translated conflict notes.

The default template was copied without modification. Its SHA-256 is:

```text
21a5fa078c1e8f63e560baf32348ca9a123b97828314baa68195e3f62e6f28b1
```

These checks do not execute the Windows EXE inside DipTrace. Installation,
native launch, inherited variables from an actual DipTrace process, and visual
printing should also be checked on Windows. No private captured schematic is
included in this repository's tests.

The 0.1.1 integration test also checks the versioned display name, both context
versions, log entries, report fields and ODS generator metadata. Mixed plugin
code/configuration versions are rejected before creating an output directory.
