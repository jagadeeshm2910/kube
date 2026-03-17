from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from datetime import datetime, timezone
from collections import deque
import asyncio
import httpx
import os

app = FastAPI(title="KARA Dashboard")

ANALYST_URL      = os.getenv("ANALYST_URL",      "http://analyst-agent:8082")
INVESTIGATOR_URL = os.getenv("INVESTIGATOR_URL", "http://investigator-agent:8081")
WATCHER_URL      = os.getenv("WATCHER_URL",      "http://watcher-agent:8080")

incidents: deque        = deque(maxlen=500)
dismissed_ids: set      = set()
active_connections: list = []


async def broadcast(message: dict):
    dead = []
    for ws in active_connections:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            active_connections.remove(ws)
        except ValueError:
            pass


@app.post("/report")
async def receive_report(request: Request):
    body = await request.json()
    body["received_at"] = datetime.now(timezone.utc).isoformat()
    incidents.appendleft(body)
    asyncio.create_task(_forward_to_analyst(body))
    await broadcast({"type": "new_incident", "data": body})
    return {"status": "received", "incident_id": body.get("incident_id")}


async def _forward_to_analyst(body: dict):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{ANALYST_URL}/report", json=body, timeout=10)
    except Exception:
        pass


@app.get("/api/incidents")
async def list_incidents():
    return list(incidents)


@app.post("/api/incidents/{incident_id}/dismiss")
async def dismiss_incident(incident_id: str):
    dismissed_ids.add(incident_id)
    await broadcast({"type": "dismiss", "incident_id": incident_id})
    return {"status": "dismissed"}


