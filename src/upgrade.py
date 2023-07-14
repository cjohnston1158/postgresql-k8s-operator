# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Upgrades implementation."""
import logging

from charms.data_platform_libs.v0.upgrade import (
    ClusterNotReadyError,
    DataUpgrade,
    DependencyModel,
    UpgradeFinishedEvent,
)
from lightkube.core.client import Client
from lightkube.core.exceptions import ApiError
from lightkube.resources.apps_v1 import StatefulSet
from ops.charm import ActionEvent, RelationChangedEvent, WorkloadEvent
from pydantic import BaseModel
from typing_extensions import override

from patroni import SwitchoverFailedError

logger = logging.getLogger(__name__)


class PostgreSQLDependencyModel(BaseModel):
    """PostgreSQL dependencies model."""

    charm: DependencyModel


class PostgreSQLUpgrade(DataUpgrade):
    """PostgreSQL upgrade class."""

    def __init__(self, charm, model: BaseModel, **kwargs) -> None:
        """Initialize the class."""
        super().__init__(charm, model, **kwargs)
        self.charm = charm

        self.framework.observe(self.charm.on.upgrade_relation_changed, self._on_upgrade_changed)
        self.framework.observe(
            getattr(self.charm.on, "postgresql_pebble_ready"), self._postgresql_on_pebble_ready
        )
        self.framework.observe(
            getattr(self.charm.on, "resume_upgrade_action"), self._on_resume_upgrade
        )

    def _get_rolling_update_partition(self) -> int:
        client = Client(namespace=self.charm.model.name)
        stateful_set = client.get(StatefulSet, name=self.charm.model.app.name)
        return stateful_set.spec.updateStrategy.rollingUpdate.partition

    @property
    def is_no_sync_member(self) -> bool:
        """Whether this member shouldn't be a synchronous standby (when it's a replica)."""
        min_ordinal_sync_standbys = int(
            self.peer_relation.data[self.charm.app].get("min-ordinal-sync-standbys", 0)
        )
        return int(self.charm.unit.name.split("/")[1]) < min_ordinal_sync_standbys

    @override
    def log_rollback_instructions(self) -> None:
        """Log rollback instructions."""
        logger.info("Run `juju refresh --revision <previous-revision> postgresql-k8s` to rollback")
        logger.info(
            "and `juju run-action postgresql-k8s/leader resume-upgrade` to finish the rollback"
        )

    def _postgresql_on_pebble_ready(self, event: WorkloadEvent) -> None:
        """Handle pebble ready event.

        Confirm that unit is healthy and set unit completed.
        """
        if not self.peer_relation:
            logger.error("Deferring on_pebble_ready: no upgrade peer relation yet")
            event.defer()
            return

        if self.peer_relation.data[self.charm.unit].get("state") != "upgrading":
            return

        if not self.charm._patroni.member_started:
            logger.error("Deferring on_pebble_ready: Patroni has not started yet")
            event.defer()
            return

        # make the last unit as the single sync_standby.
        primary_unit_name = self.charm._patroni.get_primary(unit_name_pattern=True)
        logger.error(f"primary_unit_name: {primary_unit_name}")
        unit_zero_name = f"{self.charm.app.name}/0"
        sync_standby_names = self.charm._patroni.get_sync_standby_names()
        logger.error(f"sync_standby_names: {sync_standby_names}")
        if (
            self.charm.unit.name != unit_zero_name
            and primary_unit_name != unit_zero_name
            and len(sync_standby_names) > 0
            and sync_standby_names[0] != self.charm.unit.name
        ):
            self._set_min_ordinal_sync_standbys()
            logger.error(
                "Deferring on_pebble_ready: this unit is not the only synchronous standby yet"
            )
            event.defer()
            return

        self.set_unit_completed()

    def _on_resume_upgrade(self, event: ActionEvent) -> None:
        """Handle resume upgrade action.

        Continue the upgrade by setting the partition to the next unit.
        """
        fail_message = "Nothing to resume, upgrade stack unset"
        try:
            next_partition = self._get_rolling_update_partition() - 1
            if next_partition >= 0:
                self._set_rolling_update_partition(partition=next_partition)
                event.set_results({"message": f"Upgrade will resume on unit {next_partition}"})
                return
        except ApiError:
            fail_message = "Cannot set rolling update partition"
        event.fail(fail_message)

    def _on_upgrade_changed(self, event: RelationChangedEvent) -> None:
        if not self.peer_relation:
            event.defer()
            return

        self._set_min_ordinal_sync_standbys()
        self.charm.update_config()

    @override
    def _on_upgrade_finished(self, event: UpgradeFinishedEvent) -> None:
        logger.error(f"finished for {self.charm.unit.name}")
        if self.charm.unit.name != f"{self.charm.app.name}/{self.charm.app.planned_units() - 1}":
            try:
                logger.debug("Set rolling update partition to next unit")
                next_partition = self._get_rolling_update_partition() - 1
                if next_partition >= 0:
                    self._set_rolling_update_partition(partition=next_partition)
            except ApiError:
                logger.exception("Cannot set rolling update partition")
                self.set_unit_failed()
                self.log_rollback_instructions()

    @override
    def pre_upgrade_check(self) -> None:
        """Runs necessary checks validating the cluster is in a healthy state to upgrade.

        Called by all units during :meth:`_on_pre_upgrade_check_action`.

        Raises:
            :class:`ClusterNotReadyError`: if cluster is not ready to upgrade
        """
        if not self.charm.is_cluster_initialised:
            message = "cluster has not initialised yet"
            raise ClusterNotReadyError(message, message)

        # check for backups running.

        # check for tools in relation, like pgbouncer, being upgraded first?

        primary_unit_name = self.charm._patroni.get_primary(unit_name_pattern=True)
        unit_zero_name = f"{self.charm.app.name}/0"
        if primary_unit_name == unit_zero_name:
            self._set_first_rolling_update_partition()
            return

        sync_standby_names = self.charm._patroni.get_sync_standby_names()
        if len(sync_standby_names) == 0:
            raise ClusterNotReadyError("invalid number of sync nodes", "no action!")

        if unit_zero_name in sync_standby_names:
            try:
                self.charm._patroni.switchover(unit_zero_name)
            except SwitchoverFailedError as e:
                raise ClusterNotReadyError(
                    str(e), f"try to switchover manually to {unit_zero_name}"
                )
            self._set_first_rolling_update_partition()
            return

        # if last unit is the primary, switchover to any of the sync_standbys.
        if primary_unit_name == f"{self.charm.app.name}/{self.charm.app.planned_units()-1}":
            try:
                self.charm._patroni.switchover()
            except SwitchoverFailedError as e:
                raise ClusterNotReadyError(
                    str(e), "try to manually switchover to any synchronous standby"
                )
            self._set_first_rolling_update_partition()
            return

        self._set_first_rolling_update_partition()

    def _set_min_ordinal_sync_standbys(self) -> None:
        if not self.charm.is_cluster_initialised or not self.charm.unit.is_leader():
            return

        primary_unit_name = self.charm._patroni.get_primary(unit_name_pattern=True)
        unit_zero_name = f"{self.charm.app.name}/0"

        if self.charm.app.planned_units() > 2 and primary_unit_name != unit_zero_name:
            min_ordinal_sync_standbys = self._get_rolling_update_partition()
            self.peer_relation.data[self.charm.app].update(
                {"min-ordinal-sync-standbys": str(min_ordinal_sync_standbys)}
            )

    def _set_first_rolling_update_partition(self) -> None:
        self._set_rolling_update_partition(self.charm.app.planned_units() - 1)

    def _set_rolling_update_partition(self, partition: int) -> None:
        logger.info(f"partition: {partition}")
        client = Client()
        patch = {"spec": {"updateStrategy": {"rollingUpdate": {"partition": partition}}}}
        client.patch(
            StatefulSet, name=self.charm.model.app.name, namespace=self.charm.model.name, obj=patch
        )
