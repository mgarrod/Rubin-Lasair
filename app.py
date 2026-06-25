#!/usr/bin/env python3
"""
Lasair TVS target tool — local web app.

Holds your Lasair API token server-side, queries Lasair for cataclysmic
variables and pulsating variables within your instrument's reach, and serves
a single page where you filter the results by date/time and magnitude.

Why a local app and not a website you visit: your Lasair token must stay
private (Lasair explicitly asks you not to put it in shared/browser code), and
Lasair's API does not allow direct browser calls. So this runs on your machine,
keeps the token in an environment variable, and talks to Lasair for you.

Run:
    pip install flask lasair
    export LASAIR_TOKEN=your_token_here     # from lasair-lsst.lsst.ac.uk -> My Profile
    python app.py
Then open http://127.0.0.1:5000

If you don't have a token yet, run with --demo to see the UI populated with
representative sample rows (no network calls):
    python app.py --demo
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    from flask import Flask, jsonify, render_template_string, request
except ImportError:
    sys.exit("Flask is required: pip install flask")

app = Flask(__name__)

# ---- Instrument reachability defaults (edit to your measured performance) ----
DEFAULTS = {
    "mag_bright": 12.5,   # saturation guard for the 1 m
    "mag_faint": 18.5,    # precision floor; push to ~21 for detection-only
    "dec_min": -20.0,     # northern-latitude visibility floor
}

# ---- The saved filters, expressed as Lasair (selected, tables, conditions) ---
# These mirror filters.md, written for the LSST schema at lasair-lsst.lsst.ac.uk:
#   id = diaObjectId; position = ra/decl; brightness = flux (nJy) via flux2mag();
#   outburst = positive difference flux (nPosDiaSources), not a dmdt sign.
# `{mb}`, `{mf}`, `{dec}` are filled from the request.
LASAIR_QUERIES = {
    "cv_outburst": {
        "label": "CVs in outburst",
        "selected": ("objects.diaObjectId, objects.ra, objects.decl, "
                     "flux2mag(objects.g_psfFlux) AS gmag, "
                     "flux2mag(objects.r_psfFlux) AS rmag, "
                     "objects.nPosDiaSources, objects.jump1, "
                     "objects.lastDiaSourceMjdTai, objects.tns_name, "
                     "objects.g_psfFlux, objects_ext.g_psfFluxSigma, "
                     "sherlock_classifications.classification"),
        "tables": "objects, objects_ext, sherlock_classifications",
        "conditions": ("sherlock_classifications.classification = 'CV' "
                       "AND flux2mag(objects.g_psfFlux) BETWEEN {mb} AND {mf} "
                       "AND objects.decl > {dec} "
                       "AND objects.nPosDiaSources >= 1"),
    },
    "pulsators": {
        "label": "Pulsating-variable candidates",
        "selected": ("objects.diaObjectId, objects.ra, objects.decl, "
                     "flux2mag(objects.g_psfFlux) AS gmag, "
                     "flux2mag(objects.r_psfFlux) AS rmag, "
                     "objects.nPosDiaSources, objects.jump1, "
                     "objects.lastDiaSourceMjdTai, objects.tns_name, "
                     "objects.g_psfFlux, objects_ext.g_psfFluxSigma, "
                     "sherlock_classifications.classification"),
        "tables": "objects, objects_ext, sherlock_classifications",
        "conditions": ("sherlock_classifications.classification = 'VS' "
                       "AND flux2mag(objects.g_psfFlux) BETWEEN {mb} AND {mf} "
                       "AND objects.decl > {dec}"),
    },
}

DEMO_ROWS = [
    {"diaObjectId": 169760235333878021, "ra": 182.41, "decl": 12.3,
     "gmag": 15.21, "rmag": 15.08, "lastDiaSourceMjdTai": 61029.61, "jump1": 6.2,
     "nPosDiaSources": 7, "classification": "CV",
     "g_psfFlux": 25400.0, "g_psfFluxSigma": 1180.0, "tns_name": None},
    {"diaObjectId": 169760235359568157, "ra": 47.92, "decl": 38.7,
     "gmag": 13.84, "rmag": 13.71, "lastDiaSourceMjdTai": 61030.55, "jump1": 3.1,
     "nPosDiaSources": 3, "classification": "CV",
     "g_psfFlux": 92000.0, "g_psfFluxSigma": 1500.0, "tns_name": "2026abc"},
    {"diaObjectId": 169760235333878044, "ra": 233.10, "decl": -4.1,
     "gmag": 17.62, "rmag": 17.40, "lastDiaSourceMjdTai": 61028.48, "jump1": 9.4,
     "nPosDiaSources": 12, "classification": "CV",
     "g_psfFlux": 8900.0, "g_psfFluxSigma": 1620.0, "tns_name": None},
    {"diaObjectId": 169760235333878099, "ra": 290.55, "decl": 22.9,
     "gmag": 14.95, "rmag": 14.88, "lastDiaSourceMjdTai": 61030.33, "jump1": 0.8,
     "nPosDiaSources": 1, "classification": "VS",
     "g_psfFlux": 32100.0, "g_psfFluxSigma": 9800.0, "tns_name": None},
    {"diaObjectId": 169760235333878102, "ra": 88.20, "decl": 5.6,
     "gmag": 16.10, "rmag": 15.95, "lastDiaSourceMjdTai": 61029.12, "jump1": 1.1,
     "nPosDiaSources": 1, "classification": "VS",
     "g_psfFlux": 11600.0, "g_psfFluxSigma": 850.0, "tns_name": "2026xyz"},
]


def mjd_to_iso(mjd):
    """Modified Julian Date -> ISO UTC string, for display and date filtering."""
    try:
        unix = (float(mjd) - 40587.0) * 86400.0
        return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def _to_sexagesimal(ra_deg, dec_deg):
    """Convert J2000 RA/Dec in decimal degrees to sexagesimal strings.
    RA -> 'HH:MM:SS.ss' (hours), Dec -> '+DD:MM:SS.s' (degrees, signed).
    Handles the seconds-rounding-to-60 carry correctly."""
    # RA: degrees -> hours (÷15), work in total seconds to avoid 60.00 rollover
    ra_hours = (ra_deg % 360) / 15.0
    total_rs = round(ra_hours * 3600, 2)
    rh = int(total_rs // 3600)
    rm = int((total_rs % 3600) // 60)
    rs = total_rs - rh * 3600 - rm * 60
    rh %= 24
    ra_str = "%02d:%02d:%05.2f" % (rh, rm, rs)
    # Dec: sign, then total arcseconds on the absolute value
    sign = "-" if dec_deg < 0 else "+"
    total_ds = round(abs(dec_deg) * 3600, 1)
    dd = int(total_ds // 3600)
    dm = int((total_ds % 3600) // 60)
    ds = total_ds - dd * 3600 - dm * 60
    dec_str = "%s%02d:%02d:%04.1f" % (sign, dd, dm, ds)
    return ra_str, dec_str


def _configure_tls():
    """Handle corporate TLS interception. Two opt-in env vars:

      LASAIR_CA_BUNDLE = /path/to/corporate-ca.pem
          Preferred. Points verification at your company's CA so the injected
          certificate validates normally — verification stays ON.

      LASAIR_INSECURE = 1
          Fallback. Disables certificate verification entirely. Use only on a
          trusted corporate network where TLS is being intercepted and you
          can't get the CA bundle. Your token then rides a connection the
          proxy can read (already true of corporate traffic, but stated plainly).

    Returns the value to pass as the requests `verify=` argument, or None to
    leave the client's default behavior unchanged.
    """
    ca = os.environ.get("LASAIR_CA_BUNDLE")
    if ca:
        # requests honors this env var directly; set both common names.
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
        os.environ.setdefault("CURL_CA_BUNDLE", ca)
        return ca
    if os.environ.get("LASAIR_INSECURE") in ("1", "true", "True", "yes"):
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        return False  # requests interprets verify=False as "don't verify"
    return None


def lasair_client():
    """Return an authenticated Lasair client pointed at the LSST endpoint."""
    import lasair
    token = os.environ.get("LASAIR_TOKEN")
    if not token:
        raise RuntimeError("Set LASAIR_TOKEN (lasair.lsst.ac.uk -> My Profile).")
    verify = _configure_tls()
    # The LSST broker API is served from the api. subdomain. Using the bare
    # site host (or the older lasair-lsst host) triggers a TLS hostname mismatch.
    endpoint = os.environ.get("LASAIR_ENDPOINT",
                              "https://api.lasair.lsst.ac.uk/api")
    L = lasair.lasair_client(token, endpoint=endpoint)
    # The lasair client holds a requests.Session (or uses requests directly).
    # If we need a custom verify mode, apply it to the underlying session so it
    # affects every call the client makes.
    if verify is not None:
        _apply_verify(L, verify)
    return L


def _apply_verify(client, verify):
    """Force the requests verify setting on the lasair client's HTTP layer,
    regardless of which attribute the client uses internally."""
    # Most versions keep a requests.Session; some call requests.* directly.
    sess = getattr(client, "session", None) or getattr(client, "_session", None)
    if sess is not None:
        sess.verify = verify
        return
    # Fallback: patch requests globally for this process so the client's bare
    # requests.get/post calls inherit the setting.
    import requests
    _orig_request = requests.Session.request

    def _patched(self, *a, **kw):
        kw.setdefault("verify", verify)
        return _orig_request(self, *a, **kw)

    requests.Session.request = _patched


def run_lasair(which, mb, mf, dec, limit=500):
    """Query Lasair for one saved filter. Returns list of row dicts."""
    L = lasair_client()
    q = LASAIR_QUERIES[which]
    conditions = q["conditions"].format(mb=mb, mf=mf, dec=dec)
    # lastDiaSourceMjdTai is the real "latest detection MJD" column (objects table).
    conditions = conditions + " ORDER BY lastDiaSourceMjdTai DESC"
    rows = L.query(q["selected"], q["tables"], conditions, limit=limit)
    return rows if isinstance(rows, list) else []


# Cache fetched light curves so re-clicking a row spends no extra API call.
# Keyed by diaObjectId; cleared on process restart. Bounded to avoid growth.
_LC_CACHE = {}
_LC_CACHE_MAX = 500

# Cache Rubin schedule lookups too (keyed by rounded ra,dec). Short TTL since
# the schedule updates; we keep it simple and clear on restart.
_SCHED_CACHE = {}
_SCHED_CACHE_MAX = 500

# Rubin ObsLocTAP service (the schedule viewer's programmatic TAP endpoint).
# No authentication required. The static viewer lives at
# usdf-rsp.slac.stanford.edu/obsloctap/static/viewer.html, so the service base
# is .../obsloctap. The exact TAP sync path isn't pinned in docs we can see, so
# we try a few candidate shapes and use whichever answers. Override with
# OBSLOCTAP_URL to skip the probing.
OBSLOCTAP_BASE = os.environ.get(
    "OBSLOCTAP_URL",
    "https://usdf-rsp.slac.stanford.edu/obsloctap")

# Candidate sync-endpoint paths to try, in order. The first that returns usable
# rows (or even a clean empty result) wins and is remembered for the session.
_OBSLOCTAP_SYNC_CANDIDATES = [
    "{base}/tap/sync",
    "{base}/sync",
    "{base}/tap",
    "{base}",
]
_OBSLOCTAP_WORKING_URL = None  # filled in once a candidate succeeds

# Zero point for converting nJy flux to AB magnitude: m = 31.4 - 2.5*log10(flux_nJy)
NJY_AB_ZP = 31.4


def _mjd_now():
    """Current time as MJD (UTC)."""
    return datetime.now(timezone.utc).timestamp() / 86400.0 + 40587.0


def _obsloctap_query(adql, verify):
    """Run an ADQL query against the Rubin ObsLocTAP service, trying candidate
    sync-endpoint URLs until one answers. Returns (rows, used_url, error)."""
    global _OBSLOCTAP_WORKING_URL
    import requests
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "PHASE": "RUN",
              "FORMAT": "json", "QUERY": adql}
    kw = {"params": params, "timeout": 20}
    if verify is not None:
        kw["verify"] = verify

    # If we already found a working URL this session, use it first.
    candidates = []
    if _OBSLOCTAP_WORKING_URL:
        candidates.append(_OBSLOCTAP_WORKING_URL)
    for tmpl in _OBSLOCTAP_SYNC_CANDIDATES:
        u = tmpl.format(base=OBSLOCTAP_BASE.rstrip("/"))
        if u not in candidates:
            candidates.append(u)

    last_err = None
    for url in candidates:
        try:
            r = requests.get(url, **kw)
            if r.status_code != 200:
                last_err = "HTTP %s at %s" % (r.status_code, url)
                continue
            # Try JSON first; some TAP services return VOTable XML by default.
            rows = None
            try:
                rows = _parse_tap_json(r.json())
            except ValueError:
                rows = _parse_tap_votable(r.text)
            if rows is not None:
                _OBSLOCTAP_WORKING_URL = url
                return rows, url, None
            last_err = "unparseable response at %s" % url
        except Exception as e:  # noqa: BLE401
            last_err = "%s: %s" % (url, e)
            continue
    return None, None, last_err


def fetch_schedule(ra_deg, dec_deg, radius_deg=1.75):
    """Query the Rubin ObsLocTAP service for planned visits whose field of view
    contains this position. Returns the soonest upcoming visit (and a count),
    or an 'unscheduled' marker. Cached per rounded position.

    radius_deg ~1.75 approximates Rubin's ~3.5 deg field for the cone; the
    INTERSECTS(s_region, ...) test uses the real footprint where available."""
    key = "%.3f,%.3f" % (round(ra_deg, 3), round(dec_deg, 3))
    if key in _SCHED_CACHE:
        return _SCHED_CACHE[key]

    verify = _configure_tls()
    now = _mjd_now()
    adql = (
        "SELECT TOP 50 s_ra, s_dec, t_min, t_max, target_name, "
        "execution_status, priority "
        "FROM ivoa.obsplan "
        "WHERE 1=INTERSECTS(s_region, CIRCLE('ICRS', {ra}, {dec}, {r})) "
        "AND t_max >= {now} "
        "ORDER BY t_min ASC"
    ).format(ra=ra_deg, dec=dec_deg, r=radius_deg, now=now)

    result = {"next_mjd": None, "next_iso": None, "count": 0,
              "status": None, "priority": None, "error": None}
    rows, used_url, err = _obsloctap_query(adql, verify)
    if rows is None:
        result["error"] = err or "schedule service unreachable"
    else:
        result["count"] = len(rows)
        if rows:
            first = _ci(rows[0])  # case-insensitive view of the row
            tmin = first.get("t_min")
            result["next_mjd"] = tmin
            result["next_iso"] = _mjd_to_iso_full(tmin)
            result["status"] = first.get("execution_status")
            result["priority"] = first.get("priority")
            if tmin is None:
                # Rows came back but the expected column wasn't found — report
                # the actual keys so the mismatch is diagnosable at a glance.
                result["error"] = ("rows returned but no t_min; columns seen: "
                                    + ", ".join(str(k) for k in rows[0].keys()))

    if len(_SCHED_CACHE) < _SCHED_CACHE_MAX:
        _SCHED_CACHE[key] = result
    return result


def _ci(row):
    """Return a dict with lowercased keys so column lookups are case-insensitive
    (TAP services differ on column-name casing)."""
    if not isinstance(row, dict):
        return {}
    return {(k.lower() if isinstance(k, str) else k): v for k, v in row.items()}


def _parse_tap_votable(text):
    """Minimal VOTable parser: extract <TR><TD>..</TD></TR> rows and <FIELD
    name=..> headers. Returns list of dicts, or None if it doesn't look like a
    VOTable."""
    if not text or "<VOTABLE" not in text.upper():
        return None
    import re
    fields = re.findall(r'<FIELD[^>]*\bname="([^"]+)"', text, re.IGNORECASE)
    rows = []
    for tr in re.findall(r"<TR>(.*?)</TR>", text, re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<TD>(.*?)</TD>", tr, re.IGNORECASE | re.DOTALL)
        cells = [c.strip() for c in cells]
        if fields and len(fields) == len(cells):
            row = {}
            for k, v in zip(fields, cells):
                # numeric coercion where possible
                try:
                    row[k] = float(v)
                except (TypeError, ValueError):
                    row[k] = v
            rows.append(row)
    return rows


def _parse_tap_json(data):
    """Normalize a TAP JSON response into a list of row dicts, defensively —
    TAP/ObsLocTAP services vary in JSON shape, so we handle several and never
    crash on an unexpected one.

    Shapes handled:
      A) {'metadata':[{'name':..},..], 'data':[[..],..]}   (TAP_PLUS style)
      B) {'columns':[{'name':..}|'name',..], 'data':[[..]]}
      C) [{'col':val,..}, ..]                              (already row dicts)
      D) {'data':[{'col':val},..]}                         (rows under 'data')
    Returns a list of dicts, or [] if nothing usable.
    """
    # C) top-level list
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            return data
        return []
    if not isinstance(data, dict):
        return []

    # Find a column-name list under either 'metadata' or 'columns'.
    meta = data.get("metadata")
    if meta is None:
        meta = data.get("columns")
    cols = []
    if isinstance(meta, list):
        for c in meta:
            if isinstance(c, dict):
                cols.append(c.get("name") or c.get("column_name") or c.get("ID"))
            elif isinstance(c, str):
                cols.append(c)
            else:
                cols.append(None)

    rows_in = data.get("data")
    if not isinstance(rows_in, list):
        return []

    out = []
    for row in rows_in:
        if isinstance(row, dict):
            # D) rows already dicts
            out.append(row)
        elif isinstance(row, (list, tuple)) and cols and len(cols) == len(row):
            out.append({k: v for k, v in zip(cols, row) if k is not None})
        # else: shape we can't map — skip rather than crash
    return out


def _mjd_to_iso_full(mjd):
    """MJD -> 'YYYY-MM-DD HH:MM' UTC, or '' if not parseable."""
    try:
        unix = (float(mjd) - 40587.0) * 86400.0
        return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def _point_to_mag_mjd(p):
    """Normalize one light-curve point to (mjd, mag) handling both LSST and
    ZTF return shapes. Returns None if the point can't be interpreted or is a
    non-positive flux (which has no real magnitude)."""
    import math
    # time: LSST 'midpointMjdTai', ZTF 'mjd'
    mjd = p.get("midpointMjdTai", p.get("mjd"))
    if mjd is None:
        return None
    # magnitude path 1: already a magnitude (ZTF 'magpsf', or LSST 'psfMag')
    mag = p.get("psfMag", p.get("magpsf"))
    if mag is not None:
        try:
            return (float(mjd), float(mag))
        except (TypeError, ValueError):
            return None
    # magnitude path 2: flux in nJy (LSST 'psfFlux') -> AB mag, positive only
    flux = p.get("psfFlux")
    if flux is not None:
        try:
            f = float(flux)
            if f <= 0:
                return None  # negative/zero difference flux has no magnitude
            return (float(mjd), NJY_AB_ZP - 2.5 * math.log10(f))
        except (TypeError, ValueError):
            return None
    return None


def _recent_slope(points, days=15.0):
    """Least-squares slope (mag/day) over the most recent `days` of points.
    Negative slope = brightening (magnitudes get smaller as objects brighten).
    Returns (slope, n_points_used) or (None, 0) if too few points."""
    if len(points) < 2:
        return (None, 0)
    tmax = max(t for t, _ in points)
    recent = [(t, m) for (t, m) in points if t >= tmax - days]
    if len(recent) < 2:
        recent = sorted(points)[-4:]  # fall back to last few if window too thin
    n = len(recent)
    if n < 2:
        return (None, 0)
    mt = sum(t for t, _ in recent) / n
    mm = sum(m for _, m in recent) / n
    denom = sum((t - mt) ** 2 for t, _ in recent)
    if denom == 0:
        return (None, n)
    slope = sum((t - mt) * (m - mm) for t, m in recent) / denom
    return (slope, n)


def _extract_series(raw):
    """Pull a list of light-curve point dicts out of whatever a Lasair method
    returned. Handles several shapes seen across client versions/endpoints."""
    series = raw
    # lightcurves() returns a list with one entry per requested id. That entry
    # may itself be a list of point dicts...
    if isinstance(series, list) and series and isinstance(series[0], list):
        series = series[0]
    # ...or a dict wrapping the points under a key.
    elif isinstance(series, list) and series and isinstance(series[0], dict) \
            and ("candidates" in series[0] or "diaSourcesList" in series[0]
                 or "lightcurve" in series[0] or "objectData" in series[0]):
        series = series[0]
    if isinstance(series, dict):
        # object()/objects() wrap the curve under one of these keys
        series = (series.get("candidates") or series.get("diaSourcesList")
                  or series.get("lightcurve")
                  or (series.get("objectData") or {}).get("candidates")
                  or [])
    return series if isinstance(series, list) else []


def fetch_lightcurve(dia_object_id):
    """Fetch and normalize a single object's light curve. Returns a dict with
    sorted (mjd, mag) points and a recent slope. Cached per object id.

    Resilient to client differences: the installed `lasair` package may not
    expose `lightcurves()`, and the LSST `/api/lightcurves/` endpoint has been
    seen to 404. So we try, in order, whatever methods the client actually has,
    and extract points from whatever shape comes back."""
    key = str(dia_object_id)
    if key in _LC_CACHE:
        return _LC_CACHE[key]

    L = lasair_client()
    series = []
    attempts = []

    # Strategy 1: lightcurves([id]) — present on some client versions.
    if hasattr(L, "lightcurves"):
        try:
            series = _extract_series(L.lightcurves([dia_object_id]))
        except Exception as e:
            attempts.append(f"lightcurves(): {e}")

    # Strategy 2: objects([id]) — returns the full object page incl. candidates.
    if not series and hasattr(L, "objects"):
        try:
            series = _extract_series(L.objects([dia_object_id]))
        except Exception as e:
            attempts.append(f"objects(): {e}")

    # Strategy 3: object(id) — singular form on some versions.
    if not series and hasattr(L, "object"):
        try:
            series = _extract_series(L.object(dia_object_id))
        except Exception as e:
            attempts.append(f"object(): {e}")

    if not series and attempts:
        # Surface a useful message rather than a bare attribute error.
        raise RuntimeError("; ".join(attempts))

    points = []
    for p in (series or []):
        norm = _point_to_mag_mjd(p) if isinstance(p, dict) else None
        if norm:
            points.append(norm)
    points.sort()

    slope, n_used = _recent_slope(points)
    result = {
        "points": [{"mjd": t, "mag": m} for t, m in points],
        "n": len(points),
        "slope": slope,
        "slope_n": n_used,
    }
    if len(_LC_CACHE) < _LC_CACHE_MAX:
        _LC_CACHE[key] = result
    return result


# Synthetic demo light curve: a CV-like rise so the sparkline/slope are visible.
def _demo_lightcurve(dia_object_id):
    import math, random
    random.seed(int(str(dia_object_id)[-4:]) if str(dia_object_id)[-4:].isdigit() else 0)
    base_mjd = 61010.0
    pts = []
    for i in range(28):
        t = base_mjd + i
        # quiescent then a brightening ramp over the last ~10 days
        if i < 18:
            m = 18.2 + random.uniform(-0.05, 0.05)
        else:
            m = 18.2 - (i - 18) * 0.32 + random.uniform(-0.06, 0.06)
        pts.append((t, m))
    slope, n_used = _recent_slope(pts)
    return {"points": [{"mjd": t, "mag": m} for t, m in pts],
            "n": len(pts), "slope": slope, "slope_n": n_used}


def _normalize_row_keys(r):
    """Ensure the camelCase keys the frontend expects are present, even if
    Lasair returned them prefixed ('objects.jump1') or in different casing.
    Mutates r in place by adding any missing expected key.

    Lasair queries that SELECT 'objects.colName' can come back keyed as either
    'colName' or 'objects.colName' depending on version, and aliases/casing can
    vary — so we index everything by a normalized form and backfill."""
    if not isinstance(r, dict):
        return
    norm = {}
    for k, v in list(r.items()):
        if not isinstance(k, str):
            continue
        base = k.split(".")[-1].lower()
        norm.setdefault(base, v)
    expected = ["diaObjectId", "ra", "decl", "gmag", "rmag",
                "nPosDiaSources", "jump1", "lastDiaSourceMjdTai", "tns_name",
                "g_psfFlux", "g_psfFluxSigma", "classification"]
    for key in expected:
        if r.get(key) is None and key.lower() in norm:
            r[key] = norm[key.lower()]


@app.route("/api/debug")
def api_debug():
    """Diagnostic: scans up to 30 rows from each Lasair filter and reports, per
    column of interest, how many rows include the key and how many have a
    non-null value. This distinguishes 'column never returned' from 'column
    returned but null for these objects'. Visit /api/debug. Safe to remove."""
    if app.config.get("DEMO", False):
        return jsonify({"note": "demo mode — run live to see real Lasair keys",
                        "demo_row_keys": list(DEMO_ROWS[0].keys())})
    watch = ["jump1", "nPosDiaSources", "g_psfFlux", "gmag"]
    out = {}
    for k in ("cv_outburst", "pulsators"):
        try:
            rows = run_lasair(k, 0, 30, -90, limit=30)
            n = len(rows)
            report = {"rows_scanned": n, "all_keys_seen": set(), "columns": {}}
            for col in watch:
                present = sum(1 for r in rows if col in r)
                nonnull = sum(1 for r in rows
                              if isinstance(r, dict) and r.get(col) is not None)
                report["columns"][col] = {"present_in": present,
                                          "nonnull_in": nonnull}
            for r in rows:
                if isinstance(r, dict):
                    report["all_keys_seen"].update(r.keys())
            report["all_keys_seen"] = sorted(report["all_keys_seen"])
            # include the single row with the highest chance of having jump1
            sample = next((r for r in rows if r.get("jump1") is not None),
                          rows[0] if rows else None)
            report["sample_row"] = sample
            out[k] = report
        except Exception as e:  # noqa: BLE401
            out[k] = {"error": str(e)}
    return jsonify(out)


