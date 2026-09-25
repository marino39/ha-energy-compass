"""Value past exported energy at RCE prices and import it as an external statistic.

The export value counter blueprint only counts from the day it is installed.
This one-shot tool fills the history before that, so the cost card's month and
year views are not empty. Two steps:

    compute  read-only: fetch quarter-hour RCE prices from the public PSE API and
             hourly (and, where still kept, 5-minute) export statistics from Home
             Assistant; write the hourly values to a JSON file and print a monthly
             summary. Nothing in Home Assistant changes.
    import   create the external statistic from that JSON through the recorder
             API. Refuses if the statistic already exists.
             Rollback: recorder/clear_statistics for that statistic id only.

Valuation: price = max(RCE, floor) x multiplier / 1000 per kWh (net-billing
deposit: floor 0, multiplier 1.23). Hours that still have 5-minute statistics
value each 5 minutes at its quarter-hour price; older hours use the hourly mean
price. Stop the backfill (--until) at the hour the counter started.

Requires `pip install websockets`. Example:

    python tools/export_value_backfill.py compute --url wss://ha.local:8123 \\
        --token-file ~/.ha_token --export-statistic sensor.inverter_total_energy_export \\
        --from 2025-07-01 --until 2026-09-25T08:00:00+00:00 --output backfill.json
    python tools/export_value_backfill.py import --url wss://ha.local:8123 \\
        --token-file ~/.ha_token --input backfill.json
"""

import argparse
import asyncio
import datetime as dt
import json
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_STATISTIC = "energy_compass:export_value_backfill"
QUARTER = dt.timedelta(minutes=15)


def quarter_prices(rows, day, zone, floor=0.0, multiplier=1.23):
    """Map each quarter-hour start (UTC) of a local day to its price per kWh.

    PSE rows are assigned sequentially from local midnight, which stays correct
    on 23- and 25-hour daylight-saving days.
    """
    start = dt.datetime.combine(day, dt.time(), zone).astimezone(dt.UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), zone).astimezone(
        dt.UTC
    )
    expected = int((end - start) / QUARTER)
    rows = sorted(rows, key=lambda r: (r["dtime"], r["period"]))
    if len(rows) != expected:
        raise ValueError(f"{day}: {len(rows)} quarters, expected {expected}")
    return {
        start + QUARTER * i: max(float(floor), float(r["rce_pln"]))
        * float(multiplier)
        / 1000
        for i, r in enumerate(rows)
    }


def value_hours(hours, prices, until, five_minute=()):
    """Hourly export values; 5-minute rows, where present, replace the hourly mean."""
    out = []
    for r in hours:
        hour = dt.datetime.fromtimestamp(r["start"] / 1000, dt.UTC)
        if hour >= until:
            continue
        kwh = r.get("change") or 0.0
        if kwh < -1e-9:
            raise ValueError(f"negative export at {hour}")
        quarters = [prices.get(hour + QUARTER * i) for i in range(4)]
        if kwh > 0 and None in quarters:
            raise ValueError(f"missing price at {hour}")
        price = sum(quarters) / 4 if None not in quarters else 0.0
        out.append(
            {
                "start": hour.isoformat(),
                "kwh": kwh,
                "price": price,
                "value": kwh * price,
                "method": "hourly_mean",
            }
        )
    fine = {}
    for r in five_minute:
        t = dt.datetime.fromtimestamp(r["start"] / 1000, dt.UTC)
        if t >= until:
            continue
        kwh = r.get("change") or 0.0
        q = t.replace(minute=t.minute // 15 * 15)
        if kwh > 0 and q not in prices:
            raise ValueError(f"missing price at {t}")
        hour = t.replace(minute=0)
        fine[hour] = fine.get(hour, 0.0) + kwh * prices.get(q, 0.0)
    if fine:
        first = min(fine)
        for row in out:
            hour = dt.datetime.fromisoformat(row["start"])
            if hour >= first:
                row["value"], row["method"] = fine.get(hour, 0.0), "quarter"
    return out


def statistic_rows(rows):
    """Cumulative `sum` rows, with a zero row one hour before the first."""
    first = dt.datetime.fromisoformat(rows[0]["start"])
    stats, total = (
        [
            {
                "start": (first - dt.timedelta(hours=1)).isoformat(),
                "sum": 0.0,
                "state": 0.0,
            }
        ],
        0.0,
    )
    for r in rows:
        total += r["value"]
        stats.append(
            {"start": r["start"], "sum": round(total, 6), "state": round(total, 6)}
        )
    return stats, total


def monthly(rows, zone):
    months = {}
    for r in rows:
        key = dt.datetime.fromisoformat(r["start"]).astimezone(zone).strftime("%Y-%m")
        month = months.setdefault(key, [0.0, 0.0])
        month[0] += r["kwh"]
        month[1] += r["value"]
    return months


def pse_day(day):
    query = urllib.parse.quote(f"business_date eq '{day.isoformat()}'")
    url = f"https://api.raporty.pse.pl/api/rce-pln?$filter={query}&$select=dtime,period,rce_pln,business_date"
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)["value"]


