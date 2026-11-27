"""Task 4.3's fault run, recorded (the lead's go of 2026-11-27).

Usage: python3 -I fault_run.py <repository root> <output dir>

1. identity (must be 818928662830); 2. wait, read-only, until rm-runs has no ACTIVE pointer;
3. pre-reads that must pass before anything starts: the alarm (threshold, OK), rm-answer-cheaper
(the pinned layer and the FIS variables), no FIS experiment in flight, one template, one
rm-api endpoint (captured, never printed), and the cost so far; 4. seed rfault<HHMM>;
5. start load.py, then inject_fault.sh straight away; 6. poll the alarm until it has left ALARM
after the fault ended (or a cap); 7. post-reads: alarm history, per-minute p95 and counts,
the FIS experiment, the cost after.

It never stops an experiment, never ends a Flagsmith experiment, and never prints the ask URL
or the API endpoint. Progress lines start with RM-PROGRESS (stdout and progress.log).
"""

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

repo, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
names = json.loads((repo / "contracts/names.json").read_text())

PROFILE = os.environ.get("RM_RUN_PROFILE", "abisb_IsbAdminsPS-818928662830")
REGION = names["REGION"]
ACCOUNT = names["ACCOUNT_ID"]
ALARM = names["ALARM_NAME"]
THRESHOLD = float(names["ALARM_THRESHOLD_MS"])
PIN = names["FIS_EXTENSION_LAYER_ARN"]
CHEAPER = names["ANSWER_CHEAPER_FUNCTION"]
NAMESPACE = names["CW_NAMESPACE"]
LATENCY = names["CW_METRIC_ANSWER_LATENCY_MS"]
COST = names["CW_METRIC_ANSWER_COST_MICRO_USD"]
COST_STOP = names["ANSWER_COST_STOP_MICRO_USD"]
COST_SINCE = "2026-11-26T02:30:00Z"
USERS, QUESTIONS = names["LOAD_FAULT"]["users"], names["LOAD_FAULT"]["questions"]
ANSWERS = USERS * QUESTIONS
ROUTE_PATH = names["API_ROUTE_KEY"].split(" ")[1]

POLL = float(os.environ.get("RM_RUN_POLL_SECONDS", "15"))
POINTER_CAP = float(os.environ.get("RM_RUN_POINTER_CAP_SECONDS", "1200"))
AFTER_FAULT_CAP = float(os.environ.get("RM_RUN_AFTER_FAULT_CAP_SECONDS", "1500"))
NO_ALARM_GRACE = float(os.environ.get("RM_RUN_NO_ALARM_GRACE_SECONDS", "300"))
LOAD_SETTLE = float(os.environ.get("RM_RUN_LOAD_SETTLE_SECONDS", "5"))
INJECT = os.environ.get("RM_RUN_INJECT", str(repo / "scripts/inject_fault.sh"))
ENDPOINT_PATTERN = re.compile(r"https://[a-z0-9]{10}\.execute-api\.us-east-1\.amazonaws\.com")

