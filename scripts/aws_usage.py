#!/usr/bin/env python3
"""Read-only AWS credit / spend / GPU-hours report for the asr-bench account.

Shells out to the `aws` CLI (only get-/list-/describe- calls). Cost Explorer
costs $0.01 per request, so results are cached in .cache/aws_usage.json for 6 h.

Usage: scripts/aws_usage.py [--json] [--refresh] [--since YYYY-MM-DD] [--brief]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PROFILE = "satli"
DEFAULT_REGION = "us-east-1"
CREDIT_START = "2026-10-22"  # Activate credits approved
CREDIT_TOTAL = 10000.0
PROJECT_TAG = "asr-bench"
CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "aws_usage.json"
CACHE_TTL_S = 6 * 3600
BURN_DAYS = 7
LONG_RUNNING_H = 12
LOW_CREDITS_FRAC = 0.20
EXPIRY_WARN_DAYS = 60
MAX_CE_PAGES = 5
MAX_RUNWAY_DAYS = 3650

# Everything except credits/refunds = gross usage cost.
GROSS_FILTER = {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}
CREDIT_FILTER = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit"]}}
BOX_USAGE_RE = re.compile(r"(?:^|-)BoxUsage:([a-z0-9]+)\.([a-z0-9]+)$")
GPU_FAMILY_RE = re.compile(r"^(g4dn|g5|g6e?|p\d.*)$")


class AwsError(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


# ---------- aws CLI ----------

def run_aws(args, profile, region):
    cmd = ["aws", *args, "--output", "json", "--profile", profile, "--region", region]
    env = {**os.environ, "AWS_PAGER": ""}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        raise AwsError(f"aws {' '.join(args[:2])} failed: {msg[-1] if msg else proc.returncode}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def check_prerequisites(profile):
    """Return an error string, or None if the CLI and profile exist."""
    if not shutil.which("aws"):
        return "aws CLI not found on PATH"
    proc = subprocess.run(["aws", "configure", "list-profiles"], capture_output=True, text=True)
    if proc.returncode != 0 or profile not in proc.stdout.split():
        return f"AWS profile '{profile}' not found (set AWS_PROFILE)"
    return None


# ---------- parsing ----------

def parse_ts(v):
    """Epoch seconds (number or numeric string) or ISO-8601 -> aware datetime, or None."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or re.fullmatch(r"\d+(\.\d+)?", str(v)):
        return datetime.fromtimestamp(float(v), timezone.utc)
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _amount(d):
    return abs(float(d["currencyAmount"])) if d and d.get("currencyAmount") is not None else None


def parse_credits(resp, now):
    """get-credits response -> list of normalized credit dicts."""
    out = []
    for c in resp.get("credits", []):
        end = parse_ts(c.get("endDate"))
        initial = _amount(c.get("initialAmount"))
        remaining = _amount(c.get("remainingAmount"))
        estimated = _amount(c.get("estimatedAmount"))
        effective = estimated if estimated is not None else remaining
        expired = end is not None and end <= now
        out.append({
            "id": c.get("creditId"),
            "name": c.get("description") or c.get("creditType") or "credit",
            "type": c.get("creditType"),
            "status": c.get("creditStatus"),
            "initial": initial,
            "remaining": remaining,
            "estimated": estimated,
            "effective": effective,
            "pct_used": 100 * (initial - effective) / initial if initial and effective is not None else None,
            "expires": end.date().isoformat() if end else None,
            "days_left": (end - now).days if end else None,
            "active": not expired and c.get("creditStatus", "ENABLED") != "DISABLED",
            "source": "get-credits",
        })
    return out


def fallback_credits(credit_sum, now):
    """Estimate from Cost Explorer Credit records when get-credits errors."""
    used = abs(credit_sum)
    remaining = max(CREDIT_TOTAL - used, 0.0)
    return [{
        "id": None, "name": "AWS Activate (estimated from Cost Explorer)", "type": "Promotion",
        "status": None, "initial": CREDIT_TOTAL, "remaining": None, "estimated": remaining,
        "effective": remaining, "pct_used": 100 * used / CREDIT_TOTAL, "expires": None,
        "days_left": None, "active": True, "source": "cost-explorer-fallback",
    }]


def parse_ce_days(results_by_time):
    """Flatten daily CE rows -> [(date, service, usage_type, cost, quantity)]."""
    rows = []
    for r in results_by_time:
        day = date.fromisoformat(r["TimePeriod"]["Start"][:10])
        for g in r.get("Groups", []):
            service, usage_type = (g["Keys"] + ["", ""])[:2]
            m = g["Metrics"]
            rows.append((day, service, usage_type,
                         float(m["UnblendedCost"]["Amount"]),
                         float(m.get("UsageQuantity", {}).get("Amount", 0))))
    return rows


