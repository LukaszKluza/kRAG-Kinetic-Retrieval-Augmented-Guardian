"""
prompts.py — Prompt templates built dynamically in Python.

Instead of statically entering the prompt in YAML (kagent.yaml),
we build it here based on data from K8s, RAG, and history.
"""

    
def build_analysis_prompt(
    alert: dict,
    logs: str,
    pod_info: dict,
    past_incidents: list[dict],
    runbooks: list[dict],
) -> str:
    """
    Builds the prompt for incident analysis.
    The LLM receives all the data and decides what to do.
    """

    past_context = ""
    if past_incidents:
        past_context = "\n\n## Similar Incidents from the past:\n"
        for i, inc in enumerate(past_incidents, 1):
            similarity = round((1 - inc["distance"]) * 100, 1)
            past_context += f"\n### Incident #{i} (similarity: {similarity}%)\n"
            past_context += inc["document"] + "\n"
    else:
        past_context = "\n\n## History: No similar incidents found in the database.\n"

    runbook_context = ""
    if runbooks:
        runbook_context = "\n\n## Runbooks:\n"
        for rb in runbooks:
            runbook_context += f"\n### {rb['metadata'].get('title', 'Runbook')}\n"
            runbook_context += rb["document"] + "\n"

    return f"""You are an autonomous SRE agent. You are analyzing an incident in the Kubernetes cluster.
Do not ask the user for anything. Fetch missing data and make decisions.

## Alert:
- Type: {alert.get("alertname", "Unknown")}
- Pod: {alert.get("pod", "unknown")}
- Namespace: {alert.get("namespace", "default")}
- Description: {alert.get("description", "no description")}
- Severity: {alert.get("severity", "unknown")}

## Pod Logs (last 100 lines):
```
{logs}
```

## Pod Status:
```json
{pod_info}
```
{past_context}
{runbook_context}

## Your Task:
1. Identify the root cause of the problem
2. Choose a remediation action from: [delete_pod, restart_deployment, scale_deployment]
3. Justify your choice

Answer ONLY in JSON format:
{{
  "root_cause": "short description of the cause",
  "action": "delete_pod|restart_deployment|scale_deployment",
  "target": "name of the pod or deployment",
  "namespace": "namespace",
  "replicas": 3,  // only for scale_deployment
  "reasoning": "why this action"
}}"""


def build_verification_prompt(action_taken: str, pod_status: dict) -> str:
    """Prompt to verify if the remediation action was successful."""
    return f"""Check if the remediation action was successful.

Action taken: {action_taken}

Current status:
```json
{pod_status}
```

Answer ONLY in JSON format:
{{
  "success": true/false,
  "reason": "why success or failure",
  "next_action": "null or next action if needed"
}}"""


def build_summary_prompt(alert: dict, action: str, success: bool) -> str:
    """Prompt to create a summary of the incident for saving in ChromaDB."""
    return f"""Create a concise summary of the incident for saving in the knowledge base.

Alert: {alert}
Action: {action}
Success: {success}

Answer ONLY in JSON format:
{{
  "problem": "short description of the problem (1-2 sentences)",
  "solution": "what was done and why it worked"
}}"""
