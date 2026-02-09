from __future__ import annotations

from pathlib import Path
import sys
import unittest

# Ensure package imports work when tests are discovered as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor_runners.core import AlertPlanner, HostObservation, HostStateMachine, HostTransition
from monitor_runners.models import LastNotification, RunnerState, RunnerStatus, SampleState


class HostStateMachineTests(unittest.TestCase):
    """Unit tests for host-level transition decisions."""

    def test_missing_twice_transitions_to_absent_once(self) -> None:
        """Host should become absent only on the second consecutive missing run.

        Scenario:
        - Start from an online host.
        - Apply three consecutive observations where the host is missing.

        Expected behavior:
        - First miss: status becomes offline with consecutive_missing=1.
        - Second miss: status becomes absent and `became_absent` is true.
        - Third miss: still absent, but `became_absent` is false (no re-entry event).
        """
        machine = HostStateMachine()
        obs_missing = HostObservation(present=False, online=False, busy=False, labels=[])

        first = machine.apply(
            "hoskinson1",
            RunnerState(
                status=RunnerStatus.ONLINE,
                consecutive_offline=0,
                consecutive_missing=0,
                labels="self-hosted,Linux,X64,pr,bors",
            ),
            obs_missing,
        )
        self.assertEqual(first.new_state.status, RunnerStatus.OFFLINE)
        self.assertEqual(first.new_state.consecutive_missing, 1)
        self.assertFalse(first.became_absent)
        self.assertEqual(first.sample_state, SampleState.OFFLINE)

        second = machine.apply("hoskinson1", first.new_state, obs_missing)
        self.assertEqual(second.new_state.status, RunnerStatus.ABSENT)
        self.assertEqual(second.new_state.consecutive_missing, 2)
        self.assertTrue(second.became_absent)

        third = machine.apply("hoskinson1", second.new_state, obs_missing)
        self.assertEqual(third.new_state.status, RunnerStatus.ABSENT)
        self.assertEqual(third.new_state.consecutive_missing, 3)
        self.assertFalse(third.became_absent)

    def test_reappearing_after_absent_marks_newly_present(self) -> None:
        """Reappearance after an absent streak should emit newly-present signal.

        Scenario:
        - Previous state is absent with consecutive_missing >= 2.
        - Next observation shows the host present and online.

        Expected behavior:
        - Transition marks `became_newly_present`.
        - Missing counter resets to 0.
        - Host status becomes online and sample state is Idle.
        - Previous labels are retained when observation labels are empty.
        """
        machine = HostStateMachine()
        previous = RunnerState(
            status=RunnerStatus.ABSENT,
            consecutive_offline=0,
            consecutive_missing=4,
            labels="self-hosted,Linux,X64,pr,bors",
        )
        obs_present = HostObservation(present=True, online=True, busy=False, labels=[])

        transition = machine.apply("hoskinson2", previous, obs_present)

        self.assertTrue(transition.became_newly_present)
        self.assertEqual(transition.new_state.status, RunnerStatus.ONLINE)
        self.assertEqual(transition.new_state.consecutive_missing, 0)
        self.assertEqual(
            transition.new_state.labels, "self-hosted,Linux,X64,pr,bors"
        )
        self.assertEqual(transition.sample_state, SampleState.IDLE)

    def test_persistent_offline_then_back_online(self) -> None:
        """Offline streak should produce persistent-offline, then back-online event.

        Scenario:
        - Previous state is offline with one consecutive offline check.
        - Next observation remains present+offline.
        - Following observation is present+online.

        Expected behavior:
        - Second offline check sets persistent_offline_checks=2.
        - Online recovery sets back_online_checks=2 and resets offline counter.
        """
        machine = HostStateMachine()
        prev = RunnerState(
            status=RunnerStatus.OFFLINE,
            consecutive_offline=1,
            consecutive_missing=0,
            labels="self-hosted,Linux,X64,bors",
        )

        offline_transition = machine.apply(
            "hoskinson3",
            prev,
            HostObservation(
                present=True,
                online=False,
                busy=False,
                labels=["self-hosted", "Linux", "X64", "bors"],
            ),
        )
        self.assertEqual(offline_transition.new_state.status, RunnerStatus.OFFLINE)
        self.assertEqual(offline_transition.new_state.consecutive_offline, 2)
        self.assertEqual(offline_transition.persistent_offline_checks, 2)
        self.assertIsNone(offline_transition.back_online_checks)
        self.assertEqual(offline_transition.sample_state, SampleState.OFFLINE)

        online_transition = machine.apply(
            "hoskinson3",
            offline_transition.new_state,
            HostObservation(
                present=True,
                online=True,
                busy=False,
                labels=["self-hosted", "Linux", "X64", "bors"],
            ),
        )
        self.assertEqual(online_transition.new_state.status, RunnerStatus.ONLINE)
        self.assertEqual(online_transition.new_state.consecutive_offline, 0)
        self.assertEqual(online_transition.back_online_checks, 2)
        self.assertIsNone(online_transition.persistent_offline_checks)
        self.assertEqual(online_transition.sample_state, SampleState.IDLE)