# ---------- aggregation ----------

def aggregate_spend(rows, start, top_n=5):
    """Gross cost since `start` (inclusive): total + top services."""
    by_service = {}
    for day, service, _, cost, _ in rows:
        if day >= start:
            by_service[service] = by_service.get(service, 0.0) + cost
    top = sorted(by_service.items(), key=lambda kv: -kv[1])
    return {"total": sum(by_service.values()), "services": by_service,
            "top": [{"service": s, "cost": c} for s, c in top[:top_n] if abs(c) >= 0.005]}


def aggregate_gpu(rows, start):
    """GPU BoxUsage hours/cost per instance type since `start` (inclusive)."""
    by_type = {}
    for day, _, usage_type, cost, qty in rows:
        if day < start:
            continue
        m = BOX_USAGE_RE.search(usage_type)
        if not m or not GPU_FAMILY_RE.match(m.group(1)):
            continue
        itype = f"{m.group(1)}.{m.group(2)}"
        t = by_type.setdefault(itype, {"hours": 0.0, "cost": 0.0})
        t["hours"] += qty
        t["cost"] += cost
    return {"by_type": by_type,
            "total_hours": sum(t["hours"] for t in by_type.values()),
            "total_cost": sum(t["cost"] for t in by_type.values())}


def burn_rate(rows, today, remaining, days=BURN_DAYS):
    """Average daily gross spend over the last `days` full days (ending yesterday)."""
    start = today - timedelta(days=days)
    total = sum(cost for day, _, _, cost, _ in rows if start <= day < today)
    daily = total / days
    out = {"days": days, "daily": daily, "remaining": remaining,
           "days_left": None, "runout_date": None}
    if remaining is not None and daily > 0:
        out["days_left"] = remaining / daily
        if out["days_left"] <= MAX_RUNWAY_DAYS:  # tiny spend -> date overflow / meaningless
            out["runout_date"] = (today + timedelta(days=int(out["days_left"]))).isoformat()
    return out


def summarize_instances(resp, now):
    out = []
    for res in resp.get("Reservations", []):
        for i in res.get("Instances", []):
            launch = parse_ts(i.get("LaunchTime"))
            state = i["State"]["Name"]
            up = (now - launch).total_seconds() / 3600 if launch and state == "running" else None
            out.append({"id": i["InstanceId"], "type": i["InstanceType"], "state": state,
                        "uptime_h": up})
    return sorted(out, key=lambda i: (i["state"] != "running", i["id"]))


def summarize_volumes(resp):
    vols = resp.get("Volumes", [])
    return {"count": len(vols), "total_gb": sum(v.get("Size", 0) for v in vols)}


def build_warnings(credits, instances):
    warns = []
    for i in instances:
        if i["uptime_h"] is not None and i["uptime_h"] > LONG_RUNNING_H:
            warns.append(f"{i['id']} ({i['type']}) has been running {fmt_hours(i['uptime_h'])} "
                         f"(> {LONG_RUNNING_H} h)")
    active = [c for c in credits if c["active"]]
    initial = sum(c["initial"] or 0 for c in active)
    left = sum(c["effective"] or 0 for c in active)
    if initial and left / initial < LOW_CREDITS_FRAC:
        warns.append(f"credits low: ${left:,.2f} of ${initial:,.2f} left ({100 * left / initial:.0f}%)")
    for c in active:
        if c["days_left"] is not None and c["days_left"] <= EXPIRY_WARN_DAYS and (c["effective"] or 0) > 0:
            warns.append(f"credit '{c['name']}' expires {c['expires']} (in {c['days_left']} d)")
    return warns


# ---------- Cost Explorer (cached) ----------

def ce_query(args, profile, region):
    """Run a get-cost-and-usage call, following NextPageToken. Returns ResultsByTime."""
    results, token = [], None
    for _ in range(MAX_CE_PAGES):
        page_args = ["ce", "get-cost-and-usage", *args]
        if token:
            page_args += ["--next-page-token", token]
        resp = run_aws(page_args, profile, region)
        results += resp.get("ResultsByTime", [])
        token = resp.get("NextPageToken")
        if not token:
            break
    return results


def fetch_usage(start, end, profile, region):
    return ce_query([
        "--time-period", f"Start={start},End={end}", "--granularity", "DAILY",
        "--metrics", "UnblendedCost", "UsageQuantity",
        "--group-by", "Type=DIMENSION,Key=SERVICE", "Type=DIMENSION,Key=USAGE_TYPE",
        "--filter", json.dumps(GROSS_FILTER),
    ], profile, region)


