"""AppDaemon app to update AppDaemon to the latest release."""

from __future__ import annotations

from os import environ
from re import fullmatch
from typing import TYPE_CHECKING, Any, ClassVar

from appdaemon.plugins.hass.hassapi import Hass
from git import GitCommandError, Repo

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

    def initialize(self) -> None:
        """Initialize the app."""
        self._current_branch = self.get_entity("var.current_appdaemon_branch")
        self._current_ref = self.get_entity("var.current_appdaemon_ref")

        self.listen_state(
            self.get_latest_release,
            "input_text.ad_get_latest_release",
        )

        self.run_every(self.update_variables, "immediate", 2 * 60)

    def update_variables(self, _: dict[str, Any] | None = None) -> None:
        """Update the variables with the current AppDaemon ref and branch."""
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

        self.log(
            "Current AppDaemon ref: %s | Current AppDaemon branch: %s",
            current_ref,
            current_branch,
        )

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
            ValueError: The requested tag could not be resolved.
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
        except (GitCommandError, ValueError):
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