ENV = {k: v for k, v in os.environ.items()
       if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")}
ENV["AWS_PROFILE"] = PROFILE
ENV["TZ"] = "UTC"  # the CLI prints AWS timestamps in UTC

RECORDS = out / "run.jsonl"
ALARM_POLLS = out / "alarm-polls.jsonl"
POINTER_POLLS = out / "pointer-polls.jsonl"
PROGRESS = out / "progress.log"
SUMMARY = out / "summary.json"
summary: dict = {"profile": PROFILE}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def minute(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:00Z")


def progress(message: str) -> None:
    line = f"RM-PROGRESS {iso(now())} {message}"
    print(line, flush=True)
    with PROGRESS.open("a") as f:
        f.write(line + "\n")


def append(path: Path, item: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(item) + "\n")


def save_summary() -> None:
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")


def aws(step: str, args: list[str], *, redact: bool = False, path: Path = RECORDS):
    argv = ["aws", *args, "--region", REGION]
    at = now()
    done = subprocess.run(argv, env=ENV, cwd=repo, capture_output=True, text=True)
    append(path, {
        "kind": "read", "at": iso(at), "step": step, "cmd": shlex.join(argv),
        "stdout": "<captured, not printed>\n" if redact else done.stdout,
        "stderr": done.stderr, "exit": done.returncode,
    })
    return done


def stop(reason: str, code: int = 1) -> None:
    summary["stopped"] = reason
    save_summary()
    progress(f"ABORT {reason}")
    sys.exit(code)


def json_out(done) -> object:
    return json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None


def cost(label: str) -> dict:
    """CLAUDE.md's check: each variant's Sum (and SampleCount) since the cutoff, period 3600."""
    end = minute(now() + timedelta(minutes=1))
    result = {}
    for variant in names["VARIANTS"]:
        done = aws(f"cost check {label}: {COST} Variant={variant}", [
            "cloudwatch", "get-metric-statistics", "--namespace", NAMESPACE,
            "--metric-name", COST, "--dimensions", f"Name=Variant,Value={variant}",
            "--statistics", "Sum", "SampleCount", "--period", "3600",
            "--start-time", COST_SINCE, "--end-time", end,
            "--query", "sort_by(Datapoints,&Timestamp)[].{T:Timestamp,Sum:Sum,N:SampleCount}",
            "--output", "json"])
        points = json_out(done)
        if not points:
            result[variant] = None
            continue
        result[variant] = {"sum": sum(p["Sum"] for p in points),
                           "n": sum(p["N"] for p in points)}
    return result


# --------------------------------------------------------------------------------------------
# 1. Identity
progress("START")
who = aws("identity", ["sts", "get-caller-identity", "--query", "Account", "--output", "text"])
if who.stdout.strip() != ACCOUNT:
    stop(f"identity is {who.stdout.strip() or 'unknown'} (exit {who.returncode}), not {ACCOUNT}; nothing started")
progress(f"IDENTITY {ACCOUNT}")

# 2. No ACTIVE pointer (B's reset.py), read-only
pointer_args = ["dynamodb", "get-item", "--table-name", names["RUNS_TABLE"],
                "--key", json.dumps({names["PARTITION_KEY"]: {"S": names["ACTIVE_POINTER_PK"]}}),
                "--consistent-read", "--query", "Item.{run_id:run_id.S,feature_key:feature_key.S}",
                "--output", "json"]
waited_from = time.monotonic()
announced = False
while True:
    done = aws("the ACTIVE pointer in rm-runs", pointer_args, path=POINTER_POLLS)
    if done.returncode == 0 and done.stdout.strip() == "null":
        break
    if not announced:
        progress(f"POINTER_WAIT exit={done.returncode} {done.stdout.strip()[:120]}")
        announced = True
    if time.monotonic() - waited_from > POINTER_CAP:
        stop("the ACTIVE pointer is still there; nothing started")
    time.sleep(POLL)
aws("the ACTIVE pointer in rm-runs (clear)", pointer_args)
progress("POINTER_CLEAR")

# 3. Pre-reads: each must pass before anything starts
settings = json_out(aws("alarm settings before the run", [
    "cloudwatch", "describe-alarms", "--alarm-names", ALARM, "--query",
    "MetricAlarms[0].{Namespace:Namespace,MetricName:MetricName,Dimensions:Dimensions,"
    "ExtendedStatistic:ExtendedStatistic,Period:Period,EvaluationPeriods:EvaluationPeriods,"
    "DatapointsToAlarm:DatapointsToAlarm,Threshold:Threshold,ComparisonOperator:ComparisonOperator,"
    "TreatMissingData:TreatMissingData,EvaluateLowSampleCountPercentile:EvaluateLowSampleCountPercentile,"
    "ActionsEnabled:ActionsEnabled,StateValue:StateValue,StateUpdatedTimestamp:StateUpdatedTimestamp}",
    "--output", "json"])) or {}
if settings.get("Threshold") != THRESHOLD or settings.get("StateValue") != "OK":
    stop(f"alarm threshold {settings.get('Threshold')} / state {settings.get('StateValue')}, "
         f"not {THRESHOLD} / OK; nothing started")

function = json_out(aws("rm-answer-cheaper's layer and FIS settings", [
    "lambda", "get-function-configuration", "--function-name", CHEAPER, "--query",
    "{Layers:Layers[].Arn,Wrapper:Environment.Variables.AWS_LAMBDA_EXEC_WRAPPER,"
    "ConfigLocation:Environment.Variables.AWS_FIS_CONFIGURATION_LOCATION,"
    "SlowPoll:Environment.Variables.AWS_FIS_SLOW_POLL_INTERVAL_SECONDS,Timeout:Timeout,"
    "LastModified:LastModified,State:State,LastUpdateStatus:LastUpdateStatus}",
    "--output", "json"])) or {}
if function.get("Layers") != [PIN] or not all(
        function.get(k) for k in ("Wrapper", "ConfigLocation", "SlowPoll")):
    stop("rm-answer-cheaper lacks the pinned layer or the FIS variables; nothing started")

in_flight = json_out(aws("FIS experiments not yet ended", [
    "fis", "list-experiments", "--query",
    "experiments[?state.status!='completed' && state.status!='stopped' && "
    "state.status!='failed' && state.status!='cancelled'].{id:id,status:state.status}",
    "--output", "json"]))
if in_flight != []:
    stop(f"FIS experiments in flight: {in_flight}; nothing started")

template = json_out(aws("the template", [
    "fis", "list-experiment-templates", "--query",
    f"experimentTemplates[?tags.Name=='{names['FIS_TEMPLATE']}'].{{id:id,description:description}}",
    "--output", "json"]))
if not template or len(template) != 1:
    stop(f"expected one template tagged {names['FIS_TEMPLATE']}; nothing started")
summary["template"] = template[0]

apis = aws(f"{names['API_NAME']}'s endpoint (captured, not printed)", [
    "apigatewayv2", "get-apis", "--query", f"Items[?Name=='{names['API_NAME']}'].ApiEndpoint",
    "--output", "text"], redact=True)
endpoints = apis.stdout.split()
if apis.returncode != 0 or len(endpoints) != 1 or not ENDPOINT_PATTERN.fullmatch(endpoints[0]):
    stop(f"expected one {names['API_NAME']} endpoint, found {len(endpoints)}; nothing started")
url = endpoints[0] + ROUTE_PATH
secret_strings = (endpoints[0], endpoints[0].split("//")[1].split(".")[0])

before = cost("before the load")
summary["cost_before"] = before
if any(v is None for v in before.values()):
    stop("the cost query came back empty (unknown, not zero); nothing started")
spent = sum(v["sum"] for v in before.values())
per_answer = max(v["sum"] / v["n"] for v in before.values() if v["n"])
summary["cost_projection"] = {"spent": spent, "per_answer_max_mean": per_answer,
                              "projected": spent + ANSWERS * per_answer}
if spent + ANSWERS * per_answer > COST_STOP:
    stop(f"cost projection {spent + ANSWERS * per_answer:.0f} passes {COST_STOP}; nothing started")
save_summary()
progress(f"PRECHECKS_OK threshold={settings['Threshold']} state=OK cost_spent={spent:.0f}")

# 4. Seed
run_id = "rfault" + now().strftime(names["SCRATCH_RUN_ID_TIME_FORMAT"])
summary["run_id"] = run_id
seed_argv = ["uv", "run", "python", "scripts/seed_experiment.py", run_id]
seed_at = now()
seed = subprocess.run(seed_argv, env=ENV, cwd=repo, capture_output=True, text=True)
append(RECORDS, {"kind": "write", "at": iso(seed_at), "step": f"seed {run_id}",
                 "cmd": shlex.join(seed_argv), "stdout": seed.stdout, "stderr": seed.stderr,
                 "exit": seed.returncode})
started = re.search(r"started experiment (\d+) on (\S+)", seed.stdout)
if started:
    summary["flagsmith_experiment"] = {"id": started[1], "feature": started[2], "seeded_at": iso(seed_at)}
    save_summary()
    progress(f"SEEDED flagsmith_experiment={started[1]} feature={started[2]} run_id={run_id}")
if seed.returncode != 0 or not started or "wrote the pointer" not in seed.stdout:
    stop(f"seed exit {seed.returncode}; no load or fault started"
         + (f"; experiment {started[1]} is running with no traffic" if started else ""))

# 5. Load, then the fault straight away
load_display = shlex.join(["uv", "run", "python", "scripts/load.py", "--run-id", run_id,
                           "--users", str(USERS), "--questions", str(QUESTIONS), "--url"]) \
    + " <rm-api's endpoint + /ask, from get-apis; never printed>"
load_stdout, load_stderr = (out / "load.stdout").open("w"), (out / "load.stderr").open("w")
load_at = now()
load = subprocess.Popen(["uv", "run", "python", "scripts/load.py", "--run-id", run_id,
                         "--users", str(USERS), "--questions", str(QUESTIONS), "--url", url],
                        env=ENV, cwd=repo, stdout=load_stdout, stderr=load_stderr, text=True)
summary["load"] = {"started_at": iso(load_at), "cmd": load_display}
save_summary()
progress(f"LOAD_STARTED run_id={run_id}")
time.sleep(LOAD_SETTLE)
if load.poll() is not None:
    summary["load"]["exit"] = load.returncode
    stop(f"load.py exited after {LOAD_SETTLE:.0f}s with {load.returncode}; no fault started")

fault_lines: list[tuple[str, str, str]] = []
fault_at = now()
fault = subprocess.Popen([INJECT], env=ENV, cwd=repo, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, bufsize=1)
summary["fault"] = {"started_at": iso(fault_at), "cmd": INJECT}


def follow(stream, label: str) -> None:
    for line in stream:
        line = line.rstrip("\n")
        fault_lines.append((iso(now()), label, line))
        match = re.search(r"experiment (\S+) started at (\S+)", line)
        if match:
            summary["fault"]["experiment_id"] = match[1]
            progress(f"FAULT_STARTED fis_experiment={match[1]} at={match[2]}")
        if " ended at " in line or "still running" in line:
            progress(f"FAULT_LINE {line}")


readers = [threading.Thread(target=follow, args=(fault.stdout, "stdout"), daemon=True),
           threading.Thread(target=follow, args=(fault.stderr, "stderr"), daemon=True)]
for reader in readers:
    reader.start()

# 6. Alarm polls
alarm_seen = False
state_before = None
fault_end = None
load_end = None
while True:
    at = now()
    done = subprocess.run(["aws", "cloudwatch", "describe-alarms", "--alarm-names", ALARM,
                           "--query", "MetricAlarms[0].[StateValue,StateUpdatedTimestamp]",
                           "--output", "text", "--region", REGION],
                          env=ENV, cwd=repo, capture_output=True, text=True)
    fields = done.stdout.split()
    state = fields[0] if done.returncode == 0 and fields else None
    append(ALARM_POLLS, {"at": iso(at), "exit": done.returncode, "state": state,
                         "updated": fields[1] if len(fields) > 1 else None,
                         "stderr": done.stderr.strip()[:200]})
    if state and state != state_before:
        progress(f"ALARM_STATE {state} updated={fields[1] if len(fields) > 1 else '?'}")
        state_before = state
    if state == "ALARM":
        alarm_seen = True
    if load_end is None and load.poll() is not None:
        load_end = now()
        load_stdout.flush()
        last = (out / "load.stdout").read_text().strip().splitlines()
        summary["load"].update({"ended_at": iso(load_end), "exit": load.returncode,
                                "last_line": last[-1] if last else ""})
        save_summary()
        progress(f"LOAD_DONE exit={load.returncode} {last[-1] if last else ''}")
    if fault_end is None and fault.poll() is not None:
        for reader in readers:
            reader.join(timeout=5)
        fault_end = now()
        summary["fault"].update({"exited_at": iso(fault_end), "exit": fault.returncode})
        save_summary()
        progress(f"FAULT_DONE exit={fault.returncode}")
    if fault_end is not None:
        after = (now() - fault_end).total_seconds()
        if load_end is not None and alarm_seen and state is not None and state != "ALARM":
            break
        if load_end is not None and not alarm_seen and after > NO_ALARM_GRACE:
            break
        if after > AFTER_FAULT_CAP:
            progress("CAP reached after the fault; stopping the polls")
            break
    time.sleep(POLL)

summary["alarm"] = {"seen_alarm": alarm_seen, "last_state": state, "polls_ended_at": iso(now())}
save_summary()
(out / "fault.lines.json").write_text(json.dumps(fault_lines, indent=1) + "\n")
progress("POLLS_DONE")

# 7. Post-reads
window_start = minute(load_at - timedelta(minutes=2))
window_end = minute(now() + timedelta(minutes=1))
summary["window"] = [window_start, window_end]
aws("alarm history in the window", [
    "cloudwatch", "describe-alarm-history", "--alarm-name", ALARM,
    "--history-item-type", "StateUpdate", "--start-date", iso(seed_at - timedelta(minutes=1)),
    "--end-date", window_end, "--query",
    "sort_by(AlarmHistoryItems,&Timestamp)[].{Timestamp:Timestamp,Summary:HistorySummary,Data:HistoryData}",
    "--output", "json"])
for variant in names["VARIANTS"]:
    base = ["cloudwatch", "get-metric-statistics", "--namespace", NAMESPACE, "--metric-name",
            LATENCY, "--dimensions", f"Name=Variant,Value={variant}", "--period", "60",
            "--start-time", window_start, "--end-time", window_end]
    aws(f"per-minute p95 of {LATENCY}, Variant={variant}", [
        *base, "--extended-statistics", "p95", "--query",
        "sort_by(Datapoints,&Timestamp)[].{T:Timestamp,p95:ExtendedStatistics.p95}",
        "--output", "json"])
    aws(f"per-minute count of {LATENCY}, Variant={variant}", [
        *base, "--statistics", "SampleCount", "--query",
        "sort_by(Datapoints,&Timestamp)[].{T:Timestamp,N:SampleCount}", "--output", "json"])
fis_id = summary["fault"].get("experiment_id")
if fis_id:
    aws("the FIS experiment", [
        "fis", "get-experiment", "--id", fis_id, "--query",
        'experiment.{status:state.status,reason:state.reason,startTime:startTime,endTime:endTime,'
        'action:actions."add-delay".{status:state.status,startTime:startTime,endTime:endTime}}',
        "--output", "json"])
summary["cost_after"] = cost("after the load")
save_summary()

# The ask URL and the endpoint must appear in no output file.
for path in out.iterdir():
    if path.is_file():
        text = path.read_text(errors="replace")
        if any(s in text for s in secret_strings):
            path.write_text(text.replace(secret_strings[0], "<endpoint>")
                            .replace(secret_strings[1], "<api-id>"))
            progress(f"REDACTED the endpoint in {path.name}")
progress(f"DONE run_id={run_id} flagsmith_experiment={summary.get('flagsmith_experiment', {}).get('id')}"
         f" fis_experiment={fis_id} load={summary['load'].get('last_line')}")
