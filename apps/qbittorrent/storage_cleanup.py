"""Delete eligible torrents when storage is full and restart errored torrents."""

from __future__ import annotations

from dataclasses import dataclass
from re import compile as compile_regex
from time import monotonic
from typing import TYPE_CHECKING, Any, Final

from appdaemon.plugins.hass.hassapi import Hass
from mam.model import is_mam_tracker
from requests import RequestException, Response, Session

if TYPE_CHECKING:
    from collections.abc import Callable

BYTES_PER_UNIT: Final = 1024
SECONDS_PER_DAY: Final = 24 * 60 * 60
ACTION_PREFIX: Final = "DELETE_QBT_TORRENT_"
TORRENT_HASH_PATTERN: Final = compile_regex(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class QbittorrentError(RuntimeError):
    """Raised when qBittorrent cannot complete an API operation."""


@dataclass(frozen=True, slots=True)
class TorrentCandidate:
    """A seeding torrent and its progress towards cleanup ranking targets."""

    hash: str
    name: str
    size: int
    ratio: float
    ratio_target: float
    upload_speed: int
    seeding_seconds: int
    time_target_seconds: float
    closeness: float
    deletion_score: float
    closest_target: str

    @property
    def size_formatted(self) -> str:
        """Return the torrent size using IEC units."""
        amount = float(self.size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if amount < BYTES_PER_UNIT or unit == "TiB":
                precision = 0 if unit == "B" else 1
                return f"{amount:.{precision}f} {unit}"
            amount /= BYTES_PER_UNIT
        return f"{self.size} B"

    @property
    def target_summary(self) -> str:
        """Describe the ranking target that puts the torrent highest."""
        progress = f"{self.closeness * 100:.1f}%"
        if self.closest_target == "ratio":
            return f"{progress} of the {self.ratio_target:g} ratio ranking target"
        days = self.time_target_seconds / SECONDS_PER_DAY
        return f"{progress} of the {days:g}-day seeding ranking target"

    @property
    def is_uploading(self) -> bool:
        """Return whether qBittorrent reports active upload traffic."""
        return self.upload_speed > 0


class QbittorrentWebApi:
    """Small authenticated client for the qBittorrent Web API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        ratio_progress_weight: float,
        ratio_target: float,
        seeding_days_target: float,
        timeout: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.ratio_progress_weight = ratio_progress_weight
        self.ratio_target = ratio_target
        self.seeding_days_target = seeding_days_target
        self.timeout = timeout

    def ranked_seeders(self) -> list[TorrentCandidate]:
        """Return eligible seeders after checking every configured tracker."""
        with self._authenticated_session() as session:
            torrents = self._json_list(
                self._request(
                    session,
                    "GET",
                    "/api/v2/torrents/info",
                    params={"filter": "seeding"},
                ),
            )
            ranked = rank_seeders(
                torrents,
                ratio_progress_weight=self.ratio_progress_weight,
                ratio_target=self.ratio_target,
                seeding_days_target=self.seeding_days_target,
            )
            deadline = monotonic() + 120
            eligible: list[TorrentCandidate] = []
            for candidate in ranked:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise QbittorrentError("qBittorrent tracker check timed out")
                trackers = self._json_list(
                    self._request(
                        session,
                        "GET",
                        "/api/v2/torrents/trackers",
                        params={"hash": candidate.hash},
                        timeout=min(self.timeout, remaining),
                    ),
                )
                if not any(is_mam_tracker(item.get("url")) for item in trackers):
                    eligible.append(candidate)
            return eligible

    def delete_with_files(
        self,
        torrent_hash: str,
        *,
        can_delete: Callable[[], bool],
    ) -> bool:
        """Recheck Home Assistant state after login before deleting exact content."""
        with self._authenticated_session() as session:
            if not can_delete():
                return False
            self._request(
                session,
                "POST",
                "/api/v2/torrents/delete",
                data={"hashes": torrent_hash, "deleteFiles": "true"},
            )
        return True

    def restart_errored(self) -> list[str]:
        """Start torrents currently reported in qBittorrent's errored filter."""
        with self._authenticated_session() as session:
            torrents = self._json_list(
                self._request(
                    session,
                    "GET",
                    "/api/v2/torrents/info",
                    params={"filter": "errored"},
                ),
            )
            errored = [
                torrent
                for torrent in torrents
                if TORRENT_HASH_PATTERN.fullmatch(
                    str(torrent.get("hash", "")).lower(),
                )
            ]
            if not errored:
                return []

            hashes = "|".join(str(torrent["hash"]).lower() for torrent in errored)
            self._request(
                session,
                "POST",
                "/api/v2/torrents/start",
                data={"hashes": hashes},
            )

        return [str(torrent.get("name", "Unknown torrent")) for torrent in errored]

    def _authenticated_session(self) -> Session:
        session = Session()
        session.headers.update({"Referer": f"{self.base_url}/"})
        try:
            response = self._request(
                session,
                "POST",
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
            )
        except Exception:
            session.close()
            raise
        if response.text.strip() != "Ok.":
            session.close()
            raise QbittorrentError("qBittorrent rejected the configured credentials")
        return session

    def _request(
        self,
        session: Session,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Response:
        try:
            response = session.request(
                method,
                f"{self.base_url}{path}",
                timeout=kwargs.pop("timeout", self.timeout),
                **kwargs,
            )
            response.raise_for_status()
        except RequestException as error:
            raise QbittorrentError(f"qBittorrent API request failed: {error}") from error
        return response

    @staticmethod
    def _json_list(response: Response) -> list[dict[str, Any]]:
        try:
            payload = response.json()
        except ValueError as error:
            raise QbittorrentError("qBittorrent returned invalid JSON") from error
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise QbittorrentError("qBittorrent torrents response was not a list")
        return payload


def _as_float(value: Any, default: float = 0.0) -> float:
    """Convert an API value to a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    """Convert an API value to an integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def rank_seeders(
    torrents: list[dict[str, Any]],
    *,
    ratio_progress_weight: float = 1.0,
    ratio_target: float = 5.0,
    seeding_days_target: float = 28.0,
) -> list[TorrentCandidate]:
    """Rank safe seeders by ratio and seeding time, independent of client limits."""
    time_target_seconds = seeding_days_target * SECONDS_PER_DAY

    ranked: list[TorrentCandidate] = []
    for torrent in torrents:
        torrent_hash = str(torrent.get("hash", "")).lower()
        if not TORRENT_HASH_PATTERN.fullmatch(torrent_hash):
            continue
        if str(torrent.get("category", "")).lower() == "shelfarr":
            continue
        ratio = max(_as_float(torrent.get("ratio")), 0.0)
        seeding_seconds = max(_as_int(torrent.get("seeding_time")), 0)
        ratio_progress = ratio / ratio_target
        time_progress = seeding_seconds / time_target_seconds
        weighted_ratio_progress = ratio_progress * ratio_progress_weight
        ratio_is_closer = weighted_ratio_progress >= time_progress
        closest_target = "ratio" if ratio_is_closer else "time"
        ranked.append(
            TorrentCandidate(
                hash=torrent_hash,
                name=str(torrent.get("name", "Unknown torrent")),
                size=max(
                    _as_int(torrent.get("size", torrent.get("total_size", 0))),
                    0,
                ),
                ratio=ratio,
                ratio_target=ratio_target,
                upload_speed=max(_as_int(torrent.get("upspeed")), 0),
                seeding_seconds=seeding_seconds,
                time_target_seconds=time_target_seconds,
                closeness=ratio_progress if ratio_is_closer else time_progress,
                deletion_score=max(weighted_ratio_progress, time_progress),
                closest_target=closest_target,
            ),
        )

    return sorted(
        ranked,
        key=lambda item: (item.deletion_score, item.seeding_seconds, item.ratio),
        reverse=True,
    )


class QbittorrentStorageCleanup(Hass):
    """Remove one safe seeder at a time when scratch storage is full."""

    def initialize(self) -> None:
        """Register storage and notification-action listeners."""
        self.storage_entity = str(self.args["storage_entity"])
        self.threshold_entity = str(self.args.get("threshold_entity", ""))
        self.auto_mode_entity = str(self.args["auto_mode_entity"])
        self.default_threshold = float(self.args.get("threshold", 99.9))
        self.reset_below = float(
            self.args.get("reset_below", self.default_threshold),
        )
        self.post_delete_check_delay = max(
            float(self.args.get("post_delete_check_delay", 90)),
            1.0,
        )
        self.notify_script = str(self.args.get("notify_script", "script.notify_will"))
        self.notification_id = str(
            self.args.get("notification_id", "qbt_storage_cleanup"),
        )
        self.notification_url = str(self.args.get("notification_url", ""))
        self.client = QbittorrentWebApi(
            str(self.args["qbittorrent_url"]),
            str(self.args["qbittorrent_username"]),
            str(self.args["qbittorrent_password"]),
            ratio_progress_weight=max(
                float(self.args.get("ratio_progress_weight", 1.0)),
                0.0,
            ),
            ratio_target=max(float(self.args.get("ratio_target", 5.0)), 0.01),
            seeding_days_target=max(
                float(self.args.get("seeding_days_target", 28.0)),
                0.01,
            ),
            timeout=float(self.args.get("request_timeout", 15)),
        )
        self._threshold_active = False
        self._post_delete_check_pending = False

        self.listen_state(self._storage_changed, self.storage_entity)
        if self.threshold_entity:
            self.listen_state(self._threshold_changed, self.threshold_entity)
        self.listen_state(self._auto_mode_changed, self.auto_mode_entity)
        self.listen_event(
            self._notification_action,
            "mobile_app_notification_action",
        )
        self.run_in(self._startup_check, 1)

    def _startup_check(self, _kwargs: dict[str, Any]) -> None:
        """Offer cleanup after reload when storage is already over the threshold."""
        usage = self._usage(self.get_state(self.storage_entity))
        if usage is not None and usage >= self._current_threshold():
            self._threshold_active = True
            self._offer_cleanup(usage)

    def _storage_changed(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        **kwargs: Any,
    ) -> None:
        """Offer cleanup when storage crosses the configured threshold."""
        del entity, attribute, kwargs
        old_usage = self._usage(old)
        new_usage = self._usage(new)
        if new_usage is None:
            return

        if self._post_delete_check_pending:
            return

        threshold = self._current_threshold()
        if new_usage < min(self.reset_below, threshold):
            if self._threshold_active:
                self._clear_notification()
                self._restart_errored_torrents()
            self._threshold_active = False
            return

        crossed_threshold = new_usage >= threshold and (
            old_usage is None or old_usage < threshold
        )
        if crossed_threshold and not self._threshold_active:
            self._threshold_active = True
            self._offer_cleanup(new_usage)

    def _threshold_changed(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        **kwargs: Any,
    ) -> None:
        """Offer cleanup when a changed threshold is already met by current usage."""
        del entity, attribute, kwargs
        old_threshold = self._usage(old)
        new_threshold = self._usage(new)
        usage = self._usage(self.get_state(self.storage_entity))
        if (
            new_threshold is None
            or new_threshold == old_threshold
            or usage is None
            or self._post_delete_check_pending
        ):
            return

        if usage >= new_threshold:
            self._threshold_active = True
            self._offer_cleanup(usage)
            return

        if usage < min(self.reset_below, new_threshold):
            if self._threshold_active:
                self._clear_notification()
                self._restart_errored_torrents()
            self._threshold_active = False
            return

        self._threshold_active = True

    def _offer_cleanup(self, usage: float) -> None:
        """Find the highest ranked torrent and request or perform deletion."""
        try:
            ranked = self.client.ranked_seeders()
        except QbittorrentError as error:
            self.error("Unable to rank qBittorrent seeders: %s", error)
            self._notify(
                title="qBittorrent cleanup unavailable",
                message=f"Storage is at {usage:.1f}%, but qBittorrent could not be queried.",
                icon="mdi:harddisk-alert",
            )
            return

        if not ranked:
            self._notify(
                title="qBittorrent storage full",
                message=(
                    f"Storage is at {usage:.1f}%, but there are no seeding torrents "
                    "eligible for cleanup."
                ),
                icon="mdi:harddisk-alert",
            )
            return

        candidate = next((item for item in ranked if not item.is_uploading), None)
        if candidate is None:
            self._notify(
                title="qBittorrent storage full",
                message=(
                    f"Storage is at {usage:.1f}%, but every eligible seeding torrent "
                    "is currently uploading. Nothing will be deleted."
                ),
                icon="mdi:upload-network",
            )
            return

        if self._auto_mode():
            self._delete_candidate(candidate.hash, automatic=True)
            return

        self._notify(
            title="Delete qBittorrent torrent?",
            message=(
                f"Storage is at {usage:.1f}%. Delete the highest ranked seeding "
                "torrent?\n\n"
                f"{candidate.name}\n"
                f"Size: {candidate.size_formatted}\n"
                f"Ratio: {candidate.ratio:.2f}\n"
                f"Ranking: {candidate.target_summary}"
            ),
            icon="mdi:harddisk-remove",
            actions=[
                {
                    "action": f"{ACTION_PREFIX}{candidate.hash}",
                    "title": "Delete torrent",
                },
            ],
        )
        self.log(
            "Offered deletion of %s (%s, ratio %.2f, %.1f%% towards %s target)",
            candidate.name,
            candidate.size_formatted,
            candidate.ratio,
            candidate.closeness * 100,
            candidate.closest_target,
        )

    def _auto_mode(self) -> bool:
        """Enable unattended deletion only for an explicit helper on state."""
        return self.get_state(self.auto_mode_entity) == "on"

    def _auto_mode_changed(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        **kwargs: Any,
    ) -> None:
        """Apply a mode change to an existing full-storage condition."""
        del entity, attribute, kwargs
        if old == new or self._post_delete_check_pending:
            return
        usage = self._usage(self.get_state(self.storage_entity))
        if usage is not None and usage >= self._current_threshold():
            self._offer_cleanup(usage)

    def _notification_action(
        self,
        event_type: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Delete the exact seeding torrent confirmed in the notification."""
        del event_type, kwargs
        action = data.get("action")
        if not isinstance(action, str) or not action.startswith(ACTION_PREFIX):
            return

        torrent_hash = action.removeprefix(ACTION_PREFIX).lower()
        if not TORRENT_HASH_PATTERN.fullmatch(torrent_hash):
            self.error("Ignored malformed qBittorrent deletion action")
            return

        self._delete_candidate(torrent_hash, automatic=False)

    def _delete_candidate(self, torrent_hash: str, *, automatic: bool) -> None:
        """Recheck storage, mode and eligibility before deleting one torrent."""
        if not self._ready_to_delete(automatic=automatic):
            return

        try:
            ranked = self.client.ranked_seeders()
        except QbittorrentError as error:
            self.error("Unable to delete qBittorrent torrent: %s", error)
            self._notify(
                title="Torrent was not deleted",
                message=f"qBittorrent reported: {error}",
                icon="mdi:delete-alert",
            )
            return

        if automatic:
            candidate = next((item for item in ranked if not item.is_uploading), None)
        else:
            candidate = next((item for item in ranked if item.hash == torrent_hash), None)

        if candidate is None:
            error = "the selected torrent is no longer in the eligible seeding list"
            self.error("Unable to delete qBittorrent torrent: %s", error)
            self._notify(
                title="Torrent was not deleted",
                message=f"qBittorrent reported: {error}",
                icon="mdi:delete-alert",
            )
            return

        if candidate.is_uploading:
            self.log(
                "Refused deletion of %s because it is now uploading at %i B/s",
                candidate.name,
                candidate.upload_speed,
            )
            self._notify(
                title="Torrent was not deleted",
                message=(f"{candidate.name} is uploading, so it has been left alone."),
                icon="mdi:upload-network",
            )
            return

        try:
            deleted = self.client.delete_with_files(
                candidate.hash,
                can_delete=lambda: self._ready_to_delete(automatic=automatic),
            )
        except QbittorrentError as error:
            self.error("Unable to delete qBittorrent torrent: %s", error)
            self._notify(
                title="Torrent was not deleted",
                message=f"qBittorrent reported: {error}",
                icon="mdi:delete-alert",
            )
            return

        if not deleted:
            return

        self._notify(
            title="Torrent deleted",
            message=f"Deleted {candidate.name} and its {candidate.size_formatted} of content.",
            icon="mdi:delete-check",
            persistent=False,
            sticky=False,
        )
        self.log(
            "Deleted %s torrent %s (%s)",
            "automatic" if automatic else "confirmed",
            candidate.name,
            candidate.hash,
        )
        self._threshold_active = False
        self._post_delete_check_pending = True
        self.run_in(self._post_delete_check, self.post_delete_check_delay)

    def _ready_to_delete(self, *, automatic: bool) -> bool:
        """Reject stale, below-threshold or disabled automatic cleanup."""
        if self._post_delete_check_pending:
            self.log("Ignored qBittorrent deletion while waiting for storage refresh")
            return False
        usage = self._usage(self.get_state(self.storage_entity))
        if usage is None or usage < self._current_threshold():
            self.log("Ignored qBittorrent deletion because storage is below threshold")
            self._clear_notification()
            self._threshold_active = False
            return False
        if automatic and not self._auto_mode():
            self.log("Ignored automatic qBittorrent deletion because auto mode is off")
            return False
        return True

    def _post_delete_check(self, _kwargs: dict[str, Any]) -> None:
        """Restart errored torrents or offer another deletion after refresh."""
        self._post_delete_check_pending = False
        usage = self._usage(self.get_state(self.storage_entity))
        if usage is None:
            return
        self._restart_errored_torrents()
        threshold = self._current_threshold()
        if usage >= threshold:
            self._threshold_active = True
            self.log(
                "Storage remains at %.1f%% after deletion; offering another torrent",
                usage,
            )
            self._offer_cleanup(usage)
            return
        self._threshold_active = False

    def _restart_errored_torrents(self) -> None:
        """Restart errored torrents after deleting scratch-storage content."""
        try:
            restarted = self.client.restart_errored()
        except QbittorrentError as error:
            self.error("Unable to restart errored qBittorrent torrents: %s", error)
            self._notify(
                title="Torrents were not restarted",
                message=f"qBittorrent reported: {error}",
                icon="mdi:restart-alert",
                persistent=False,
                sticky=False,
            )
            return

        if restarted:
            self.log(
                "Restarted %i errored qBittorrent torrent(s): %s",
                len(restarted),
                ", ".join(restarted),
            )

    def _current_threshold(self) -> float:
        """Return the helper value, falling back to the YAML threshold."""
        if not self.threshold_entity:
            return self.default_threshold
        threshold = self._usage(self.get_state(self.threshold_entity))
        return self.default_threshold if threshold is None else threshold

    def _notify(
        self,
        *,
        title: str,
        message: str,
        icon: str,
        actions: list[dict[str, Any]] | None = None,
        persistent: bool = True,
        sticky: bool = True,
    ) -> None:
        """Send a notification through the shared Will notification script."""
        variables: dict[str, Any] = {
            "title": title,
            "message": message,
            "notification_id": self.notification_id,
            "mobile_notification_icon": icon,
            "sticky": sticky,
            "persistent": persistent,
            # Keep this as a native list: script.notify_will forwards it directly
            # to the companion app's `data.actions` notification field.
            "actions": actions or [],
        }
        if self.notification_url:
            variables["url"] = self.notification_url
        self.call_service(
            "script/turn_on",
            entity_id=self.notify_script,
            variables=variables,
        )

    def _clear_notification(self) -> None:
        """Clear the cleanup prompt once storage has fallen sufficiently."""
        self.call_service(
            "script/turn_on",
            entity_id=self.notify_script,
            variables={
                "clear_notification": True,
                "notification_id": self.notification_id,
                "message": "clear_notification",
            },
        )

    @staticmethod
    def _usage(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
