"""Lambda invokes that OpenTelemetry's botocore instrumentation must leave alone (task 5.1).

The runtime starts under ``opentelemetry-instrument`` (the AgentCore CLI's entry point), and
its CodeZip ships opentelemetry-instrumentation-botocore 0.65b0 through
aws-opentelemetry-distro. For a Lambda ``Invoke`` whose payload parses as JSON, that
instrumentation adds a ``"headers"`` key to the payload to carry the trace context
(``extensions/lmbd.py``, ``_inject_current_span``). answer_request refuses any field but
``question`` and ``identity``. So in run r202611271927 every probe invoke failed: "not an
answer_request: fields missing [], and 1 unexpected field(s)".

Inside ``suppress_instrumentation()``, ``is_instrumentation_enabled()`` is false. The upstream
``_patched_api_call``, and the AWS distro's patched copy of it, then hand the call to botocore
untouched, so no extension runs. Where opentelemetry is not installed, nothing instruments the
call, and there is nothing to suppress.
"""

from __future__ import annotations

import contextlib

try:
    from opentelemetry.instrumentation.utils import suppress_instrumentation
except ImportError:  # no opentelemetry here, so nothing instruments the call
    suppress_instrumentation = contextlib.nullcontext

__all__ = ["suppress_instrumentation"]
