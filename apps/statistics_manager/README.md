# Label-driven Statistics helpers

`StatisticsSensorManager` manages native Home Assistant Statistics config entries for directly labelled numeric sensors. Adding `trend72Hours` creates a 72-hour `change` helper; removing the source label or deleting the source registry entry removes its owned helper. Device and area labels do not select their child entities.

The app uses AppDaemon's authenticated Home Assistant plugin for standard REST and WebSocket requests. It has no Backplane runtime dependency, separate historical database or MQTT statistics implementation. Home Assistant's Statistics integration owns the calculation and Recorder history restoration.

## Configuration

```yaml
statistics_sensor_manager:
  module: statistics_manager.app
  class: StatisticsSensorManager
  label: trend72Hours
  policy_id: change72h
  hours: 72
  precision: 2
  reconcile_interval: 3600
  debounce: 2
  storage_path: /data/statistics_manager/ownership.json
  dry_run: false
```

The manager creates the selection label when initially absent, then persists its actual registry ID. Label display-name changes retain that identity. Deleting the selection label is unsupported; a missing saved label suspends cleanup. `manager_id` defaults to the AppDaemon application name and forms part of stable ownership identity. Keep it and `policy_id` stable across deployments.

The Statistics characteristic is `change`: newest sample minus oldest sample in the window. The buffer has a maximum age and no sample-count cap. Precision defaults to two decimals and `keep_last_sample` is false. A moisture difference is in percentage points, not a relative percentage change.

## Ownership and recovery

The source entity registry ID and policy identify a helper independently of its current source entity ID. At creation, its native original name contains `AppDaemon statistics [manager:policy:source-registry-id]`. This reserved machine-readable provenance survives friendly-name changes and is recoverable even when the creation response or local mapping is lost. Do not assign this reserved name format to manual helpers.

The manager sets a separate friendly entity name and config-entry title. Its first entity ID is derived from the source where that ID is free; established helper entity IDs remain stable during source renames. Manually disabled helpers remain disabled. The JSON ownership file is atomically replaced after verified discovery; invalid JSON triggers recovery from native provenance. Ambiguous ownership suspends reconciliation rather than selecting a resource to delete.

Registry events, reconnects and startup wake one asynchronous worker. The worker coalesces events for two seconds and performs an hourly fallback reconciliation. Each mutation uses fresh registry discovery, and deletion additionally verifies the config entry disappeared. An unavailable or nonnumeric state does not remove a known source's helper. Failed source operations are reported independently, while connection failures suspend reconciliation and retry with backoff capped at five minutes.

## Dashboard consumers

`sensor.statistics_sensor_manager` exposes `companions`, a mapping from current source entity ID to actual helper entity ID, plus managed/labelled counts, changes, warnings and last successful reconciliation time. Dashboard consumers use this mapping rather than synthesising a helper ID from the source name.

A numeric helper value alone does not establish full historical coverage. Plant cards require `age_coverage_ratio >= 0.8`, a numeric source and a numeric helper before displaying a trend. Values below −2 percentage points mean drying, values above +2 mean wetting, and the inclusive interval between them is stable. These thresholds belong to the plant presentation, not the generic manager.

Recorder must retain and record the source readings for historical restoration. Excluded sources build their Statistics buffer from live readings and cannot recover excluded history after restart. The coverage attribute describes the span of available samples, not their frequency or sensor accuracy.

## Focused tests

Run the lifecycle tests with Python 3.13:

```fish
env PYTHONPATH=apps python -m unittest discover -s tests -p test_statistics_manager.py -v
```
