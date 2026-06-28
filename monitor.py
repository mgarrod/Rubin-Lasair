#!/usr/bin/env python3
"""
Lasair real-time alert monitor.

Subscribes to one of your Lasair *active* (streaming) filters over Kafka and
shows each incoming alert as a new row on a live web page. Initially it just
displays the raw data of each alert — whatever attributes your filter's SELECT
produces, plus Lasair's added UTC timestamp.

ARCHITECTURE
  - A background thread runs the Kafka consumer loop, appending each alert to an
    in-memory buffer.
  - A Flask app serves a page that polls /api/alerts every few seconds and
    appends any new alerts as table rows (newest on top).
  Two threads, one process. No database — the buffer is in memory and resets on
  restart (bounded so it can't grow without limit).

PREREQUISITES
  pip install confluent_kafka lasair flask

  You must have an ACTIVE (streaming) filter in Lasair set to a "kafka stream"
  option (plain, or lite-lightcurve). Its TOPIC name is shown on the filter's
  detail page — it looks like 'lasair_2MyFilterName' (the string 'lasair_' + the
  filter ID + a squashed version of the filter name).

CONFIGURE — put these in a .env file (copy .env.example to .env)
  LASAIR_TOPICS     required. Comma-separated topic names, one per streaming
                    filter, e.g. 'lasair_2CVfilter, lasair_3Pulsators'. Each
                    topic name is on its filter's detail page.
                    (LASAIR_TOPIC, singular, is still accepted for one topic.)
  LASAIR_GROUP_ID   optional. Identifies you to Kafka. Reuse to resume; change to
                    replay the ~7-day backlog. Default 'moeller-monitor-1'. Each
                    topic gets its own offset (this value + a per-topic suffix).
  LASAIR_KAFKA      optional. Default 'lasair-lsst-kafka.lsst.ac.uk:9092'.
  MONITOR_PORT      optional. Default 5001 (runs alongside the main app on 5000).

RUN
  cp .env.example .env    # then edit .env with your topics
  python monitor.py
  # open http://127.0.0.1:5001

  Try the layout with no Kafka connection (two synthetic topics):
  python monitor.py --demo
"""

import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

try:
    from flask import Flask, jsonify, render_template_string
except ImportError:
    sys.exit("Flask required: pip install flask")

# ---------------------------------------------------------------------------
# Shared state between the Kafka thread and the web thread.
# deque with maxlen = automatically bounded; oldest drop off the end.
ALERTS = deque(maxlen=2000)
ALERTS_LOCK = threading.Lock()
SEQ = {"n": 0}            # monotonic id so the page can ask "anything after id X?"
# Per-topic status, keyed by topic name. Each: state/detail/received/last_msg_utc.
STATUS = {}
STATUS_LOCK = threading.Lock()


def _set_status(topic, **kw):
    with STATUS_LOCK:
        s = STATUS.setdefault(topic, {"topic": topic, "state": "starting",
                                      "detail": "", "received": 0,
                                      "last_msg_utc": None})
        s.update(kw)


