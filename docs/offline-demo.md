# Offline demo

No model, nothing sent. Work on a copy of the demo:

```
cp -r examples/demo /tmp/demo        # Windows: Copy-Item -Recurse examples\demo $env:TEMP\demo
python -m orgcore rehearse /tmp/demo
```

Real workers stay off until you type `yes`, and then they run for one project and one task at a time. The sessions count against your own plan's limits. Steps: [real-workers.md](real-workers.md).

Hard limits, fixed in code rather than settings: no API key, no spending, no outside contact. See a [real run](real-run/README.md).
