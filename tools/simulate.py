"""Live stress simulation: run the full automatic check over many listings.

Samples real TravelStaytion listings (seed IDs plus their "similar
properties", round-robin by country), runs ``parity.cli auto`` on each in
its own work folder, and classifies where every run succeeded or failed:

    subject  -> TravelStaytion page parsed (title, licence, coordinates, photos)
    discover -> OTA candidates generated
    identity -> VERIFIED / IDENTITY_UNVERIFIED / NOT_FOUND per channel
    price    -> PRICED / UNAVAILABLE / PRICE_FAILED and the error text
    crash    -> Python traceback or timeout

Writes ``report.md`` and ``report.csv`` in the output folder.  Live runs make
real requests, so keep samples small and paced (the default pause is 20 s).

    python tools/simulate.py --sample 12 --channels airbnb --out .parity/sim
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

import httpx

DEFAULT_SEEDS = [
    "1069699", "337856", "819062", "820056", "845891",
    "240068", "323069", "718977", "993361",
]
_PROPERTY_RE = re.compile(r"window\.property\s*=\s*(\{.*?\});", re.DOTALL)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


def fetch_property(client: httpx.Client, listing_id: str) -> dict | None:
    response = client.get(f"https://www.travelstaytion.com/stay/{listing_id}")
    if response.status_code != 200:
        return None
    match = _PROPERTY_RE.search(response.text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def sample_listings(client: httpx.Client, seeds: list[str], size: int) -> list[dict]:
    """Breadth-first over 'similar properties', then round-robin by country."""
    queue = deque(seeds)
    seen: set[str] = set()
    by_country: dict[str, list[dict]] = defaultdict(list)
    fetched = 0
    while queue and fetched < size * 3:
        listing_id = queue.popleft()
        if listing_id in seen:
            continue
        seen.add(listing_id)
        prop = fetch_property(client, listing_id)
        fetched += 1
        time.sleep(1.0)
        if not prop:
            by_country["(unparseable)"].append({"id": listing_id, "country": None})
            continue
        by_country[str(prop.get("country") or "?")].append({
            "id": listing_id,
            "country": prop.get("country"),
            "title": prop.get("title"),
            "licence": prop.get("licenseNumber"),
            "bedrooms": prop.get("bedrooms"),
            "photos": len(prop.get("gallery") or []),
            "has_coords": prop.get("lat") is not None and prop.get("lng") is not None,
        })
        similar = (prop.get("similar") or {}).get("data") or []
        for item in similar:
            if isinstance(item, dict) and item.get("id"):
                queue.append(str(item["id"]))
    picked: list[dict] = []
    countries = sorted(by_country)
    while len(picked) < size and any(by_country[c] for c in countries):
        for country in countries:
            if by_country[country] and len(picked) < size:
                picked.append(by_country[country].pop(0))
    return picked


def classify(run_dir: Path, log: str, returncode: int | None, timed_out: bool) -> dict:
    out: dict = {"crash": "", "channels": {}}
    if timed_out:
        out["crash"] = "timeout"
    elif "Traceback (most recent call last)" in log:
        last = [line for line in log.splitlines() if line.strip()][-1:]
        out["crash"] = (last[0] if last else "traceback")[:200]
    elif returncode not in (0, None):
        out["crash"] = f"exit {returncode}"
    out["subject_ok"] = "-> " in log and "identifying subject" in log
    out["licence_match"] = "licence number matches" in log
    out["photo_checks"] = log.count("falling back to photo hashing")
    out["keypoint_used"] = "keypoint matching:" in log
    out["human_checks"] = log.count("ACTION NEEDED")
    results_path = run_dir / "results.json"
    if results_path.exists():
        try:
            data = json.loads(results_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for row in data.get("results", []):
            channel = row.get("channel") or row.get("site") or "?"
            out["channels"][channel] = {
                "status": row.get("status"),
                "difference_percent": row.get("difference_percent"),
                "error": (row.get("error") or "")[:300],
                "subject_total": row.get("travelstaytion_guest_total"),
                "candidate_total": row.get("ota_guest_total"),
                "candidate_url": row.get("candidate_url"),
                "currency": row.get("currency"),
                "nights": row.get("nights"),
            }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", nargs="*", default=DEFAULT_SEEDS)
    parser.add_argument("--ids", nargs="*", help="Run exactly these IDs (skip sampling)")
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--channels", nargs="+", default=["airbnb", "booking"])
    parser.add_argument(
        "--currencies", nargs="*", default=[],
        help="Rotate these currencies across listings (default: listing's own)",
    )
    parser.add_argument("--exclude", nargs="*", default=[], help="IDs not to sample")
    parser.add_argument("--no-web-search", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--pause", type=float, default=20.0, help="Seconds between listings")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds per listing")
    parser.add_argument("--out", default=".parity/simulation")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(
        headers=_HEADERS,
        timeout=25.0,
        follow_redirects=True,
        trust_env=os.environ.get("PARITY_TRUST_ENV_PROXY") == "1",
    )
    if args.ids:
        listings = [{"id": item} for item in args.ids]
    else:
        print(f"[sim] sampling {args.sample} listings from {len(args.seeds)} seed(s)...", file=sys.stderr)
        listings = sample_listings(client, args.seeds, args.sample + len(args.exclude))
        listings = [item for item in listings if item["id"] not in set(args.exclude)]
        listings = listings[:args.sample]
    (out_dir / "sample.json").write_text(json.dumps(listings, indent=2), encoding="utf-8")

    rows = []
    for position, listing in enumerate(listings, start=1):
        listing_id = listing["id"]
        run_dir = out_dir / listing_id
        run_dir.mkdir(exist_ok=True)
        command = [
            sys.executable, "-m", "parity.cli", "auto", listing_id,
            "--channels", *args.channels,
            "--work-dir", str(run_dir / ".parity" / "automatic"),
            "--governor-db", str(run_dir / ".parity" / "parity.db"),
            "--output", str(run_dir / "results.json"),
            "--dashboard-output", str(run_dir / "dashboard.html"),
        ]
        if args.currencies:
            listing["currency"] = args.currencies[(position - 1) % len(args.currencies)]
            command += ["--currency", listing["currency"]]
        if args.no_web_search:
            command.append("--no-web-search")
        if args.headless:
            command.append("--headless")
        print(f"[sim] {position}/{len(listings)} {listing_id} {listing.get('country') or ''}", file=sys.stderr)
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=args.timeout,
                encoding="utf-8", errors="replace",
            )
            log, returncode = completed.stdout + completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as error:
            log = (error.stdout or "") + (error.stderr or "") if isinstance(error.stdout, str) else ""
            returncode, timed_out = None, True
        (run_dir / "log.txt").write_text(log, encoding="utf-8")
        verdict = classify(run_dir, log, returncode, timed_out)
        verdict.update(listing)
        verdict["seconds"] = round(time.monotonic() - started)
        rows.append(verdict)
        summary = ", ".join(f"{c}={v['status']}" for c, v in verdict["channels"].items()) or "no results"
        print(f"[sim]    -> {summary}{' CRASH ' + verdict['crash'] if verdict['crash'] else ''}", file=sys.stderr)
        if position < len(listings):
            time.sleep(args.pause)

    write_reports(out_dir, rows, args.channels)
    print(f"[sim] wrote {out_dir / 'report.md'}", file=sys.stderr)
    return 0


def write_reports(out_dir: Path, rows: list[dict], channels: list[str]) -> None:
    with (out_dir / "report.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "id", "country", "currency", "title", "licence", "bedrooms", "photos", "seconds", "crash",
            *[f"{c}_{k}" for c in channels for k in ("status", "diff_pct", "error")],
        ])
        for row in rows:
            writer.writerow([
                row.get("id"), row.get("country"), row.get("currency"), row.get("title"), row.get("licence"),
                row.get("bedrooms"), row.get("photos"), row.get("seconds"), row.get("crash"),
                *[
                    value
                    for c in channels
                    for value in (
                        row["channels"].get(c, {}).get("status"),
                        row["channels"].get(c, {}).get("difference_percent"),
                        row["channels"].get(c, {}).get("error"),
                    )
                ],
            ])
    lines = ["# Simulation report", "", f"Listings: {len(rows)}", ""]
    crashes = [row for row in rows if row["crash"]]
    lines.append(f"Crashes/timeouts: {len(crashes)}")
    for channel in channels:
        counts = Counter(row["channels"].get(channel, {}).get("status") or "NONE" for row in rows)
        lines.append(f"{channel}: " + ", ".join(f"{k}={v}" for k, v in counts.most_common()))
    lines += ["", "| ID | Country | Licence | " + " | ".join(channels) + " | Notes |",
              "| --- | --- | --- | " + " | ".join("---" for _ in channels) + " | --- |"]
    for row in rows:
        cells = []
        notes = [row["crash"]] if row["crash"] else []
        for channel in channels:
            info = row["channels"].get(channel, {})
            status = info.get("status") or "—"
            if info.get("difference_percent") is not None:
                status += f" ({info['difference_percent']}%)"
            cells.append(status)
            if info.get("error") and info.get("status") not in ("PRICED",):
                notes.append(f"{channel}: {info['error'][:140]}")
        lines.append(
            f"| {row.get('id')} | {row.get('country') or ''} | {'yes' if row.get('licence') else 'no'} | "
            + " | ".join(cells) + " | " + "; ".join(notes).replace("|", "/") + " |"
        )
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