def _record_alert(topic, obj):
    """Add one alert dict to the buffer, tagged with its source topic."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(obj, dict) and isinstance(obj.get("diaObjectId"), int):
        obj = {**obj, "diaObjectId": str(obj["diaObjectId"])}
    with ALERTS_LOCK:
        SEQ["n"] += 1
        ALERTS.appendleft({
            "_seq": SEQ["n"],
            "_topic": topic,
            "_arrived_utc": now,
            "data": obj,
        })
    with STATUS_LOCK:
        s = STATUS.setdefault(topic, {"topic": topic, "state": "listening",
                                      "detail": "", "received": 0,
                                      "last_msg_utc": None})
        s["received"] += 1
        s["last_msg_utc"] = now


# ---------------------------------------------------------------------------
# Kafka consumer thread
def kafka_loop(kafka_server, group_id, topic, stop_event):
    """Run the Lasair Kafka consumer, recording every alert. Reconnects on
    error with backoff. Lasair's stream can be quiet or briefly unavailable, so
    a None poll is normal (not an error) and we just keep waiting."""
    try:
        from lasair import lasair_consumer
    except ImportError:
        _set_status(topic, state="error",
                    detail="lasair client not installed (pip install lasair confluent_kafka)")
        return

    backoff = 5
    while not stop_event.is_set():
        try:
            _set_status(topic, state="connecting", detail=f"{kafka_server}")
            consumer = lasair_consumer(kafka_server, group_id, topic)
            _set_status(topic, state="listening", detail="")
            backoff = 5  # reset after a successful connect
            while not stop_event.is_set():
                msg = consumer.poll(timeout=20)
                if msg is None:
                    # No new alerts in this window — normal. Keep polling.
                    continue
                if msg.error():
                    _set_status(topic, state="stream-error", detail=str(msg.error()))
                    break  # drop out to reconnect
                try:
                    obj = json.loads(msg.value())
                except (ValueError, TypeError) as e:
                    # Record the raw payload so nothing is silently lost.
                    obj = {"_unparsed": str(msg.value())[:500], "_error": str(e)}
                _record_alert(topic, obj)
            try:
                consumer.close()
            except Exception:
                pass
        except Exception as e:  # noqa: BLE401
            _set_status(topic, state="reconnecting", detail=str(e))
            # wait with backoff, but stay responsive to stop_event
            for _ in range(backoff):
                if stop_event.is_set():
                    break
                time.sleep(1)
            backoff = min(backoff * 2, 60)
    _set_status(topic, state="stopped")


# ---------------------------------------------------------------------------
# Demo thread — synthesizes alerts so you can see the page work with no Kafka.
def demo_loop(stop_event):
    _set_status("lasair_demoA", state="listening (demo)")
    _set_status("lasair_demoB", state="listening (demo)")
    import random
    while not stop_event.is_set():
        topic = random.choice(["lasair_demoA", "lasair_demoB"])
        _record_alert(topic, {
            "diaObjectId": 170028521667166400 + random.randint(0, 9999),
            "ra": round(random.uniform(0, 360), 5),
            "decl": round(random.uniform(-30, 60), 5),
            "gmag": round(random.uniform(14, 21), 3),
            "jump1": round(random.choice([0.0, 0.0, random.uniform(0, 8)]), 3),
            "nPosDiaSources": random.randint(1, 300),
            "classification": random.choice(["VS", "VS", "VS", "CV"]),
            "UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })
        for _ in range(random.randint(2, 6)):
            if stop_event.is_set():
                break
            time.sleep(1)


# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/alerts")
def api_alerts():
    """Return alerts newer than ?after=<seq>, plus current status. The page
    calls this on a timer and appends whatever's new."""
    from flask import request
    after = request.args.get("after", "0")
    try:
        after = int(after)
    except ValueError:
        after = 0
    with ALERTS_LOCK:
        fresh = [a for a in ALERTS if a["_seq"] > after]
        all_alerts = list(ALERTS)
    # collect column order from the full buffer so headers never disappear
    preferred = ["diaObjectId", "ra", "decl", "gmag", "rmag",
                 "jump1", "nPosDiaSources", "classification", "UTC"]
    seen = set()
    for a in all_alerts:
        d = a.get("data", {})
        if isinstance(d, dict):
            seen.update(d.keys())
    ordered = [c for c in preferred if c in seen] + \
              [c for c in sorted(seen) if c not in preferred]
    with STATUS_LOCK:
        statuses = [dict(s) for s in STATUS.values()]
    total = sum(s.get("received", 0) for s in statuses)
    return jsonify({
        "alerts": fresh,
        "columns": ordered,
        "statuses": statuses,
        "total_received": total,
        "max_seq": SEQ["n"],
    })


