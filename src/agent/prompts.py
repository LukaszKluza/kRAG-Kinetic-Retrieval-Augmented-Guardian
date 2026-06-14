def build_analysis_prompt(
    alert: dict,
    logs: str,
    pod_info: dict,
    past_incidents: list[dict],
    runbooks: list[dict],
) -> str:
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
2. Choose the best remediation action. CRITICAL target naming rules:
   - delete_pod      → target MUST be the POD name from the Alert section above (e.g. "crasher-0").
                       K8s will auto-recreate it via its owner workload.
                       Use this for OOMKilled or stuck pods — it resets accumulated state.
   - restart_deployment → target MUST be the workload name from pod_info["owner_workload"]["name"]
                          (e.g. "crasher"). Do NOT use the container name (e.g. "memory-worker").
   - scale_deployment   → target MUST be the workload name from pod_info["owner_workload"]["name"].
                          WARNING: scaling does NOT fix OOMKill — each new replica will also
                          run out of memory. Only use if the fix is distributing load.
3. For OOMKilled pods: prefer delete_pod — it gives the pod a clean memory slate immediately.

Answer ONLY in JSON format:
{{
  "root_cause": "short description of the cause",
  "action": "delete_pod|restart_deployment|scale_deployment",
  "target": "pod name (for delete_pod) OR workload name from owner_workload (for restart/scale)",
  "namespace": "namespace",
  "replicas": 3,
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
