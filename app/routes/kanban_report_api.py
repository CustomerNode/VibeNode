"""
Kanban board reports — analytics endpoints for velocity, cycle time,
status breakdown, stale tasks, remediation rate, tag distribution,
session activity, subtask depth, blockers, completion trend, and workload.
"""

from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request

from ..config import get_active_project
from ..db import create_repository

bp = Blueprint('kanban_reports', __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_repo():
    return create_repository()


def _get_project_id():
    # Reports go through the same alias resolution as the kanban routes — see
    # app.config.resolve_project_alias. Without this, a user who adopts a
    # shared cloud project_id would see their tasks but get empty velocity /
    # status / cycle-time charts because reports queried the local id.
    from ..config import resolve_project_alias
    pid = request.args.get('project_id')
    if pid:
        return resolve_project_alias(pid)
    return resolve_project_alias(get_active_project())


def _parse_date(s):
    """Parse an ISO date string, returning a datetime or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def _get_date_range():
    """Extract start_date and end_date from request.args.

    Defaults to last 30 days if not provided.
    Returns (start_date_str, end_date_str) as ISO date strings (YYYY-MM-DD).
    """
    today = datetime.now(timezone.utc).date()
    default_start = (today - timedelta(days=30)).isoformat()
    default_end = today.isoformat()

    start_date = request.args.get('start_date', '').strip() or default_start
    end_date = request.args.get('end_date', '').strip() or default_end

    # Validate format — fall back to defaults on bad input
    try:
        datetime.fromisoformat(start_date)
    except (ValueError, TypeError):
        start_date = default_start
    try:
        datetime.fromisoformat(end_date)
    except (ValueError, TypeError):
        end_date = default_end

    return start_date, end_date


# ---------------------------------------------------------------------------
# 1. Velocity — tasks completed per day/week
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/velocity")
def report_velocity():
    """Tasks completed per day in the given date range (default last 30 days)."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        start_date, end_date = _get_date_range()

        rows = repo.execute_sql(
            """
            SELECT DATE(sh.changed_at) as day, COUNT(*) as completed
            FROM task_status_history sh
            JOIN tasks t ON sh.task_id = t.id
            WHERE t.project_id = ?
              AND sh.new_status = 'complete'
              AND DATE(sh.changed_at) >= ?
              AND DATE(sh.changed_at) <= ?
            GROUP BY DATE(sh.changed_at)
            ORDER BY day ASC
            """,
            (project_id, start_date, end_date),
        )
        # Fill gaps with 0
        start_dt = datetime.fromisoformat(start_date).date() if isinstance(start_date, str) else start_date
        end_dt = datetime.fromisoformat(end_date).date() if isinstance(end_date, str) else end_date
        day_map = {r['day']: r['completed'] for r in rows}
        result = []
        total_days = (end_dt - start_dt).days
        for i in range(total_days, -1, -1):
            d = (end_dt - timedelta(days=i)).isoformat()
            result.append({'day': d, 'completed': day_map.get(d, 0)})

        # Weekly aggregation
        weekly = {}
        for item in result:
            dt = datetime.fromisoformat(item['day'])
            week_start = (dt - timedelta(days=dt.weekday())).isoformat()
            weekly[week_start] = weekly.get(week_start, 0) + item['completed']
        weeks = [{'week': k, 'completed': v} for k, v in sorted(weekly.items())]

        return jsonify({'daily': result, 'weekly': weeks})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 2. Cycle Time — avg time from working -> complete per task
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/cycle-time")
def report_cycle_time():
    """Cycle time (working→complete) and lead time (creation→complete).

    Plan Section 13, lines 2564-2605: includes median, P75, P90, and
    distribution histogram buckets.
    Accepts ?start_date=&end_date= to scope the date range.
    """
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        start_date, end_date = _get_date_range()

        rows = repo.execute_sql(
            """
            SELECT
                t.id,
                t.title,
                t.created_at,
                MIN(CASE WHEN sh.new_status = 'working' THEN sh.changed_at END) as started,
                MAX(CASE WHEN sh.new_status = 'complete' THEN sh.changed_at END) as completed
            FROM tasks t
            JOIN task_status_history sh ON sh.task_id = t.id
            WHERE t.project_id = ?
            GROUP BY t.id
            HAVING completed IS NOT NULL
              AND DATE(completed) >= ?
              AND DATE(completed) <= ?
            """,
            (project_id, start_date, end_date),
        )
        cycles = []
        leads = []
        for r in rows:
            end_dt = _parse_date(r['completed'])
            if not end_dt:
                continue

            # Cycle time: working → complete
            start_dt = _parse_date(r['started'])
            cycle_hours = None
            if start_dt and end_dt:
                cycle_hours = (end_dt - start_dt).total_seconds() / 3600

            # Lead time: creation → complete
            created_dt = _parse_date(r['created_at'])
            lead_hours = None
            if created_dt and end_dt:
                lead_hours = (end_dt - created_dt).total_seconds() / 3600

            task_entry = {
                'task_id': r['id'],
                'title': r['title'],
                'hours': round(cycle_hours, 1) if cycle_hours is not None else None,
                'lead_hours': round(lead_hours, 1) if lead_hours is not None else None,
            }
            cycles.append(task_entry)
            if cycle_hours is not None:
                leads.append(cycle_hours)

        # Compute percentiles for cycle time
        leads_sorted = sorted(leads) if leads else []
        count = len(leads_sorted)

        def _percentile(sorted_vals, pct):
            if not sorted_vals:
                return 0
            idx = int(len(sorted_vals) * pct / 100)
            idx = min(idx, len(sorted_vals) - 1)
            return round(sorted_vals[idx], 1)

        avg = round(sum(leads) / count, 1) if count > 0 else 0
        median = _percentile(leads_sorted, 50)
        p75 = _percentile(leads_sorted, 75)
        p90 = _percentile(leads_sorted, 90)

        return jsonify({
            'tasks': cycles,
            'average_hours': avg,
            'median_hours': median,
            'p75_hours': p75,
            'p90_hours': p90,
            'count': count,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 3. Status Breakdown — count of tasks per status
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/distribution")
def report_status_breakdown():
    """Count of tasks per status for the project."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        rows = repo.execute_sql(
            """
            SELECT status, COUNT(*) as count
            FROM tasks
            WHERE project_id = ?
            GROUP BY status
            ORDER BY CASE status
                WHEN 'not_started' THEN 1
                WHEN 'working' THEN 2
                WHEN 'validating' THEN 3
                WHEN 'remediating' THEN 4
                WHEN 'complete' THEN 5
                ELSE 6
            END
            """,
            (project_id,),
        )
        total = sum(r['count'] for r in rows)
        breakdown = []
        for r in rows:
            pct = round((r['count'] / total) * 100, 1) if total else 0
            breakdown.append({
                'status': r['status'],
                'count': r['count'],
                'percent': pct,
            })
        return jsonify({'breakdown': breakdown, 'total': total})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 4. Stale Tasks — tasks not updated in >3 days
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/stale")
def report_stale():
    """Tasks not updated in more than 3 days (excludes complete)."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        days = int(request.args.get('days', 3))
        rows = repo.execute_sql(
            """
            SELECT id, title, status, updated_at
            FROM tasks
            WHERE project_id = ?
              AND status != 'complete'
              AND updated_at < DATETIME('now', ? || ' days')
            ORDER BY updated_at ASC
            """,
            (project_id, str(-days)),
        )
        stale = []
        now = datetime.now(timezone.utc)
        for r in rows:
            updated = _parse_date(r['updated_at'])
            days_stale = round((now - updated).total_seconds() / 86400, 1) if updated else 0
            stale.append({
                'task_id': r['id'],
                'title': r['title'],
                'status': r['status'],
                'updated_at': r['updated_at'],
                'days_stale': days_stale,
            })
        return jsonify({'stale': stale, 'threshold_days': days})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 5. Remediation Rate — % of tasks that went through remediating
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/remediation")
def report_remediation_rate():
    """Percentage of tasks that transitioned through remediating status."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        total_row = repo.execute_sql(
            "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = ?",
            (project_id,),
        )
        total = total_row[0]['cnt'] if total_row else 0

        remediated_row = repo.execute_sql(
            """
            SELECT COUNT(DISTINCT t.id) as cnt
            FROM tasks t
            JOIN task_status_history sh ON sh.task_id = t.id
            WHERE t.project_id = ?
              AND sh.new_status = 'remediating'
            """,
            (project_id,),
        )
        remediated = remediated_row[0]['cnt'] if remediated_row else 0
        rate = round((remediated / total) * 100, 1) if total else 0

        return jsonify({
            'total_tasks': total,
            'remediated_tasks': remediated,
            'rate_percent': rate,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 6. Tag Distribution — task counts per tag
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/tags")
def report_tag_distribution():
    """Count of tasks per tag for the project."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        rows = repo.execute_sql(
            """
            SELECT tt.tag, COUNT(*) as count
            FROM task_tags tt
            JOIN tasks t ON tt.task_id = t.id
            WHERE t.project_id = ?
            GROUP BY tt.tag
            ORDER BY count DESC
            """,
            (project_id,),
        )
        return jsonify({'tags': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 7. Session Activity — sessions linked per task
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/session-activity")
def report_session_activity():
    """Number of sessions linked per task."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        rows = repo.execute_sql(
            """
            SELECT t.id, t.title, t.status, COUNT(ts.session_id) as session_count
            FROM tasks t
            LEFT JOIN task_sessions ts ON ts.task_id = t.id
            WHERE t.project_id = ?
            GROUP BY t.id
            ORDER BY session_count DESC
            """,
            (project_id,),
        )
        return jsonify({'tasks': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 8. Subtask Depth — distribution of nesting depths
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/subtask-depth")
def report_subtask_depth():
    """Distribution of subtask nesting depths."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        rows = repo.execute_sql(
            """
            WITH RECURSIVE depth_cte AS (
                SELECT id, title, 0 as depth
                FROM tasks
                WHERE project_id = ? AND parent_id IS NULL
              UNION ALL
                SELECT t.id, t.title, d.depth + 1
                FROM tasks t
                JOIN depth_cte d ON t.parent_id = d.id
            )
            SELECT depth, COUNT(*) as count
            FROM depth_cte
            GROUP BY depth
            ORDER BY depth ASC
            """,
            (project_id,),
        )
        return jsonify({'depths': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 9. Blockers — tasks with open issues
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/blockers")
def report_blockers():
    """Tasks with unresolved issues."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        rows = repo.execute_sql(
            """
            SELECT t.id, t.title, t.status,
                   COUNT(ti.id) as open_issues
            FROM tasks t
            JOIN task_issues ti ON ti.task_id = t.id
            WHERE t.project_id = ?
              AND ti.resolved_at IS NULL
            GROUP BY t.id
            ORDER BY open_issues DESC
            """,
            (project_id,),
        )
        return jsonify({'blockers': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 10. Completion Trend — cumulative completions over time
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/cumulative-flow")
def report_completion_trend():
    """Cumulative flow: both created and completed lines over a date range.

    Plan Section 13 lines 2675-2691: gap between lines = WIP.
    Accepts ?start_date=&end_date= (default last 30 days).
    """
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        start_date, end_date = _get_date_range()

        # Completed per day
        completed_rows = repo.execute_sql(
            """
            SELECT DATE(sh.changed_at) as day, COUNT(*) as cnt
            FROM task_status_history sh
            JOIN tasks t ON sh.task_id = t.id
            WHERE t.project_id = ?
              AND sh.new_status = 'complete'
              AND DATE(sh.changed_at) >= ?
              AND DATE(sh.changed_at) <= ?
            GROUP BY DATE(sh.changed_at)
            ORDER BY day ASC
            """,
            (project_id, start_date, end_date),
        )

        # Created per day
        created_rows = repo.execute_sql(
            """
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM tasks
            WHERE project_id = ?
              AND DATE(created_at) >= ?
              AND DATE(created_at) <= ?
            GROUP BY DATE(created_at)
            ORDER BY day ASC
            """,
            (project_id, start_date, end_date),
        )

        start_dt = datetime.fromisoformat(start_date).date() if isinstance(start_date, str) else start_date
        end_dt = datetime.fromisoformat(end_date).date() if isinstance(end_date, str) else end_date
        completed_map = {r['day']: r['cnt'] for r in completed_rows}
        created_map = {r['day']: r['cnt'] for r in created_rows}

        result = []
        cum_completed = 0
        cum_created = 0
        total_days = (end_dt - start_dt).days
        for i in range(total_days, -1, -1):
            d = (end_dt - timedelta(days=i)).isoformat()
            cum_completed += completed_map.get(d, 0)
            cum_created += created_map.get(d, 0)
            result.append({
                'day': d,
                'cumulative': cum_completed,
                'cumulative_created': cum_created,
            })

        return jsonify({'trend': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 11. Workload — tasks per owner
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/owner-activity")
def report_workload():
    """Per-owner stats: claimed, completed, in progress, avg cycle time.

    Plan Section 13 lines 2720-2742: includes heatmap data.
    Accepts ?start_date=&end_date= (default last 30 days).
    """
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        start_date, end_date = _get_date_range()

        # Tasks by owner and status (including complete)
        rows = repo.execute_sql(
            """
            SELECT COALESCE(owner, '(unassigned)') as owner,
                   status,
                   COUNT(*) as count
            FROM tasks
            WHERE project_id = ?
            GROUP BY owner, status
            ORDER BY owner, status
            """,
            (project_id,),
        )
        # Group by owner
        workload = {}
        for r in rows:
            owner = r['owner']
            if owner not in workload:
                workload[owner] = {
                    'owner': owner, 'claimed': 0, 'completed': 0,
                    'in_progress': 0, 'total': 0, 'by_status': {},
                }
            workload[owner]['total'] += r['count']
            workload[owner]['by_status'][r['status']] = r['count']
            if r['status'] == 'complete':
                workload[owner]['completed'] += r['count']
            elif r['status'] in ('working', 'validating', 'remediating'):
                workload[owner]['in_progress'] += r['count']
            workload[owner]['claimed'] += r['count']

        # Activity heatmap: status changes by day-of-week × hour
        heatmap_rows = repo.execute_sql(
            """
            SELECT strftime('%w', sh.changed_at) as dow,
                   CAST(strftime('%H', sh.changed_at) AS INTEGER) as hour,
                   COUNT(*) as cnt
            FROM task_status_history sh
            JOIN tasks t ON sh.task_id = t.id
            WHERE t.project_id = ?
              AND DATE(sh.changed_at) >= ?
              AND DATE(sh.changed_at) <= ?
            GROUP BY dow, hour
            """,
            (project_id, start_date, end_date),
        )
        heatmap = []
        for r in heatmap_rows:
            heatmap.append({
                'day_of_week': int(r['dow']),
                'hour': int(r['hour']),
                'count': r['cnt'],
            })

        return jsonify({
            'workload': list(workload.values()),
            'heatmap': heatmap,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 12. Throughput — simple task count per status
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/throughput")
def report_throughput():
    """Current count of tasks in each status (pipeline throughput)."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        rows = repo.execute_sql(
            "SELECT status, COUNT(*) as count FROM tasks WHERE project_id = ? GROUP BY status",
            (project_id,),
        )
        return jsonify({'throughput': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 13. Session Utilization — tasks with vs without linked sessions
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/session-efficiency")
def report_session_utilization():
    """Tasks with linked sessions vs tasks without."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        rows = repo.execute_sql(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN ts.task_id IS NOT NULL THEN 1 ELSE 0 END) as with_sessions,
                SUM(CASE WHEN ts.task_id IS NULL THEN 1 ELSE 0 END) as without_sessions
            FROM tasks t
            LEFT JOIN (SELECT DISTINCT task_id FROM task_sessions) ts ON ts.task_id = t.id
            WHERE t.project_id = ?
            """,
            (project_id,),
        )
        r = rows[0] if rows else {'total': 0, 'with_sessions': 0, 'without_sessions': 0}
        total = r['total'] or 0
        with_s = r['with_sessions'] or 0
        rate = round((with_s / total) * 100, 1) if total else 0
        return jsonify({
            'total': total,
            'with_sessions': with_s,
            'without_sessions': r['without_sessions'] or 0,
            'utilization_percent': rate,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 14. Issue Frequency — tasks with most validation issues
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/issue-frequency")
def report_issue_frequency():
    """Tasks ranked by number of validation issues (open and resolved)."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        limit = int(request.args.get('limit', 20))
        rows = repo.execute_sql(
            """
            SELECT t.id, t.title, t.status,
                   COUNT(ti.id) as issue_count,
                   SUM(CASE WHEN ti.resolved_at IS NULL THEN 1 ELSE 0 END) as open_count,
                   SUM(CASE WHEN ti.resolved_at IS NOT NULL THEN 1 ELSE 0 END) as resolved_count
            FROM tasks t
            JOIN task_issues ti ON ti.task_id = t.id
            WHERE t.project_id = ?
            GROUP BY t.id
            ORDER BY issue_count DESC
            LIMIT ?
            """,
            (project_id, limit),
        )
        return jsonify({'tasks': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 15. WIP Limits — work-in-progress count with threshold
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/wip-limits")
def report_wip_limits():
    """Current work-in-progress count vs configurable limit."""
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        limit = int(request.args.get('limit', 5))
        rows = repo.execute_sql(
            "SELECT COUNT(*) as wip_count FROM tasks WHERE project_id = ? AND status = 'working'",
            (project_id,),
        )
        wip = rows[0]['wip_count'] if rows else 0
        return jsonify({
            'wip_count': wip,
            'wip_limit': limit,
            'over_limit': wip > limit,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 16. Time in Status — avg/median/max time in each status (plan line 2833)
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/time-in-status")
def report_time_in_status():
    """Average, median, and max time tasks spend in each status.

    Plan Section 13, lines 2609-2637: includes median (computed in Python).
    """
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        rows = repo.execute_sql(
            """
            WITH durations AS (
                SELECT sh.task_id, sh.old_status,
                       julianday(sh.changed_at) - julianday(
                           LAG(sh.changed_at) OVER (PARTITION BY sh.task_id ORDER BY sh.changed_at)
                       ) AS days_in_status
                FROM task_status_history sh
                WHERE sh.task_id IN (SELECT id FROM tasks WHERE project_id = ?)
            )
            SELECT old_status as status, days_in_status
            FROM durations
            WHERE old_status IS NOT NULL
              AND days_in_status IS NOT NULL
              AND days_in_status > 0
            """,
            (project_id,),
        )
        # Group by status and compute avg, median, max in Python
        from collections import defaultdict
        groups = defaultdict(list)
        for r in rows:
            groups[r['status']].append(r['days_in_status'])

        statuses = []
        for status, durations in groups.items():
            durations.sort()
            count = len(durations)
            avg_days = round(sum(durations) / count, 2) if count else 0
            max_days = round(max(durations), 2) if count else 0
            mid = count // 2
            median_days = round(
                (durations[mid] + durations[mid - 1]) / 2 if count % 2 == 0 and count > 1
                else durations[mid],
                2
            ) if count > 0 else 0
            statuses.append({
                'status': status,
                'avg_days': avg_days,
                'median_days': median_days,
                'max_days': max_days,
                'transitions': count,
            })
        return jsonify({'statuses': statuses})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 17. Activity Log — chronological accomplishments (plan line 2841)
# ---------------------------------------------------------------------------

@bp.route("/api/kanban/report/activity-log")
def report_activity_log():
    """Chronological timeline of status changes — daily accomplishments log.

    Uses cursor-based pagination keyed on changed_at (plan line 2850-2852).
    Accepts ?start_date=&end_date= (default last 30 days).
    """
    try:
        repo = _get_repo()
        project_id = _get_project_id()
        start_date, end_date = _get_date_range()
        page_size = int(request.args.get('page_size', 50))
        cursor = request.args.get('cursor')  # ISO datetime cursor

        params = [project_id, start_date, end_date]
        cursor_clause = ''
        if cursor:
            cursor_clause = 'AND sh.changed_at < ?'
            params.append(cursor)

        params.append(page_size + 1)  # fetch one extra to detect has_more

        rows = repo.execute_sql(
            f"""
            SELECT sh.task_id, t.title,
                   sh.old_status, sh.new_status,
                   sh.changed_at,
                   DATE(sh.changed_at) as day,
                   TIME(sh.changed_at) as time
            FROM task_status_history sh
            JOIN tasks t ON sh.task_id = t.id
            WHERE t.project_id = ?
              AND DATE(sh.changed_at) >= ?
              AND DATE(sh.changed_at) <= ?
              {cursor_clause}
            ORDER BY sh.changed_at DESC
            LIMIT ?
            """,
            tuple(params),
        )

        has_more = len(rows) > page_size
        if has_more:
            rows = rows[:page_size]

        next_cursor = rows[-1]['changed_at'] if has_more and rows else None

        return jsonify({
            'entries': rows,
            'has_more': has_more,
            'next_cursor': next_cursor,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
