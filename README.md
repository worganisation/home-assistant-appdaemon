# Home Assistant: AppDaemon

## Codex Usage

The [Codex usage monitor](tools/codex_usage/README.md) tracks multiple ChatGPT accounts through the Codex app-server and publishes MQTT sensors for quota windows, resets, credits and token activity. Use `just codex-login <profile>` locally to enroll an account through Home Assistant's existing SSH connection.

## MAM account and seeding monitor

[Configuration, data coverage and validation](apps/mam/README.md) for the MAM
account sensors, conservative download budget and individual torrent diagnostics.

## qBittorrent Storage Cleanup

`apps/qbittorrent/storage_cleanup.py` watches the qBittorrent scratch-storage sensor and removes the least productive eligible seeder when usage meets `input_number.qbittorrent_storage_cleanup_threshold`. The YAML `threshold` is the fallback when the helper is unavailable. Threshold and mode changes take effect without reloading AppDaemon.

Upload counters are sampled every five minutes and persisted in `/data/qbittorrent/activity.sqlite3`. The retention score is the higher of upload per byte stored over the last 24 hours and the daily average over the last seven days. Partial windows use the actual continuous observation duration. Lower scores are deleted first; ties favour the longest time since meaningful upload, then the largest torrent. Lifetime ratio does not affect ranking.

Torrents require 24 hours since completion and 24 hours of continuous observations. A counter reset, changed completion time, invalid counter, or observation gap exceeding 15 minutes starts a fresh observation period. History survives AppDaemon reloads, retains eight days of samples for window-boundary interpolation, and removes torrents absent from a successful full-client snapshot. Uploads spanning sample boundaries are apportioned linearly.

Currently uploading torrents are protected. Upload activity of at least 1 MiB within a rolling hour starts a two-hour grace period; subsequent positive upload samples extend it while the rolling-hour total meets that threshold. Shelfarr torrents and any torrent with a MyAnonamouse tracker are excluded from deletion. These protections apply to both automatic and notification-confirmed cleanup.

`input_boolean.qbittorrent_storage_cleanup_auto_mode` enables unattended deletion only when explicitly on. Otherwise, the app sends a notification offering deletion of the selected torrent and its files. Confirmation always refers to that exact torrent. Eligibility, recent activity, trackers, storage usage and automatic mode are rechecked before the delete request.

After a deletion, the app waits 90 seconds for the storage sensor to refresh, attempts to restart errored torrents, and considers another candidate if usage still meets the threshold. While storage remains high, the five-minute sampling callback retries cleanup, including when all torrents are protected or an API query fails. Grace periods are never overridden to reclaim space. Unknown storage usage prevents deletion.

Add these values to `/homeassistant/secrets.yaml`, which is shared with AppDaemon:

```yaml
qbittorrent_url: http://<qBittorrent-host>:<Web-UI-port>
qbittorrent_username: <Web-UI-username>
qbittorrent_password: <Web-UI-password>
```

The configured qBittorrent user must be allowed to list, start, and delete torrents
and their content. No additional Python runtime dependency is required.

## Student Loan (SLC)

`apps/slc/slc_balance.py` polls the UK Student Loans Company account overview and
publishes eight Home Assistant MQTT sensors under one `Student Loan` device.

### AppDaemon Runtime Dependencies

Install `httpx2` and `paho-mqtt` into the same Python environment that runs
AppDaemon. For the Home Assistant AppDaemon runtime, add them to the runtime package
list, for example:

```yaml
python_packages:
  - httpx2
  - paho-mqtt
```

### Secrets

```yaml
slc_username: <email or customer reference number>
slc_password: <password>
slc_secret_answer: <secret answer>
appdaemon_mqtt_host: <mqtt broker host>
appdaemon_mqtt_username: <mqtt username>
appdaemon_mqtt_password: <mqtt password>
```

### Entities

- `sensor.slc_balance`
- `sensor.slc_interest_rate`
- `sensor.slc_as_of_date`
- `sensor.slc_current_year`
- `sensor.slc_salary_repayments`
- `sensor.slc_direct_repayments`
- `sensor.slc_interest_added`
- `sensor.slc_last_successful_scrape`

Optional diagnostic entity: `sensor.slc_last_poll`.

