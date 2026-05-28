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

import json
import time
import logging
import requests
import operator
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END

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
# OLLAMA_URL = "http://ollama.ollama.svc.cluster.local:80"  # in the cluster
# OLLAMA_URL = "http://127.0.0.1:8083/api/a2a/kagent/krag-agent/"
LLM_MODEL = "llama3.2"
MAX_RETRIES = 2  # how many times the agent can try to fix the issue before giving up


# TypedDict defines what is passed between nodes.
# Each node receives the full state and can enrich it.

class AgentState(TypedDict):
    # Output
    alert: dict                        # raw alert from Alertmanager

    # Collected by nodes
    logs: str                          # logs from the crashing pod
    pod_info: dict                     # describe_pod()
    past_incidents: list[dict]         # results from ChromaDB
    runbooks: list[dict]               # runbooks from ChromaDB
    action_plan: dict                  # LLM decision (JSON)
    action_result: str                 # result of action execution
    verification: dict                 # verification result
    retry_count: Annotated[int, operator.add]                 # number of repair attempts
    success: bool                      # whether the issue was resolved
    kagent_session_id: str


KAGENT_SESSION_URL = "http://127.0.0.1:8083/api/sessions"
KAGENT_EXECUTE_URL = "http://127.0.0.1:8080" # Port wykonawczy kAgenta

def call_llm(prompt: str) -> str:
    """Sends a prompt to Ollama and returns the response as a string."""
    response = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": LLM_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        },
    )
    response.raise_for_status()
    return response.json()["response"]

def node_trigger_alert_info_krag(state: AgentState) -> AgentState:
    KAGENT_A2A_URL = "http://127.0.0.1:8146/alert/"
    
    alert = state["alert"]

    clean_alert = {k: v for k, v in alert.items() if v != 'unknown'}

    alert_name = clean_alert.pop('alertname', 'unknown alert').lower()
    severity = clean_alert.pop('severity', 'info').lower()
    description = clean_alert.pop('description', 'no diagnostic description provided.')

    reset_color = "\u001b[0m"
    bold_white = "\u001b[1;37m"

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
    
    # prompt_for_builtin = (
    #     f"SRE Task. Our local diagnostic system received an alert: {clean_alert}.\n"
    # )
    
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
    """
    Node 1: Fetches logs and pod description from K8s.
    How does it know which pod? From the alert that came from Alertmanager.
    """
    alert = state["alert"]
    pod_name = alert.get("pod", "unknown")
    namespace = alert.get("namespace", "default")

    logger.info(f"[fetch_logs] Pod: {pod_name} / namespace: {namespace}")

    logs = get_pod_logs(pod_name, namespace, tail=100)
    pod_info = describe_pod(pod_name, namespace)

    return {"logs": logs, "pod_info": pod_info}


def node_query_rag(state: AgentState) -> AgentState:
    """
    Node 2: Queries ChromaDB for similar incidents and runbooks.
    Uses the alert description as a vector query.
    """
    alert = state["alert"]
    query = f"{alert.get('alertname', '')} {alert.get('description', '')}"

    logger.info(f"[query_rag] Searching for similar incidents for: {query[:80]}")

    past = search_similar_incidents(query, n_results=3)
    books = search_runbooks(query, n_results=2)

    logger.info(f"[query_rag] Found: {len(past)} incidents, {len(books)} runbooks")

    return {"past_incidents": past, "runbooks": books}


def node_reason(state: AgentState) -> AgentState:
    """
    Node 3: LLM analyzes all the data and decides what to do.
    Receives: alert + logs + history + runbooks → returns action plan.
    """
    prompt = build_analysis_prompt(
        alert=state["alert"],
        logs=state["logs"],
        pod_info=state["pod_info"],
        past_incidents=state["past_incidents"],
        runbooks=state["runbooks"],
    )

    logger.info("[reason] Sending prompt to LLM...")
    raw = call_llm(prompt, session_id=state.get("kagent_session_id"))

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

    logger.info(f"[reason] Plan: {plan.get('action')} on {plan.get('target')}")
    return {"action_plan": plan}


