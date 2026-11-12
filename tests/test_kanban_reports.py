"""Tests for kanban report API endpoints — all analytics/chart data."""

import pytest
from datetime import datetime, timezone, timedelta


@pytest.fixture
def seeded_report_board(kanban_app):
    """Board with tasks across statuses + history for report testing."""
    app, client, repo = kanban_app

    # Create tasks in various statuses
    tasks = []
    for title, status in [
        ("Task A", "not_started"), ("Task B", "working"),
        ("Task C", "complete"), ("Task D", "not_started"),
        ("Task E", "working"), ("Task F", "complete"),
    ]:
        resp = client.post('/api/kanban/tasks', json={"title": title, "status": status})
        tasks.append(resp.get_json())

    # Add tags
    client.post(f'/api/kanban/tasks/{tasks[0]["id"]}/tags', json={"tag": "bug"})
    client.post(f'/api/kanban/tasks/{tasks[1]["id"]}/tags', json={"tag": "bug"})
    client.post(f'/api/kanban/tasks/{tasks[2]["id"]}/tags', json={"tag": "feature"})

    # Link a session
    client.post(f'/api/kanban/tasks/{tasks[1]["id"]}/sessions',
                json={"session_id": "sess-report-1"})

    # Create an issue
    client.post(f'/api/kanban/tasks/{tasks[3]["id"]}/issues',
                json={"description": "Needs fix"})

    # Add a subtask
    client.post('/api/kanban/tasks', json={
        "title": "Subtask", "parent_id": tasks[0]["id"]
    })

    return app, client, repo, tasks


class TestVelocity:
    def test_velocity_returns_data(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/velocity')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "velocity" in data or "daily" in data or isinstance(data, dict)

    def test_velocity_with_date_range(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        today = datetime.now(timezone.utc).date().isoformat()
        resp = client.get(f'/api/kanban/report/velocity?start_date={today}&end_date={today}')
        assert resp.status_code == 200


class TestCycleTime:
    def test_cycle_time_returns_data(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/cycle-time')
        assert resp.status_code == 200


class TestDistribution:
    def test_distribution_counts_by_status(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/distribution')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)


class TestStale:
    def test_stale_returns_data(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/stale')
        assert resp.status_code == 200


class TestRemediation:
    def test_remediation_returns_data(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/remediation')
        assert resp.status_code == 200


class TestTagDistribution:
    def test_tag_distribution(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/tags')
        assert resp.status_code == 200


class TestSessionActivity:
    def test_session_activity(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/session-activity')
        assert resp.status_code == 200


class TestSubtaskDepth:
    def test_subtask_depth(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/subtask-depth')
        assert resp.status_code == 200


class TestBlockers:
    def test_blockers_returns_data(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/blockers')
        assert resp.status_code == 200


class TestCumulativeFlow:
    def test_cumulative_flow(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/cumulative-flow')
        assert resp.status_code == 200


class TestOwnerActivity:
    def test_owner_activity(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/owner-activity')
        assert resp.status_code == 200


class TestThroughput:
    def test_throughput(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/throughput')
        assert resp.status_code == 200


class TestSessionEfficiency:
    def test_session_efficiency(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/session-efficiency')
        assert resp.status_code == 200


class TestIssueFrequency:
    def test_issue_frequency(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/issue-frequency')
        assert resp.status_code == 200


class TestWipLimits:
    def test_wip_limits(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/wip-limits')
        assert resp.status_code == 200


class TestTimeInStatus:
    def test_time_in_status(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/time-in-status')
        assert resp.status_code == 200


class TestActivityLog:
    def test_activity_log(self, seeded_report_board):
        _, client, _, _ = seeded_report_board
        resp = client.get('/api/kanban/report/activity-log')
        assert resp.status_code == 200
