"""probe: is the cheaper model healthy again? (tasks.md 2.4; design.md section 6).

PROBE_INVOKES direct invokes of rm-answer-cheaper, run in parallel, each the
answer_request with PROBE_QUESTION as PROBE_IDENTITY (a fixed identity that reaches no
Flagsmith call). The probe passes when every invoke answers with a valid answer_response
under the alarm's threshold. An invoke at or above it, an error, a function error, a timeout,
or a reply that fails answer_response fails it. Imports only contracts, the standard
library, boto3 and botocore.
"""

from __future__ import annotations

import functools
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

import boto3
from botocore.config import Config

from contracts import names

_REQUEST = {"question": names.PROBE_QUESTION, "identity": names.PROBE_IDENTITY}
# answer_response.json's model_id enum: the two models an answer function serves.
_MODEL_IDS = (names.MODEL_CURRENT, names.MODEL_CHEAPER)


@functools.cache
def _client():
    """The Lambda client, made on first use, in us-east-1. One attempt per invoke, since a
    retry would hide a failure and stretch the timing; it reads for as long as the answer
    function may run."""
    return boto3.client(
        "lambda",
        region_name=names.REGION,
        config=Config(read_timeout=names.ANSWER_TIMEOUT_SECONDS, retries={"total_max_attempts": 1}),
    )


def _whole(value: Any) -> bool:
    """A non-negative JSON integer, as JSON Schema counts them (49.0 is one; True is not)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return value.is_integer() and value >= 0
    return isinstance(value, int) and value >= 0


def is_answer_response(value: Any) -> bool:
    """contracts/schemas/answer_response.json, checked by hand: the runtime ships no
    jsonschema. A test checks the two agree."""
    if not isinstance(value, dict) or set(value) != set(names.ANSWER_RESPONSE_FIELDS):
        return False
    answer, input_tokens, output_tokens, model_id = (
        value[field] for field in names.ANSWER_RESPONSE_FIELDS
    )
    return (
        isinstance(answer, str)
        and len(answer) >= 1
        and _whole(input_tokens)
        and _whole(output_tokens)
        and model_id in _MODEL_IDS
    )


def _invoke(client) -> tuple[float, str | None]:
    """One invoke: its milliseconds, and what was wrong with it, if anything."""
    began = time.monotonic()
    try:
        response = client.invoke(
            FunctionName=names.ANSWER_CHEAPER_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(_REQUEST),
        )
        raw = response["Payload"].read()
    except Exception as error:  # noqa: BLE001 - any failure of the invoke fails the probe
        return (time.monotonic() - began) * 1000, f"an invoke raised {type(error).__name__}"
    elapsed_ms = (time.monotonic() - began) * 1000
    if response.get("FunctionError"):
        return elapsed_ms, f"an invoke returned a function error ({response['FunctionError']})"
    try:
        reply = json.loads(raw)
    except ValueError:
        reply = None
    if not is_answer_response(reply):
        return elapsed_ms, "an invoke's reply fails answer_response"
    return elapsed_ms, None


def probe_cheaper(threshold_ms: float, n: int = names.PROBE_INVOKES) -> dict[str, Any]:
    """{"passed", "max_ms", "reason"}. It waits no longer than threshold_ms: an invoke still
    running then has timed out, and max_ms is None because the slowest is not known. The client
    is made here, once, and shared: boto3's guide makes a client before handing it to threads,
    since making clients from the default session in several threads at once is not safe."""
    client = _client()
    pool = ThreadPoolExecutor(max_workers=n)
    try:
        futures = [pool.submit(_invoke, client) for _ in range(n)]
        done, running = wait(futures, timeout=threshold_ms / 1000)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if running:
        return {
            "passed": False,
            "max_ms": None,
            "reason": f"{len(running)} of {n} invokes timed out at the {threshold_ms:g} ms "
            "threshold",
        }
    outcomes = [future.result() for future in done]
    max_ms = max(elapsed for elapsed, _ in outcomes)
    problems = [problem for _, problem in outcomes if problem]
    if problems:
        return {"passed": False, "max_ms": max_ms, "reason": problems[0]}
    if max_ms >= threshold_ms:
        return {
            "passed": False,
            "max_ms": max_ms,
            "reason": f"the slowest invoke took {max_ms:.0f} ms, at or above the "
            f"{threshold_ms:g} ms threshold",
        }
    return {
        "passed": True,
        "max_ms": max_ms,
        "reason": f"{n} invokes answered; the slowest took {max_ms:.0f} ms, under the "
        f"{threshold_ms:g} ms threshold",
    }
