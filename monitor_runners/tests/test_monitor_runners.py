from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import unittest

# Ensure package imports work when tests are discovered as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor_runners import (
    CANONICAL_HOSTS,
    GitHubRunnersPayload,
    MonitorState,
    MonitorStats,
    process_monitoring_run,
    render_weekly_report,
)


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _runner(
    name: str,
    *,
    status: str = "online",
    busy: bool = False,
    labels: list[str] | None = None,
) -> dict:
    label_values = labels or ["self-hosted", "Linux", "X64", "pr", "bors"]
    return {
        "name": name,
        "status": status,
        "busy": busy,
        "labels": [{"name": label, "type": "custom"} for label in label_values],
    }


def _full_payload(
    *,
    missing: set[str] | None = None,
    overrides: dict[str, dict] | None = None,
    extra_runners: list[dict] | None = None,
) -> dict:
    missing = missing or set()
    overrides = overrides or {}
    runners = []
    for host in CANONICAL_HOSTS:
        if host in missing:
            continue
        options = overrides.get(host, {})
        runners.append(_runner(host, **options))
    runners.extend(extra_runners or [])
    return {"runners": runners}


def _payload_obj(payload: dict) -> GitHubRunnersPayload:
    """Simulate payload deserialization from API JSON."""
    return GitHubRunnersPayload.from_dict(payload)


def _empty_state() -> MonitorState:
    """Simulate loading initial state JSON from disk."""
    return MonitorState.from_dict({})


def _empty_stats() -> MonitorStats:
    """Simulate loading initial stats JSON from disk."""
    return MonitorStats.from_dict({})


def _reload_state(state: MonitorState) -> MonitorState:
    """Simulate writing/reading state JSON between runs."""
    return MonitorState.from_dict(state.to_dict())


def _reload_stats(stats: MonitorStats) -> MonitorStats:
    """Simulate writing/reading stats JSON between runs."""
    return MonitorStats.from_dict(stats.to_dict())


