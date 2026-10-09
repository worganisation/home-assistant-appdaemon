# Codex usage monitor

The AppDaemon `codex_usage_monitor` app polls each enrolled ChatGPT account every five minutes and publishes MQTT discovery sensors. Quota windows retain their server-defined durations and reset timestamps. Token activity is independent of quota availability; the daily sensor reports the latest date returned by Codex, not an inferred local “today”. Credits are not converted into currency.

## Runtime

The runtime requires Linux amd64, AppDaemon, `pydantic`, `aiohttp`, `httpx2` and `paho-mqtt`. AppDaemon supplies `aiohttp`; the repository and existing AppDaemon package configuration supply the other libraries. The installer verifies the official Codex 0.154.0 package against its pinned SHA-256 and compares installed files with that verified archive on each startup. It preserves older runtime directories and credentials.

The AppDaemon App configuration includes this startup command, in addition to unrelated commands:

```yaml
init_commands:
  - /opt/appdaemon/bin/python /config/tools/codex_usage/install.py
```

`apps/apps.yaml` configures the binary, private storage and control socket paths, polling interval, and existing MQTT credentials. Arguments are validated together at initialization. A process lock prevents two monitor instances from owning the same storage directory. Missing binaries are reported during onboarding; existing accounts show failed polls until the runtime is installed.

## Enroll and inspect accounts

Run these commands from the repository on a Mac with `uv`, `just`, and an existing trusted SSH identity for Home Assistant:

```fish
just codex-login personal
just codex-login work
just codex-status
```

The helper uses SSH alias `hass` by default. It displays the official device-login URL and a one-time code. Complete login in your browser using the account intended for that profile. Device-code login must be enabled in the account's security settings or workspace policy. The command waits for identity verification and reports the enrolled account. Ctrl+C cancels a pending login; an interrupted connection expires within fifteen minutes. Repeating `codex-login` reauthenticates the same profile. A different account cannot replace its identity, and an already enrolled identity cannot be registered twice.

Use an existing alias or an address override when the Mac's network requires another route. The hostname override preserves the alias's configured user and identity and still requires a matching known host key:

```fish
just codex-login personal --ssh-target hass --hostname 192.168.68.70
just codex-status --ssh-target hasspi
```

The helper invokes `curl` in Home Assistant's Terminal App against `/share/codex-monitor/control.sock`. AppDaemon and Terminal must both mount `/share` and have permission to access its owner-only socket directory. This interface is not a TCP listener and accepts only onboarding and status operations. It cannot run inference, consume resets, or forward arbitrary RPC calls. No container shell, copied desktop credentials or public webhook is required.

Each profile has a private credential generation under `/data/codex/homes`. Codex manages refresh tokens there. Profile records under `/data/codex/profiles` bind the chosen name to the verified email and account/workspace ID. Fresh login attempts use a separate directory and are promoted only after identity verification. Failed attempts are removed; prior active generations remain private for recovery. Never commit, print or copy these credential files into Home Assistant entities.

## Sensors and dashboard

MQTT groups sensors under `Codex — <profile>`. Entity IDs include a stable hash of the profile name and, for quotas, the bucket ID. Each present primary/secondary window has used percentage, remaining percentage and next-reset sensors, with `window_duration_minutes` metadata. Optional values remain unknown. Null or unavailable account identifiers prevent enrollment or further publication under an unverified identity.

Quota and token activity have separate freshness clocks. A group becomes unavailable after fifteen minutes without a successful refresh. Authentication failures disable both groups immediately. MQTT reconnects republish existing observations with their remaining lifetime; broker availability alone cannot mark readings healthy. Sensor expiry also protects against a stalled AppDaemon event loop. Removed quota windows have their MQTT discovery entries retired after a successful snapshot.

Monitor status and last successful update remain diagnostic sensors. A successful update of one group does not refresh the other group's data. `needs_login` requires `just codex-login <profile>`; `degraded` indicates an endpoint failure; `stale` indicates missing or expired observations. Reauthentication clears cached readings until fresh data arrives.

```fish
just codex-dashboard
```

This command prints account sections for the existing `home-will/tooling` storage-mode view. The sections contain gauges, Mushroom detail cards and a seven-day mini-graph. Apply them through Home Assistant's dashboard API/MCP after confirming actual entity IDs; preserve the Cursor sections and use a fresh configuration hash. Dashboard exports are not authored configuration. Refresh the Codex sections when enrolling accounts or when the service changes its window layout. Historical graphs accumulate through Home Assistant Recorder; the collector does not backfill history.

## Operations and recovery

Code deploys through the user-owned Git checkout. AppDaemon startup options are native App configuration. Stop the monitor before changing its storage or socket ownership, and restart the App after changing startup commands. A normal app reload cancels pending logins, stops polling, reaps child processes and publishes offline availability.

To roll back, restore the prior Git checkout and AppDaemon startup options, restart the App, and restore the prior Tooling view through its API. Preserve `/data/codex` if account credentials are needed for recovery. Retained entities become unavailable when the monitor stops; deleting them is a separate cleanup action.