class AlertPlannerTests(unittest.TestCase):
    """Unit tests for alert planning and dedupe/edit rules."""

    def test_should_edit_when_only_persistent_offline_unchanged(self) -> None:
        """Planner should request edit when offline set is unchanged and stable.

        Scenario:
        - One host is persistently offline.
        - Last notification already tracks the same offline set and has message id.
        - No back-online/newly-present/absent-entry events are present.

        Expected behavior:
        - Notification is still needed.
        - `should_edit` is true (update existing message instead of posting new one).
        """
        planner = AlertPlanner()
        transitions = [
            HostTransition(
                host="hoskinson4",
                new_state=RunnerState(
                    status=RunnerStatus.OFFLINE,
                    consecutive_offline=3,
                    consecutive_missing=0,
                    labels="self-hosted,Linux,X64,pr,bors",
                ),
                sample_state=SampleState.OFFLINE,
                persistent_offline_checks=3,
            )
        ]
        last = LastNotification(
            offline_set=["hoskinson4"],
            message_id="123456",
            updated_at="2026-02-09T11:30:00Z",
        )

        plan = planner.build(transitions, last)

        self.assertTrue(plan.should_notify)
        self.assertTrue(plan.should_edit)
        self.assertEqual(plan.offline_set, ["hoskinson4"])
        self.assertEqual(plan.last_message_id, "123456")
        self.assertIn("offline for multiple checks", plan.message)

    def test_no_edit_when_back_online_exists(self) -> None:
        """Back-online events should force a new message instead of edit-only path.

        Scenario:
        - Transition list contains a back-online event.
        - A prior message id exists.

        Expected behavior:
        - Notification is emitted.
        - `should_edit` is false because recovery events are not edit-only updates.
        """
        planner = AlertPlanner()
        transitions = [
            HostTransition(
                host="hoskinson5",
                new_state=RunnerState(
                    status=RunnerStatus.ONLINE,
                    consecutive_offline=0,
                    consecutive_missing=0,
                    labels="self-hosted,Linux,X64,pr,bors",
                ),
                sample_state=SampleState.IDLE,
                back_online_checks=4,
            )
        ]
        last = LastNotification(
            offline_set=[],
            message_id="999",
            updated_at="2026-02-09T11:45:00Z",
        )

        plan = planner.build(transitions, last)

        self.assertTrue(plan.should_notify)
        self.assertFalse(plan.should_edit)
        self.assertIn("back online", plan.message)

    def test_offline_set_contains_absent_and_persistent_hosts(self) -> None:
        """Offline set should include both absent hosts and persistent offline hosts.

        Scenario:
        - One host is persistently offline.
        - Another host is absent (already absent, not necessarily newly absent).

        Expected behavior:
        - `offline_set` includes both hosts for dedupe state tracking.
        """
        planner = AlertPlanner()
        transitions = [
            HostTransition(
                host="hoskinson1",
                new_state=RunnerState(
                    status=RunnerStatus.OFFLINE,
                    consecutive_offline=2,
                    consecutive_missing=0,
                    labels="self-hosted,Linux,X64,pr,bors",
                ),
                sample_state=SampleState.OFFLINE,
                persistent_offline_checks=2,
            ),
            HostTransition(
                host="hoskinson2",
                new_state=RunnerState(
                    status=RunnerStatus.ABSENT,
                    consecutive_offline=0,
                    consecutive_missing=3,
                    labels="self-hosted,Linux,X64,pr,bors",
                ),
                sample_state=SampleState.OFFLINE,
                became_absent=False,
            ),
        ]

        plan = planner.build(transitions, LastNotification())

        self.assertEqual(plan.offline_set, ["hoskinson1", "hoskinson2"])


if __name__ == "__main__":
    unittest.main()
