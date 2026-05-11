"""
ingest_docs.py — Loads runbooks and K8s documentation into ChromaDB.

Run once before starting the agent (and every time you add new runbooks):
    python ingestion/ingest_docs.py

What is loaded:
- Built-in K8s runbooks (written manually)
- Optionally: .md files from the runbooks directory
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.rag import ingest_runbook
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("krag.ingest")


# ── Built-in K8s runbooks ──────────────────────────────────────────────────────
# Add your own runbooks here. The more, the smarter the agent becomes.

RUNBOOKS = [
    {
        "title": "CrashLoopBackOff — diagnosis and repair",
        "content": """
CrashLoopBackOff means the container starts, crashes, and Kubernetes tries to restart it,
and the cycle repeats. Kubernetes adds exponential delays between restarts (10s, 20s, 40s...).

Most common causes:
1. Error in the application (exception on startup, configuration error)
2. Missing ConfigMap or Secret (application cannot read it)
3. Insufficient memory (OOMKilled) — check: kubectl describe pod <name>
4. Incorrect entrypoint in Dockerfile
5. Database connection issues (connection refused on startup)

Diagnosis:
    kubectl logs <pod> --previous    # logs from the PREVIOUS run
    kubectl describe pod <pod>       # look for: Exit Code, OOMKilled, Reason

Fix:
- Application error → delete pod (it will recreate) or fix code + restart deployment
- OOMKilled → increase resources.limits.memory in the deployment
- Missing Secret → kubectl create secret ...
- Database issue → check if the DB service is running
        """,
        "source": "builtin",
    },
    {
        "title": "OOMKilled — pod killed due to insufficient memory",
        "content": """
OOMKilled (Out of Memory Killed) — the Linux kernel killed the container because it exceeded its memory limit.

Symptoms:
- Exit Code: 137
- kubectl describe pod shows: Reason: OOMKilled
- Logs terminated abruptly, without error message

Diagnosis:
    kubectl describe pod <name> | grep -A5 "Last State"
    kubectl top pod <name>    # currently used memory

Fix:
1. Temporary: delete pod (it will give some breathing room)
2. Permanent: increase memory limit in the deployment:
   resources:
     limits:
       memory: "512Mi"   # increase this value
     requests:
       memory: "256Mi"

Don't remove limits entirely — one pod could starve the entire node.
        """,
        "source": "builtin",
    },
    {
        "title": "Pod in state Pending — cannot be scheduled",
        "content": """
Pending means the pod has not been assigned to any node.

Most common causes:
1. Insufficient resources (CPU/RAM) on nodes
2. NodeSelector or affinity doesn't match any node
3. PersistentVolumeClaim cannot be bound
4. Docker image doesn't exist or is private without imagePullSecret

Diagnosis:
    kubectl describe pod <name>    # section Events will show the reason
    kubectl get nodes              # check if nodes are Ready
    kubectl describe node <node>  # check Allocatable vs Requests

Fix:
- Insufficient resources → scale_deployment (reduce replicas) or add node
- Invalid image → fix image: in deployment spec
        """,
        "source": "builtin",
    },
    {
        "title": "High CPU Usage — pod uses too much CPU",
        "content": """
High CPU usage may be a symptom of a loop, thread leak, or attack.

Diagnosis:
    kubectl top pod                         # all pods, sorted by CPU
    kubectl top pod --sort-by=cpu
    kubectl exec <pod> -- top               # processes inside the container

Immediate fix:
1. Restart the pod (delete_pod) — often clears leaked goroutines/threads
2. Scale down + scale up deploymentu

Target fix:
- Add CPU limit in resources
- Investigate the application for infinite loops
        """,
        "source": "builtin",
    },
]


def main():
    logger.info(f"Loading {len(RUNBOOKS)} runbooks into ChromaDB...")

    for rb in RUNBOOKS:
        try:
            doc_id = ingest_runbook(
                title=rb["title"],
                content=rb["content"],
                source=rb.get("source", "manual"),
            )
            logger.info(f"  ✓ {rb['title']} → {doc_id}")
        except Exception as e:
            logger.error(f"  ✗ {rb['title']}: {e}")

    # Opcjonalnie: ładuj pliki .md z katalogu runbooks/
    runbooks_dir = os.path.join(os.path.dirname(__file__), "..", "runbooks")
    if os.path.isdir(runbooks_dir):
        for filename in os.listdir(runbooks_dir):
            if filename.endswith(".md"):
                path = os.path.join(runbooks_dir, filename)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                title = filename.replace(".md", "").replace("-", " ").title()
                ingest_runbook(title=title, content=content, source=path)
                logger.info(f"  ✓ File: {filename}")

    logger.info("Ingestion completed.")


if __name__ == "__main__":
    main()
