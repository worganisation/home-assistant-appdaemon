"""AppDaemon app to update AppDaemon to the latest release."""

from __future__ import annotations

from os import environ
from re import fullmatch
from typing import TYPE_CHECKING, ClassVar

from appdaemon.plugins.hass.hassapi import Hass
from git import GitCommandError, Repo
from git.exc import BadName

if TYPE_CHECKING:
    from appdaemon.entity import Entity

environ["GIT_SSH_COMMAND"] = (
    "ssh -o StrictHostKeyChecking=no -i /homeassistant/.ssh/github"
)


class Updater(Hass):
    """AppDaemon app to update AppDaemon to the latest release."""

    REPO: ClassVar[Repo] = Repo("/config")

    _current_branch: Entity
    _current_ref: Entity
    _last_runtime: tuple[str, int, int, tuple[str, ...]] | None = None
    _observed_sha: str
    _initialized_apps: set[str]

    def initialize(self) -> None:
        """Initialize the app."""
        self._current_branch = self.get_entity("var.current_appdaemon_branch")
        self._current_ref = self.get_entity("var.current_appdaemon_ref")

        self._observed_sha = self.REPO.head.commit.hexsha
        self._initialized_apps = set()
        self.listen_event(
            self.record_initialization,
            "app_initialized",
            namespace="admin",
        )

        self.listen_state(
            self.get_latest_release,
            "input_text.ad_get_latest_release",
        )

        self.run_every(self.update_variables, "immediate", 2 * 60)

    def update_variables(self, _: dict[str, object] | None = None) -> None:
        """Refresh Git entities and publish changes in observed release runtime counts.

        Args:
            _: Scheduler callback metadata.
        """
        try:
            current_branch = self.REPO.active_branch.name
        except TypeError:
            current_branch = "HEAD"

        try:
            current_ref = self.REPO.git.describe(tags=True, exact_match=True)
        except GitCommandError:
            current_ref = self.REPO.head.commit.hexsha[:7]

        self.current_branch = current_branch
        self.current_ref = current_ref

        if (
            current_branch == "HEAD"
            and fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", current_ref)
            and not self.REPO.is_dirty()
        ):
            self.report_runtime(current_ref)
        else:
            self._last_runtime = None

        self.log(
            "Current AppDaemon ref: %s | Current AppDaemon branch: %s",
            current_ref,
            current_branch,
        )

    def record_initialization(
        self,
        event_type: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> None:
        """Record AppDaemon's successful app initialization events at this checkout.

        Args:
            event_type: AppDaemon event name.
            data: Event payload containing the initialized app name.
            **kwargs: AppDaemon callback metadata.
        """
        del event_type, kwargs
        app = data.get("app")
        if isinstance(app, str) and self.REPO.head.commit.hexsha == self._observed_sha:
            self._initialized_apps.add(app)

    def report_runtime(self, tag: str) -> None:
        """Report the active and inactive app counts sampled at a release checkout.

        Args:
            tag: Exact release tag at the detached HEAD.
        """
        active = str(self.get_state("sensor.active_apps", namespace="admin"))
        inactive = str(self.get_state("sensor.inactive_apps", namespace="admin"))
        if not active.isdecimal() or not inactive.isdecimal():
            return

        sha = self.REPO.head.commit.hexsha
        if self._observed_sha != sha:
            self._observed_sha = sha
            self._initialized_apps.clear()

        sample = (sha, int(active), int(inactive), tuple(sorted(self._initialized_apps)))
        if sample == self._last_runtime:
            return

        response: object = self.call_service(
            "rest_command/report_release_checkout",
            return_response=True,
            appdaemon=True,
            tag=tag,
            sha=sample[0],
            status="app_runtime",
            active=sample[1],
            inactive=sample[2],
            initialized=list(sample[3]),
        )
        match response:
            case {"result": {"response": {"status": 204}}}:
                self._last_runtime = sample
            case _:
                self.error("GitHub did not acknowledge the runtime report for %s", tag)

    def get_latest_release(
        self,
        entity: str,
        attribute: str,
        old: object,
        new: object,
        **kwargs: object,
    ) -> None:
        """Check out the requested release and report the observed commit.

        Args:
            entity: Home Assistant helper that changed.
            attribute: Attribute that triggered the callback.
            old: Previous helper value.
            new: Requested release tag.
            **kwargs: AppDaemon callback metadata.

        Raises:
            GitCommandError: Fetch, stash or checkout failed.
            BadName: The requested tag does not exist.
            ValueError: The Git reference could not be resolved.
        """
        del entity, attribute, kwargs

        if (
            not old
            or old in ("unavailable", new)
            or not isinstance(new, str)
            or not fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", new)
        ):
            return

        try:
            self.REPO.git.fetch("--all", "--tags", "--prune")
            target = self.REPO.commit(f"refs/tags/{new}")
            self.REPO.git.stash(
                "push",
                "--include-untracked",
                "-m",
                f"Before release {new}",
            )
            self.REPO.git.checkout("--detach", target.hexsha)
            observed = self.REPO.head.commit.hexsha
        except (BadName, GitCommandError, ValueError):
            self.call_service(
                "rest_command/report_release_checkout",
                appdaemon=True,
                tag=new,
                status="failed",
            )
            raise

        self.call_service(
            "rest_command/report_release_checkout",
            appdaemon=True,
            tag=new,
            status="checked_out",
            sha=observed,
        )
        self.update_variables()

    @property
    def current_ref(self) -> str:
        """Get the current AppDaemon ref."""
        return str(self._current_ref.get_state())

    @current_ref.setter
    def current_ref(self, value: str, /) -> None:
        self.call_service(
            "var/set",
            entity_id=self._current_ref.entity_id,
            value=value,
            force_update=True,
        )

    @property
    def current_branch(self) -> str:
        """Get the current AppDaemon branch."""
        return str(self._current_branch.get_state())

    @current_branch.setter
    def current_branch(self, value: str, /) -> None:
        self.call_service(
            "var/set",
            entity_id=self._current_branch.entity_id,
            value=value,
            force_update=True,
        )
