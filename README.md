# Home traffic monitor

Samples road traffic every 10 minutes with a GitHub Actions job and shows averages by
day and hour on a GitHub Pages dashboard.

- `collect.py` takes one sample; `.github/workflows/collect.yml` runs it every 10 minutes
- `data/traffic_log.csv` is the growing log
- `docs/` is the dashboard (served by GitHub Pages)

## Setup (all doable from a phone browser)
1. Settings → Secrets and variables → Actions → add `TOMTOM_API_KEY` and `LOCATION`.
2. Settings → Pages → Deploy from a branch → `main` / `/docs`.
3. Actions tab → collect-traffic → Run workflow (runs once now, then every 10 minutes).
4. Open `https://<your-username>.github.io/<repo>/` and Add to Home Screen.