@app.post("/api/incidents/{incident_id}/reinvestigate")
async def reinvestigate(incident_id: str):
    inc = next((i for i in incidents if i.get("incident_id") == incident_id), None)
    if not inc:
        return {"error": "incident not found"}
    payload = {
        "incident_id": f"INC-RE-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "namespace":   inc.get("namespace", "default"),
        "pod_name":    inc.get("pod_name"),
        "container_name": inc.get("container_name"),
        "reason":      inc.get("reason", "Unknown"),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "message":     "Re-investigation triggered from KARA Dashboard",
        "restart_count": 0,
        "node_name":   None,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{INVESTIGATOR_URL}/investigate", json=payload, timeout=30)
        return resp.json()


@app.post("/api/investigate")
async def manual_investigate(request: Request):
    body = await request.json()
    payload = {
        "incident_id":    f"INC-MANUAL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "namespace":      body.get("namespace", "default"),
        "pod_name":       body.get("pod_name"),
        "container_name": body.get("container_name"),
        "reason":         body.get("reason", "Unknown"),
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "message":        "Manual investigation triggered from KARA Dashboard",
        "restart_count":  0,
        "node_name":      None,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{INVESTIGATOR_URL}/investigate", json=payload, timeout=30)
        return resp.json()


@app.get("/api/agent-health")
async def agent_health():
    results = {}
    async with httpx.AsyncClient() as client:
        for name, url in [
            ("watcher",      WATCHER_URL),
            ("investigator", INVESTIGATOR_URL),
            ("analyst",      ANALYST_URL),
        ]:
            try:
                r = await client.get(f"{url}/health", timeout=3)
                results[name] = "healthy" if r.status_code == 200 else "degraded"
            except Exception:
                results[name] = "unreachable"
    results["dashboard"] = "healthy"
    return results


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    await websocket.send_json({
        "type":      "init",
        "incidents": list(incidents)[:100],
        "dismissed": list(dismissed_ids),
    })
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        try:
            active_connections.remove(websocket)
        except ValueError:
            pass


@app.get("/health")
@app.get("/ready")
@app.get("/live")
async def probe():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KARA – Kubernetes Autonomous Remediation Agents</title>
<style>
:root {
  --bg:      #0d1117;
  --bg2:     #161b22;
  --bg3:     #1c2330;
  --border:  #30363d;
  --text:    #e6edf3;
  --muted:   #7d8590;
  --green:   #3fb950;
  --red:     #f85149;
  --yellow:  #d29922;
  --blue:    #58a6ff;
  --purple:  #bc8cff;
  --orange:  #ffa657;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 13px; height: 100vh; overflow: hidden; display: flex; flex-direction: column; }

/* ── Header ── */
.header { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 10px 20px; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
.logo { font-size: 16px; font-weight: 700; letter-spacing: 0.5px; }
.logo span { color: var(--blue); }
.ws-pill { display: flex; align-items: center; gap: 6px; background: var(--bg3); border: 1px solid var(--border); border-radius: 20px; padding: 4px 10px; font-size: 11px; color: var(--muted); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--red); transition: background 0.3s; }
.dot.on { background: var(--green); box-shadow: 0 0 5px var(--green); }

/* ── Pipeline ── */
.pipeline { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 12px 20px; flex-shrink: 0; }
.pipeline-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 10px; }
.pipeline-row { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.agent-card { background: var(--bg3); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; min-width: 120px; text-align: center; position: relative; transition: border-color 0.2s; }
.agent-card.me { border-color: var(--blue); }
.agent-card .aname { font-weight: 600; font-size: 12px; }
.agent-card .arole { font-size: 10px; color: var(--muted); margin-top: 2px; }
.agent-card .astatus { position: absolute; top: 7px; right: 7px; width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.agent-card .astatus.healthy    { background: var(--green); box-shadow: 0 0 5px var(--green); }
.agent-card .astatus.degraded   { background: var(--yellow); }
.agent-card .astatus.unreachable{ background: var(--red); }
.arrow { color: var(--muted); font-size: 18px; padding: 0 2px; }
.stat-box { margin-left: auto; text-align: right; padding-left: 20px; }
.stat-num { font-size: 24px; font-weight: 700; color: var(--blue); line-height: 1; }
.stat-lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; }

/* ── Main layout ── */
.main { display: flex; flex: 1; overflow: hidden; }
.left  { display: flex; flex-direction: column; flex: 1; overflow: hidden; border-right: 1px solid var(--border); }
.right { width: 360px; flex-shrink: 0; display: flex; flex-direction: column; background: var(--bg2); overflow: hidden; }

/* ── Toolbar ── */
.toolbar { padding: 10px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; background: var(--bg2); flex-shrink: 0; }
.toolbar-title { flex: 1; font-size: 13px; font-weight: 600; }
.badge { background: var(--bg3); border: 1px solid var(--border); border-radius: 10px; padding: 1px 8px; font-size: 11px; color: var(--muted); }
.btn { background: var(--bg3); border: 1px solid var(--border); color: var(--text); border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 11px; font-family: inherit; transition: all 0.1s; white-space: nowrap; }
.btn:hover { border-color: var(--blue); color: var(--blue); }
.btn.red:hover { border-color: var(--red); color: var(--red); }
.btn.primary { background: var(--blue); border-color: var(--blue); color: #000; font-weight: 600; }
.btn.primary:hover { background: #79c0ff; border-color: #79c0ff; }

/* ── Table ── */
.tbl-wrap { flex: 1; overflow-y: auto; }
table { width: 100%; border-collapse: collapse; }
thead th { position: sticky; top: 0; z-index: 2; background: var(--bg2); border-bottom: 1px solid var(--border); padding: 7px 12px; text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); font-weight: 500; }
tbody tr { cursor: pointer; transition: background 0.1s; }
tbody tr:hover td { background: var(--bg3); }
tbody tr.sel td { background: #1a2332 !important; }
tbody tr.sel td:first-child { border-left: 2px solid var(--blue); }
tbody tr.faded td { opacity: 0.38; }
td { padding: 7px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; max-width: 0; }
.mono { font-family: 'SFMono-Regular', 'Consolas', monospace; font-size: 11px; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.badge-reason { display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 10px; font-weight: 600; white-space: nowrap; }
.r-CrashLoopBackOff { background: rgba(248,81,73,.15); color: #ff7b72; border: 1px solid rgba(248,81,73,.35); }
.r-OOMKilled        { background: rgba(255,166,87,.15); color: var(--orange); border: 1px solid rgba(255,166,87,.35); }
.r-ImagePullBackOff,
.r-ErrImagePull     { background: rgba(188,140,255,.15); color: var(--purple); border: 1px solid rgba(188,140,255,.35); }
.r-HighRestartCount { background: rgba(210,153,34,.15); color: var(--yellow); border: 1px solid rgba(210,153,34,.35); }
.r-Failed           { background: rgba(248,81,73,.10); color: #ff9492; border: 1px solid rgba(248,81,73,.25); }
.r-default          { background: rgba(125,133,144,.15); color: var(--muted); border: 1px solid var(--border); }

.flash { animation: flashRow 1.4s ease; }
@keyframes flashRow { 0%,10% { background: rgba(88,166,255,.18) !important; } 100% { background: transparent; } }

.empty-row td { text-align: center; padding: 48px 20px; color: var(--muted); }

/* ── Detail panel ── */
.detail-scroll { flex: 1; overflow-y: auto; }
.no-sel { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: var(--muted); gap: 10px; opacity: 0.6; }
.no-sel svg { opacity: 0.5; }

.d-header { padding: 14px 16px; border-bottom: 1px solid var(--border); }
.d-header .d-id { font-size: 11px; font-family: monospace; color: var(--muted); margin-bottom: 4px; word-break: break-all; }
.d-header .d-pod { font-size: 14px; font-weight: 600; word-break: break-all; }
.d-header .d-badge { margin-top: 6px; }

.d-section { padding: 10px 16px; border-bottom: 1px solid var(--border); }
.d-section h4 { font-size: 10px; text-transform: uppercase; letter-spacing: 0.7px; color: var(--muted); margin-bottom: 8px; font-weight: 500; }
.kv { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 4px; }
.kv .k { color: var(--muted); flex-shrink: 0; }
.kv .v { color: var(--text); text-align: right; word-break: break-all; font-family: monospace; font-size: 11px; }

.rec-list { list-style: none; }
.rec-list li { padding: 3px 0 3px 16px; position: relative; color: var(--muted); line-height: 1.5; }
.rec-list li::before { content: '→'; position: absolute; left: 0; color: var(--blue); }

.log-block { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; max-height: 180px; overflow-y: auto; font-family: monospace; font-size: 10px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; color: #8b949e; }

.ev { padding: 4px 0; border-bottom: 1px solid rgba(48,54,61,0.5); font-size: 11px; line-height: 1.5; }
.ev:last-child { border-bottom: none; }
.ev-warn   { color: var(--yellow); }
.ev-normal { color: var(--muted); }

.d-actions { padding: 10px 16px; display: flex; gap: 8px; border-bottom: 1px solid var(--border); }

/* ── Investigate form ── */
.inv-form { padding: 14px 16px; flex-shrink: 0; border-top: 1px solid var(--border); }
.inv-form h3 { font-size: 12px; font-weight: 600; margin-bottom: 10px; color: var(--text); letter-spacing: 0.3px; }
.frow { margin-bottom: 7px; }
.frow label { display: block; font-size: 10px; color: var(--muted); margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.5px; }
.frow input, .frow select { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text); border-radius: 5px; padding: 5px 9px; font-size: 12px; font-family: inherit; outline: none; transition: border-color 0.15s; }
.frow input:focus, .frow select:focus { border-color: var(--blue); }
.frow select option { background: var(--bg2); }
.inv-status { margin-top: 7px; font-size: 11px; color: var(--muted); min-height: 16px; }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="logo">K<span>ARA</span> &nbsp;<span style="font-weight:300;color:var(--muted);font-size:12px;">Kubernetes Autonomous Remediation Agents</span></div>
  <div class="ws-pill"><div class="dot" id="wsdot"></div><span id="wslabel">Connecting…</span></div>
</div>

<!-- Pipeline -->
<div class="pipeline">
  <div class="pipeline-label">Live Pipeline</div>
  <div class="pipeline-row">
    <div class="agent-card">
      <div class="astatus" id="s-watcher"></div>
      <div class="aname">Watcher</div>
      <div class="arole">K8s event stream</div>
    </div>
    <div class="arrow">→</div>
    <div class="agent-card">
      <div class="astatus" id="s-investigator"></div>
      <div class="aname">Investigator</div>
      <div class="arole">Collects diagnostics</div>
    </div>
    <div class="arrow">→</div>
    <div class="agent-card me">
      <div class="astatus healthy" id="s-dashboard"></div>
      <div class="aname">Dashboard</div>
      <div class="arole">Visualize &amp; control</div>
    </div>
    <div class="arrow">→</div>
    <div class="agent-card">
      <div class="astatus" id="s-analyst"></div>
      <div class="aname">Analyst</div>
      <div class="arole">Root cause + report</div>
    </div>
    <div class="stat-box">
      <div class="stat-num" id="total-count">0</div>
      <div class="stat-lbl">incidents</div>
    </div>
  </div>
</div>

<!-- Main -->
<div class="main">

  <!-- Left: incident table -->
  <div class="left">
    <div class="toolbar">
      <span class="toolbar-title">Incidents</span>
      <span class="badge" id="active-badge">0 active</span>
      <button class="btn" onclick="clearDismissed()">Clear dismissed</button>
      <button class="btn red" onclick="dismissAll()">Dismiss all</button>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead>
          <tr>
            <th style="width:72px">Time</th>
            <th>Pod</th>
            <th style="width:90px">Namespace</th>
            <th style="width:150px">Reason</th>
            <th style="width:32px"></th>
          </tr>
        </thead>
        <tbody id="tbody">
          <tr class="empty-row"><td colspan="5">No incidents yet — waiting for KARA detections…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Right: detail + form -->
  <div class="right">
    <div class="detail-scroll" id="detail-scroll">
      <div class="no-sel" id="no-sel">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><circle cx="12" cy="16" r="0.5" fill="currentColor"/></svg>
        <span style="font-size:12px;">Select an incident to view details</span>
      </div>
      <div id="detail-body" style="display:none;"></div>
    </div>
    <div class="inv-form">
      <h3>Manual Investigation</h3>
      <div class="frow"><label>Namespace</label><input id="f-ns" type="text" placeholder="wp-dev" value="wp-dev"></div>
      <div class="frow"><label>Pod Name</label><input id="f-pod" type="text" placeholder="wordpress-abc123"></div>
      <div class="frow">
        <label>Reason</label>
        <select id="f-reason">
          <option value="Unknown">Unknown</option>
          <option value="CrashLoopBackOff">CrashLoopBackOff</option>
          <option value="OOMKilled">OOMKilled</option>
          <option value="ImagePullBackOff">ImagePullBackOff</option>
          <option value="HighRestartCount">HighRestartCount</option>
          <option value="Failed">Failed</option>
        </select>
      </div>
      <button class="btn primary" style="width:100%;margin-top:4px;" onclick="triggerInvestigation()">Investigate Pod</button>
      <div class="inv-status" id="inv-status"></div>
    </div>
  </div>

</div>

<script>
'use strict';
let ws;
let incidents = [];
let dismissed = new Set();
let selectedId = null;

/* ── WebSocket ── */
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');

  ws.onopen = () => {
    document.getElementById('wsdot').className = 'dot on';
    document.getElementById('wslabel').textContent = 'Connected';
  };
  ws.onclose = () => {
    document.getElementById('wsdot').className = 'dot';
    document.getElementById('wslabel').textContent = 'Reconnecting…';
    setTimeout(connect, 3000);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'init') {
      incidents = msg.incidents || [];
      dismissed = new Set(msg.dismissed || []);
      renderTable();
    } else if (msg.type === 'new_incident') {
      incidents.unshift(msg.data);
      renderTable(msg.data.incident_id);
      updateCounts();
    } else if (msg.type === 'dismiss') {
      dismissed.add(msg.incident_id);
      renderTable();
      updateCounts();
    }
    updateCounts();
  };
}

/* ── Helpers ── */
function ftime(ts) {
  if (!ts) return '—';
  try { return new Date(ts).toLocaleTimeString(); } catch { return ts; }
}
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function rclass(r) {
  const m = {
    CrashLoopBackOff: 'r-CrashLoopBackOff',
    OOMKilled:        'r-OOMKilled',
    ImagePullBackOff: 'r-ImagePullBackOff',
    ErrImagePull:     'r-ErrImagePull',
    HighRestartCount: 'r-HighRestartCount',
    Failed:           'r-Failed',
  };
  return m[r] || 'r-default';
}
function rbadge(r) {
  return '<span class="badge-reason ' + rclass(r) + '">' + esc(r || '—') + '</span>';
}

/* ── Table ── */
function updateCounts() {
  const active = incidents.filter(i => !dismissed.has(i.incident_id)).length;
  document.getElementById('total-count').textContent = incidents.length;
  document.getElementById('active-badge').textContent = active + ' active';
}

function renderTable(flashId) {
  const tbody = document.getElementById('tbody');
  if (!incidents.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No incidents yet — waiting for KARA detections…</td></tr>';
    return;
  }
  tbody.innerHTML = incidents.map(inc => {
    const id   = inc.incident_id || '';
    const pod  = inc.pod_name || '—';
    const ns   = inc.namespace || '—';
    const r    = inc.reason || '—';
    const ts   = inc.received_at || inc.timestamp;
    const cls  = [
      dismissed.has(id) ? 'faded' : '',
      id === selectedId ? 'sel' : '',
      id === flashId    ? 'flash' : '',
    ].join(' ');
    return `<tr class="${cls}" onclick="selectIncident('${esc(id)}')">
      <td class="mono">${ftime(ts)}</td>
      <td class="mono ellipsis" title="${esc(pod)}" style="max-width:1px;">${esc(pod)}</td>
      <td class="mono ellipsis" style="color:var(--muted);max-width:1px;" title="${esc(ns)}">${esc(ns)}</td>
      <td>${rbadge(r)}</td>
      <td><button class="btn" style="padding:1px 6px;" onclick="event.stopPropagation();dismissOne('${esc(id)}')">✕</button></td>
    </tr>`;
  }).join('');
}

/* ── Detail panel ── */
function selectIncident(id) {
  selectedId = id;
  renderTable();
  const inc = incidents.find(i => i.incident_id === id);
  if (!inc) return;

  document.getElementById('no-sel').style.display = 'none';
  const body = document.getElementById('detail-body');
  body.style.display = 'block';

  const cs      = inc.container_statuses || [];
  const evts    = inc.events || [];
  const recs    = inc.recommendations || [];
  const logs    = inc.logs || {};
  const podInfo = inc.pod_info || {};
  const dep     = inc.deployment_config || {};

  /* logs — handle {current_logs:[],previous_logs:[]} or {container:{current:[],previous:[]}} */
  let curLogs = [], prevLogs = [];
  if (Array.isArray(logs.current_logs)) {
    curLogs  = logs.current_logs;
    prevLogs = logs.previous_logs || [];
  } else {
    Object.values(logs).forEach(v => {
      if (v && Array.isArray(v.current))  curLogs  = curLogs.concat(v.current);
      if (v && Array.isArray(v.previous)) prevLogs = prevLogs.concat(v.previous);
    });
  }

  const restarts = cs.length ? cs[0].restart_count : (inc.restart_count ?? '—');

  body.innerHTML = `
<div class="d-header">
  <div class="d-id">${esc(inc.incident_id)}</div>
  <div class="d-pod">${esc(inc.pod_name || '—')}</div>
  <div class="d-badge">${rbadge(inc.reason)}</div>
</div>

<div class="d-actions">
  <button class="btn" style="flex:1;" onclick="reinvestigate('${esc(id)}')">↻ Re-investigate</button>
  <button class="btn red" style="flex:1;" onclick="dismissOne('${esc(id)}')">✕ Dismiss</button>
</div>

<div class="d-section">
  <h4>Incident</h4>
  <div class="kv"><span class="k">Namespace</span><span class="v">${esc(inc.namespace||'—')}</span></div>
  <div class="kv"><span class="k">Container</span><span class="v">${esc(inc.container_name||'—')}</span></div>
  <div class="kv"><span class="k">Restarts</span><span class="v">${esc(restarts)}</span></div>
  <div class="kv"><span class="k">Detected</span><span class="v">${ftime(inc.received_at||inc.timestamp)}</span></div>
  ${inc.investigation_duration_ms ? `<div class="kv"><span class="k">Investigated in</span><span class="v">${inc.investigation_duration_ms.toFixed(0)} ms</span></div>` : ''}
</div>

${inc.summary ? `<div class="d-section"><h4>Summary</h4><div style="color:var(--muted);font-size:12px;line-height:1.6;">${esc(inc.summary)}</div></div>` : ''}

${recs.length ? `<div class="d-section"><h4>Recommendations</h4><ul class="rec-list">${recs.map(r => `<li>${esc(r)}</li>`).join('')}</ul></div>` : ''}

${cs.length ? `<div class="d-section"><h4>Container Status</h4>${cs.map(c => `
  <div class="kv"><span class="k">${esc(c.name)}</span><span class="v">${esc(c.state||'—')}${c.state_reason ? ' / '+esc(c.state_reason) : ''}</span></div>
  ${c.image ? `<div class="kv"><span class="k">Image</span><span class="v">${esc(c.image)}</span></div>` : ''}
  ${c.restart_count != null ? `<div class="kv"><span class="k">Restarts</span><span class="v">${c.restart_count}</span></div>` : ''}
`).join('<hr style="border:none;border-top:1px solid var(--border);margin:4px 0;">')}</div>` : ''}

${(dep.memory_limit || dep.cpu_limit || dep.replicas != null) ? `<div class="d-section"><h4>Deployment</h4>
  ${dep.replicas != null ? `<div class="kv"><span class="k">Replicas</span><span class="v">${dep.available_replicas ?? '?'} / ${dep.replicas}</span></div>` : ''}
  ${dep.memory_limit ? `<div class="kv"><span class="k">Memory limit</span><span class="v">${esc(dep.memory_limit)}</span></div>` : ''}
  ${dep.cpu_limit ? `<div class="kv"><span class="k">CPU limit</span><span class="v">${esc(dep.cpu_limit)}</span></div>` : ''}
  ${dep.image ? `<div class="kv"><span class="k">Image</span><span class="v">${esc(dep.image)}</span></div>` : ''}
</div>` : ''}

${(podInfo.node_name || podInfo.pod_ip) ? `<div class="d-section"><h4>Pod Info</h4>
  ${podInfo.node_name ? `<div class="kv"><span class="k">Node</span><span class="v">${esc(podInfo.node_name)}</span></div>` : ''}
  ${podInfo.pod_ip ? `<div class="kv"><span class="k">IP</span><span class="v">${esc(podInfo.pod_ip)}</span></div>` : ''}
  ${podInfo.phase ? `<div class="kv"><span class="k">Phase</span><span class="v">${esc(podInfo.phase)}</span></div>` : ''}
</div>` : ''}

${evts.length ? `<div class="d-section"><h4>Events (${evts.length})</h4>${evts.slice(0,15).map(ev => `
  <div class="ev">
    <span class="${ev.type==='Warning'?'ev-warn':'ev-normal'}">[${esc(ev.type||'Normal')}]</span>
    <span style="color:var(--text);margin-left:4px;">${esc(ev.reason||'')}</span>
    <span style="color:var(--muted);margin-left:4px;">${esc(ev.message||'')}</span>
  </div>`).join('')}</div>` : ''}

${curLogs.length ? `<div class="d-section"><h4>Logs (current)</h4><div class="log-block">${curLogs.slice(-40).map(esc).join('\n')}</div></div>` : ''}
${prevLogs.length ? `<div class="d-section"><h4>Logs (previous)</h4><div class="log-block">${prevLogs.slice(-20).map(esc).join('\n')}</div></div>` : ''}
  `;
  document.getElementById('detail-scroll').scrollTop = 0;
}

/* ── Actions ── */
async function dismissOne(id) {
  dismissed.add(id);
  await fetch('/api/incidents/' + encodeURIComponent(id) + '/dismiss', { method: 'POST' });
  if (selectedId === id) {
    selectedId = null;
    document.getElementById('no-sel').style.display = 'flex';
    document.getElementById('detail-body').style.display = 'none';
  }
  renderTable();
  updateCounts();
}

function dismissAll() {
  incidents.forEach(i => dismissed.add(i.incident_id));
  renderTable();
  updateCounts();
}

function clearDismissed() {
  incidents = incidents.filter(i => !dismissed.has(i.incident_id));
  dismissed.clear();
  renderTable();
  updateCounts();
}

async function reinvestigate(id) {
  const st = document.getElementById('detail-body').querySelector('.d-actions .btn');
  try {
    const resp = await fetch('/api/incidents/' + encodeURIComponent(id) + '/reinvestigate', { method: 'POST' });
    if (resp.ok) alert('Re-investigation started — check the incident feed.');
    else alert('Error: ' + await resp.text());
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function triggerInvestigation() {
  const ns  = document.getElementById('f-ns').value.trim();
  const pod = document.getElementById('f-pod').value.trim();
  const rsn = document.getElementById('f-reason').value;
  const st  = document.getElementById('inv-status');

  if (!ns || !pod) {
    st.style.color = 'var(--red)';
    st.textContent = 'Namespace and pod name are required.';
    return;
  }
  st.style.color = 'var(--muted)';
  st.textContent = 'Investigating…';
  try {
    const resp = await fetch('/api/investigate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ namespace: ns, pod_name: pod, reason: rsn }),
    });
    if (resp.ok) {
      st.style.color = 'var(--green)';
      st.textContent = '✓ Report will appear in the feed.';
    } else {
      st.style.color = 'var(--red)';
      st.textContent = 'Error: ' + await resp.text();
    }
  } catch(e) {
    st.style.color = 'var(--red)';
    st.textContent = 'Failed: ' + e.message;
  }
}

/* ── Agent health ── */
async function refreshHealth() {
  try {
    const data = await fetch('/api/agent-health').then(r => r.json());
    ['watcher','investigator','analyst','dashboard'].forEach(n => {
      const el = document.getElementById('s-' + n);
      if (el) el.className = 'astatus ' + (data[n] || 'unreachable');
    });
  } catch {}
}

connect();
refreshHealth();
setInterval(refreshHealth, 15000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8083)
