# Repair catalogue

An AppDaemon app that describes Home Assistant repairs with a dedicated
AI Task. It never resolves repairs, dismisses issues, invokes suggested actions,
posts GitHub issues or sends notifications. Other repair automations operate independently.

## Configuration

The `repair_catalogue` entry in `apps/apps.yaml` starts the worker when loaded.
`ai_task_entity` identifies a dedicated **Repairs AI** task that uses the same
provider and model as `ai_task.habit_reminder_ai`, with independent settings.
`ai_config_revision` identifies the configured
provider/model settings for cache invalidation; increasing it invalidates cached
analyses. The app uses Home Assistant's AI task credentials.

The companion Home Assistant Recorder configuration excludes the catalogue sensors
and note draft, preventing generated text from accumulating in the history database.
The storage-mode Repairs dashboard at `/dashboard-repairs` consumes the MQTT
entities. Its live configuration is maintained through Home Assistant's dashboard
editor or MCP API; neither repository maintains a separate dashboard template.

The runtime uses the existing authenticated HASS plugin's
`websocket_send_json(timeout=30, silent=True, ...)` adapter from AppDaemon
**4.5.13**. Compatibility depends on this internal API's request and response
contract. The only allowed commands are repairs listing, entity registry listing,
HA configuration metadata, person configuration and Lovelace configuration reads.
It also reads installed English issue translations through `frontend/get_translations`.
Only each repair's title and description enter its context; unavailable translations
are disclosed and the user is directed to native Repairs rather than an invented flow.
AI requests use the established `ai_task/generate_data` service response pattern.

## Lifecycle and storage

SQLite lives at `/data/repairs/catalogue.sqlite3` in the AppDaemon App's persistent storage,
with owner-only database permissions. Backup coverage depends on inclusion of
AppDaemon's persistent data. The database is runtime data outside Git. Its two tables are:
`repairs` keeps lifecycle, current context, notes and latest analysis; `analyses`
keeps successful revisions with their input fingerprint and occurrence number.

The worker reconciles at startup, after repair/plugin events (five-second burst
debounce), and every 15 minutes. It publishes cached data on MQTT reconnect.
Only a successful full repair/context snapshot can mark absent repairs resolved.
Dismissed records remain distinct. Reappearing resolved repairs increment their
occurrence and trigger a new analysis. History on the dashboard is limited to 20
resolved records; the database retains all records and revisions.

The SHA-256 cache key covers repair metadata, bounded context, user notes,
occurrence, prompt version, AI entity and configured AI revision. Timestamps are
excluded. Entity context contains existence, integration and availability, never
personal location values. At most 30 referenced entities are inspected. Each
placeholder is bounded to 8,000 characters, with truncation disclosed. Configuration
context includes only matching structural entity references or person tracker IDs;
no action bodies, secrets, entire dashboards or bulk logs reach the model.

Entity context comes from entity/statistic placeholder fields, not arbitrary resource
paths. Typo suggestions remain separate from affected references. Model input excludes
cache hashes and samples large lists across their length with explicit totals and
omission counts. Statistics context is limited to eight entity checks and eight list
entries; the full list remains available in native Repairs. SQLite retains bounded
repair metadata independently of the compact model input.

AI runs serially with a 180-second timeout. Malformed output cannot replace a
saved analysis. Retry delays are one minute then five minutes; after the third
failure a user must request a retry. Before accepting output the worker re-fetches
repairs and context and rejects a stale fingerprint or a no-longer-active repair.
Opening the dashboard never triggers generation. Provider failures leave repair
metadata available and retain previous explanations with their generation status.

Repair-specific guidance distinguishes obsolete statistics and configuration references
from device failures. Output uses compact facts and fragments, without introductory
narration or generic disclaimers. The prompt targets one to three numbered actions;
validation accepts up to five to avoid rejecting useful longer procedures, without
a minimum sentence length or required final punctuation. Each field has a strict
character limit. Prompt version changes invalidate cached analyses.
Navigation and its action share a step. Optional fields use `None` when there is
nothing useful to display. The `involvement` field describes only physical actions
at a device; logins and UI actions belong in `steps`. The dashboard omits empty
optional sections and labels hands-on requirements **Physical actions**.
AI identity, prompt version and occurrence remain cache inputs, not model evidence.
Evidence is rendered directly from repair placeholders, without AI rewriting.
Long reference lists retain complete entries and disclose omitted details; statistics
include their total count. The model generates the other analysis fields.
Validation rejects empty/oversized fields, dangling conjunctions and malformed numbered
steps. These checks do not establish factual accuracy; rejected output follows the bounded retry policy
and cannot replace a saved analysis.

INFO logs record refresh duration, active/pending/failed counts, cache hits, generation
duration, attempt number, retry delay and stale-output rejection. A cache hit is an
active record with a ready analysis matching its current input fingerprint. Log
references are hashes of repair keys; prompts, notes and generated text are omitted.

## MQTT contract

Retained discovery under `homeassistant` creates `sensor.ai_repairs`,
`sensor.ai_repairs_history`, `select.ai_repairs_selected`, `text.ai_repairs_note`,
`button.ai_repairs_save_note` and `button.ai_repairs_reanalyse`.
Payloads live under `appdaemon/repairs`. Sensors contain a count plus `repairs`,
`last_sync` and `sync_error`. Each record contains `key`, `repair`, `analysis`,
`note`, `status`, `stale`, timestamps and occurrence number.

Non-retained commands go to `appdaemon/repairs/command/{select,note,save_note,reanalyse}`.
Select carries a repair key, note carries a draft up to 255 characters, and button
payloads are ignored. Note changes are saved only by the save button. Selection
changes discard unsaved drafts; controls are shared across users. Availability
uses a retained last will and clean shutdown message. Commands queued during
analysis are applied before accepting its result.

The original kitchen radiator instruction is seeded once, preserving dated
historical evidence. User edits are never overwritten by later reconciliation.

## Offline validation

```sh
uv run --frozen basedpyright
prek run --all-files
```

Validation uses the repository's type, formatting, lint and workflow checks.
Stopping the app preserves SQLite history and makes its MQTT entities unavailable.
The live dashboard has an independent lifecycle.