@app.route("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lasair Live Alert Monitor</title>
<style>
  :root{--void:#0a0e14;--panel:#121821;--line:#243240;--ink:#e8eef4;
    --dim:#7d909f;--flare:#ff7a3c;--pulse:#3ca7ff;--grid:#1a2330;--ok:#6fe0a0}
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--void);color:var(--ink);overflow:hidden;
    font:14px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    display:flex;flex-direction:column}
  header{padding:18px 24px 14px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:16px;letter-spacing:.14em;text-transform:uppercase;font-weight:600}
  h1 .flare{color:var(--flare)} h1 .pulse{color:var(--pulse)}
  .statusbar{display:flex;gap:18px;align-items:center;margin-top:8px;
    color:var(--dim);font-size:12px;letter-spacing:.03em;flex-wrap:wrap}
  .streams{display:flex;gap:10px;flex-wrap:wrap}
  .stream{display:flex;align-items:center;gap:7px;padding:3px 10px;
    border:1px solid var(--line);border-radius:20px}
  .stream .nm{color:var(--ink)} .stream .rc{color:var(--dim)}
  .stream .err{color:#ff8a8a;font-size:11px}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;
    background:var(--dim)}
  .dot.listening{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .dot.connecting,.dot.reconnecting,.dot.starting{background:var(--flare)}
  .dot.error,.dot.stream-error{background:#ff5a5a}
  .pill{padding:2px 9px;border:1px solid var(--line);border-radius:20px}
  main{flex:1;overflow:auto;min-height:0;padding:0 24px 24px}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{position:sticky;top:0;z-index:2;background:var(--void);text-align:left;
    color:var(--dim);font-size:10px;letter-spacing:.1em;text-transform:uppercase;
    padding:10px 12px;box-shadow:inset 0 -1px 0 var(--line);white-space:nowrap}
  td{padding:9px 12px;border-bottom:1px solid var(--grid);white-space:nowrap;
    font-variant-numeric:tabular-nums}
  tr.new td{animation:flash 1.4s ease-out}
  @keyframes flash{from{background:rgba(255,122,60,.22)}to{background:transparent}}
  td.meta{color:var(--dim)}
  .oid a{color:var(--pulse);text-decoration:none;border-bottom:1px dotted}
  .tag{font-size:10px;padding:2px 7px;border-radius:20px}
  .tag.cv{background:rgba(255,122,60,.15);color:var(--flare);border:1px solid rgba(255,122,60,.35)}
  .tag.vs{background:rgba(60,167,255,.15);color:var(--pulse);border:1px solid rgba(60,167,255,.35)}
  .tag.sn{background:rgba(255,0,0,.15);color:var(--pulse);border:1px solid rgba(255,0,0,.35)}
  .empty{padding:48px;text-align:center;color:var(--dim)}
  .count{color:var(--ink);font-weight:700}
  .locate-btn{background:none;border:none;cursor:pointer;color:var(--dim);padding:2px 5px;border-radius:3px;line-height:1;display:inline-flex;align-items:center}
  .locate-btn:hover{color:var(--pulse)}
  #atlas{position:relative;flex:2;min-height:0;border-bottom:1px solid var(--line)}
  #aladin-lite-div{position:absolute;inset:0;width:100%;height:100%}
  #flash-canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:10}
</style>
</head>
<body>
<header>
  <h1>RUBIN LSST/LASAIR <span class="flare">LIVE</span> <span class="pulse">ALERTS</span></h1>
  <div class="statusbar">
    <span><span class="count" id="count">0</span> alerts received</span>
    <span id="streams" class="streams"></span>
  </div>
</header>
<section id="atlas">
  <div id="aladin-lite-div"></div>
  <canvas id="flash-canvas"></canvas>
</section>
<main>
  <table>
    <thead><tr id="head"><th>#</th><th>Topic</th><th>Arrived (UTC)</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="3" class="empty">Waiting for alerts…</td></tr></tbody>
  </table>
</main>
<script src="https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js"></script>
<script>
let lastSeq = 0;
let aladin;
const flashCanvas = document.getElementById('flash-canvas');
const flashCtx = flashCanvas.getContext('2d');
const FLASH_COLORS = { CV:'255,122,60', VS:'60,167,255', SN:'255,0,0' };
const FLASH_DURATION = 3000;
const activeFlashes = [];
let flashLoopRunning = false;

function resizeFlashCanvas(){
  flashCanvas.width  = flashCanvas.offsetWidth;
  flashCanvas.height = flashCanvas.offsetHeight;
}
resizeFlashCanvas();
window.addEventListener('resize', resizeFlashCanvas);

function drawFlashes(now){
  for(let i = activeFlashes.length - 1; i >= 0; i--)
    if(now - activeFlashes[i].start >= FLASH_DURATION) activeFlashes.splice(i, 1);
  flashCtx.clearRect(0, 0, flashCanvas.width, flashCanvas.height);
  for(const f of activeFlashes){
    const t = Math.min((now - f.start) / FLASH_DURATION, 1);
    const op = 1 - t;
    flashCtx.beginPath();
    flashCtx.arc(f.x, f.y, 6 + t * 28, 0, Math.PI * 2);
    flashCtx.strokeStyle = `rgba(${f.rgb},${op * 0.9})`;
    flashCtx.lineWidth = 2;
    flashCtx.stroke();
    const arm = 10 + t * 8, gap = 4;
    flashCtx.strokeStyle = `rgba(${f.rgb},${op})`;
    flashCtx.lineWidth = 1.5;
    flashCtx.beginPath();
    flashCtx.moveTo(f.x - arm - gap, f.y); flashCtx.lineTo(f.x - gap, f.y);
    flashCtx.moveTo(f.x + gap, f.y);       flashCtx.lineTo(f.x + arm + gap, f.y);
    flashCtx.moveTo(f.x, f.y - arm - gap); flashCtx.lineTo(f.x, f.y - gap);
    flashCtx.moveTo(f.x, f.y + gap);       flashCtx.lineTo(f.x, f.y + arm + gap);
    flashCtx.stroke();
  }
  if(activeFlashes.length > 0) requestAnimationFrame(drawFlashes);
  else flashLoopRunning = false;
}

function flashCoords(ra, dec, classification){
  const xy = aladin.world2pix(ra, dec);
  if(!xy) return;
  const [x, y] = xy;
  const rgb = FLASH_COLORS[classification] || '255,255,255';
  activeFlashes.push({ x, y, rgb, start: performance.now() });
  if(!flashLoopRunning){ flashLoopRunning = true; requestAnimationFrame(drawFlashes); }
}

A.init.then(() => {
  aladin = A.aladin('#aladin-lite-div', {
    survey:                   'https://alasky.cds.unistra.fr/MellingerRGB/',
    fov:                      650,
    projection:               'AIT',
    cooFrame:                 'galactic',
    showReticle:              false,
    showZoomControl:          true,
    showFullscreenControl:    false,
    showLayersControl:        false,
    showGotoControl:          false,
    showSimbadPointerControl: false,
    showCooGridControl:       true,
    showShareControl:         false,
    showContextMenu:          false,
    showCooGrid:              false,
    showProjectionControl:    false,
  });
});
let columns = [];
let haveRows = false;

function classTag(v){
  if(v === 'CV') return '<span class="tag cv">CV</span>';
  if(v === 'VS') return '<span class="tag vs">VS</span>';
  if(v === 'SN') return '<span class="tag sn">SN</span>';
  return v == null ? '—' : v;
}
function cell(col, val){
  if(val == null) return '—';
  if(col === 'classification') return classTag(val);
  if(col === 'diaObjectId')
    return `<span class="oid"><a href="https://lasair.lsst.ac.uk/objects/${val}/" target="_blank" rel="noopener">${val}</a></span>`;
  if(typeof val === 'number') return (Number.isInteger(val)? val : val.toFixed(4));
  if(typeof val === 'object') return JSON.stringify(val).slice(0,80);
  return String(val);
}
function shortTopic(t){
  // strip the 'lasair_<id>' prefix for display, keep something readable
  if(!t) return '—';
  return t.replace(/^lasair_\d*/, '') || t;
}
const COL_LABELS = {
  diaObjectId:'Object ID', ra:'RA', decl:'Dec', gmag:'g mag', rmag:'r mag',
  imag:'i mag', zmag:'z mag', ymag:'y mag',
  jump1:'Jump', nPosDiaSources:'Pos Sources', classification:'Class', UTC:'UTC',
};
function colLabel(c){ return COL_LABELS[c] || c; }
function ensureHead(cols){
  if(!cols.length) return;
  if(JSON.stringify(cols) === JSON.stringify(columns)) return;
  columns = cols;
  const head = document.getElementById('head');
  head.innerHTML = '<th>#</th><th>Topic</th><th>Arrived (UTC)</th><th></th>' +
    cols.map(c=>`<th>${colLabel(c)}</th>`).join('');
}
function addRows(alerts){
  if(!alerts.length) return;
  const tb = document.getElementById('rows');
  if(!haveRows){ tb.innerHTML=''; haveRows=true; }
  // newest first; server already gives newest-first within batch
  alerts.forEach((a, i) => {
    const d = a.data || {};
    const tr = document.createElement('tr');
    const locateTd = (d.ra != null && d.decl != null)
      ? `<td class="meta"><button class="locate-btn" onclick="flashCoords(${d.ra},${d.decl},'${d.classification||''}')" title="Show on atlas"><svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="6.5" cy="6.5" r="2.5"/><line x1="6.5" y1="0" x2="6.5" y2="4"/><line x1="6.5" y1="9" x2="6.5" y2="13"/><line x1="0" y1="6.5" x2="4" y2="6.5"/><line x1="9" y1="6.5" x2="13" y2="6.5"/></svg></button></td>`
      : `<td class="meta"></td>`;
    tr.innerHTML = `<td class="meta">${a._seq}</td>`+
      `<td class="meta" title="${a._topic||''}">${shortTopic(a._topic)}</td>`+
      `<td class="meta">${a._arrived_utc}</td>`+
      locateTd+
      columns.map(c=>`<td>${cell(c, d[c])}</td>`).join('');
    tr.dataset.arrived = Date.now();
    tb.insertBefore(tr, tb.firstChild);
    setTimeout(() => tr.classList.add('new'), i * 100);
    if(aladin && d.ra != null && d.decl != null)
      setTimeout(() => flashCoords(d.ra, d.decl, d.classification), i * 100);
  });
  pruneRows(tb);
}
function pruneRows(tb){
  if(tb.children.length <= 100) return;
  const cutoff = Date.now() - 3_600_000;
  for(let i = tb.children.length - 1; i >= 100; i--){
    if(+(tb.children[i].dataset.arrived || 0) < cutoff)
      tb.removeChild(tb.children[i]);
  }
}
function renderStreams(statuses){
  const box = document.getElementById('streams');
  box.innerHTML = (statuses||[]).map(s=>{
    const cls = (s.state||'').replace(/[^a-z-]/g,'');
    const err = (s.state==='stream-error'||s.state==='error'||s.state==='reconnecting')
      ? `<span class="err" title="${(s.detail||'').replace(/"/g,'')}">!</span>` : '';
    return `<span class="stream">
      <span class="dot ${cls}"></span>
      <span class="nm" title="${s.topic||''}">${shortTopic(s.topic)}</span>
      <span class="rc">${s.received||0}</span>${err}
    </span>`;
  }).join('');
}
async function tick(){
  try{
    const r = await fetch('/api/alerts?after='+lastSeq);
    const j = await r.json();
    renderStreams(j.statuses);
    document.getElementById('count').textContent = (j.total_received || 0).toLocaleString();
    // rows
    ensureHead(j.columns || []);
    if(j.alerts && j.alerts.length){
      addRows(j.alerts);
      lastSeq = j.max_seq;
    }
    pruneRows(document.getElementById('rows'));
  }catch(e){
    console.error('poll error', e);
  }
}
setInterval(tick, 3000);
tick();
</script>
</body>
</html>"""


def load_dotenv(path=".env"):
    """Minimal .env reader (no dependency). Sets os.environ for KEY=VALUE lines.
    Ignores blank lines and # comments; strips optional surrounding quotes.
    Existing environment variables take precedence (not overwritten)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _parse_topics(raw):
    """Split a topics string on commas/whitespace/newlines into a clean list."""
    if not raw:
        return []
    parts = []
    for chunk in raw.replace("\n", ",").split(","):
        t = chunk.strip()
        if t:
            parts.append(t)
    return parts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true",
                    help="synthesize alerts; no Kafka connection")
    ap.add_argument("--env", default=".env", help="path to .env file")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    load_dotenv(args.env)

    port = args.port or int(os.environ.get("MONITOR_PORT", "5001"))
    stop = threading.Event()
    threads = []

    if args.demo:
        threads.append(threading.Thread(target=demo_loop, args=(stop,), daemon=True))
    else:
        # LASAIR_TOPICS (plural, comma-separated) is preferred; LASAIR_TOPIC
        # (singular) is still accepted for one topic.
        raw = os.environ.get("LASAIR_TOPICS") or os.environ.get("LASAIR_TOPIC", "")
        topics = _parse_topics(raw)
        if not topics:
            sys.exit("No topics configured. Put LASAIR_TOPICS in your .env file, "
                     "e.g.\n  LASAIR_TOPICS=lasair_2CVfilter, lasair_3Pulsators\n"
                     "(topic names are on each filter's detail page). "
                     "Or run with --demo.")
        kafka_server = os.environ.get("LASAIR_KAFKA",
                                      "lasair-lsst-kafka.lsst.ac.uk:9092")
        base_group = os.environ.get("LASAIR_GROUP_ID", "moeller-monitor-1")
        # One consumer thread per topic. Give each a distinct group_id suffix so
        # their Kafka offsets are tracked independently.
        for topic in topics:
            _set_status(topic, state="starting")
            gid = f"{base_group}-{topic}"
            threads.append(threading.Thread(
                target=kafka_loop, args=(kafka_server, gid, topic, stop),
                daemon=True))

    for t in threads:
        t.start()
    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
