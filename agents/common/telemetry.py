"""
OBS-02: Azure Monitor telemetry — optional, additive observability layer.

Deployed (DEPLOYMENT_MODE=azure) only: emits structured events as Python logging
records, which Azure Monitor's OpenTelemetry distro exports to Application Insights
as `traces` rows with `customDimensions` holding every field passed via emit_event().
KQL against that table drives the Azure Monitor Workbook in infra/observability/
(run history, retry lineage, token usage by CVE / over time).

Locally (DEPLOYMENT_MODE=local, or APPLICATIONINSIGHTS_CONNECTION_STRING unset),
this whole module is a no-op — the Streamlit dashboard remains the local option
(see TESTING_DOCKER.md / TESTING_PODMAN.md). See PLAN.md section 4.4a for the
overall DEPLOYMENT_MODE pattern this follows.

Hard safety requirement: nothing in this module may ever raise into a caller.
An Azure Monitor outage, a bad connection string, a missing package, or a
transient exporter error must degrade to "telemetry disabled, logged once" —
never to a failed fix/watch run. Every public function here is wrapped
accordingly; this is the one module in the codebase where broad `except
Exception` is intentional throughout, not an oversight.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state = {"initialized": False, "enabled": False}


def init_telemetry(role_name: str) -> bool:
    """
    Call once near the top of main(). Returns True if telemetry is active.

    Idempotent and safe to call more than once (e.g. from tests) — only the
    first call does any work. Reads APPLICATIONINSIGHTS_CONNECTION_STRING;
    if unset, telemetry is disabled by design (this is the expected state for
    local Docker/Podman testing, not an error condition).
    """
    with _lock:
        if _state["initialized"]:
            return _state["enabled"]
        _state["initialized"] = True

        connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
        if not connection_string:
            logger.info(
                "APPLICATIONINSIGHTS_CONNECTION_STRING not set — Azure Monitor "
                "telemetry disabled. Expected when DEPLOYMENT_MODE=local; see "
                "DEPLOYMENT_AAF.md section 3.6 to enable it for an AAF deployment."
            )
            return False

        try:
            from azure.monitor.opentelemetry import configure_azure_monitor

            configure_azure_monitor(
                connection_string=connection_string,
                # "" = root logger, so every module's plain logging.info/.warning/
                # .error calls are captured too, not just emit_event()'s.
                logger_name="",
            )
            os.environ.setdefault("OTEL_SERVICE_NAME", role_name)
            _state["enabled"] = True
            logger.info("Azure Monitor telemetry initialized (role=%s).", role_name)
        except Exception as exc:
            # Deliberately broad: a telemetry backend outage must never stop the
            # remediation pipeline from running. Log loudly (this is the one place
            # ops should see it) and continue with telemetry disabled.
            logger.error(
                "Azure Monitor telemetry failed to initialize — continuing WITHOUT "
                "it; the fix/watch pipeline itself is unaffected. Cause: %s", exc
            )
            _state["enabled"] = False

        return _state["enabled"]


def is_enabled() -> bool:
    return _state["enabled"]


def emit_event(event_name: str, **fields) -> None:
    """
    Emit one structured telemetry event, e.g.:
        emit_event("FixAttemptCompleted", tracking_id=..., prompt_tokens=..., ...)

    No-ops silently if telemetry was never initialized or failed to initialize.
    Never raises — a failure to emit one event is logged and swallowed, exactly
    like init_telemetry()'s failure mode.
    """
    if not _state["enabled"]:
        return
    try:
        logger.info(event_name, extra={"custom_dimensions": _sanitize(fields)})
    except Exception as exc:
        logger.warning(
            "Telemetry emit failed for event=%s (non-fatal, fix/watch flow "
            "continues): %s", event_name, exc,
        )


def shutdown_telemetry() -> None:
    """
    Flush buffered telemetry before process exit.

    The Fixer and Watcher are one-shot scripts (PLAN.md section 2 — Hosted
    Agents, not long-running servers), not always-on web apps. OpenTelemetry's
    exporters batch asynchronously, so without an explicit flush here, events
    emitted just before the process exits can be silently dropped. Call this
    from a `finally:` around main() so it still runs on sys.exit() paths.
    """
    if not _state["enabled"]:
        return
    try:
        from opentelemetry._logs import get_logger_provider
        get_logger_provider().shutdown()
    except Exception as exc:
        logger.warning("Telemetry shutdown/flush failed (non-fatal): %s", exc)


def _sanitize(fields: dict) -> dict:
    """customDimensions values must be JSON-primitive; drop Nones, flatten lists."""
    out = {}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            out[key] = ",".join(str(v) for v in value)
        else:
            out[key] = value
    return out
