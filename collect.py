#!/usr/bin/env python3
"""Take one traffic sample and append it to the CSV logs in data/.

Needs two environment variables (repository secrets, so the home address never
appears in the repo):

    TOMTOM_API_KEY   free key from developer.tomtom.com
    LOCATION         the home address (used for the "My street" tab only)

Extra places (places.json) are resolved once, then each is timed in BOTH
directions with TomTom's routing API (live traffic vs. free-flow travel time).
Requests per sample = 1 (home) + 2 per place.
"""
import csv, hashlib, json, math, os, sys, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
HOME_LOG = os.path.join("data", "traffic_log.csv")
PLACES_LOG = os.path.join("data", "places_log.csv")
PLACES_CFG = "places.json"
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
    if cfgs and home:
        resolved = load_resolved()
        changed = False
        for cfg in cfgs:
            old = resolved.get(cfg["id"])
            if old and old.get("config_hash") == cfg_hash(cfg):
                continue
            try:
                resolved[cfg["id"]] = resolve_place(key, cfg, home)
                changed = True
                print("resolved:", cfg["id"], resolved[cfg["id"]]["matched_address"])
            except Exception as e:
                print(f"could not resolve {cfg['id']}:", e, file=sys.stderr)
        if changed:
            os.makedirs("data", exist_ok=True)
            json.dump(resolved, open(RESOLVED, "w"), indent=1)

        rows = []
        for cfg in cfgs:
            pl = resolved.get(cfg["id"])
            if not pl:
                continue
            for d in pl["directions"]:
                try:
                    length, trav, free_s = route_times(key, d["from"], d["to"])
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
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
