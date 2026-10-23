# Instance configuration

An instance is a directory outside this repository that holds your organization's private state. The code never assumes where it is: pass the path to a command or set `ORGCORE_INSTANCE`. If neither is given, the command stops with an error.

```
<instance>/
  org.json            organization, lines, projects, weights
  policy.json         spending, subscription windows, hard gates, consent
  reporting.json      time zone, report times, quiet hours, delivery
  state/setup.json    guided-setup checkpoint (optional until setup starts)
  projects/backlog.md task board, see backlog-format.md
```

A complete fictional instance is in `examples/demo/`; `examples/org.example.json` is the same `org.json` as a standalone starting point.

## Rules

- Every field is required unless the schema marks it optional. There are no built-in personal defaults.
- Unknown fields are rejected so that typos do not pass silently.
- All errors across all files are reported together, each with the file and field path, for example `org.json: lines[0].projects[1].weight: missing required field`.
- `weight` values are relative shares. They are normalized among lines, then among the projects of a line.
- `policy.json`: `paid_apis_allowed` must be `false` and every `hard_gates` entry must be `true` in this version. `live_enabled: true` requires an explicit, scoped `live_consent`.
- `reporting.json`: `report_times: []` and `quiet_hours: null` mean "none", stated explicitly. Only `delivery.active: "local"` is accepted; other channels can be recorded as `preferred`.
- `state/setup.json`: a block marked `accepted` carries the sha256 of the answer it accepted (`config.block_hash`).
- Saving through `config.save_file` validates first, writes atomically, and refuses to replace an existing file unless `overwrite=True`.

Check an instance:

```
python -m orgcore check <instance-dir>
```