def fetch_credit_sum(end, profile, region):
    results = ce_query([
        "--time-period", f"Start={CREDIT_START},End={end}", "--granularity", "MONTHLY",
        "--metrics", "UnblendedCost", "--filter", json.dumps(CREDIT_FILTER),
    ], profile, region)
    return sum(float(r["Total"]["UnblendedCost"]["Amount"]) for r in results)


def load_cache(key, refresh, now):
    if refresh or not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    if data.get("key") != key or now.timestamp() - data.get("fetched_at", 0) > CACHE_TTL_S:
        return {}
    return data


def save_cache(data):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data))


# ---------- report ----------

def collect(args, profile, region, now):
    today = now.date()
    since = date.fromisoformat(args.since)
    month_start = today.replace(day=1)
    errors = []

    ident = run_aws(["sts", "get-caller-identity"], profile, region)
    account = ident["Account"]

    # Credits (free API); fall back to Cost Explorer estimate on error.
    cache_key = {"profile": profile, "account": account, "since": args.since, "today": today.isoformat()}
    cache = load_cache(cache_key, args.refresh, now)
    credits, credits_note = [], None
    try:
        start_epoch = int((now - timedelta(days=360)).timestamp())
        credits = parse_credits(run_aws(
            ["billing", "get-credits", "--account-id", account, "--start-date", str(start_epoch)],
            profile, region), now)
        if not credits:
            raise AwsError("get-credits returned no credits")
    except (AwsError, ValueError, KeyError) as e:
        credits_note = f"get-credits unavailable ({e}); estimated from Cost Explorer Credit records"

    end = (today + timedelta(days=1)).isoformat()
    cache_dirty = False
    if "usage" not in cache:
        try:
            qstart = min(since, month_start, today - timedelta(days=BURN_DAYS))
            cache = {"key": cache_key, "fetched_at": now.timestamp(),
                     "usage": fetch_usage(qstart.isoformat(), end, profile, region)}
            cache_dirty = True
        except AwsError as e:
            errors.append(f"cost explorer: {e}")
    if credits_note and "usage" in cache:
        if "credit_sum" not in cache:
            try:
                cache["credit_sum"] = fetch_credit_sum(end, profile, region)
                cache_dirty = True
            except AwsError as e:
                errors.append(f"cost explorer (credits): {e}")
        if "credit_sum" in cache:
            credits = fallback_credits(cache["credit_sum"], now)
    if cache_dirty:
        save_cache(cache)

    rows = parse_ce_days(cache.get("usage", []))
    active = [c for c in credits if c["active"]]
    remaining = sum(c["effective"] or 0 for c in active) if active else None

    instances, volumes = [], {"count": 0, "total_gb": 0}
    tag = ["--filters", f"Name=tag:Project,Values={PROJECT_TAG}"]
    try:
        instances = summarize_instances(run_aws(
            ["ec2", "describe-instances", *tag,
             "Name=instance-state-name,Values=pending,running,stopping,stopped"],
            profile, region), now)
        volumes = summarize_volumes(run_aws(["ec2", "describe-volumes", *tag], profile, region))
    except AwsError as e:
        errors.append(f"ec2: {e}")

    age_h = (now.timestamp() - cache["fetched_at"]) / 3600 if "fetched_at" in cache else None
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "profile": profile, "account": account, "region": region,
        "since": args.since, "month_start": month_start.isoformat(),
        "credits": credits, "credits_note": credits_note,
        "credits_total_remaining": remaining,
        "spend": {"since": aggregate_spend(rows, since), "month": aggregate_spend(rows, month_start)},
        "gpu": {"since": aggregate_gpu(rows, since), "month": aggregate_gpu(rows, month_start)},
        "burn": burn_rate(rows, today, remaining),
        "instances": instances, "volumes": volumes,
        "warnings": build_warnings(credits, instances),
        "ce_age_hours": age_h,
        "errors": errors,
    }


def fmt_hours(h):
    return f"{int(h // 24)}d {int(h % 24)}h" if h >= 24 else f"{int(h)}h{int(h % 1 * 60):02d}m"


def usd(x):
    return "n/a" if x is None else f"${x:,.2f}"