def node_execute(state: AgentState) -> AgentState:
    """
    Node 4: Executes a repair action on the K8s cluster.
    The action comes from the LLM's action plan.
    """
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


def node_verify(state: AgentState) -> AgentState:
    """
    Node 5: Waits a moment and checks if the pod has recovered.
    If not — increments retry_count (edge decides whether to try again).
    """
    logger.info("[verify] Waiting 30s for stabilization...")
    time.sleep(30)

    alert = state["alert"]
    pod_name = alert.get("pod", "unknown")
    namespace = alert.get("namespace", "default")

    healthy = is_pod_healthy(pod_name, namespace)

    pod_info = describe_pod(pod_name, namespace)
    prompt = build_verification_prompt(state["action_result"], pod_info)
    raw = call_llm(prompt, session_id=state.get("kagent_session_id"))
    try:
        verification = json.loads(raw)
    except json.JSONDecodeError:
        verification = {"success": healthy, "reason": "auto-detect", "next_action": None}

    logger.info(f"[verify] Healthy: {healthy}, LLM: {verification}")

    return {
        "verification": verification, 
        "success": healthy,
        "retry_count": 1
    }


def node_store_memory(state: AgentState) -> AgentState:
    """
    Node 6: Stores the resolved incident in ChromaDB.
    Called ONLY when the repair is successful.
    """
    prompt = build_summary_prompt(
        alert=state["alert"],
        action=str(state["action_plan"]),
        success=state["success"],
    )
    raw = call_llm(prompt, session_id=state.get("kagent_session_id"))
    try:
        summary = json.loads(raw)
        doc_id = store_incident(
            problem=summary["problem"],
            solution=summary["solution"],
            metadata={"alertname": state["alert"].get("alertname")},
        )
        logger.info(f"[store_memory] Stored incident: {doc_id}")
    except Exception as e:
        logger.warning(f"[store_memory] Failed to store: {e}")

    return state


def edge_should_retry(state: AgentState) -> str:
    """
    After verification: should we retry, or terminate?
    Returns the name of the next node.
    """
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
    """
    Węzeł, który bierze rozwiązanie z lokalnego modelu i pcha je do 
    wbudowanego w klaster 'kagent', aby ten dokończył dzieła (np. powiadomił zespół).
    """
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
        # Generuje piękny plik PNG przy użyciu wewnętrznego silnika Mermaid/PyGraphviz
        png_bytes = compiled_graph.get_graph().draw_mermaid_png()
        with open("krag_graph.png", "wb") as f:
            f.write(png_bytes)
        logger.info("[krag] Ostateczna wizualizacja grafu zapisana do pliku: krag_graph.png")
    except Exception as e:
        logger.warning(f"[krag] Nie udało się wygenerować obrazka (brak wymaganych bibliotek systemowych): {e}")
        # Rezerwowy zapis do formatu tekstowego Mermaid (wklejasz na mermaid.live i masz wykres)
        try:
            with open("krag_graph.md", "w") as f:
                f.write(f"```mermaid\n{graph.compile().get_graph().draw_mermaid()}\n```")
            logger.info("[krag] Zapisano tekstowy schemat Mermaid do pliku: krag_graph.md")
        except Exception:
            pass

    return graph.compile()


krag_graph = build_graph()


def run_agent(alert: dict) -> dict:
    """
    Public API — invoke the agent with an alert.
    alert = {"alertname": "PodCrashLooping", "pod": "crash-test",
              "namespace": "default", "description": "..."}
    """
    session_id = "unknown"
    
    # 1. Rejestracja sesji w kAgent Session Manager (:8083)
    try:
        session_payload = {"agent_ref": "kagent__NS__krag_agent"}
        resp = requests.post(KAGENT_SESSION_URL, json=session_payload, timeout=5.0)
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
    final_state = krag_graph.invoke(initial_state)
    logger.info(f"[krag] END — success: {final_state['success']}")
    return final_state
