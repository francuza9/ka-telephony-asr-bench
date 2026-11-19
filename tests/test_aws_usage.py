import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import aws_usage as au  # noqa: E402

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def day(d, groups):
    return {"TimePeriod": {"Start": d, "End": d}, "Total": {}, "Groups": [
        {"Keys": [svc, ut], "Metrics": {"UnblendedCost": {"Amount": str(c), "Unit": "USD"},
                                        "UsageQuantity": {"Amount": str(q), "Unit": "Hrs"}}}
        for svc, ut, c, q in groups]}


EC2 = "Amazon Elastic Compute Cloud - Compute"
CE_DAYS = [
    day("2026-10-25", [(EC2, "BoxUsage:g6.xlarge", 10.0, 12.5), ("Amazon S3", "TimedStorage-ByteHrs", 1.0, 5)]),
    day("2026-11-02", [(EC2, "BoxUsage:g6e.xlarge", 30.0, 10.0), (EC2, "BoxUsage:g6.xlarge", 5.0, 6.25),
                       (EC2, "BoxUsage:t3.micro", 0.5, 24), (EC2, "EBS:VolumeUsage.gp3", 2.0, 100)]),
    # last 7 full days: 2026-11-12 .. 2026-11-18
    day("2026-11-15", [(EC2, "USE1-BoxUsage:g6.xlarge", 14.0, 17.5)]),
    day("2026-11-19", [(EC2, "BoxUsage:g6.xlarge", 99.0, 99.0)]),  # today: excluded from burn
]
CREDITS = {"credits": [{
    "creditId": "123", "accountId": "100678004861", "creditType": "Promotion",
    "description": "AWS Activate", "creditStatus": "ENABLED",
    "initialAmount": {"currencyCode": "USD", "currencyAmount": "10000.00"},
    "remainingAmount": {"currencyCode": "USD", "currencyAmount": "9000.00"},
    "estimatedAmount": {"currencyCode": "USD", "currencyAmount": "8900.00"},
    "startDate": 1792627200, "endDate": "2027-10-22T00:00:00+00:00",
}]}
INSTANCES = {"Reservations": [{"Instances": [
    {"InstanceId": "i-run", "InstanceType": "g6.xlarge", "State": {"Name": "running"},
     "LaunchTime": "2026-11-18T22:00:00+00:00"},
    {"InstanceId": "i-stop", "InstanceType": "g6e.xlarge", "State": {"Name": "stopped"},
     "LaunchTime": "2026-11-01T00:00:00+00:00"},
]}]}
VOLUMES = {"Volumes": [{"Size": 200}, {"Size": 100}]}


def fake_aws(responses, calls):
    """Mock subprocess.run: route on the aws service/subcommand."""
    def run(cmd, **kw):
        if cmd[:3] == ["aws", "configure", "list-profiles"]:
            return subprocess.CompletedProcess(cmd, 0, "default\nsatli\n", "")
        svc_cmd = f"{cmd[1]} {cmd[2]}"
        calls.append(cmd)
        if "credit" in svc_cmd or svc_cmd == "ce get-cost-and-usage":
            key = svc_cmd
            if svc_cmd == "ce get-cost-and-usage":
                key = "ce-credit" if "MONTHLY" in cmd else "ce-usage"
            resp = responses[key]
            if isinstance(resp, Exception):
                return subprocess.CompletedProcess(cmd, 254, "", "An error occurred (AccessDenied)")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(resp), "")
        resp = responses[svc_cmd]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(resp), "")
    return run