@app.route("/api/targets")
def api_targets():
    demo = app.config.get("DEMO", False)
    mb = float(request.args.get("mag_bright", DEFAULTS["mag_bright"]))
    mf = float(request.args.get("mag_faint", DEFAULTS["mag_faint"]))
    dec = float(request.args.get("dec_min", DEFAULTS["dec_min"]))
    since = request.args.get("since", "")  # ISO date string, optional
    kinds = request.args.getlist("kind") or ["cv_outburst", "pulsators"]

    rows = []
    errors = []
    if demo:
        for r in DEMO_ROWS:
            r = dict(r)
            label = "CVs in outburst" if r["classification"] == "CV" else "Pulsating-variable candidates"
            r["_filter"] = label
            rows.append(r)
    else:
        for k in kinds:
            try:
                for r in run_lasair(k, mb, mf, dec):
                    r = dict(r)
                    r["_filter"] = LASAIR_QUERIES[k]["label"]
                    rows.append(r)
            except Exception as e:  # noqa: BLE401 — surface to UI
                errors.append(f"{k}: {e}")

    # Post-filter in Python: magnitude band, dec, and date/time window.
    # lastDiaSourceMjdTai is the real latest-detection MJD, selectable from the
    # objects table, so the "since" date filter works on the initial table now.
    out = []
    for r in rows:
        # Lasair may return column keys with a table prefix (e.g.
        # 'objects.nPosDiaSources') or different casing than our SELECT. Build a
        # normalized lookup (lowercased, prefix-stripped) and backfill any
        # expected key that's missing so the frontend finds it reliably.
        _normalize_row_keys(r)
        g = r.get("gmag")
        if g is not None and not (mb <= float(g) <= mf):
            continue
        row_dec = r.get("decl")
        if row_dec is not None and float(row_dec) < dec:
            continue
        mjd = r.get("lastDiaSourceMjdTai")
        iso = mjd_to_iso(mjd) if mjd is not None else ""
        if since and iso and iso[:10] < since:
            continue
        r["last_seen_utc"] = iso
        # diaObjectId is an 18-digit integer that exceeds JavaScript's safe
        # integer range (2^53), so if sent as a JSON number the browser rounds
        # off the last digits and the object link 404s. Send it as a string.
        if r.get("diaObjectId") is not None:
            r["diaObjectId"] = str(r["diaObjectId"])
        # Sexagesimal J2000 coordinates for display/copy (RA in h:m:s, Dec in d:m:s).
        row_ra = r.get("ra")
        if row_ra is not None and row_dec is not None:
            try:
                r["ra_hms"], r["dec_dms"] = _to_sexagesimal(float(row_ra), float(row_dec))
            except (TypeError, ValueError):
                pass
        # Signal-to-noise of the latest g detection: flux / its uncertainty.
        # This is the real-vs-noise number — high SNR = detection well above
        # its error bar. Guard against missing/zero sigma.
        flux = r.get("g_psfFlux")
        sigma = r.get("g_psfFluxSigma")
        try:
            if flux is not None and sigma not in (None, 0):
                r["g_snr"] = abs(float(flux)) / abs(float(sigma))
        except (TypeError, ValueError):
            pass
        out.append(r)

    out.sort(key=lambda x: x.get("lastDiaSourceMjdTai") or 0, reverse=True)
    return jsonify({"rows": out, "errors": errors, "count": len(out)})


