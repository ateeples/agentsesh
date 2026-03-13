"""Web UI server for sesh — human observability dashboard.

Serves a single-page dashboard with REST API endpoints backed by the
same Database layer as the CLI and MCP server. Zero external dependencies —
uses stdlib http.server.

Usage:
    sesh-web                    # default port 7433
    sesh-web --port 8080
    python -m sesh.web.server
"""

import json
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..config import Config, find_config, find_sesh_dir
from ..db import Database
from ..analyzers.trends import analyze_trends
from ..formatters.handoff import format_handoff

# Re-resolve DB per request to avoid stale connections across threads
_db_lock = threading.Lock()


def _get_db_path() -> Path:
    """Resolve the sesh database path."""
    db_path_env = os.environ.get("SESH_DB")
    if db_path_env:
        return Path(db_path_env)

    config_path = find_config()
    if config_path:
        config = Config(config_path)
        sesh_parent = config_path.parent.parent
        return sesh_parent / config.db_path

    sesh_dir = find_sesh_dir()
    if sesh_dir:
        return sesh_dir / "sesh.db"

    print("Error: No .sesh/ directory found. Run `sesh init` first.", file=sys.stderr)
    sys.exit(1)


def _json_response(handler: BaseHTTPRequestHandler, data, status: int = 200) -> None:
    """Send a JSON response."""
    body = json.dumps(data, indent=2, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, html: str, status: int = 200) -> None:
    """Send an HTML response."""
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class SeshHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the sesh dashboard."""

    db_path: Path  # Set by the server

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        routes = {
            "": self._serve_dashboard,
            "/api/sessions": self._api_sessions,
            "/api/sessions/latest": self._api_session_latest,
            "/api/stats": self._api_stats,
            "/api/report": self._api_report,
            "/api/search": self._api_search,
        }

        # Check for /api/sessions/<id> pattern
        if path.startswith("/api/sessions/") and path != "/api/sessions/latest":
            session_id = path[len("/api/sessions/"):]
            return self._api_session_detail(session_id)

        handler = routes.get(path)
        if handler:
            try:
                handler(params)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
        else:
            _json_response(self, {"error": "Not found"}, 404)

    def _serve_dashboard(self, params):
        """Serve the single-page dashboard."""
        _html_response(self, DASHBOARD_HTML)

    def _api_sessions(self, params):
        """List sessions."""
        limit = int(params.get("last", [50])[0])
        db = Database(self.db_path)
        try:
            sessions = db.list_sessions(limit=limit)
            _json_response(self, sessions)
        finally:
            db.close()

    def _api_session_latest(self, params):
        """Get the most recent session with full details."""
        db = Database(self.db_path)
        try:
            sessions = db.list_sessions(limit=1)
            if not sessions:
                _json_response(self, {"error": "No sessions"}, 404)
                return
            session = db.get_session(sessions[0]["id"])
            tool_calls = db.get_tool_calls(sessions[0]["id"])
            patterns = db.get_patterns(sessions[0]["id"])
            _json_response(self, {
                "session": session,
                "tool_calls": tool_calls,
                "patterns": patterns,
            })
        finally:
            db.close()

    def _api_session_detail(self, session_id: str):
        """Get full session detail."""
        db = Database(self.db_path)
        try:
            session = db.get_session(session_id)
            if not session:
                _json_response(self, {"error": "Session not found"}, 404)
                return
            tool_calls = db.get_tool_calls(session_id)
            patterns = db.get_patterns(session_id)
            _json_response(self, {
                "session": session,
                "tool_calls": tool_calls,
                "patterns": patterns,
            })
        finally:
            db.close()

    def _api_stats(self, params):
        """Aggregate stats."""
        db = Database(self.db_path)
        try:
            stats = db.get_stats()
            tool_stats = db.get_tool_stats()
            _json_response(self, {"stats": stats, "tool_stats": tool_stats})
        finally:
            db.close()

    def _api_report(self, params):
        """Trend report."""
        limit = int(params.get("last", [20])[0])
        db = Database(self.db_path)
        try:
            summaries = db.get_session_summaries(limit=limit)
            if not summaries:
                _json_response(self, {"error": "No sessions"}, 404)
                return
            report = analyze_trends(summaries)
            _json_response(self, {
                "sessions_analyzed": report.sessions_analyzed,
                "grade_trajectory": report.grade_trajectory,
                "grade_change": report.grade_change,
                "avg_score": report.avg_score,
                "avg_error_rate": report.avg_error_rate,
                "avg_bash_overuse": report.avg_bash_overuse,
                "avg_blind_edits": report.avg_blind_edits,
                "avg_parallel_missed": report.avg_parallel_missed,
                "recurring_patterns": dict(report.recurring_patterns),
                "pattern_frequency": dict(report.pattern_frequency),
                "grade_distribution": dict(report.grade_distribution),
                "sessions": [
                    {
                        "id": s.session_id,
                        "grade": s.grade,
                        "score": s.score,
                        "tool_calls": s.tool_calls,
                        "errors": s.errors,
                        "error_rate": s.error_rate,
                        "duration_minutes": s.duration_minutes,
                        "pattern_types": s.pattern_types,
                    }
                    for s in report.session_summaries
                ],
            })
        finally:
            db.close()

    def _api_search(self, params):
        """Full-text search."""
        query = params.get("q", [""])[0]
        limit = int(params.get("limit", [10])[0])
        if not query:
            _json_response(self, {"error": "Missing ?q= parameter"}, 400)
            return
        db = Database(self.db_path)
        try:
            results = db.search(query, limit=limit)
            _json_response(self, results)
        finally:
            db.close()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sesh — Agent Session Intelligence</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text-dim: #8b949e; --text-bright: #f0f6fc;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --blue: #58a6ff; --purple: #bc8cff;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
  }
  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }

  header { display: flex; align-items: baseline; gap: 16px; margin-bottom: 32px; }
  header h1 { font-size: 24px; font-weight: 600; color: var(--text-bright); }
  header .subtitle { color: var(--text-dim); font-size: 14px; }

  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px; margin-bottom: 32px;
  }
  .stat-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px;
  }
  .stat-card .label { font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
  .stat-card .value { font-size: 28px; font-weight: 600; margin-top: 4px; }
  .stat-card .value.grade { color: var(--green); }

  .section { margin-bottom: 32px; }
  .section h2 {
    font-size: 16px; font-weight: 600; color: var(--text-bright);
    margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border);
  }

  .grade-bar { display: flex; gap: 4px; height: 32px; margin-bottom: 16px; }
  .grade-bar .segment {
    border-radius: 4px; display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 600; min-width: 24px; transition: flex 0.3s;
  }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 12px; color: var(--text-dim); text-transform: uppercase;
       letter-spacing: 0.5px; padding: 8px 12px; border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 14px; }
  tr:hover td { background: var(--surface); }
  .mono { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; }

  .grade-badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 12px; font-weight: 600;
  }
  .grade-A, .grade-A\\+ { background: #0d2818; color: var(--green); }
  .grade-B { background: #1c1d0f; color: var(--yellow); }
  .grade-C { background: #2a1a0a; color: #e3b341; }
  .grade-D, .grade-F { background: #2d1117; color: var(--red); }

  .pattern-list { list-style: none; }
  .pattern-list li {
    padding: 8px 12px; border-left: 3px solid var(--border);
    margin-bottom: 4px; font-size: 14px;
  }
  .pattern-list li.warning { border-left-color: var(--yellow); }
  .pattern-list li.concern { border-left-color: var(--red); }

  .trend-indicator { font-size: 14px; }
  .trend-improving { color: var(--green); }
  .trend-declining { color: var(--red); }
  .trend-stable { color: var(--text-dim); }

  .search-box {
    width: 100%; padding: 10px 16px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); font-size: 14px; margin-bottom: 16px;
  }
  .search-box:focus { outline: none; border-color: var(--blue); }
  .search-box::placeholder { color: var(--text-dim); }

  .empty { text-align: center; padding: 48px; color: var(--text-dim); }

  .tool-chart { display: flex; flex-direction: column; gap: 4px; }
  .tool-row { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .tool-name { width: 140px; text-align: right; color: var(--text-dim); font-family: monospace; }
  .tool-bar-bg { flex: 1; height: 20px; background: var(--surface); border-radius: 3px; overflow: hidden; }
  .tool-bar { height: 100%; background: var(--blue); border-radius: 3px; transition: width 0.3s; }
  .tool-count { width: 60px; font-family: monospace; color: var(--text-dim); }

  @media (max-width: 768px) {
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .tool-name { width: 80px; font-size: 11px; }
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>sesh</h1>
    <span class="subtitle">Agent Session Intelligence</span>
    <span class="subtitle" id="trajectory"></span>
  </header>

  <div class="stats-grid" id="stats-grid"></div>

  <div class="section">
    <h2>Grade Distribution</h2>
    <div class="grade-bar" id="grade-bar"></div>
  </div>

  <div class="section">
    <h2>Tool Usage</h2>
    <div class="tool-chart" id="tool-chart"></div>
  </div>

  <div class="section">
    <h2>Recurring Patterns</h2>
    <ul class="pattern-list" id="pattern-list"></ul>
  </div>

  <div class="section">
    <h2>Sessions</h2>
    <input class="search-box" id="search" placeholder="Search sessions... (press Enter)" />
    <table>
      <thead>
        <tr>
          <th>Session</th><th>Grade</th><th>Score</th>
          <th>Tools</th><th>Errors</th><th>Duration</th><th>Date</th>
        </tr>
      </thead>
      <tbody id="session-table"></tbody>
    </table>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);

const gradeColors = {
  'A+': '#3fb950', 'A': '#3fb950', 'B': '#d29922',
  'C': '#e3b341', 'D': '#f85149', 'F': '#f85149'
};

async function api(path) {
  const r = await fetch('/api/' + path);
  if (!r.ok) return null;
  return r.json();
}

function gradeBadge(grade) {
  const cls = grade ? grade.replace('+', '\\\\+') : '';
  return `<span class="grade-badge grade-${grade || 'N'}">${grade || '?'}</span>`;
}

async function loadDashboard() {
  const [stats, report, sessions] = await Promise.all([
    api('stats'), api('report'), api('sessions?last=100')
  ]);

  // Stats cards
  if (stats && stats.stats) {
    const s = stats.stats;
    const avgGrade = s.avg_score >= 95 ? 'A+' : s.avg_score >= 90 ? 'A' :
      s.avg_score >= 75 ? 'B' : s.avg_score >= 60 ? 'C' : s.avg_score >= 45 ? 'D' : 'F';
    $('#stats-grid').innerHTML = `
      <div class="stat-card"><div class="label">Sessions</div><div class="value">${s.total_sessions}</div></div>
      <div class="stat-card"><div class="label">Avg Grade</div><div class="value grade">${avgGrade}</div></div>
      <div class="stat-card"><div class="label">Avg Score</div><div class="value">${(s.avg_score||0).toFixed(1)}</div></div>
      <div class="stat-card"><div class="label">Error Rate</div><div class="value">${((s.avg_error_rate||0)*100).toFixed(1)}%</div></div>
      <div class="stat-card"><div class="label">Total Calls</div><div class="value">${(s.total_tool_calls||0).toLocaleString()}</div></div>
      <div class="stat-card"><div class="label">Avg Duration</div><div class="value">${(s.avg_duration||0).toFixed(0)}m</div></div>
    `;
  }

  // Trajectory
  if (report) {
    const t = report.grade_trajectory || 'stable';
    const icon = t === 'improving' ? '↗' : t === 'declining' ? '↘' : '→';
    $('#trajectory').innerHTML = `<span class="trend-indicator trend-${t}">${icon} ${t}</span>`;
  }

  // Grade distribution
  if (report && report.grade_distribution) {
    const dist = report.grade_distribution;
    const total = Object.values(dist).reduce((a,b) => a+b, 0) || 1;
    const grades = ['A+', 'A', 'B', 'C', 'D', 'F'];
    $('#grade-bar').innerHTML = grades
      .filter(g => dist[g])
      .map(g => {
        const pct = (dist[g] / total) * 100;
        return `<div class="segment" style="flex:${pct};background:${gradeColors[g]}20;color:${gradeColors[g]}">${g} (${dist[g]})</div>`;
      }).join('');
  }

  // Tool usage chart
  if (stats && stats.tool_stats && stats.tool_stats.length) {
    const maxUses = Math.max(...stats.tool_stats.map(t => t.uses));
    $('#tool-chart').innerHTML = stats.tool_stats.slice(0, 15).map(t => {
      const pct = (t.uses / maxUses) * 100;
      return `<div class="tool-row">
        <span class="tool-name">${t.name}</span>
        <div class="tool-bar-bg"><div class="tool-bar" style="width:${pct}%"></div></div>
        <span class="tool-count">${t.uses}</span>
      </div>`;
    }).join('');
  }

  // Recurring patterns
  if (report && report.recurring_patterns) {
    const entries = Object.entries(report.recurring_patterns).sort((a,b) => b[1] - a[1]);
    if (entries.length) {
      $('#pattern-list').innerHTML = entries.map(([type, count]) => {
        const sev = count > 5 ? 'concern' : count > 2 ? 'warning' : '';
        return `<li class="${sev}">${type}: ${count} occurrences</li>`;
      }).join('');
    } else {
      $('#pattern-list').innerHTML = '<li>No recurring patterns detected</li>';
    }
  }

  // Session table
  if (sessions && sessions.length) {
    renderSessions(sessions);
  } else {
    $('#session-table').innerHTML = '<tr><td colspan="7" class="empty">No sessions yet. Run sesh init && sesh log to get started.</td></tr>';
  }
}

function renderSessions(sessions) {
  $('#session-table').innerHTML = sessions.map(s => {
    const dur = s.duration_minutes ? `${Math.round(s.duration_minutes)}m` : '?';
    const date = s.start_time ? new Date(s.start_time).toLocaleDateString() : s.ingested_at ? new Date(s.ingested_at).toLocaleDateString() : '?';
    return `<tr>
      <td class="mono">${(s.id||'').substring(0,12)}..</td>
      <td>${gradeBadge(s.grade)}</td>
      <td>${s.score || 0}</td>
      <td>${s.tool_call_count || 0}</td>
      <td>${s.error_count || 0}</td>
      <td>${dur}</td>
      <td class="mono" style="color:var(--text-dim)">${date}</td>
    </tr>`;
  }).join('');
}

// Search
$('#search').addEventListener('keydown', async (e) => {
  if (e.key !== 'Enter') return;
  const q = e.target.value.trim();
  if (!q) {
    const sessions = await api('sessions?last=100');
    if (sessions) renderSessions(sessions);
    return;
  }
  const results = await api('search?q=' + encodeURIComponent(q));
  if (results && results.length) {
    $('#session-table').innerHTML = results.map(r => `<tr>
      <td class="mono">${(r.session_id||'').substring(0,12)}..</td>
      <td>${gradeBadge(r.grade)}</td>
      <td>${r.score || 0}</td>
      <td colspan="3">${r.snippet || ''}</td>
      <td class="mono" style="color:var(--text-dim)">${r.ingested_at ? new Date(r.ingested_at).toLocaleDateString() : '?'}</td>
    </tr>`).join('');
  } else {
    $('#session-table').innerHTML = '<tr><td colspan="7" class="empty">No results found</td></tr>';
  }
});

// Auto-refresh every 30s
loadDashboard();
setInterval(loadDashboard, 30000);
</script>
</body>
</html>"""


def main():
    """Entry point for the web UI server."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="sesh-web",
        description="sesh web dashboard — agent session observability",
    )
    parser.add_argument("--port", type=int, default=7433, help="Port to serve on (default: 7433)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--db", help="Override database path")
    args = parser.parse_args()

    if args.db:
        os.environ["SESH_DB"] = args.db

    db_path = _get_db_path()
    SeshHandler.db_path = db_path

    server = HTTPServer((args.host, args.port), SeshHandler)
    print(f"sesh dashboard: http://{args.host}:{args.port}")
    print(f"database: {db_path}")
    print("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
