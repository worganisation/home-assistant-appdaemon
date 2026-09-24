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

## Tracker response fields

`response_paths` maps verified `jsonLoad.php` fields. Unsatisfied count and limit
come from `unsat.count` and `unsat.limit`; satisfied totals combine `sSat` and
`inactSat`. Seeding totals combine `sSat`, `seedUnsat`, `seedHnr`, and `upAct`.
Hit-and-run totals combine `seedHnr` and `inactHnr`. Nested lists of paths sum
these disjoint categories only when every count is valid. Missing data remains
unknown rather than silently becoming zero. Category `red` flags are not counts
and do not create alerts on their own.

Connectivity accepts JSON booleans or MAM's `yes`/`no` strings. Notification
counters are restricted to private messages, clients about to be dropped,
tickets, waiting tickets, requests, and topics; they appear as counts and labels,
never message contents. The notices entity also exposes the counter breakdown.
The API's `wedges` balance is tracked without spending any wedges.

Exact `uploaded_bytes` and `downloaded_bytes` take precedence over formatted
values. Tracker-confirmed per-torrent seed times, personal deadlines and VIP
expiry remain unavailable; the monitor does not scrape website pages.

## Interpretation

Credited upload includes purchased credit. It is not qBittorrent's uploaded-byte
counter. Formatted account totals are an approximate fallback when exact bytes are absent. Download budget is
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
restarts. Missing, invalid, or rejected MAM sessions notify immediately on detection,
without waiting for the 90-minute stale-data threshold. The alert explains session
replacement, the `mam_monitor_mam_id` secret, and the AppDaemon reload requirement;
it repeats neither the credential nor raw API responses. Unchanged session failures
stay quiet, and a successful fresh account poll sends an explicit recovery notification.
Transport and other source failures retain the stale-data alert and polling backoff.
Site-notice alerts name each notification type, count and where to
review it on MAM; they update when that summary changes and stay quiet while
unchanged. Cleared notices are reported explicitly. No private-message or ticket
contents are fetched. Critical unresolved issues repeat at most daily; fresh source data is
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
