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
import httpx
from pydantic import BaseModel
import json
from a2a.client import A2ACardResolver  # noqa: PLC0415
from a2a.client import ClientConfig, create_client  # noqa: PLC0415
from a2a.helpers import new_text_message  # noqa: PLC0415
from a2a.types.a2a_pb2 import Role, SendMessageRequest  # noqa: PLC0415
from a2a.server.context import ServerCallContext  # jeśli jest dostępny w SDK klienta
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
    result = await run_agent(alert_dict)

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
        result = await run_agent(alert_dict)
        logger.info(
            f"Alert handled: {alert_dict['alertname']} | "
            f"success={result['success']} | "
            f"action={result.get('action_plan', {}).get('action')}"
        )
    except Exception as e:
        logger.error(f"Error occurred while handling alert {alert_dict}: {e}", exc_info=True)
            

def extract_text_aggressively(chunk):
    """
    Ekstrakcja tekstu niezależnie od tego, czy agent użył pól 'text', 'data' czy 'artifacts'.
    """
    try:
        # 1. Przeszukaj historię (jeśli dostępna)
        if hasattr(chunk, 'history'):
            for h in chunk.history:
                for part in h.parts:
                    # Sprawdź 'text' (stary format)
                    if hasattr(part, 'text') and part.text:
                        return part.text
                    # Sprawdź 'data' (nowy format struct_value)
                    if hasattr(part, 'data') and part.data.struct_value:
                        # Użyj .get() dla bezpiecznego dostępu do pól Protobufa
                        fields = part.data.struct_value.fields
                        if 'args' in fields:
                            args = fields['args'].struct_value.fields
                            if 'questions' in args:
                                q_json = args['questions'].string_value
                                return json.loads(q_json)[0]['question']
                                
        # 2. Przeszukaj artefakty (jeśli są w tasku)
        if hasattr(chunk, 'task') and chunk.task.artifacts:
            for art in chunk.task.artifacts:
                for part in art.parts:
                    if hasattr(part, 'text') and part.text:
                        # Spróbuj sparsować, jeśli to JSON w tekście
                        try:
                            data = json.loads(part.text)
                            return data['parameters']['questions'] # lub odpowiednia ścieżka
                        except:
                            return part.text
    except Exception as e:
        return f"Błąd parsowania: {e}"
    
    return None


@app.post("/dupa7")
async def dupa7(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
        
    user_prompt = body.get("prompt", "Why is the sky blue?")
    
    # 1. GENERUJEMY ID SESJI I WŁAŚCIWEGO UŻYTKOWNIKA BRAMY
    forced_session_id = f"ctx-{uuid.uuid4()}"
    forced_brama_user = f"A2A_USER_{forced_session_id}"
    
    SESSION_MANAGER_URL = "http://127.0.0.1:8083/api/sessions"
    PROXY_KAGENT_URL = "http://127.0.0.1:8080"  # Wracamy do portu 8080, bo bazy są już zgrane

    # 2. ZAPISUJEMY SESJĘ W BAZIE (Wymuszamy tożsamość)
    async with httpx.AsyncClient() as init_client:
        try:
            session_payload = {
                "id": forced_session_id,
                "agent_ref": "kagent__NS__krag_agent",
                "user_id": forced_brama_user
            }
            
            headers_for_8083 = {
                "X-User-Id": forced_brama_user,
                "X-Authenticated-Userid": forced_brama_user,
                "X-Forwarded-User": forced_brama_user,
                "kagent-user-id": forced_brama_user,
                "Authorization": f"Bearer {forced_brama_user}",
                "Content-Type": "application/json"
            }

            session_resp = await init_client.post(
                SESSION_MANAGER_URL, 
                json=session_payload, 
                headers=headers_for_8083, 
                timeout=10.0
            )
            session_resp.raise_for_status()
            session_data = session_resp.json()
            
            saved_session_id = session_data["data"]["id"]
            saved_user_id = session_data["data"]["user_id"]
            
            logger.info(f"[krag] 💥 Baza zsynchronizowana -> ID: {saved_session_id}, USER: {saved_user_id}")
            
        except Exception as e:
            logger.error(f"[krag] ❌ Błąd parowania sesji na 8083: {e}")
            return {"status": "error", "detail": f"Failed to create session on 8083: {e}"}

    # 3. POBIERAMY KARTĘ AGENTA
    async with httpx.AsyncClient() as metadata_client:
        try:
            resolver = A2ACardResolver(httpx_client=metadata_client, base_url=PROXY_KAGENT_URL)
            real_agent_card = await resolver.get_agent_card()
        except Exception as e:
            return {"status": "error", "detail": f"Failed to fetch agent card: {e}"}

    # 4. STRZAŁ PO ODPOWIEDŹ Z WYDŁUŻONYM TIMEOUTEM (60 SEKUND)
    # Przekazujemy timeout bezpośrednio do klienta HTTPX obsługującego klienta RPC
    async with httpx.AsyncClient(base_url=PROXY_KAGENT_URL, timeout=60.0) as rpc_http_client:
        try:
            config = ClientConfig(streaming=False)
            client = await create_client(agent=real_agent_card, client_config=config)
            
            # Podmieniamy klienta w transporcie, żeby na pewno wymusić wyższy timeout
            if hasattr(client, '_transport') and hasattr(client._transport, 'httpx_client'):
                client._transport.httpx_client = rpc_http_client
            
            # Budujemy wiadomość
            message = new_text_message(user_prompt, role=Role.ROLE_USER)
            message.context_id = saved_session_id
            req = SendMessageRequest(message=message)
            
            logger.info(f"[krag] ⏳ Agent myśli... Wysyłam żądanie (timeout rozszerzony do 60s)...")
            
            raw_chunks = []
            async for chunk in client.send_message(req):
                found_text = extract_text_aggressively(chunk)
                if found_text:
                    logger.info(f"[krag] SUKCES: {found_text}")
                    return {"status": "completed", "response": found_text}
                        
            agent_response_text = "".join(raw_chunks).strip()
            
            # Jeśli jakimś cudem parser nie wyciągnął tekstu, ale chunk przyszedł pusty
            if not agent_response_text:
                agent_response_text = "Odebrano puste chunki lub brak pola tekstowego, sprawdź logi wyżej."

            logger.info(f"[krag] ✅ SUKCES OSTATECZNY! Odpowiedź: {agent_response_text}")
            return {
                "status": "completed",
                "session_id": saved_session_id,
                "response": agent_response_text
            }
            
        except Exception as e:
            logger.error(f"[krag] ❌ Błąd podczas oczekiwania na odpowiedź agenta: {e}")
            return {"status": "error", "detail": f"Agent communication failed: {e}"}