def render(r, brief=False):
    L = []
    s, m = r["spend"]["since"], r["spend"]["month"]
    gs, gm = r["gpu"]["since"], r["gpu"]["month"]
    b = r["burn"]
    L.append(f"AWS usage | profile {r['profile']} ({r['account']}, {r['region']}) | {r['generated_at']}")

    L.append("")
    L.append("CREDITS" + (" (ESTIMATED)" if r["credits_note"] else ""))
    for c in r["credits"]:
        used = f"{c['pct_used']:.1f}% used" if c["pct_used"] is not None else "used n/a"
        exp = f"expires {c['expires']} ({c['days_left']} d)" if c["expires"] else "expiry n/a"
        if not c["active"]:
            exp += " [inactive]"
        est = f", est. remaining {usd(c['estimated'])}" if c["source"] == "get-credits" and c["estimated"] is not None else ""
        rem = usd(c["remaining"]) if c["remaining"] is not None else usd(c["estimated"])
        L.append(f"  {c['name']} ({c['type']}): original {usd(c['initial'])}, remaining {rem}{est}, {used}, {exp}")
        if brief:
            break
    if not r["credits"]:
        L.append("  (no credit data)")
    if r["credits_note"]:
        L.append(f"  note: {r['credits_note']}")

    L.append("")
    L.append(f"SPEND (gross, credits excluded): {usd(s['total'])} since {r['since']}, "
             f"{usd(m['total'])} this month")
    if not brief:
        for t in s["top"]:
            L.append(f"  {t['service'][:44]:<44} {usd(t['cost']):>12} {usd(m['services'].get(t['service'], 0)):>12} (month)")
        if not s["top"]:
            L.append("  no spend recorded yet")

    L.append("")
    L.append(f"GPU HOURS: {gs['total_hours']:.1f} h ({usd(gs['total_cost'])}) since {r['since']}, "
             f"{gm['total_hours']:.1f} h ({usd(gm['total_cost'])}) this month")
    if not brief:
        for itype in sorted(set(gs["by_type"]) | set(gm["by_type"])):
            a = gs["by_type"].get(itype, {"hours": 0, "cost": 0})
            c = gm["by_type"].get(itype, {"hours": 0, "cost": 0})
            L.append(f"  {itype:<14} since: {a['hours']:8.1f} h {usd(a['cost']):>10}   month: {c['hours']:8.1f} h {usd(c['cost']):>10}")
        if not gs["by_type"]:
            L.append("  no GPU instance usage recorded yet")

    L.append("")
    if b["days_left"] is not None and b["runout_date"] is None:
        runway = f"runway > {MAX_RUNWAY_DAYS // 365} years at this rate"
    elif b["days_left"] is not None:
        runway = f"credits last ~{b['days_left']:.0f} days (until {b['runout_date']})"
    elif b["remaining"] is None:
        runway = "runway n/a (no credit data)"
    else:
        runway = "no spend in window, runway unbounded"
    L.append(f"BURN RATE: {usd(b['daily'])}/day avg over last {b['days']} days; {runway}")

    if not brief:
        L.append("")
        L.append("RIGHT NOW (Project=asr-bench)")
        for i in r["instances"]:
            up = f", up {fmt_hours(i['uptime_h'])}" if i["uptime_h"] is not None else ""
            L.append(f"  {i['id']}  {i['type']:<12} {i['state']}{up}")
        if not r["instances"]:
            L.append("  no instances")
        L.append(f"  EBS volumes: {r['volumes']['count']} ({r['volumes']['total_gb']} GB)")

    for w in r["warnings"]:
        L.append(f"WARNING: {w}")
    for e in r["errors"]:
        L.append(f"ERROR: {e}")

    if not brief:
        age = r["ce_age_hours"]
        src = "fetched now" if age is not None and age < 0.01 else f"cached, {age:.1f} h old" if age is not None else "unavailable"
        L.append("")
        L.append(f"Note: Cost Explorer data lags up to ~24 h (CE data {src}).")
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--refresh", action="store_true", help="bypass the 6 h Cost Explorer cache")
    p.add_argument("--since", default=CREDIT_START, metavar="YYYY-MM-DD")
    p.add_argument("--brief", action="store_true", help="short summary (used by infra/teardown.sh)")
    args = p.parse_args(argv)
    try:
        date.fromisoformat(args.since)
    except ValueError:
        p.error("--since must be YYYY-MM-DD")

    profile = os.environ.get("AWS_PROFILE", DEFAULT_PROFILE)
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    err = check_prerequisites(profile)
    if err:
        print(f"aws_usage: {err}", file=sys.stderr)
        return 2
    try:
        report = collect(args, profile, region, utcnow())
    except (AwsError, KeyError) as e:
        print(f"aws_usage: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else render(report, args.brief))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