class HomeAssistant:
    def __init__(self, url, token, insecure):
        self.url, self.token, self.insecure, self.next_id = (
            url.rstrip("/") + "/api/websocket",
            token,
            insecure,
            1,
        )

    async def __aenter__(self):
        import websockets

        context = None
        if self.url.startswith("wss://"):
            context = ssl.create_default_context()
            if self.insecure:
                context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        self.ws = await websockets.connect(self.url, ssl=context, max_size=2**28)
        await self.ws.recv()
        await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        if json.loads(await self.ws.recv()).get("type") != "auth_ok":
            raise SystemExit("authentication failed")
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def call(self, message):
        self.next_id += 1
        await self.ws.send(json.dumps({"id": self.next_id, **message}))
        reply = json.loads(await self.ws.recv())
        if not reply.get("success"):
            raise SystemExit(f"{message['type']} failed: {reply.get('error')}")
        return reply["result"]

    async def changes(self, statistic, period, start, end):
        result = await self.call(
            {
                "type": "recorder/statistics_during_period",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "statistic_ids": [statistic],
                "period": period,
                "types": ["change"],
            }
        )
        return result.get(statistic, [])


async def compute(args):
    zone = ZoneInfo(args.zone)
    until = dt.datetime.fromisoformat(args.until).astimezone(dt.UTC)
    start = dt.datetime.combine(args.start, dt.time(), zone).astimezone(dt.UTC)
    prices, day = {}, args.start
    while dt.datetime.combine(day, dt.time(), zone) < until.astimezone(zone):
        prices.update(
            quarter_prices(pse_day(day), day, zone, args.floor, args.multiplier)
        )
        day += dt.timedelta(days=1)
    async with HomeAssistant(
        args.url, args.token_file.expanduser().read_text().strip(), args.insecure
    ) as ha:
        hours = await ha.changes(args.export_statistic, "hour", start, until)
        five = await ha.changes(
            args.export_statistic,
            "5minute",
            max(start, until - dt.timedelta(days=10)),
            until,
        )
    rows = value_hours(hours, prices, until, five)
    args.output.write_text(
        json.dumps(
            {
                "until": until.isoformat(),
                "export_statistic": args.export_statistic,
                "rows": rows,
            },
            indent=1,
        )
    )
    for key, (kwh, value) in monthly(rows, zone).items():
        print(
            f"{key}  {kwh:8.1f} kWh  {value:9.2f}  {value / kwh if kwh else 0:6.3f}/kWh"
        )
    print(
        f"total {sum(r['kwh'] for r in rows):.1f} kWh, {sum(r['value'] for r in rows):.2f}; {len(rows)} hours -> {args.output}"
    )


async def import_(args):
    rows = json.loads(args.input.read_text())["rows"]
    stats, total = statistic_rows(rows)
    source = args.statistic_id.split(":", 1)[0]
    metadata = {
        "has_mean": False,
        "mean_type": 0,
        "has_sum": True,
        "name": args.name,
        "source": source,
        "statistic_id": args.statistic_id,
        "unit_of_measurement": args.currency,
        "unit_class": None,
    }
    async with HomeAssistant(
        args.url, args.token_file.expanduser().read_text().strip(), args.insecure
    ) as ha:
        existing = await ha.call({"type": "recorder/list_statistic_ids"})
        if any(m["statistic_id"] == args.statistic_id for m in existing):
            raise SystemExit(
                f"{args.statistic_id} already exists; inspect it before importing again"
            )
        await ha.call(
            {"type": "recorder/import_statistics", "metadata": metadata, "stats": stats}
        )
    print(
        f"imported {args.statistic_id}: {len(stats)} rows, total {total:.2f} {args.currency}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("compute", "import"):
        p = sub.add_parser(name)
        p.add_argument("--url", required=True, help="wss://host:8123 (or ws://)")
        p.add_argument(
            "--token-file",
            type=Path,
            required=True,
            help="file with a long-lived access token",
        )
        p.add_argument(
            "--insecure", action="store_true", help="skip TLS certificate verification"
        )
    c = sub.choices["compute"]
    c.add_argument(
        "--export-statistic",
        required=True,
        help="statistic id of the exported-energy meter (kWh)",
    )
    c.add_argument(
        "--from",
        dest="start",
        type=dt.date.fromisoformat,
        required=True,
        help="first local day",
    )
    c.add_argument(
        "--until", required=True, help="UTC hour where the export value counter starts"
    )
    c.add_argument("--zone", default="Europe/Warsaw")
    c.add_argument("--multiplier", type=float, default=1.23)
    c.add_argument("--floor", type=float, default=0.0, help="PLN/MWh")
    c.add_argument("--output", type=Path, default=Path("export_value_backfill.json"))
    i = sub.choices["import"]
    i.add_argument("--input", type=Path, default=Path("export_value_backfill.json"))
    i.add_argument("--statistic-id", default=DEFAULT_STATISTIC)
    i.add_argument("--name", default="Export value (backfill)")
    i.add_argument("--currency", default="PLN")
    args = parser.parse_args(argv)
    asyncio.run(compute(args) if args.command == "compute" else import_(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