@app.route("/api/lightcurve")
def api_lightcurve():
    """Fetch one object's light curve on demand (called when a row is opened)."""
    oid = request.args.get("diaObjectId", "")
    if not oid:
        return jsonify({"error": "diaObjectId required"}), 400
    try:
        if app.config.get("DEMO", False):
            data = _demo_lightcurve(oid)
        else:
            data = fetch_lightcurve(oid)
        return jsonify(data)
    except Exception as e:  # noqa: BLE401 — surface to UI
        return jsonify({"error": str(e), "points": [], "n": 0, "slope": None}), 200


@app.route("/api/schedule")
def api_schedule():
    """Check whether/when Rubin will revisit a position (called on row open)."""
    try:
        ra = float(request.args.get("ra", ""))
        dec = float(request.args.get("dec", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "ra and dec required"}), 400
    if app.config.get("DEMO", False):
        return jsonify(_demo_schedule(ra, dec))
    try:
        return jsonify(fetch_schedule(ra, dec))
    except Exception as e:  # noqa: BLE401
        return jsonify({"error": str(e), "next_mjd": None, "count": 0}), 200


def _demo_schedule(ra, dec):
    """Synthetic schedule for demo mode: pretend some fields are revisited."""
    # deterministic per-position so it's stable across clicks
    seed = int(abs(ra * 1000 + dec * 10)) % 5
    if seed in (0, 1):  # ~40% have an upcoming visit
        dt_days = [0.4, 1.2][seed]
        mjd = _mjd_now() + dt_days
        return {"next_mjd": mjd, "next_iso": _mjd_to_iso_full(mjd),
                "count": 3 - seed, "status": "Planned",
                "priority": seed, "error": None}
    return {"next_mjd": None, "next_iso": None, "count": 0,
            "status": None, "priority": None, "error": None}


