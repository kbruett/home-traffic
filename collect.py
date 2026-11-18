#!/usr/bin/env python3
"""Take one traffic sample and append it to data/traffic_log.csv.

Run by GitHub Actions every 10 minutes. Needs two environment variables
(stored as repository secrets, so the address never appears in the repo):

    TOMTOM_API_KEY   free key from developer.tomtom.com
    LOCATION         the address to monitor, e.g. "1 Main St, Town, NJ 07950"
"""
import csv, json, os, sys, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
LOG = os.path.join("data", "traffic_log.csv")
FIELDS = ["timestamp", "dow", "hour", "current_mph", "freeflow_mph", "congestion_pct"]


def get(url):
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.load(r)


def sample(key, address):
    q = urllib.parse.quote(address)
    pos = get(f"https://api.tomtom.com/search/2/geocode/{q}.json?key={key}&limit=1")["results"][0]["position"]
    f = get("https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
            f"?key={key}&point={pos['lat']},{pos['lon']}&unit=MPH")["flowSegmentData"]
    cur, free = f["currentSpeed"], f["freeFlowSpeed"]
    now = datetime.now(TZ)
    return {
        "timestamp": now.isoformat(timespec="seconds"),  # includes UTC offset
        "dow": now.weekday(),
        "hour": now.hour,
        "current_mph": cur,
        "freeflow_mph": free,
        "congestion_pct": round(max(0, 1 - cur / free) * 100, 1) if free else 0,
    }


def main():
    key, address = os.environ.get("TOMTOM_API_KEY"), os.environ.get("LOCATION")
    if not key or not address:
        sys.exit("Set the TOMTOM_API_KEY and LOCATION repository secrets.")
    row = sample(key, address)
    os.makedirs("data", exist_ok=True)
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(row)


if __name__ == "__main__":
    main()
