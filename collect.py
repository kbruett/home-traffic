#!/usr/bin/env python3
"""Take one traffic sample and append it to the CSV logs in data/.

Needs two environment variables (repository secrets, so the home address never
appears in the repo):

    TOMTOM_API_KEY   free key from developer.tomtom.com
    LOCATION         the home address (used for the "My street" tab only)

Extra places (places.json) are resolved once, then each is timed in BOTH
directions with TomTom's routing API (live traffic vs. free-flow travel time).
Commutes (commutes.json) are home <-> office routes, timed both ways only on the
days/hours they matter (default 5-11 AM and 3-8 PM on that office's days).
Traffic incidents (crashes, closures, jams) are written to data/incidents.json.
Requests per sample = 1 (home) + 2 per place + 1 (incidents) + 2-4 (active commute).
"""
import csv, hashlib, json, math, os, sys, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
HOME_LOG = os.path.join("data", "traffic_log.csv")
PLACES_LOG = os.path.join("data", "places_log.csv")
PLACES_CFG = "places.json"
COMMUTES_CFG = "commutes.json"
INCIDENTS = os.path.join("data", "incidents.json")
DEFAULT_WINDOWS = [[5, 11], [15, 20]]
RESOLVED = os.path.join("data", "places_resolved.json")
CACHE = ".cache"
HOME_FIELDS = ["timestamp", "dow", "hour", "current_mph", "freeflow_mph", "congestion_pct"]
PLACE_FIELDS = HOME_FIELDS + ["place", "direction", "travel_s", "free_s"]
MPS_TO_MPH = 2.23694
R_EARTH = 6371000.0


def get(url):
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.load(r)


# ---------- geometry ----------
def offset(lat, lon, bearing_deg, dist_m):
    b, d = math.radians(bearing_deg), dist_m / R_EARTH
    la, lo = math.radians(lat), math.radians(lon)
    la2 = math.asin(math.sin(la) * math.cos(d) + math.cos(la) * math.sin(d) * math.cos(b))
    lo2 = lo + math.atan2(math.sin(b) * math.sin(d) * math.cos(la), math.cos(d) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


def bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(a))


