"""
server.py — HTTP server receiving webhooks from Alertmanager.

Alertmanager sends POST request to /webhook when an alert is triggered.
We parse the payload and asynchronously run graph.run_agent().

Testing with a sample alert:
    curl -X POST http://localhost:8888/webhook \
      -H "Content-Type: application/json" \
      -d '{"alerts": [{"labels": {"alertname": "PodCrashLooping",
           "pod": "crash-test", "namespace": "default",
           "severity": "critical"},
           "annotations": {"description": "Pod crushes 5 times"}}]}'
"""

import logging
import asyncio
from fastapi import FastAPI, BackgroundTasks, Request
from pydantic import BaseModel

from agent.graph import run_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("krag.server")

app = FastAPI(title="kRAG Webhook Server", version="0.1.0")


class AlertLabels(BaseModel):
    alertname: str = "Unknown"
    pod: str = "unknown"
    namespace: str = "default"
    severity: str = "unknown"

    class Config:
        extra = "allow"


class AlertAnnotations(BaseModel):
    description: str = ""
    summary: str = ""

    class Config:
        extra = "allow"


class Alert(BaseModel):
    labels: AlertLabels
    annotations: AlertAnnotations
    status: str = "firing"

    class Config:
        extra = "allow"


class AlertmanagerPayload(BaseModel):
    alerts: list[Alert]
    version: str = "4"
    groupKey: str = ""

    class Config:
        extra = "allow"


@app.get("/health")
async def health():
    """Health check — K8s uses this to check if the server is alive."""
    return {"status": "ok", "service": "krag-webhook"}


@app.post("/webhook")
async def receive_alert(payload: AlertmanagerPayload, background_tasks: BackgroundTasks):
    """
    Main endpoint — receives alerts from Alertmanager.

    Alertmanager may send multiple alerts at once (batch).
    We handle each alert separately in the background (background task),
    so the server doesn't block while fixing the issue (which can take minutes).
    """
    firing_alerts = [a for a in payload.alerts if a.status == "firing"]
    logger.info(f"Received {len(payload.alerts)} alerts, {len(firing_alerts)} firing")

    for alert in firing_alerts:
        alert_dict = {
            "alertname": alert.labels.alertname,
            "pod": alert.labels.pod,
            "namespace": alert.labels.namespace,
            "severity": alert.labels.severity,
            "description": alert.annotations.description or alert.annotations.summary,
        }
        logger.info(f"Queuing repair for: {alert_dict['alertname']} / {alert_dict['pod']}")

        # Run agent in background — server immediately responds with 200 OK
        background_tasks.add_task(handle_alert_async, alert_dict)

    return {
        "status": "accepted",
        "alerts_queued": len(firing_alerts),
    }


@app.post("/test")
async def test_alert(request: Request):
    """
    Endpoint for manual testing without Alertmanager.
    You can send any JSON with alert data.

    curl -X POST http://localhost:8888/test \
      -H "Content-Type: application/json" \
      -d '{"pod": "crash-test", "namespace": "default",
           "alertname": "PodCrashLooping", "description": "test"}'
    """
    body = await request.json()
    alert_dict = {
        "alertname": body.get("alertname", "ManualTest"),
        "pod": body.get("pod", "unknown"),
        "namespace": body.get("namespace", "default"),
        "severity": body.get("severity", "critical"),
        "description": body.get("description", "manual test"),
    }
    logger.info(f"Manual test: {alert_dict}")

    # For /test we run synchronously to see the result immediately
    result = await asyncio.get_event_loop().run_in_executor(None, run_agent, alert_dict)

    return {
        "status": "completed",
        "success": result["success"],
        "action": result.get("action_plan", {}).get("action"),
        "root_cause": result.get("action_plan", {}).get("root_cause"),
    }


async def handle_alert_async(alert_dict: dict):
    """Runs the agent asynchronously (in a background task)."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_agent, alert_dict)
        logger.info(
            f"Alert handled: {alert_dict['alertname']} | "
            f"success={result['success']} | "
            f"action={result.get('action_plan', {}).get('action')}"
        )
    except Exception as e:
        logger.error(f"Error occurred while handling alert {alert_dict}: {e}", exc_info=True)
