# Hammersmith commute

`commute.app.Commute` owns commute session transitions, TfL requests, train selection, notification scheduling and timing learning. Home Assistant owns the shower detector, office-day/calendar gate, GPS station detection, notification rendering and phone delivery. The input entity IDs and timings are fixed to Will's existing commute.

## State and ownership

`var.will_commute_session` is the sole durable store for the session and the latest ten qualifying total/walking samples. The app restores it through the HASS plugin and writes each transition and its samples together using `var.set`. It accepts a restored session without `started_at` by using its recorded completion timestamp. It never infers a completed shower for learning. The existing `will_shower_completed` event carries confirmed completion; `mobile_app_notification_cleared` with tag `will_commute` dismisses the day's session.

The app requires `var.will_commute_plan` and waits while any legacy commute session/feed/notification automation is enabled. This protects against duplicate writers during independently deployed repository checkouts. The existing delivery toggle controls notification submission only; sessions, learning and active-session feed collection continue while it is off.

Session timestamps and samples survive app and Home Assistant restarts. Feed data is ephemeral: activation clears departures and the visible tag, then an active restored session requests fresh data. App shutdown clears its notification when Home Assistant is reachable. Missing or invalid durable state does not get replaced with an empty session. Companion entity IDs, including `sensor.will_commute_next_departure` and `sensor.will_commute_status`, remain available to dashboards; stale plans report AppDaemon unavailability rather than a fresh countdown.

## Behaviour

A confidently attributed shower at home starts one session per office morning, between 06:00 and noon Europe/London. Leaving home provides the existing fallback for a shower attributed within 45 minutes. Confirmed completion refines the estimate; without completion, preparation timing remains provisional and no timing samples are learned. Home departure, station entry, office arrival, cancellation, dismissal and expiry follow the existing sequence and bounds. Total timing uses the median after three samples, otherwise 20 minutes. Walking uses the clamped median plus 30 seconds of location uncertainty, otherwise five minutes; the station buffer is one minute.

Session checks run every 15 seconds. Departures and notification delivery refresh independently every 30 seconds, with immediate updates on session changes and at the target train's departure time. HTTP work never holds the session lock. Status refreshes once a minute; timetables are cached per session/local date and failed timetable requests retry at most once a minute. Each request has a ten-second timeout. Overlapping refreshes are skipped, and late results cannot publish into an ended or different session.

`feed.py` only accepts unambiguous day schedules and routes through Goldhawk Road and Paddington H&C. Live estimates require a fresh Goldhawk prediction explicitly reporting a train at a Hammersmith platform, an eligible destination and one applicable interval. Good service or minor delays permit estimates; only good service permits timetable fallback. Two-minute source/status freshness, ten-row cache bounds, the three-train display limit and the estimated/scheduled distinction are preserved. `script.set_will_commute_departures` and `will_commute_departures_updated` remain the HA snapshot input interface. `script.render_will_commute_notification` retains its send-free test preview.

## Diagnostics

The app logs failed TfL requests, failed HA writes and notification submission errors. `var.will_commute_feed` exposes the ephemeral timetable/status cache. `var.will_commute_plan` contains the current selection, generated timestamp and explanatory status; Home Assistant marks plans stale after 45 seconds without an update. A successful notification service call confirms submission, not handset delivery. No new credentials or runtime packages are required.
