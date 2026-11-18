// Parses traffic_log.csv and computes everything the dashboard shows.
// Works in the browser (window.TrafficStats) and in Node (module.exports) for testing.
(function (root) {
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const r1 = (x) => Math.round(x * 10) / 10;
  const avg = (v) => (v.length ? r1(v.reduce((a, b) => a + b, 0) / v.length) : null);

  function parseCSV(text) {
    const lines = text.trim().split(/\r?\n/);
    lines.shift();
    return lines
      .map((l) => {
        const [ts, dow, hour, mph, free, cong] = l.split(",");
        return { ts, t: new Date(ts), dow: +dow, hour: +hour, mph: +mph, free: +free, cong: +cong };
      })
      .filter((r) => !isNaN(r.cong) && !isNaN(r.t.getTime()));
  }

  function build(rows, nowMs) {
    if (!rows.length) return { empty: true };
    const slot = {};
    rows.forEach((r) => (slot[r.dow * 24 + r.hour] = slot[r.dow * 24 + r.hour] || []).push(r.cong));
    const S = (d, h) => slot[d * 24 + h] || [];

    const last = rows[rows.length - 1];
    const same = rows.slice(0, -1).filter((r) => r.dow === last.dow && r.hour === last.hour).map((r) => r.cong);

    const heat = DAYS.map((_, d) => [...Array(24).keys()].map((h) => avg(S(d, h))));
    const counts = DAYS.map((_, d) => [...Array(24).keys()].map((h) => S(d, h).length));
    const hourly = (days) => [...Array(24).keys()].map((h) => avg(days.flatMap((d) => S(d, h))));
    const hrAll = hourly([0, 1, 2, 3, 4, 5, 6]);
    const dow = DAYS.map((_, d) => avg([...Array(24).keys()].flatMap((h) => S(d, h))));

    const rk = (arr) => arr.map((v, i) => [i, v]).filter((x) => x[1] != null);
    const hrs = rk(hrAll), days = rk(dow);
    const maxBy = (a) => a.reduce((m, x) => (x[1] > m[1] ? x : m));
    const minBy = (a) => a.reduce((m, x) => (x[1] < m[1] ? x : m));

    const cutoff = last.t.getTime() - 24 * 3600 * 1000;
    const recent = rows
      .filter((r) => r.t.getTime() >= cutoff)
      .map((r) => ({ t: r.ts.substring(11, 16), cong: r.cong, mph: r.mph }));

    const first = rows[0];
    const spanDays = Math.max(1, Math.floor((last.t - first.t) / 86400000) + 1);

    return {
      empty: false,
      now: {
        mph: last.mph, free: last.free, cong: last.cong,
        typical: avg(same), typical_n: same.length,
        age_min: Math.round(((nowMs ?? Date.now()) - last.t.getTime()) / 60000),
      },
      stats: {
        samples: rows.length, days: spanDays,
        since: first.t.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "America/New_York" }),
        avg_cong: avg(rows.map((r) => r.cong)), avg_mph: avg(rows.map((r) => r.mph)),
        busiest_hour: hrs.length ? maxBy(hrs) : null, quietest_hour: hrs.length ? minBy(hrs) : null,
        busiest_day: days.length ? [DAYS[maxBy(days)[0]], maxBy(days)[1]] : null,
        quietest_day: days.length ? [DAYS[minBy(days)[0]], minBy(days)[1]] : null,
        worst_cong: Math.max(...rows.map((r) => r.cong)),
      },
      heat, counts,
      hourly: { all: hrAll, weekday: hourly([0, 1, 2, 3, 4]), weekend: hourly([5, 6]) },
      dow, recent, days: DAYS,
    };
  }

  const api = { parseCSV, build, DAYS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TrafficStats = api;
})(typeof window !== "undefined" ? window : globalThis);
