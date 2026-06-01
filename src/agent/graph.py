"""
graph.py — Main logic of the agent as a LangGraph.

State graph (StateGraph) defines the decision flow:

  [START]
     │
     ▼
 fetch_logs ─────────────────────────────────────┐
     │                                           │
     ▼                                           │
 query_rag (searching in history + runbooks)     │
     │                                           │
     ▼                                           │
  reason (LLM decides what to do)                │
     │                                           │
     ▼                                           │
  execute (kubectl action)                       │
     │                                           │
     ▼                                           │
  verify ──── success? ──── NO ──── (max 2x) ────┘
     │
     │ YES
     ▼
store_memory ──► [END]
"""

import asyncio
import json
import time
import logging
import uuid
import httpx
import requests
import operator
from fastapi import Request
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from a2a.client import ClientConfig, create_client, A2ACardResolver
from a2a.types.a2a_pb2 import Role, SendMessageRequest
from a2a.helpers import new_text_message

from agent.tools import (
    get_pod_logs,
    describe_pod,
    delete_pod,
    restart_deployment,
    scale_deployment,
    is_pod_healthy,
)
from agent.rag import search_similar_incidents, search_runbooks, store_incident
from agent.prompts import (
    build_analysis_prompt,
    build_verification_prompt,
    build_summary_prompt,
)

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"           # locally
SESSION_MANAGER_URL = "http://127.0.0.1:8083/api/sessions"
PROXY_KAGENT_URL = "http://127.0.0.1:8080"
LLM_MODEL = "llama3.2"
MAX_RETRIES = 2 


class AgentState(TypedDict):
    alert: dict                   

    logs: str                       
    pod_info: dict                     
    past_incidents: list[dict]                      
    runbooks: list[dict]                             
    action_plan: dict                              
    action_result: str                            
    verification: dict                         
    retry_count: Annotated[int, operator.add]     
    success: bool                                 
    kagent_session_id: str


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


def extract_text_safely(chunk):
    try:
        if chunk.HasField('message'):
            msg = chunk.message
            if hasattr(msg, 'content'):
                return str(msg.content)
            if hasattr(msg, 'parts'):
                return "".join([p.text for p in msg.parts if hasattr(p, 'text')])
            return str(msg)
            
        if chunk.HasField('task'):
            return str(chunk.task)
            
    except Exception as e:
        return f"Błąd parsowania: {e}"
    return None
    