Failed polls preserve the last successful sensor readings and their MQTT availability. Transport errors, HTTP 408/429/5xx responses, and the SLC “Services unavailable” page stay silent. Authentication failures, unexpected page layouts, and other HTTP errors send a one-shot notification that clears after a successful poll. The diagnostic entity records the failed attempt and its error; `sensor.slc_last_successful_scrape` indicates when the readings were last refreshed.

Default poll interval is 6 hours. MQTT discovery must be enabled in Home Assistant.

## Pro Breeze Portable AC

`apps/pro_breeze_ac/pro_breeze_ac.py` controls a Pro Breeze portable air conditioner locally with
TinyTuya. It is intended for Tuya local protocol `3.5` devices that are not working
reliably through `localtuya`.

### AppDaemon Runtime Dependencies

Install `tinytuya` and `paho-mqtt` into the same Python environment that runs
AppDaemon. For the Home Assistant AppDaemon runtime, add them to the runtime package
list, for example:

```yaml
python_packages:
  - tinytuya
  - paho-mqtt
```

If AppDaemon runs in a venv or container, install it there instead:

```shell
pip install tinytuya paho-mqtt
```

### MQTT Entities

The app publishes Home Assistant MQTT discovery for `climate.pro_breeze_ac` and uses
MQTT command topics to translate native climate service calls back to Tuya DPS writes.
MQTT discovery must be enabled in Home Assistant.

The same MQTT device also exposes `switch.pro_breeze_ac_polling_enabled`. Turn it on
when the AC is plugged in and off when it is unplugged. Off cancels TinyTuya polling,
closes the device socket, ignores climate commands, and marks the climate entity
unavailable. The switch command is retained by the MQTT broker, so the setting
survives AppDaemon restarts. Polling defaults to off until a retained on command or
a new switch command is received. The switch is unavailable when AppDaemon is
disconnected; the climate entity also requires both AppDaemon and the AC to be
available.

Configure an MQTT user for AppDaemon and provide these secrets:

```yaml
appdaemon_mqtt_host: <mqtt broker host>
appdaemon_mqtt_username: <mqtt username>
appdaemon_mqtt_password: <mqtt password>
```

The optional `raw_sensor` is still useful for diagnostics. It is updated with a compact
state and stores the latest TinyTuya payload in attributes: `raw_status`, `dps`,
`known_dps`, and `last_updated`.

### Confirmed DP Map

- `dp_power`: `1`, boolean power.
- `dp_target_temp`: `2`, target temperature in Celsius, writable range `16`-`32`.
- `dp_current_temp`: `3`, ambient temperature in Celsius, read-only.
- `dp_mode`: `4`, values `Cool`, `Dry`, and `Fan`.
- `dp_fan`: `5`, values `High`, `Mid`, and `Low`.
- `dp_swing`: `15`, values `ON` and `OFF`.
- `dp_sleep`: `101`, boolean sleep mode, exposed as climate preset `sleep`.

DP IDs are treated as strings internally because TinyTuya status payloads commonly
use string keys under `dps`.

### Diagnostics

The AppDaemon app exposes the AC through MQTT climate discovery and the optional
diagnostic raw sensor. Use the raw sensor attributes and AppDaemon logs to inspect
the latest TinyTuya payload when troubleshooting.

### Availability and State

MQTT state topics are retained so Home Assistant can restore the climate entity quickly
after restart.

The climate entity starts unavailable until a poll returns a nonempty DPS dictionary.
MQTT reconnects republish the last established availability without clearing device
failures or treating a broker connection as proof that the AC is reachable. Valid
state values are published before the entity becomes available.

If the AC is unreachable, the app logs the TinyTuya error, updates the raw sensor with
error details when configured, and retries on the next scheduled poll. After
`availability_failure_threshold` consecutive failures (default `3`, minimum `1`),
availability is offline. A successful poll resets the failure count. Invalid or empty
DPS payloads count as failures. TinyTuya sockets are closed on failure and shutdown.

Each MQTT client ID must have exactly one active owner. Repeated MQTT disconnects
can indicate duplicate AppDaemon instances or surviving clients after a reload;
check broker logs for `already connected` messages. Changing the client ID does not
resolve multiple apps publishing to the same state and availability topics.

## Hammersmith commute

The [commute app](apps/commute/README.md) owns session decisions, TfL polling, train selection and notification scheduling while retaining Home Assistant inputs, diagnostic entities and persisted timing samples.