@app.after_request
def _no_cache(resp):
    """Prevent the browser from serving a stale cached copy of the page or its
    inline JS. This app's HTML/JS changes between runs, and a cached page was a
    recurring source of 'my change isn't showing up' confusion."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    return render_template_string(PAGE, defaults=DEFAULTS,
                                  demo=app.config.get("DEMO", False))


# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rubin LSST/Lasair TVS Targets @ Moeller Observatory</title>
<style>
  :root{
    --void:#0a0e14; --panel:#121821; --line:#243240; --ink:#e8eef4;
    --dim:#7d909f; --flare:#ff7a3c; --pulse:#3ca7ff; --grid:#1a2330;
  }
  *{box-sizing:border-box}
  body{margin:0;background:
    radial-gradient(circle at 18% 12%, rgba(255,122,60,.06), transparent 40%),
    radial-gradient(circle at 82% 78%, rgba(60,167,255,.06), transparent 42%),
    var(--void);
    color:var(--ink);font:15px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    height:100vh;overflow:hidden}
  header{padding:22px 26px 16px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:17px;letter-spacing:.14em;text-transform:uppercase;font-weight:600}
  h1 .flare{color:var(--flare)} h1 .pulse{color:var(--pulse)}
  h1 .sub-h1{color:var(--dim);font-weight:400}
  .sub{color:var(--dim);font-size:12.5px;margin-top:5px;letter-spacing:.03em}
  .wrap{display:grid;grid-template-columns:280px 1fr;gap:0;height:calc(100vh - 86px)}
  aside{border-right:1px solid var(--line);padding:22px;overflow-y:auto}
  main{padding:22px 26px;display:flex;flex-direction:column;min-height:0;height:calc(100vh - 86px)}
  .tablewrap{flex:1;overflow:auto;min-height:0;border:1px solid var(--line);border-radius:8px}
  .field{margin-bottom:20px}
  label{display:block;color:var(--dim);font-size:11px;letter-spacing:.12em;
    text-transform:uppercase;margin-bottom:7px}
  input[type=number],input[type=date]{width:100%;background:var(--panel);
    border:1px solid var(--line);color:var(--ink);padding:9px 10px;border-radius:5px;
    font:inherit}
  input:focus{outline:none;border-color:var(--pulse)}
  .pair{display:flex;gap:8px}
  .chk{display:flex;align-items:center;gap:9px;margin:8px 0;color:var(--ink);
    font-size:13.5px;cursor:pointer}
  .chk input{accent-color:var(--flare);width:15px;height:15px}
  button{width:100%;background:var(--flare);color:#1a0d06;border:none;
    padding:12px;border-radius:6px;font:inherit;font-weight:700;letter-spacing:.06em;
    text-transform:uppercase;cursor:pointer;margin-top:6px}
  button:hover{filter:brightness(1.08)} button:active{transform:translateY(1px)}
  .meta{display:flex;justify-content:space-between;align-items:baseline;
    margin-bottom:14px;color:var(--dim);font-size:12.5px;letter-spacing:.04em}
  .count{color:var(--ink);font-size:22px;font-weight:700}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--dim);font-size:10.5px;letter-spacing:.1em;
    text-transform:uppercase;padding:9px 12px;
    position:sticky;top:0;z-index:2;background:var(--void);
    box-shadow:inset 0 -1px 0 var(--line)}
  th.hashelp{cursor:help;text-decoration:underline dotted var(--dim);text-underline-offset:3px}
  th.hashelp:hover{color:var(--ink)}
  td{padding:10px 12px;border-bottom:1px solid var(--grid);white-space:nowrap}
  tr.target{cursor:pointer}
  tr.target:hover td{background:rgba(60,167,255,.05)}
  tr.target.open td{background:rgba(60,167,255,.08)}
  .caret{display:inline-block;width:10px;color:var(--dim);transition:transform .15s}
  tr.target.open .caret{transform:rotate(90deg);color:var(--pulse)}
  .lcrow td{padding:0;border-bottom:1px solid var(--grid);background:var(--void)}
  .lcwrap{padding:16px 18px;display:flex;gap:22px;align-items:center}
  .lcwrap.loading,.lcwrap.error{color:var(--dim);font-size:12.5px;padding:18px}
  .lcwrap.error{color:#ff8a8a}
  .spark{flex:0 0 auto}
  .lcstats{font-size:12.5px;line-height:1.7}
  .lcstats .k{color:var(--dim);letter-spacing:.06em;text-transform:uppercase;font-size:10.5px}
  .lcstats .v{color:var(--ink);font-variant-numeric:tabular-nums}
  .lcstats .v.bright{color:var(--flare);font-weight:700}
  .lcstats .v.fade{color:var(--pulse)}
  .sched{font-size:12.5px;line-height:1.7;padding-left:18px;border-left:1px solid var(--line);margin-left:4px}
  .sched .v{color:var(--ink);font-variant-numeric:tabular-nums}
  .sched .schedv{color:var(--pulse)}
  .sched .v.dim{color:var(--dim)}
  .sched.gap .gapv{color:#6fe0a0;font-weight:600}
  .tag{font-size:10.5px;padding:3px 8px;border-radius:20px;letter-spacing:.05em;cursor:help}
  .tag.cv{background:rgba(255,122,60,.15);color:var(--flare);border:1px solid rgba(255,122,60,.35)}
  .tag.vs{background:rgba(60,167,255,.15);color:var(--pulse);border:1px solid rgba(60,167,255,.35)}
  .tag.other{background:rgba(125,144,159,.15);color:var(--dim);border:1px solid rgba(125,144,159,.4)}
  .tns{display:inline-block;font-size:9.5px;letter-spacing:.04em;padding:2px 6px;
    margin-left:6px;border-radius:4px;background:rgba(180,140,255,.15);color:#c9a7ff;
    border:1px solid rgba(180,140,255,.4);vertical-align:middle}
  td.noise{color:#ff8a8a} td.solid{color:#6fe0a0}
  td.coord{font-variant-numeric:tabular-nums;white-space:nowrap}
  td.coord .cval{color:var(--ink)}
  .copy{background:none;border:none;color:var(--dim);cursor:pointer;font-size:13px;
    padding:2px 4px;margin-left:5px;border-radius:4px;width:auto;vertical-align:baseline;
    text-transform:none;letter-spacing:normal;font-weight:400}
  .copy:hover{color:var(--pulse);background:rgba(60,167,255,.12)}
  .copy.ok{color:#6fe0a0}
  td a{color:var(--pulse);text-decoration:none;border-bottom:1px dotted}
  .num{font-variant-numeric:tabular-nums}
  .rise{color:var(--flare);font-weight:600}
  .empty,.err{padding:40px 12px;color:var(--dim);text-align:center}
  .err{color:#ff8a8a;text-align:left;padding:12px;font-size:12px;
    border:1px solid rgba(255,138,138,.3);border-radius:6px;margin-bottom:14px;white-space:pre-wrap}
  .banner{background:rgba(60,167,255,.1);border:1px solid rgba(60,167,255,.3);
    color:var(--pulse);padding:8px 12px;border-radius:6px;font-size:12px;margin-bottom:14px}
  @media(max-width:760px){.wrap{grid-template-columns:1fr}aside{border-right:none;border-bottom:1px solid var(--line)}}
</style>
</head>
<body>
<header>
  <h1>RUBIN LSST/LASAIR <span class="flare">TVS</span> <span class="pulse">TARGETS</span> <span class="sub-h1">@ MOELLER OBSERVATORY</span></h1>
  <div class="sub">Cataclysmic &amp; pulsating variables within reach of the PlaneWave CDK1000</div>
</header>
<div class="wrap">
  <aside>
    <div class="field">
      <label>Magnitude band (g)</label>
      <div class="pair">
        <input type="number" id="mag_bright" step="0.1" value="{{defaults.mag_bright}}" title="bright limit / saturation guard">
        <input type="number" id="mag_faint" step="0.1" value="{{defaults.mag_faint}}" title="faint limit / precision floor">
      </div>
    </div>
    <div class="field">
      <label>Declination floor (°)</label>
      <input type="number" id="dec_min" step="1" value="{{defaults.dec_min}}">
    </div>
    <div class="field">
      <label>Last seen since</label>
      <input type="date" id="since">
    </div>
    <div class="field">
      <label>Filters</label>
      <label class="chk"><input type="checkbox" id="k_cv" checked> CVs in outburst</label>
      <label class="chk"><input type="checkbox" id="k_vs" checked> Pulsating variables</label>
    </div>
    <button id="run">Pull targets</button>
  </aside>
  <main>
    {% if demo %}<div class="banner">Demo mode — representative sample rows, no live Lasair call. Restart without --demo and set LASAIR_TOKEN for live data.</div>{% endif %}
    <div class="meta"><span><span class="count" id="count">—</span> targets</span>
      <span id="stamp"></span></div>
    <div id="errors"></div>
    <div class="tablewrap">
    <table>
      <thead><tr>
        <th>Object</th><th>Source filter</th><th>g</th><th>r</th>
        <th class="hashelp" title="g-band signal-to-noise: latest g flux ÷ its uncertainty (g_psfFlux / g_psfFluxSigma). High = a solid detection well above its error bar; below ~5 is likely noise. Red = noise, green = solid.">g SNR</th><th class="hashelp" title="jump1: largest sigma (σ) jump in recent flux versus the object's baseline over the prior 70–10 days. High = a sharp, statistically significant brightening — the signature of an outburst. ≥5σ is highlighted.">jump σ</th><th class="hashelp" title="nPosDiaSources: number of detections with POSITIVE difference flux (brighter than the reference template). More positive detections = a more sustained, confirmed brightening rather than a single blip.">n⁺ det</th><th>RA (J2000)</th><th>Dec (J2000)</th><th>Last seen (UTC)</th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="10" class="empty">Set your cuts and press “Pull targets”.</td></tr></tbody>
    </table>
    </div>
  </main>
</div>
<script>
const $ = id => document.getElementById(id);
// Full names for Sherlock's contextual classification codes (hover tooltip).
const SHERLOCK_NAMES = {
  CV: "Cataclysmic Variable", VS: "Variable Star", SN: "Supernova",
  AGN: "Active Galactic Nucleus", BS: "Bright Star", NT: "Nuclear Transient",
  ORPHAN: "Orphan (no catalogue match)", UNCLEAR: "Unclear classification",
  NULL: "No classification"
};
function tag(c){
  const code = c || "NULL";
  const cls = code==='CV' ? 'cv' : (code==='VS' ? 'vs' : 'other');
  const full = SHERLOCK_NAMES[code] || code;
  return `<span class="tag ${cls}" title="${full}">${code}</span>`;
}
// Badge for WHICH of the user's filters produced this row (not Sherlock's class).
// Short label + full filter name on hover; Sherlock's class also shown in tooltip.
function filterTag(o){
  const f = o._filter || "";
  const isCV = f.indexOf("CV") === 0 || f.toLowerCase().indexOf("outburst") >= 0;
  const short = isCV ? "CV outburst" : (f ? "Pulsator" : (o.classification || "—"));
  const cls = isCV ? 'cv' : (f ? 'vs' : 'other');
  const sher = o.classification ? `  ·  Sherlock class: ${o.classification}` : "";
  const title = (f || "—") + sher;
  return `<span class="tag ${cls}" title="${title}">${short}</span>`;
}
function lasairLink(id){
  return `<a href="https://lasair.lsst.ac.uk/objects/${id}/" target="_blank" rel="noopener">${id}</a>`;
}
// Render a coordinate with a copy-to-clipboard icon. `sexa` is the HH:MM:SS /
// ±DD:MM:SS string; `dec` is the decimal-degree fallback if sexa is missing.
function coordCell(sexa, dec){
  const val = sexa || (dec!=null ? Number(dec).toFixed(5) : '—');
  if(val === '—') return '—';
  const esc = val.replace(/'/g, "\\'");
  return `<span class="cval">${val}</span>` +
    `<button class="copy" title="Copy ${val}" onclick="copyText('${esc}', this)">⧉</button>`;
}
function copyText(text, btn){
  const done = ()=>{ const o=btn.textContent; btn.textContent='✓';
    btn.classList.add('ok'); setTimeout(()=>{btn.textContent=o;btn.classList.remove('ok');},1100); };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done).catch(()=>fallbackCopy(text,done));
  } else { fallbackCopy(text, done); }
}
function fallbackCopy(text, done){
  const ta=document.createElement('textarea'); ta.value=text;
  ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta);
  ta.select(); try{ document.execCommand('copy'); done(); }catch(e){} 
  document.body.removeChild(ta);
}
async function pull(){
  const p = new URLSearchParams();
  p.set('mag_bright', $('mag_bright').value);
  p.set('mag_faint', $('mag_faint').value);
  p.set('dec_min', $('dec_min').value);
  if($('since').value) p.set('since', $('since').value);
  if($('k_cv').checked) p.append('kind','cv_outburst');
  if($('k_vs').checked) p.append('kind','pulsators');
  $('rows').innerHTML = '<tr><td colspan="10" class="empty">Querying Lasair…</td></tr>';
  try{
    const r = await fetch('/api/targets?'+p.toString());
    const d = await r.json();
    $('count').textContent = d.count;
    $('stamp').textContent = new Date().toISOString().slice(0,16).replace('T',' ')+' UTC';
    $('errors').innerHTML = (d.errors&&d.errors.length)
      ? '<div class="err">'+d.errors.join('\n')+'</div>' : '';
    if(!d.rows.length){
      $('rows').innerHTML = '<tr><td colspan="10" class="empty">No targets match these cuts. Widen the magnitude band or lower the declination floor.</td></tr>';
      return;
    }
    $('rows').innerHTML = d.rows.map((o,i)=>{
      const npos = (o.nPosDiaSources!=null)? o.nPosDiaSources : '—';
      const jump = (o.jump1!=null)? Number(o.jump1).toFixed(1) : '—';
      const jumpHot = (o.jump1!=null && o.jump1 >= 5)?'rise':'';
      // g-band SNR: low = likely noise, high = solid detection
      const snr = (o.g_snr!=null)? Number(o.g_snr).toFixed(1) : '—';
      const snrCls = (o.g_snr==null)?'' : (o.g_snr < 5 ? 'noise' : (o.g_snr >= 10 ? 'solid' : ''));
      // TNS flag: a name means it's already reported (someone has claimed it)
      const tns = o.tns_name ? `<span class="tns" title="Already on TNS as ${o.tns_name} — likely already being followed">TNS ${o.tns_name}</span>` : '';
      return `<tr class="target" data-oid="${o.diaObjectId}" data-i="${i}" data-ra="${o.ra}" data-dec="${o.decl}">
        <td><span class="caret">▸</span> ${lasairLink(o.diaObjectId)} ${tns}</td>
        <td>${filterTag(o)}</td>
        <td class="num">${o.gmag!=null?Number(o.gmag).toFixed(2):'—'}</td>
        <td class="num">${o.rmag!=null?Number(o.rmag).toFixed(2):'—'}</td>
        <td class="num ${snrCls}">${snr}</td>
        <td class="num ${jumpHot}">${jump}</td>
        <td class="num">${npos}</td>
        <td class="coord">${coordCell(o.ra_hms, o.ra)}</td>
        <td class="coord">${coordCell(o.dec_dms, o.decl)}</td>
        <td>${o.last_seen_utc||'—'}</td>
      </tr>
      <tr class="lcrow" id="lc-${i}" style="display:none"><td colspan="10"></td></tr>`;
    }).join('');
    wireRows();
  }catch(e){
    $('rows').innerHTML = '<tr><td colspan="10" class="err">Request failed: '+e+'</td></tr>';
  }
}

// --- Light-curve expand-on-click ---------------------------------------
const lcLoaded = new Set();   // which rows already fetched (client-side memo)

function sparkline(points){
  // points: [{mjd, mag}] ascending. Magnitude axis inverted (bright = up).
  if(!points || points.length < 2) return '<span class="lcstats">Not enough points to plot.</span>';
  const W=220, H=54, pad=4;
  const ts=points.map(p=>p.mjd), ms=points.map(p=>p.mag);
  const t0=Math.min(...ts), t1=Math.max(...ts);
  const m0=Math.min(...ms), m1=Math.max(...ms);
  const x=t=>(t1===t0)?pad:pad+(t-t0)/(t1-t0)*(W-2*pad);
  // invert: smaller mag (brighter) -> higher on screen (smaller y)
  const y=m=>(m1===m0)?H/2:pad+(m-m0)/(m1-m0)*(H-2*pad);
  const d=points.map((p,i)=>(i?'L':'M')+x(p.mjd).toFixed(1)+' '+y(p.mag).toFixed(1)).join(' ');
  const last=points[points.length-1];
  return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
    <path d="${d}" fill="none" stroke="var(--flare)" stroke-width="1.6"
      stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(last.mjd).toFixed(1)}" cy="${y(last.mag).toFixed(1)}" r="2.6" fill="var(--pulse)"/>
  </svg>`;
}

function slopeBlock(d){
  if(d.slope==null) return '<div><span class="k">recent slope</span><br><span class="v">—</span></div>';
  const perDay = d.slope;                 // mag/day; negative = brightening
  const cls = perDay < -0.02 ? 'bright' : (perDay > 0.02 ? 'fade' : '');
  const word = perDay < -0.02 ? 'brightening' : (perDay > 0.02 ? 'fading' : 'flat');
  return `<div>
    <span class="k">recent slope (${d.slope_n} pts)</span><br>
    <span class="v ${cls}">${perDay.toFixed(3)} mag/day · ${word}</span>
  </div>`;
}

async function toggleRow(tr){
  const i = tr.getAttribute('data-i');
  const oid = tr.getAttribute('data-oid');
  const lc = $('lc-'+i);
  const cell = lc.firstElementChild;
  const isOpen = lc.style.display !== 'none';
  if(isOpen){ lc.style.display='none'; tr.classList.remove('open'); return; }
  tr.classList.add('open'); lc.style.display='';
  if(lcLoaded.has(oid)) return;             // already rendered once; keep it
  cell.innerHTML = '<div class="lcwrap loading">Fetching light curve &amp; Rubin schedule…</div>';
  const ra = tr.getAttribute('data-ra'), dec = tr.getAttribute('data-dec');
  try{
    // Fire both lookups together; neither blocks the other.
    const lcReq = fetch('/api/lightcurve?diaObjectId='+encodeURIComponent(oid)).then(r=>r.json());
    const schReq = (ra && dec && ra!=='null' && dec!=='null')
      ? fetch('/api/schedule?ra='+encodeURIComponent(ra)+'&dec='+encodeURIComponent(dec)).then(r=>r.json())
      : Promise.resolve(null);
    const [d, sch] = await Promise.all([lcReq, schReq]);
    if(d.error){
      cell.innerHTML = '<div class="lcwrap error">Could not load light curve: '+d.error+'</div>';
      return;                               // don't memo failures, allow retry
    }
    cell.innerHTML = `<div class="lcwrap">
      ${sparkline(d.points)}
      <div class="lcstats">
        <div><span class="k">points</span><br><span class="v">${d.n}</span></div>
        ${slopeBlock(d)}
      </div>
      ${scheduleBlock(sch)}
    </div>`;
    lcLoaded.add(oid);
    // Backfill the row's "last seen" from the curve's latest epoch, since the
    // objects table has no SELECT-able last-detection time.
    if(d.points && d.points.length){
      const lastMjd = d.points[d.points.length-1].mjd;
      const unix = (lastMjd - 40587.0) * 86400000;
      const iso = new Date(unix).toISOString().slice(0,16).replace('T',' ');
      tr.querySelector('td:last-child').textContent = iso;
    }
  }catch(e){
    cell.innerHTML = '<div class="lcwrap error">Request failed: '+e+'</div>';
  }
}

// Render the Rubin-schedule block for the detail panel.
function scheduleBlock(sch){
  if(!sch) return '';
  if(sch.error){
    return `<div class="sched"><span class="k">Rubin schedule</span><br>
      <span class="v dim" title="${sch.error}">lookup unavailable</span></div>`;
  }
  if(!sch.next_mjd){
    // Nothing scheduled = your follow-up is most valuable here.
    return `<div class="sched gap"><span class="k">Rubin revisit</span><br>
      <span class="v gapv">none scheduled</span></div>`;
  }
  // How soon, in human terms
  const nowMjd = Date.now()/86400000 + 40587.0;
  const days = sch.next_mjd - nowMjd;
  let when;
  if(days < 0.5) when = 'in <12 h';
  else if(days < 1) when = 'tonight/tomorrow';
  else when = 'in ~' + Math.round(days) + ' d';
  const prio = (sch.priority===0) ? ' · next visit'
             : (sch.priority===1) ? ' · within the hour'
             : (sch.priority===2) ? ' · 24 h forecast' : '';
  return `<div class="sched"><span class="k">Rubin revisit (${sch.count})</span><br>
    <span class="v schedv">${when} — ${sch.next_iso} UTC${prio}</span></div>`;
}

function wireRows(){
  document.querySelectorAll('tr.target').forEach(tr=>{
    tr.addEventListener('click', ev=>{
      if(ev.target.closest('a')) return;    // let the Lasair link work normally
      toggleRow(tr);
    });
  });
}
$('run').addEventListener('click', pull);
window.addEventListener('load', pull);
</script>
</body>
</html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true",
                    help="serve representative sample rows, no Lasair calls")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    app.config["DEMO"] = args.demo
    if not args.demo and not os.environ.get("LASAIR_TOKEN"):
        print("No LASAIR_TOKEN set. Either export it, or run with --demo.\n"
              "Get a token at lasair-lsst.lsst.ac.uk -> sign in -> My Profile.",
              file=sys.stderr)
    app.run(debug=False, port=args.port)