def compass(b):
    return ["Northbound", "Eastbound", "Southbound", "Westbound"][int(((b + 45) % 360) // 90)]


# ---------- TomTom calls ----------
def flow_url(key, lat, lon):
    return ("https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
            f"?key={key}&point={lat},{lon}&unit=MPH")


def home_point(key, address):
    """Geocode the home address once per run (cached in an untracked file)."""
    path = os.path.join(CACHE, "home.json")
    if os.path.exists(path):
        return json.load(open(path))
    q = urllib.parse.quote(address)
    pos = get(f"https://api.tomtom.com/search/2/geocode/{q}.json?key={key}&limit=1")["results"][0]["position"]
    os.makedirs(CACHE, exist_ok=True)
    json.dump(pos, open(path, "w"))
    return pos


def sample_home(key, pos):
    f = get(flow_url(key, pos["lat"], pos["lon"]))["flowSegmentData"]
    cur, free = f["currentSpeed"], f["freeFlowSpeed"]
    return cur, free


def street_at(key, lat, lon):
    url = f"https://api.tomtom.com/search/2/reverseGeocode/{lat},{lon}.json?key={key}&radius=40"
    try:
        return get(url)["addresses"][0]["address"].get("streetName", "")
    except Exception:
        return ""


def resolve_place(key, cfg, bias):
    """Find the place, work out which way the named road runs, and pick two route ends."""
    q = urllib.parse.quote(cfg["query"])
    url = (f"https://api.tomtom.com/search/2/search/{q}.json?key={key}&limit=1&countrySet=US"
           f"&lat={bias['lat']}&lon={bias['lon']}&radius=15000")
    res = get(url)["results"][0]
    p = res["position"]
    lat, lon = p["lat"], p["lon"]

    # Sample the road segment at the point and 30 m N/E/S/W; keep the one on the named road.
    cands, matched = [], None
    for brg in (None, 0, 90, 180, 270):
        la, lo = (lat, lon) if brg is None else offset(lat, lon, brg, 30)
        try:
            pts = get(flow_url(key, la, lo))["flowSegmentData"]["coordinates"]["coordinate"]
        except Exception:
            continue
        a, b = pts[0], pts[-1]
        mid = pts[len(pts) // 2]
        if distance_m(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) < 5:
            continue
        name = street_at(key, mid["latitude"], mid["longitude"])
        cand = {"bearing": bearing(a["latitude"], a["longitude"], b["latitude"], b["longitude"]), "street": name}
        cands.append(cand)
        if cfg.get("road", "").lower() in name.lower():
            matched = cand
            break
    chosen = matched or (cands[0] if cands else {"bearing": 0.0, "street": ""})
    brg = float(cfg.get("bearing", chosen["bearing"]))
    span = float(cfg.get("span_m", 300))
    a = offset(lat, lon, brg + 180, span)
    b = offset(lat, lon, brg, span)
    return {
        "id": cfg["id"], "name": cfg["name"], "query": cfg["query"], "road": cfg.get("road", ""),
        "span_m": span, "config_hash": cfg_hash(cfg),
        "matched_address": res.get("address", {}).get("freeformAddress", ""),
        "matched_name": (res.get("poi") or {}).get("name", ""),
        "matched_type": res.get("type", ""),
        "lat": lat, "lon": lon, "bearing": round(brg, 1),
        "road_matched": bool(matched or "bearing" in cfg), "road_street": chosen["street"],
        "from_home_m": round(distance_m(lat, lon, bias["lat"], bias["lon"])),
        "directions": [
            {"label": compass(brg), "from": {"lat": a[0], "lon": a[1]}, "to": {"lat": b[0], "lon": b[1]}},
            {"label": compass(brg + 180), "from": {"lat": b[0], "lon": b[1]}, "to": {"lat": a[0], "lon": a[1]}},
        ],
    }


def cfg_hash(cfg):
    return hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:10]


def route_times(key, frm, to):
    url = ("https://api.tomtom.com/routing/1/calculateRoute/"
           f"{frm['lat']:.6f},{frm['lon']:.6f}:{to['lat']:.6f},{to['lon']:.6f}/json"
           f"?key={key}&traffic=true&travelMode=car&routeType=fastest"
           "&computeTravelTimeFor=all&routeRepresentation=summaryOnly")
    s = get(url)["routes"][0]["summary"]
    return s["lengthInMeters"], s["travelTimeInSeconds"], s.get("noTrafficTravelTimeInSeconds", s["travelTimeInSeconds"])



# ---------- commutes ----------
def pt_to_seg_m(lat, lon, a, b):
    """Distance in metres from a point to segment a-b (equirectangular approximation)."""
    k = math.cos(math.radians(lat)) * 111320.0
    px, py = (lon) * k, lat * 110540.0
    ax, ay = a[1] * k, a[0] * 110540.0
    bx, by = b[1] * k, b[0] * 110540.0
    dx, dy = bx - ax, by - ay
    t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def min_dist_to_line(lat, lon, line):
    return min((pt_to_seg_m(lat, lon, line[i], line[i + 1]) for i in range(len(line) - 1)), default=1e9)


def decimate(points, step_m=150):
    out = [points[0]]
    for p in points[1:]:
        if distance_m(out[-1][0], out[-1][1], p[0], p[1]) >= step_m:
            out.append(p)
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def road_summary(instructions, total_m):
    """Collapse turn-by-turn guidance into the main roads used, in order, with distance."""
    segs = []
    for i, ins in enumerate(instructions):
        start = ins.get("routeOffsetInMeters", 0)
        end = instructions[i + 1].get("routeOffsetInMeters", total_m) if i + 1 < len(instructions) else total_m
        nums = ins.get("roadNumbers") or []
        name = nums[0] if nums else (ins.get("street") or "")
        if not name or end <= start:
            continue
        if segs and segs[-1]["name"] == name:
            segs[-1]["m"] += end - start
        else:
            segs.append({"name": name, "m": end - start, "numbered": bool(nums)})
    keep = [s for s in segs if s["numbered"] or s["m"] >= 2000]
    merged = []
    for s in keep:
        if merged and merged[-1]["name"] == s["name"]:
            merged[-1]["m"] += s["m"]
        else:
            merged.append(dict(s))
    return [{"name": s["name"], "miles": round(s["m"] / 1609.34, 1)} for s in merged if s["m"] >= 400]


def route_geometry(key, frm, to, guidance=False):
    url = ("https://api.tomtom.com/routing/1/calculateRoute/"
           f"{frm[0]:.6f},{frm[1]:.6f}:{to[0]:.6f},{to[1]:.6f}/json?key={key}&traffic=true&travelMode=car&routeType=fastest"
           + ("&instructionsType=text" if guidance else ""))
    r = get(url)["routes"][0]
    pts = [(p["latitude"], p["longitude"]) for leg in r["legs"] for p in leg["points"]]
    return r, pts


def resolve_commute(key, cfg, home):
    q = urllib.parse.quote(cfg["office"])
    url = (f"https://api.tomtom.com/search/2/search/{q}.json?key={key}&limit=1&countrySet=US"
           f"&lat={home['lat']}&lon={home['lon']}&radius=80000")
    res = get(url)["results"][0]
    p = res["position"]
    route, pts = route_geometry(key, (home["lat"], home["lon"]), (p["lat"], p["lon"]), guidance=True)
    summ = route["summary"]
    roads = road_summary((route.get("guidance") or {}).get("instructions", []), summ["lengthInMeters"])
    os.makedirs(CACHE, exist_ok=True)
    json.dump(decimate(pts), open(os.path.join(CACHE, f"{cfg['id']}.json"), "w"))
    office = {"lat": p["lat"], "lon": p["lon"]}
    return {
        "id": cfg["id"], "kind": "commute", "name": cfg["name"], "short": cfg.get("short", cfg["name"]),
        "days": cfg["days"], "windows": cfg.get("windows", DEFAULT_WINDOWS), "query": cfg["office"],
        "config_hash": cfg_hash(cfg),
        "matched_address": res.get("address", {}).get("freeformAddress", ""), "matched_type": res.get("type", ""),
        "lat": p["lat"], "lon": p["lon"], "road_matched": True,
        "route_miles": round(summ["lengthInMeters"] / 1609.34, 1), "roads": roads,
        "directions": [
            {"label": "To office", "from": "home", "to": office},
            {"label": "To home", "from": office, "to": "home"},
        ],
    }


def commute_active(pl, now):
    return now.weekday() in pl["days"] and any(a <= now.hour < b for a, b in pl["windows"])


def corridor(key, pl, home):
    """Decimated route polyline, cached per run in an untracked file (never committed)."""
    path = os.path.join(CACHE, f"{pl['id']}.json")
    if os.path.exists(path):
        return json.load(open(path))
    _, pts = route_geometry(key, (home["lat"], home["lon"]), (pl["lat"], pl["lon"]))
    os.makedirs(CACHE, exist_ok=True)
    line = decimate(pts)
    json.dump(line, open(path, "w"))
    return line


# ---------- incidents ----------
INC_LABEL = {1: "Crash", 2: "Fog", 3: "Dangerous conditions", 4: "Rain", 5: "Ice", 6: "Traffic jam", 7: "Lane closed",
             8: "Road closed", 9: "Road work", 10: "Wind", 11: "Flooding", 14: "Broken-down vehicle"}
INC_FIELDS = ("{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,"
              "events{description,iconCategory},startTime,endTime,from,to,length,delay,roadNumbers,timeValidity}}}")


def fetch_incidents(key, lat_min, lon_min, lat_max, lon_max):
    url = ("https://api.tomtom.com/traffic/services/5/incidentDetails?key=" + key
           + f"&bbox={lon_min:.5f},{lat_min:.5f},{lon_max:.5f},{lat_max:.5f}"
           + "&fields=" + urllib.parse.quote(INC_FIELDS, safe="{},")
           + "&language=en-US&timeValidityFilter=present")
    return get(url).get("incidents", [])


def inc_coords(inc):
    g = inc.get("geometry") or {}
    c = g.get("coordinates") or []
    if g.get("type") == "Point":
        return [(c[1], c[0])] if len(c) >= 2 else []
    return [(q[1], q[0]) for q in c if len(q) >= 2]


def summarize(inc):
    pr = inc.get("properties", {})
    cat = pr.get("iconCategory", 0)
    mag = pr.get("magnitudeOfDelay", 0)
    # Keep what affects a drive; skip minor road work and weather labels with no delay.
    if cat in (9, 10, 2, 4, 14) and mag < 2:
        return None
    if cat == 0 and mag < 2:
        return None
    ev = (pr.get("events") or [{}])[0]
    return {
        "id": pr.get("id", ""), "category": INC_LABEL.get(cat, "Incident"), "severity": mag,
        "description": ev.get("description", ""), "from": pr.get("from", ""), "to": pr.get("to", ""),
        "roads": pr.get("roadNumbers") or [], "delay_s": pr.get("delay") or 0, "length_m": pr.get("length") or 0,
        "start": pr.get("startTime", ""),
    }


def home_incidents(key, home, radius_m=3500):
    dlat, dlon = radius_m / 110540.0, radius_m / (111320.0 * math.cos(math.radians(home["lat"])))
    out = []
    for inc in fetch_incidents(key, home["lat"] - dlat, home["lon"] - dlon, home["lat"] + dlat, home["lon"] + dlon):
        cs = inc_coords(inc)
        if not cs or min(distance_m(home["lat"], home["lon"], a, b) for a, b in cs) > radius_m:
            continue
        s = summarize(inc)
        if s:
            out.append(s)
    return out


def route_incidents(key, line, buffer_m=150):
    lats, lons = [p[0] for p in line], [p[1] for p in line]
    pad = 0.003
    out = []
    for inc in fetch_incidents(key, min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad):
        cs = inc_coords(inc)
        if not cs or min(min_dist_to_line(a, b, line) for a, b in cs) > buffer_m:
            continue
        s = summarize(inc)
        if s:
            out.append(s)
    return out


def save_incidents(scopes):
    """scopes: {scope_id: (label, [items])}. Rewrites only if the content changed."""
    cur = json.load(open(INCIDENTS)) if os.path.exists(INCIDENTS) else {}
    for sid, (label, items) in scopes.items():
        items = sorted(items, key=lambda x: (-x["severity"], x["id"]))
        cur[sid] = {"label": label, "items": items}
    new = json.dumps(cur, indent=1, sort_keys=True)
    old = open(INCIDENTS).read() if os.path.exists(INCIDENTS) else ""
    if new != old:
        os.makedirs("data", exist_ok=True)
        open(INCIDENTS, "w").write(new)


# ---------- logging ----------
def append(path, fields, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def base_row(now):
    return {"timestamp": now.isoformat(timespec="seconds"), "dow": now.weekday(), "hour": now.hour}


def load_resolved():
    return json.load(open(RESOLVED)) if os.path.exists(RESOLVED) else {}


def main():
    key, address = os.environ.get("TOMTOM_API_KEY"), os.environ.get("LOCATION")
    if not key or not address:
        sys.exit("Set the TOMTOM_API_KEY and LOCATION repository secrets.")
    now = datetime.now(TZ)
    ok = 0

    home = None
    try:
        home = home_point(key, address)
        cur, free = sample_home(key, home)
        row = base_row(now)
        row.update(current_mph=cur, freeflow_mph=free,
                   congestion_pct=round(max(0, 1 - cur / free) * 100, 1) if free else 0)
        append(HOME_LOG, HOME_FIELDS, [row])
        print("home:", row)
        ok += 1
    except Exception as e:
        print("home sample failed:", e, file=sys.stderr)

    cfgs = json.load(open(PLACES_CFG)) if os.path.exists(PLACES_CFG) else []
    commutes = json.load(open(COMMUTES_CFG)) if os.path.exists(COMMUTES_CFG) else []
    if home and (cfgs or commutes):
        resolved = load_resolved()
        changed = False
        for cfg, fn in [(c, resolve_place) for c in cfgs] + [(c, resolve_commute) for c in commutes]:
            old = resolved.get(cfg["id"])
            if old and old.get("config_hash") == cfg_hash(cfg):
                continue
            try:
                resolved[cfg["id"]] = fn(key, cfg, home)
                changed = True
                print("resolved:", cfg["id"], resolved[cfg["id"]]["matched_address"])
            except Exception as e:
                print(f"could not resolve {cfg['id']}:", e, file=sys.stderr)
        if changed:
            os.makedirs("data", exist_ok=True)
            json.dump(resolved, open(RESOLVED, "w"), indent=1)

        def coord(v):
            return {"lat": home["lat"], "lon": home["lon"]} if v == "home" else v

        rows = []
        wanted = [c["id"] for c in cfgs] + [c["id"] for c in commutes]
        for pid in wanted:
            pl = resolved.get(pid)
            if not pl:
                continue
            if pl.get("kind") == "commute" and not commute_active(pl, now):
                continue
            for d in pl["directions"]:
                try:
                    length, trav, free_s = route_times(key, coord(d["from"]), coord(d["to"]))
                    row = base_row(now)
                    row.update(current_mph=round(length / trav * MPS_TO_MPH, 1),
                               freeflow_mph=round(length / free_s * MPS_TO_MPH, 1),
                               congestion_pct=round(max(0, 1 - free_s / trav) * 100, 1),
                               place=pl["id"], direction=d["label"], travel_s=trav, free_s=free_s)
                    rows.append(row)
                except Exception as e:
                    print(f"route failed {pl['id']} {d['label']}:", e, file=sys.stderr)
        if rows:
            append(PLACES_LOG, PLACE_FIELDS, rows)
            ok += 1
            for r in rows:
                print("place:", r)

        # Incidents: near home always; along a commute route only while it is being monitored.
        scopes = {}
        try:
            scopes["home"] = ("Near home", home_incidents(key, home))
        except Exception as e:
            print("incidents (home) failed:", e, file=sys.stderr)
        for pid in [c["id"] for c in commutes]:
            pl = resolved.get(pid)
            if not pl:
                continue
            if not commute_active(pl, now):
                scopes[pid] = (pl["name"], [])
                continue
            try:
                scopes[pid] = (pl["name"], route_incidents(key, corridor(key, pl, home)))
            except Exception as e:
                print(f"incidents ({pid}) failed:", e, file=sys.stderr)
        if scopes:
            save_incidents(scopes)
            print("incidents:", {k: len(v[1]) for k, v in scopes.items()})
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
