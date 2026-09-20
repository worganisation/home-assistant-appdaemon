# MAM monitor

The `mam_monitor` AppDaemon app publishes the `MAM` MQTT device and `sensor.mam_*`
entities. It reads the documented MAM user-data endpoint every 30 minutes and
qBittorrent every five minutes. It does not search, download, change torrent
settings, spend bonus points, or call the Dynamic Seedbox API.

## Credentials and configuration

`apps.yaml` supplies the MAM, qBittorrent and MQTT credentials through `!secret`
references to Home Assistant's `/homeassistant/secrets.yaml`. Set the dedicated
MAM API session in that file:

```yaml
mam_monitor_mam_id: "YOUR_DEDICATED_MAM_SESSION"
```

The session must permit Home Assistant's egress; the monitor does not use the VPN
IP updater's session. An empty string leaves MAM unconfigured while qBittorrent
monitoring continues. Reload the AppDaemon app or restart AppDaemon after editing
the secret. Do not put the session in Git, chat, logs, or command arguments.

The configured session is not copied to a separate credential file. Server cookie
rotations are stored privately in `/data/mam/cookies.txt` (mode 0600, directory
0700). Changing the configured session resets those cookies on its next scheduled
poll. Authentication errors block further MAM requests with that credential.
Cooldowns and backoff survive restarts; deleting runtime state is not an
appropriate retry mechanism.

`input_number.mam_download_reserve_gib` controls the spare credit in GiB. The app
initializes a new zero-valued helper to 2 once and persists that initialization.
Subsequent changes, including zero, survive restarts. When the helper is unavailable,
the last known reserve is retained, with 2 as the initial fallback.

`input_number.mam_unsatisfied_limit_override` uses zero for automatic selection.
An API-provided limit takes precedence. Otherwise a positive override applies;
known class limits follow, with User conservatively limited to 20 because account
age is not documented. Unknown classes have an unavailable limit.

## Optional response fields

The supplied API documentation specifies the top-level `classname`, `uploaded`,
`downloaded`, `ratio`, and `seedbonus` fields. It names optional `clientStats`,
`notif`, and `snatch_summary` request flags but does not define their response
structures. The monitor requests these flags but does not guess their schemas.

`response_paths` maps the following normalized names to lists of verified JSON
object keys: `unsatisfied`, `limit`, `satisfied`, `seeding`, `leeching`, `connectable`,
and `notices`. Paths are empty by default. Count values must be nonnegative
integers; connectivity must be a JSON boolean; notices must be a list of plain
strings. Arbitrary nested objects, URLs, cookie values, and raw tracker messages
are not published. Verify the optional schema from an authenticated response
privately before populating these paths. Until then, affected sensors remain
unknown and account coverage explicitly reads `partial`.

No endpoint for tracker-confirmed per-torrent seed times, personal deadlines,
wedge balance, or VIP expiry is established by the supplied documentation.
The monitor does not scrape website pages to fill these gaps.

## Interpretation

Credited upload includes purchased credit. It is not qBittorrent's uploaded-byte
counter. MAM's formatted account totals are approximate. Download budget is
credited upload minus counted download, all locally outstanding MAM file bytes,
and the configurable reserve. Outstanding freeleech downloads are conservatively
counted because their status is unverified. Tracker reporting delay can make this
estimate optimistic after a transfer completes; it is not permission to download.

Every file must be complete, including skipped covers and supplemental files,
before local completed-seeding progress is displayed. Seeding hours come from
qBittorrent, not the tracker. Counter resets require verification. The estimated
30-day deadline uses the local added timestamp and can differ from MAM's snatch
time. Missing timestamps remain unknown. `stalledUP` with a working MAM tracker
is normal seeding; connectability is not evidence of seed-time credit.

Tracker matching uses exact domain boundaries across every torrent's tracker list,
independent of its category. Snapshot failure does not mark torrents deleted.
Disappeared torrents with an unresolved local obligation remain in durable state.
All observed torrents contribute to totals and checks; the attention-first detail
payload is bounded to 200 entries and explicitly reports truncation. Missing
entries require manual verification; the monitor cannot certify their satisfaction.

MAM data expires after 90 minutes and client data after 15 minutes. Authentication
failures invalidate account data immediately. MQTT last-will and 180-second entity
expiry also cover monitor failures. Retained discovery/state support HA and broker
restarts. `no_detected_issues` is not a compliance guarantee.

Notifications use `script.notify_will`, link to `/mam-monitor/overview`, debounce
transient client/connectability problems for 15 minutes, and deduplicate across
restarts. Critical unresolved issues repeat at most daily; fresh source data is
required before resolving its alerts. A consolidated notification carries at most
eight updates and links to remaining dashboard detail.

## Home Assistant configuration

The Home Assistant repository owns the two input-number helpers and Recorder
exclusions for `sensor.mam_details`, `sensor.mam_issues`, and `sensor.mam_notices`.
Scalar numerical entities retain history and appropriate statistics metadata.
The `mam-monitor` dashboard is a live storage-mode dashboard maintained through
Home Assistant APIs, not repository dashboard exports.

## Validation

```sh
PYTHONPATH=apps uv run --frozen python -m unittest discover -s tests -p 'test_mam*.py'
uv run --frozen basedpyright
prek run --all-files
```