@pytest.fixture
def env(tmp_path, monkeypatch):
    calls = []
    responses = {
        "sts get-caller-identity": {"Account": "100678004861"},
        "billing get-credits": CREDITS,
        "ce-usage": {"ResultsByTime": CE_DAYS},
        "ce-credit": {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "-1500.00"}}}]},
        "ec2 describe-instances": INSTANCES,
        "ec2 describe-volumes": VOLUMES,
    }
    monkeypatch.setattr(au, "CACHE_PATH", tmp_path / ".cache" / "aws_usage.json")
    monkeypatch.setattr(au.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(au, "utcnow", lambda: NOW)
    monkeypatch.setattr(au.subprocess, "run", lambda *a, **k: fake_aws(responses, calls)(*a, **k))
    monkeypatch.setenv("AWS_PROFILE", "satli")
    return SimpleNamespace(calls=calls, responses=responses)


def args(**kw):
    return SimpleNamespace(**{"since": "2026-10-22", "refresh": False, **kw})


def n_ce(calls):
    return sum(1 for c in calls if c[1] == "ce")


# ---------- parsing ----------

def test_parse_credits():
    [c] = au.parse_credits(CREDITS, NOW)
    assert c["name"] == "AWS Activate" and c["initial"] == 10000 and c["remaining"] == 9000
    assert c["estimated"] == 8900 and c["effective"] == 8900
    assert c["pct_used"] == pytest.approx(11.0)
    assert c["expires"] == "2027-10-22" and c["days_left"] == 336 and c["active"]


def test_parse_credits_epoch_expiry_and_expired():
    resp = {"credits": [{**CREDITS["credits"][0], "endDate": 1700000000}]}
    [c] = au.parse_credits(resp, NOW)
    assert not c["active"]


def test_parse_ts_variants():
    assert au.parse_ts(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert au.parse_ts("2026-11-19T00:00:00Z").day == 19
    assert au.parse_ts(None) is None


def test_parse_ce_days():
    rows = au.parse_ce_days(CE_DAYS[:1])
    assert rows[0] == (date(2026, 10, 25), EC2, "BoxUsage:g6.xlarge", 10.0, 12.5)


# ---------- aggregation ----------

def test_gpu_hours_aggregation():
    rows = au.parse_ce_days(CE_DAYS)
    since = au.aggregate_gpu(rows, date(2026, 10, 22))
    assert since["by_type"]["g6.xlarge"]["hours"] == pytest.approx(12.5 + 6.25 + 17.5 + 99)
    assert since["by_type"]["g6e.xlarge"] == {"hours": 10.0, "cost": 30.0}
    assert "t3.micro" not in since["by_type"]  # non-GPU family
    assert set(since["by_type"]) == {"g6.xlarge", "g6e.xlarge"}  # no EBS/S3 rows
    month = au.aggregate_gpu(rows, date(2026, 11, 1))
    assert month["by_type"]["g6.xlarge"]["hours"] == pytest.approx(6.25 + 17.5 + 99)
    assert month["total_hours"] == pytest.approx(6.25 + 17.5 + 99 + 10)
    assert month["total_cost"] == pytest.approx(5 + 14 + 99 + 30)


def test_gpu_family_matching():
    rows = au.parse_ce_days([day("2026-11-02", [
        (EC2, "BoxUsage:p4d.24xlarge", 1, 1), (EC2, "BoxUsage:g4dn.xlarge", 1, 1),
        (EC2, "BoxUsage:g5.xlarge", 1, 1), (EC2, "BoxUsage:m5.xlarge", 1, 1),
        (EC2, "SpotUsage:g6.xlarge", 1, 1)])])
    assert set(au.aggregate_gpu(rows, date(2026, 11, 1))["by_type"]) == {
        "p4d.24xlarge", "g4dn.xlarge", "g5.xlarge"}


def test_spend_aggregation():
    rows = au.parse_ce_days(CE_DAYS)
    since = au.aggregate_spend(rows, date(2026, 10, 22))
    assert since["total"] == pytest.approx(10 + 1 + 30 + 5 + 0.5 + 2 + 14 + 99)
    assert since["top"][0]["service"] == EC2
    month = au.aggregate_spend(rows, date(2026, 11, 1))
    assert month["services"].get("Amazon S3") is None


# ---------- burn rate ----------

def test_burn_rate_math():
    rows = au.parse_ce_days(CE_DAYS)
    b = au.burn_rate(rows, TODAY, 8900.0)
    # window 11-12..11-18 holds only the 14.0 row; today's 99 is excluded
    assert b["daily"] == pytest.approx(2.0)
    assert b["days_left"] == pytest.approx(4450.0)
    assert b["runout_date"] is None  # > 10 years: no date
    b = au.burn_rate(rows, TODAY, 800.0)
    assert b["days_left"] == pytest.approx(400.0) and b["runout_date"] == "2027-12-24"


def test_burn_rate_zero_spend():
    b = au.burn_rate([], TODAY, 1000.0)
    assert b["daily"] == 0 and b["days_left"] is None and b["runout_date"] is None


# ---------- instances / warnings ----------

def test_instances_and_volumes():
    inst = au.summarize_instances(INSTANCES, NOW)
    assert inst[0]["id"] == "i-run" and inst[0]["uptime_h"] == pytest.approx(14.0)
    assert inst[1]["state"] == "stopped" and inst[1]["uptime_h"] is None
    assert au.summarize_volumes(VOLUMES) == {"count": 2, "total_gb": 300}


def test_warnings():
    credits = au.parse_credits(CREDITS, NOW)
    inst = au.summarize_instances(INSTANCES, NOW)
    assert any("i-run" in w for w in au.build_warnings(credits, inst))
    low = [{**credits[0], "effective": 1500.0, "days_left": 30}]
    w = au.build_warnings(low, [])
    assert any("credits low" in x for x in w) and any("expires" in x for x in w)
    assert au.build_warnings(credits, []) == []


# ---------- end to end (mocked subprocess) ----------

def test_collect_happy_path_and_cache(env):
    r = au.collect(args(), "satli", "us-east-1", NOW)
    assert r["credits"][0]["source"] == "get-credits"
    assert r["credits_total_remaining"] == 8900
    assert r["spend"]["since"]["total"] == pytest.approx(161.5)
    assert r["burn"]["daily"] == pytest.approx(2.0)
    assert n_ce(env.calls) == 1
    au.collect(args(), "satli", "us-east-1", NOW)  # cached
    assert n_ce(env.calls) == 1
    au.collect(args(refresh=True), "satli", "us-east-1", NOW)
    assert n_ce(env.calls) == 2


def test_cache_expires_after_6h(env):
    au.collect(args(), "satli", "us-east-1", NOW)
    later = NOW.replace(hour=23)
    au.collect(args(), "satli", "us-east-1", later)
    assert n_ce(env.calls) == 2


def test_fallback_when_get_credits_fails(env):
    env.responses["billing get-credits"] = RuntimeError("denied")
    r = au.collect(args(), "satli", "us-east-1", NOW)
    c = r["credits"][0]
    assert c["source"] == "cost-explorer-fallback"
    assert c["effective"] == 8500.0 and c["pct_used"] == pytest.approx(15.0)
    assert "estimated" in r["credits_note"]
    assert n_ce(env.calls) == 2
    # credit sum is cached too
    au.collect(args(), "satli", "us-east-1", NOW)
    assert n_ce(env.calls) == 2


def test_main_json_and_text(env, capsys):
    assert au.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["volumes"] == {"count": 2, "total_gb": 300}
    assert au.main([]) == 0
    out = capsys.readouterr().out
    assert "GPU HOURS" in out and "g6e.xlarge" in out and "lags up to ~24 h" in out
    assert au.main(["--brief"]) == 0
    assert "RIGHT NOW" not in capsys.readouterr().out


def test_missing_cli(monkeypatch, capsys):
    monkeypatch.setattr(au.shutil, "which", lambda _: None)
    assert au.main([]) == 2
    assert "aws CLI not found" in capsys.readouterr().err


def test_missing_profile(env, monkeypatch, capsys):
    monkeypatch.setenv("AWS_PROFILE", "nope")
    assert au.main([]) == 2
    assert "profile 'nope' not found" in capsys.readouterr().err


def test_only_readonly_calls(env):
    au.collect(args(), "satli", "us-east-1", NOW)
    env.responses["billing get-credits"] = RuntimeError("x")
    au.collect(args(refresh=True), "satli", "us-east-1", NOW)
    for c in env.calls:
        assert c[2].split("-")[0] in {"get", "list", "describe"}, c