class MonitorRunnerBehaviorTests(unittest.TestCase):
    """Behavior-oriented tests for the monitoring rules.

    Each test describes an operational scenario (multiple runs over time)
    and checks the externally visible outputs:
    - persisted state structure and key fields
    - persisted stats structure and sampled states
    - notification decisions/messages

    The goal is to document expected behavior, not internal implementation.
    """

    def test_ephemeral_names_collapse_to_host(self) -> None:
        """Ephemeral runner names should be folded into their host.

        Scenario:
        - The payload contains host `hoskinson1`.
        - The payload also contains one ephemeral instance named
          `hoskinson1-...` that is busy.

        Expected behavior:
        - Monitoring state remains keyed by host names only.
        - Host `hoskinson1` is considered online.
        - Host `hoskinson1` is sampled as Active in stats because one
          online instance is busy.
        - No alert is emitted, because this is not an error transition.
        """
        payload = _full_payload(
            overrides={"hoskinson1": {"busy": False}},
            extra_runners=[
                _runner(
                    "hoskinson1-1770320856-360031733",
                    status="online",
                    busy=True,
                    labels=["self-hosted", "Linux", "X64", "bors", "ephemeral"],
                )
            ],
        )

        result = process_monitoring_run(
            _payload_obj(payload),
            previous_state=_empty_state(),
            previous_stats=_empty_stats(),
            now=_ts(2026, 2, 9, 10),
        )

        self.assertFalse(result.should_notify)
        self.assertEqual(result.new_state["runners"]["hoskinson1"]["status"], "online")
        self.assertEqual(result.new_stats["runners"]["hoskinson1"]["history"][-1]["state"], "Active")
        self.assertEqual(set(result.new_stats["runners"].keys()), set(CANONICAL_HOSTS))
        self.assertNotIn("ephemeral", result.new_state["runners"]["hoskinson1"]["labels"])

    def test_absent_then_newly_present_notifications(self) -> None:
        """A host missing twice becomes absent, keeps alerting, then reappears.

        Scenario timeline:
        - Run 0: all hosts present (baseline).
        - Run 1: `hoskinson3` missing from payload once.
        - Run 2: `hoskinson3` missing again.
        - Run 3: `hoskinson3` still missing with last_notification metadata
          indicating an existing Zulip message for this offline set.
        - Run 4: `hoskinson3` present again.

        Expected behavior:
        - After first miss: status is offline, consecutive_missing=1, no alert.
        - After second miss: status becomes absent and an absent alert is sent.
        - Third miss keeps alerting and marks should_edit=true when offline set
          remains unchanged.
        - On reappearance: a newly-present alert is sent and
          consecutive_missing resets to 0.
        """
        run0 = process_monitoring_run(
            _payload_obj(_full_payload()),
            previous_state=_empty_state(),
            previous_stats=_empty_stats(),
            now=_ts(2026, 2, 9, 10),
        )
        run1 = process_monitoring_run(
            _payload_obj(_full_payload(missing={"hoskinson3"})),
            previous_state=_reload_state(run0.state),
            previous_stats=_reload_stats(run0.stats),
            now=_ts(2026, 2, 9, 10, 15),
        )
        run2 = process_monitoring_run(
            _payload_obj(_full_payload(missing={"hoskinson3"})),
            previous_state=_reload_state(run1.state),
            previous_stats=_reload_stats(run1.stats),
            now=_ts(2026, 2, 9, 10, 30),
        )
        run2_state_with_notification = _reload_state(run2.state)
        run2_state_with_notification.last_notification.offline_set = ["hoskinson3"]
        run2_state_with_notification.last_notification.message_id = "22222"
        run2_state_with_notification.last_notification.updated_at = "2026-02-09T10:30:00Z"

        run3 = process_monitoring_run(
            _payload_obj(_full_payload(missing={"hoskinson3"})),
            previous_state=run2_state_with_notification,
            previous_stats=_reload_stats(run2.stats),
            now=_ts(2026, 2, 9, 10, 45),
        )
        run4 = process_monitoring_run(
            _payload_obj(_full_payload()),
            previous_state=_reload_state(run3.state),
            previous_stats=_reload_stats(run3.stats),
            now=_ts(2026, 2, 9, 11, 0),
        )

        self.assertFalse(run1.should_notify)
        self.assertEqual(run1.new_state["runners"]["hoskinson3"]["status"], "offline")
        self.assertEqual(run1.new_state["runners"]["hoskinson3"]["consecutive_missing"], 1)

        self.assertTrue(run2.should_notify)
        self.assertEqual(run2.new_state["runners"]["hoskinson3"]["status"], "absent")
        self.assertIn("absent from API payload for multiple checks", run2.message)
        self.assertIn("`hoskinson3`", run2.message)
        self.assertEqual(run2.offline_set, ["hoskinson3"])

        self.assertTrue(run3.should_notify)
        self.assertIn("absent from API payload for multiple checks", run3.message)
        self.assertIn("`hoskinson3`", run3.message)
        self.assertTrue(run3.should_edit)

        self.assertTrue(run4.should_notify)
        self.assertIn("newly present in API payload", run4.message)
        self.assertIn("`hoskinson3`", run4.message)
        self.assertEqual(run4.new_state["runners"]["hoskinson3"]["status"], "online")
        self.assertEqual(run4.new_state["runners"]["hoskinson3"]["consecutive_missing"], 0)

    def test_persistent_offline_then_back_online(self) -> None:
        """Offline transitions should alert on persistence and recovery.

        Scenario timeline:
        - Run 0: all hosts present and online (baseline).
        - Run 1: `hoskinson4` is present but offline (first offline check).
        - Run 2: `hoskinson4` still present and offline (second check).
        - Run 3: `hoskinson4` still offline with last_notification metadata
          indicating an existing Zulip message for this offline set.
        - Run 4: `hoskinson4` comes back online.

        Expected behavior:
        - First offline check does not alert.
        - Second offline check emits persistent-offline alert.
        - Third offline check keeps alerting and marks should_edit=true
          because the offline set did not change.
        - Recovery emits a back-online alert and should_edit=false.
        """
        run0 = process_monitoring_run(
            _payload_obj(_full_payload()),
            previous_state=_empty_state(),
            previous_stats=_empty_stats(),
            now=_ts(2026, 2, 9, 11),
        )
        run1 = process_monitoring_run(
            _payload_obj(_full_payload(overrides={"hoskinson4": {"status": "offline"}})),
            previous_state=_reload_state(run0.state),
            previous_stats=_reload_stats(run0.stats),
            now=_ts(2026, 2, 9, 11, 15),
        )
        run2 = process_monitoring_run(
            _payload_obj(_full_payload(overrides={"hoskinson4": {"status": "offline"}})),
            previous_state=_reload_state(run1.state),
            previous_stats=_reload_stats(run1.stats),
            now=_ts(2026, 2, 9, 11, 30),
        )

        self.assertFalse(run1.should_notify)
        self.assertTrue(run2.should_notify)
        self.assertIn("offline for multiple checks", run2.message)
        self.assertIn("`hoskinson4`", run2.message)
        self.assertEqual(run2.offline_set, ["hoskinson4"])

        run2_state_with_notification = _reload_state(run2.state)
        run2_state_with_notification.last_notification.offline_set = ["hoskinson4"]
        run2_state_with_notification.last_notification.message_id = "12345"
        run2_state_with_notification.last_notification.updated_at = "2026-02-09T11:30:00Z"

        run3 = process_monitoring_run(
            _payload_obj(_full_payload(overrides={"hoskinson4": {"status": "offline"}})),
            previous_state=run2_state_with_notification,
            previous_stats=_reload_stats(run2.stats),
            now=_ts(2026, 2, 9, 11, 45),
        )
        self.assertTrue(run3.should_notify)
        self.assertTrue(run3.should_edit)

        run4 = process_monitoring_run(
            _payload_obj(_full_payload()),
            previous_state=_reload_state(run3.state),
            previous_stats=_reload_stats(run3.stats),
            now=_ts(2026, 2, 9, 12),
        )
        self.assertTrue(run4.should_notify)
        self.assertIn("back online", run4.message)
        self.assertIn("`hoskinson4`", run4.message)
        self.assertFalse(run4.should_edit)

    def test_mixed_snapshot_offline_and_absent_emits_single_consistent_alert(self) -> None:
        """One run with mixed transitions should produce one coherent alert.

        Scenario timeline:
        - Run 0: all hosts present and online (baseline).
        - Run 1: `hoskinson3` missing once (first miss, no alert).
        - Run 2: `hoskinson3` missing again and `hoskinson4` present+offline.
        - Run 3: `hoskinson3` still missing and `hoskinson4` still present+offline.
        - Run 4: `hoskinson3` still missing and `hoskinson4` still present+offline.
        - Run 5: `hoskinson3` still missing and `hoskinson4` now missing once.
        - Run 6: `hoskinson3` still missing and `hoskinson4` missing again.

        Expected behavior:
        - `hoskinson3` transitions to absent and triggers alerting.
        - `hoskinson4` transitions to first-check offline but does not yet
          trigger persistent-offline alerting.
        - The run emits exactly one notification decision with one absent section
          and no persistent-offline section.
        - On run 3, `hoskinson4` reaches persistent offline and joins `offline_set`.
        - On run 4, `hoskinson4` remains persistently offline.
        - On run 5, `hoskinson4` changes from present+offline to missing and is
          treated as first-miss offline (not persistent, not absent).
        - On run 6, `hoskinson4` becomes absent.
        """
        run0 = process_monitoring_run(
            _payload_obj(_full_payload()),
            previous_state=_empty_state(),
            previous_stats=_empty_stats(),
            now=_ts(2026, 2, 9, 12, 30),
        )
        run1 = process_monitoring_run(
            _payload_obj(_full_payload(missing={"hoskinson3"})),
            previous_state=_reload_state(run0.state),
            previous_stats=_reload_stats(run0.stats),
            now=_ts(2026, 2, 9, 12, 45),
        )
        run2 = process_monitoring_run(
            _payload_obj(
                _full_payload(
                    missing={"hoskinson3"},
                    overrides={"hoskinson4": {"status": "offline"}},
                )
            ),
            previous_state=_reload_state(run1.state),
            previous_stats=_reload_stats(run1.stats),
            now=_ts(2026, 2, 9, 13, 0),
        )
        run3 = process_monitoring_run(
            _payload_obj(
                _full_payload(
                    missing={"hoskinson3"},
                    overrides={"hoskinson4": {"status": "offline"}},
                )
            ),
            previous_state=_reload_state(run2.state),
            previous_stats=_reload_stats(run2.stats),
            now=_ts(2026, 2, 9, 13, 15),
        )
        run4 = process_monitoring_run(
            _payload_obj(
                _full_payload(
                    missing={"hoskinson3"},
                    overrides={"hoskinson4": {"status": "offline"}},
                )
            ),
            previous_state=_reload_state(run3.state),
            previous_stats=_reload_stats(run3.stats),
            now=_ts(2026, 2, 9, 13, 30),
        )
        run5 = process_monitoring_run(
            _payload_obj(_full_payload(missing={"hoskinson3", "hoskinson4"})),
            previous_state=_reload_state(run4.state),
            previous_stats=_reload_stats(run4.stats),
            now=_ts(2026, 2, 9, 13, 45),
        )
        run6 = process_monitoring_run(
            _payload_obj(_full_payload(missing={"hoskinson3", "hoskinson4"})),
            previous_state=_reload_state(run5.state),
            previous_stats=_reload_stats(run5.stats),
            now=_ts(2026, 2, 9, 14, 0),
        )

        self.assertFalse(run1.should_notify)

        self.assertTrue(run2.should_notify)
        self.assertFalse(run2.should_edit)
        self.assertIn("absent from API payload for multiple checks", run2.message)
        self.assertEqual(run2.message.count("absent from API payload for multiple checks"), 1)
        self.assertIn("`hoskinson3`", run2.message)
        self.assertNotIn("offline for multiple checks", run2.message)

        self.assertEqual(run2.new_state["runners"]["hoskinson4"]["status"], "offline")
        self.assertEqual(run2.new_state["runners"]["hoskinson4"]["consecutive_offline"], 1)
        self.assertEqual(run2.offline_set, ["hoskinson3"])

        self.assertTrue(run3.should_notify)
        self.assertIn("absent from API payload for multiple checks", run3.message)
        self.assertIn("offline for multiple checks", run3.message)
        self.assertIn("`hoskinson3`", run3.message)
        self.assertIn("`hoskinson4`", run3.message)
        self.assertEqual(run3.new_state["runners"]["hoskinson4"]["status"], "offline")
        self.assertEqual(run3.new_state["runners"]["hoskinson4"]["consecutive_missing"], 0)
        self.assertEqual(run3.new_state["runners"]["hoskinson4"]["consecutive_offline"], 2)
        self.assertEqual(run3.offline_set, ["hoskinson3", "hoskinson4"])

        self.assertTrue(run4.should_notify)
        self.assertIn("absent from API payload for multiple checks", run4.message)
        self.assertIn("offline for multiple checks", run4.message)
        self.assertIn("`hoskinson3`", run4.message)
        self.assertIn("`hoskinson4`", run4.message)
        self.assertEqual(run4.new_state["runners"]["hoskinson4"]["status"], "offline")
        self.assertEqual(run4.new_state["runners"]["hoskinson4"]["consecutive_missing"], 0)
        self.assertEqual(run4.new_state["runners"]["hoskinson4"]["consecutive_offline"], 3)
        self.assertEqual(run4.offline_set, ["hoskinson3", "hoskinson4"])

        self.assertTrue(run5.should_notify)
        self.assertIn("absent from API payload for multiple checks", run5.message)
        self.assertNotIn("offline for multiple checks", run5.message)
        self.assertIn("`hoskinson3`", run5.message)
        self.assertNotIn("`hoskinson4`", run5.message)
        self.assertEqual(run5.new_state["runners"]["hoskinson4"]["status"], "offline")
        self.assertEqual(run5.new_state["runners"]["hoskinson4"]["consecutive_missing"], 1)
        self.assertEqual(run5.new_state["runners"]["hoskinson4"]["consecutive_offline"], 0)
        self.assertEqual(run5.offline_set, ["hoskinson3"])

        self.assertTrue(run6.should_notify)
        self.assertIn("absent from API payload for multiple checks", run6.message)
        self.assertNotIn("offline for multiple checks", run6.message)
        self.assertIn("`hoskinson3`", run6.message)
        self.assertIn("`hoskinson4`", run6.message)
        self.assertEqual(run6.new_state["runners"]["hoskinson4"]["status"], "absent")
        self.assertEqual(run6.new_state["runners"]["hoskinson4"]["consecutive_missing"], 2)
        self.assertEqual(run6.new_state["runners"]["hoskinson4"]["consecutive_offline"], 0)
        self.assertEqual(run6.offline_set, ["hoskinson3", "hoskinson4"])

    def test_stats_output_keeps_only_hosts(self) -> None:
        """Stats should drop non-canonical keys and keep 7-day window only.

        Scenario:
        - Previous stats include:
          - host `hoskinson` with one old entry (>7 days) and one
            recent entry.
          - one historical ephemeral key `hoskinson-...`.
        - Current payload is missing `hoskinson`.

        Expected behavior:
        - Output stats are keyed only by hosts.
        - Historical ephemeral key is removed.
        - Older-than-7-days canonical sample is pruned.
        - Current missing host is appended as Offline.
        """
        now = _ts(2026, 2, 9, 13)
        previous_stats = {
            "runners": {
                "hoskinson": {
                    "history": [
                        {
                            "timestamp": (now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "state": "Idle",
                        },
                        {
                            "timestamp": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "state": "Idle",
                        },
                    ],
                    "labels": "self-hosted,Linux,X64,bors",
                },
                "hoskinson-1770329103-28118661": {
                    "history": [{"timestamp": "2026-02-05T22:10:38Z", "state": "Idle"}],
                    "labels": "self-hosted,Linux,X64,ephemeral",
                },
            },
            "last_cleanup": "2026-02-08T13:00:00Z",
        }

        result = process_monitoring_run(
            _payload_obj(_full_payload(missing={"hoskinson"})),
            previous_state=_empty_state(),
            previous_stats=MonitorStats.from_dict(previous_stats),
            now=now,
        )

        self.assertEqual(set(result.new_stats["runners"].keys()), set(CANONICAL_HOSTS))
        self.assertNotIn("hoskinson-1770329103-28118661", result.new_stats["runners"])
        hoskinson_history = result.new_stats["runners"]["hoskinson"]["history"]
        self.assertEqual(len(hoskinson_history), 2)
        self.assertEqual(hoskinson_history[-1]["state"], "Offline")

    def test_weekly_report_reflects_offline_share(self) -> None:
        """Weekly report percentages should reflect observed host behavior.

        Scenario:
        - First run: all hosts online/idle.
        - Second run: host `hoskinson` missing from payload.

        Expected behavior:
        - Missing host counts as Offline in stats.
        - Report row for `hoskinson` shows 50% Idle / 50% Offline across
          the two samples.
        - Report still includes overall statistics section.
        """
        run0 = process_monitoring_run(
            _payload_obj(_full_payload()),
            previous_state=_empty_state(),
            previous_stats=_empty_stats(),
            now=_ts(2026, 2, 9, 14),
        )
        run1 = process_monitoring_run(
            _payload_obj(_full_payload(missing={"hoskinson"})),
            previous_state=_reload_state(run0.state),
            previous_stats=_reload_stats(run0.stats),
            now=_ts(2026, 2, 9, 14, 15),
        )
        report = render_weekly_report(run1.stats, generated_at=_ts(2026, 2, 9, 14, 20))

        self.assertIn("Weekly Runner Statistics Report", report)
        self.assertIn("| `hoskinson` | 50.0% | 0.0% | 50.0% |", report)
        self.assertIn("Overall Statistics", report)

    def test_deserializes_real_github_api_payload_fixture(self) -> None:
        """Real GH API payload should deserialize into host outputs.

        Scenario:
        - Load a raw payload captured from
          `/orgs/leanprover-community/actions/runners`.
        - Run one monitoring pass with no prior state/stats.

        Expected behavior:
        - The payload is accepted as-is (JSON object with `runners` array).
        - Output state/stats are generated for all hosts only.
        - Host presence is inferred from runner names using the host-prefix rule.
        - Present hosts are sampled as Idle/Active/Offline based on payload
          status+busy aggregation.
        - Missing hosts are treated as first-miss offline
          (`consecutive_missing == 1`) with Offline stats samples.
        - No notification is sent on this initial pass.
        """
        fixture_path = Path(__file__).parent / "fixtures/github_runners_payload.txt"
        self.assertTrue(fixture_path.exists(), "missing fixture: github_runners_payload.txt")
        payload = json.loads(fixture_path.read_text())
        payload_obj = GitHubRunnersPayload.from_dict(payload)
        self.assertGreater(len(payload_obj.runners), 0)
        self.assertEqual(payload_obj.total_count, len(payload_obj.runners))

        now = _ts(2026, 2, 9, 20)
        result = process_monitoring_run(
            payload_obj, previous_state=_empty_state(), previous_stats=_empty_stats(), now=now
        )

        self.assertEqual(set(result.new_state["runners"].keys()), set(CANONICAL_HOSTS))
        self.assertEqual(set(result.new_stats["runners"].keys()), set(CANONICAL_HOSTS))
        self.assertEqual(result.new_state["last_run"], "2026-02-09T20:00:00Z")
        self.assertFalse(result.should_notify)

        grouped: dict[str, list[dict]] = {host: [] for host in CANONICAL_HOSTS}
        name_re = re.compile(r"^(hoskinson(?:[1-9])?)(?:-.*)?$")
        for runner in payload_obj.runners:
            name = runner.name
            match = name_re.fullmatch(name)
            if match is None:
                continue
            host = match.group(1)
            if host in grouped:
                grouped[host].append(
                    {
                        "status": runner.status,
                        "busy": runner.busy,
                    }
                )

        for host in CANONICAL_HOSTS:
            state_row = result.new_state["runners"][host]
            stats_state = result.new_stats["runners"][host]["history"][-1]["state"]
            entries = grouped[host]

            if entries:
                expected_online = any(r.get("status") == "online" for r in entries)
                expected_busy = any(
                    r.get("status") == "online" and bool(r.get("busy")) for r in entries
                )
                expected_stats_state = (
                    "Active" if expected_online and expected_busy else
                    "Idle" if expected_online else
                    "Offline"
                )

                self.assertEqual(state_row["consecutive_missing"], 0)
                self.assertIn(state_row["status"], {"online", "offline"})
                self.assertEqual(stats_state, expected_stats_state)
            else:
                self.assertEqual(state_row["status"], "offline")
                self.assertEqual(state_row["consecutive_missing"], 1)
                self.assertEqual(stats_state, "Offline")


if __name__ == "__main__":
    unittest.main()
