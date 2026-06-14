import logging
import asyncio
from fastapi import FastAPI, BackgroundTasks, Request
import httpx
from pydantic import BaseModel
import json
from agent.graph import run_agent
from google.protobuf.internal.containers import MessageMap
import uuid


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
    return {"status": "ok", "service": "krag-webhook"}


@app.post("/webhook")
async def receive_alert(payload: AlertmanagerPayload, background_tasks: BackgroundTasks):
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

        background_tasks.add_task(handle_alert_async, alert_dict)

    return {
        "status": "accepted",
        "alerts_queued": len(firing_alerts),
    }


@app.post("/test")
async def test_alert(request: Request):
    body = await request.json()
    alert_dict = {
        "alertname": body.get("alertname", "ManualTest"),
        "pod": body.get("pod", "unknown"),
        "namespace": body.get("namespace", "default"),
        "severity": body.get("severity", "critical"),
        "description": body.get("description", "manual test"),
    }
    logger.info(f"Manual test: {alert_dict}")

    result = await run_agent(alert_dict)

    return {
        "status": "completed",
        "success": result["success"],
        "action": result.get("action_plan", {}).get("action"),
        "root_cause": result.get("action_plan", {}).get("root_cause"),
    }


async def handle_alert_async(alert_dict: dict):
    try:
        loop = asyncio.get_event_loop()
        result = await run_agent(alert_dict)
        logger.info(
            f"Alert handled: {alert_dict['alertname']} | "
            f"success={result['success']} | "
            f"action={result.get('action_plan', {}).get('action')}"
        )
    except Exception as e:
        logger.error(f"Error occurred while handling alert {alert_dict}: {e}", exc_info=True)
            

def extract_text_aggressively(chunk):
    try:
        if hasattr(chunk, 'history'):
            for h in chunk.history:
                for part in h.parts:
                    if hasattr(part, 'text') and part.text:
                        return part.text
                    if hasattr(part, 'data') and part.data.struct_value:
                        fields = part.data.struct_value.fields
                        if 'args' in fields:
                            args = fields['args'].struct_value.fields
                            if 'questions' in args:
                                q_json = args['questions'].string_value
                                return json.loads(q_json)[0]['question']
                                
        if hasattr(chunk, 'task') and chunk.task.artifacts:
            for art in chunk.task.artifacts:
                for part in art.parts:
                    if hasattr(part, 'text') and part.text:
                        try:
                            data = json.loads(part.text)
                            return data['parameters']['questions']
                        except:
                            return part.text
    except Exception as e:
        return f"Błąd parsowania: {e}"
    
    return None