async def call_llm(prompt: str) -> str:
    forced_session_id = f"ctx-{uuid.uuid4()}"
    forced_gate_user = f"A2A_USER_{forced_session_id}"
    
    async with httpx.AsyncClient() as init_client:
        session_payload = {
            "id": forced_session_id,
            "agent_ref": "kagent__NS__krag_agent",
            "user_id": forced_gate_user
        }
        
        headers_for_8083 = {
            "X-User-Id": forced_gate_user,
            "X-Authenticated-Userid": forced_gate_user,
            "X-Forwarded-User": forced_gate_user,
            "kagent-user-id": forced_gate_user,
            "Authorization": f"Bearer {forced_gate_user}",
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
        logger.info(f"[krag] Baza zsynchronizowana -> ID: {saved_session_id}")

    async with httpx.AsyncClient() as metadata_client:
        resolver = A2ACardResolver(httpx_client=metadata_client, base_url=PROXY_KAGENT_URL)
        real_agent_card = await resolver.get_agent_card()

    async with httpx.AsyncClient(base_url=PROXY_KAGENT_URL, timeout=180.0) as rpc_http_client:
        config = ClientConfig(streaming=False)
        client = await create_client(agent=real_agent_card, client_config=config)
        
        if hasattr(client, '_transport') and hasattr(client._transport, 'httpx_client'):
            client._transport.httpx_client = rpc_http_client
        
        message = new_text_message(prompt, role=Role.ROLE_USER)
        message.context_id = saved_session_id
        req = SendMessageRequest(message=message)
        
        logger.info(f"[krag] Agent myśli... (timeout 180s)...")
        
        raw_chunks = []
        async for chunk in client.send_message(req):
            found_text = extract_text_aggressively(chunk)
            if isinstance(found_text, list):
                found_text = "".join([str(x) for x in found_text])
            
            if found_text:
                raw_chunks.append(str(found_text))
                
        agent_response_text = "".join(raw_chunks).strip()
        
        if not agent_response_text:
            agent_response_text = "Odebrano puste chunki lub brak pola tekstowego."

        logger.info(f"[krag] SUKCES!")
        return agent_response_text


def node_trigger_alert_info_krag(state: AgentState) -> AgentState:
    KAGENT_A2A_URL = "http://127.0.0.1:8146/alert/"
    
    alert = state["alert"]

    clean_alert = {k: v for k, v in alert.items() if v != 'unknown'}

    alert_name = clean_alert.pop('alertname', 'unknown alert').lower()
    severity = clean_alert.pop('severity', 'info').lower()
    description = clean_alert.pop('description', 'no diagnostic description provided.')

    if severity == "critical":
        icon = "🔴"
    elif severity == "warning":
        icon = "🟡"
    else:
        icon = "🔵"

    prompt_for_builtin = f"""# {icon} SRE Report: Diagnostic Action

    ### 📌 Summary

    *severity:* {severity}
    *alert:    {alert_name}*

    ### 🔬 Context & Resolution
    {description}
    """
    
    try:
        logger.info("[kagent-init] Przekazuję raport do wbudowanego krag-agent...")
        query_params = {
            "response": prompt_for_builtin
        }
        response = requests.post(
            KAGENT_A2A_URL,
            params=query_params,
            timeout=10.0
        )
        logger.info(f"[kagent-bridge] Wbudowany agent odpowiedział: {response}")
        
    except Exception as e:
        logger.error(f"[kagent-bridge] Nie udało się skomunikować z wbudowanym agentem: {e}")
        
    return state 


def node_fetch_logs(state: AgentState) -> AgentState:
    alert = state["alert"]
    pod_name = alert.get("pod", "unknown")
    namespace = alert.get("namespace", "default")

    logger.info(f"[fetch_logs] Pod: {pod_name} / namespace: {namespace}")

    logs = get_pod_logs(pod_name, namespace, tail=100)
    pod_info = describe_pod(pod_name, namespace)

    return {"logs": logs, "pod_info": pod_info}


def node_query_rag(state: AgentState) -> AgentState:
    alert = state["alert"]
    query = f"{alert.get('alertname', '')} {alert.get('description', '')}"

    logger.info(f"[query_rag] Searching for similar incidents for: {query[:80]}")

    past = search_similar_incidents(query, n_results=3)
    books = search_runbooks(query, n_results=2)

    logger.info(f"[query_rag] Found: {len(past)} incidents, {len(books)} runbooks")

    return {"past_incidents": past, "runbooks": books}


async def node_reason(state: AgentState) -> AgentState:
    prompt = build_analysis_prompt(
        alert=state["alert"],
        logs=state["logs"],
        pod_info=state["pod_info"],
        past_incidents=state["past_incidents"],
        runbooks=state["runbooks"],
    )

    logger.info("[reason] Sending prompt to LLM...")
    logger.info(f"[reason] === PROMPT SENT TO LLM ===\n{prompt}\n=== END PROMPT ===")
    raw = await call_llm(prompt)
    logger.info(f"[reason] === LLM RAW RESPONSE ===\n{raw}\n=== END RESPONSE ===")
    
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[reason] LLM did not return JSON: {raw[:200]}")
        pod = state["alert"].get("pod", "unknown")
        ns = state["alert"].get("namespace", "default")
        plan = {
            "action": "delete_pod",
            "target": pod,
            "namespace": ns,
            "root_cause": "unknown (fallback)",
            "reasoning": "LLM did not return valid JSON, using fallback",
        }

    logger.info(
        f"[reason] === ACTION PLAN ===\n"
        f"  root_cause : {plan.get('root_cause')}\n"
        f"  action     : {plan.get('action')}\n"
        f"  target     : {plan.get('target')}\n"
        f"  namespace  : {plan.get('namespace')}\n"
        f"  reasoning  : {plan.get('reasoning')}\n"
        f"=== END PLAN ==="
    )
    return {"action_plan": plan}


def node_execute(state: AgentState) -> AgentState:
    plan = state["action_plan"]
    action = plan.get("action")
    target = plan.get("target")
    namespace = plan.get("namespace", "default")

    logger.info(f"[execute] Executing: {action} on {target} ({namespace})")

    if action == "delete_pod":
        result = delete_pod(target, namespace)
    elif action == "restart_deployment":
        result = restart_deployment(target, namespace)
    elif action == "scale_deployment":
        replicas = plan.get("replicas", 3)
        result = scale_deployment(target, replicas, namespace)
    else:
        result = f"Unknown action: {action}"
        logger.error(f"[execute] {result}")

    logger.info(f"[execute] Result: {result}")
    return {"action_result": result}


async def node_verify(state: AgentState) -> AgentState:
    logger.info("[verify] Waiting 30s for stabilization...")
    time.sleep(30)

    alert = state["alert"]
    pod_name = alert.get("pod", "unknown")
    namespace = alert.get("namespace", "default")

    healthy = is_pod_healthy(pod_name, namespace)

    pod_info = describe_pod(pod_name, namespace)
    prompt = build_verification_prompt(state["action_result"], pod_info)
    
    logger.info("[verify] Asking LLM to confirm recovery...")
    raw = await call_llm(prompt)
    logger.info(f"[verify] === LLM VERIFICATION RESPONSE ===\n{raw}\n=== END ===")
    
    try:
        verification = json.loads(raw)
    except json.JSONDecodeError:
        verification = {"success": healthy, "reason": "auto-detect", "next_action": None}

    logger.info(
        f"[verify] === VERIFICATION RESULT ===\n"
        f"  pod_healthy (k8s): {healthy}\n"
        f"  llm_success      : {verification.get('success')}\n"
        f"  reason           : {verification.get('reason')}\n"
        f"  next_action      : {verification.get('next_action')}\n"
        f"=== END RESULT ==="
    )
    return {
        "verification": verification, 
        "success": healthy,
        "retry_count": 1
    }


async def node_store_memory(state: AgentState) -> AgentState:
    prompt = build_summary_prompt(
        alert=state["alert"],
        action=str(state["action_plan"]),
        success=state["success"],
    )

    raw = await call_llm(prompt)
    logger.info(f"[store_memory] === LLM SUMMARY RESPONSE ===\n{raw}\n=== END ===")

    try:
        summary = json.loads(raw)
        doc_id = store_incident(
            problem=summary["problem"],
            solution=summary["solution"],
            metadata={"alertname": state["alert"].get("alertname")},
        )
        logger.info(
            f"[store_memory] === INCIDENT STORED IN CHROMADB ===\n"
            f"  doc_id  : {doc_id}\n"
            f"  problem : {summary.get('problem')}\n"
            f"  solution: {summary.get('solution')}\n"
            f"=== END ==="
        )
    except Exception as e:
        logger.warning(f"[store_memory] Failed to store: {e}")

    return state


def edge_should_retry(state: AgentState) -> str:
    if state.get("success", False):
        logger.info("[edge] Repair successful. Routing to store_memory.")
        return "store_memory"

    retries = state.get("retry_count", 0)
    logger.info(f"[edge DEBUG] Sprawdzam warunek pętli. Aktualny licznik: {retries}/{MAX_RETRIES}")
    if retries < MAX_RETRIES:
        logger.info(f"[edge] Condition failed. Retry {retries}/{MAX_RETRIES} -> Routing back to 'reason'")  
        return "reason"

    logger.warning(f"[edge] Exceeded MAX_RETRIES ({MAX_RETRIES}) — giving up. Routing to fallback execution path.")
    return "store_memory"                 


def node_trigger_builtin_krag(state: AgentState) -> AgentState:
    KAGENT_A2A_URL = "http://127.0.0.1:8146/alert/"
    
    local_solution = state.get("action_result", "Brak szczegółów")
    alert_name = state["alert"].get("alertname")
    
    prompt_for_builtin = (
        f"SRE Task: Our local diagnostic system has resolved the issue with alert {alert_name}.\n"
        f"Action summary: {local_solution}.\n"
    )
    
    try:
        logger.info("[kagent-bridge] Przekazuję raport do wbudowanego krag-agent...")
        query_params = {
            "response": prompt_for_builtin
        }
        response = requests.post(
            KAGENT_A2A_URL,
            params=query_params,
            timeout=10.0
        )
        logger.info(f"[kagent-bridge] Wbudowany agent odpowiedział: {response}")
        
    except Exception as e:
        logger.error(f"[kagent-bridge] Nie udało się skomunikować z wbudowanym agentem: {e}")
        
    return state 


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("trigger_initial_message_kagent", node_trigger_alert_info_krag)
    graph.add_node("fetch_logs", node_fetch_logs)
    graph.add_node("query_rag", node_query_rag)
    graph.add_node("reason", node_reason)
    graph.add_node("execute", node_execute)
    graph.add_node("verify", node_verify)
    graph.add_node("store_memory", node_store_memory)
    graph.add_node("trigger_kagent", node_trigger_builtin_krag)

    graph.add_edge(START, "trigger_initial_message_kagent")
    graph.add_edge("trigger_initial_message_kagent", "fetch_logs")
    graph.add_edge("fetch_logs", "query_rag")
    graph.add_edge("query_rag", "reason")
    graph.add_edge("reason", "execute")
    graph.add_edge("execute", "verify")

    graph.add_conditional_edges(
        "verify", 
        edge_should_retry,
        {
            "reason": "reason",
            "store_memory": "store_memory"
        }
    )

    graph.add_edge("store_memory", "trigger_kagent")
    graph.add_edge("trigger_kagent", END)

    try:
        compiled_graph = graph.compile()
        png_bytes = compiled_graph.get_graph().draw_mermaid_png()
        with open("krag_graph.png", "wb") as f:
            f.write(png_bytes)
        logger.info("[krag] Ostateczna wizualizacja grafu zapisana do pliku: krag_graph.png")
    except Exception as e:
        logger.warning(f"[krag] Nie udało się wygenerować obrazka (brak wymaganych bibliotek systemowych): {e}")
        try:
            with open("krag_graph.md", "w") as f:
                f.write(f"```mermaid\n{graph.compile().get_graph().draw_mermaid()}\n```")
            logger.info("[krag] Zapisano tekstowy schemat Mermaid do pliku: krag_graph.md")
        except Exception:
            pass

    return graph.compile()


krag_graph = build_graph()


async def run_agent(alert: dict) -> dict:
    session_id = "unknown"
    
    try:
        session_payload = {"agent_ref": "kagent__NS__krag_agent"}
        resp = requests.post(SESSION_MANAGER_URL, json=session_payload, timeout=5.0)
        resp.raise_for_status()
        session_id = resp.json()["data"]["id"]
        logger.info(f"[krag-init] 🎉 Pomyślnie zainicjalizowano sesję kAgenta: {session_id}")
    except Exception as e:
        logger.error(f"[krag-init] 🔥 Nie udało się stworzyć sesji w kAgencie: {e}. Używam fallback UUID.")
        import uuid
        session_id = f"fallback-{uuid.uuid4()}"

    initial_state: AgentState = {
        "alert": alert,
        "logs": "",
        "pod_info": {},
        "past_incidents": [],
        "runbooks": [],
        "action_plan": {},
        "action_result": "",
        "verification": {},
        "retry_count": 0,
        "success": False,
        "kagent_session_id": session_id,
    }

    logger.info(f"[krag] START — alert: {alert.get('alertname')} / pod: {alert.get('pod')}")
    final_state = await krag_graph.ainvoke(initial_state)
    logger.info(f"[krag] END — success: {final_state['success']}")
    return final_state
