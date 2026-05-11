"""
tools.py — Wrapper for Kubernetes Python SDK
Each function is a "tool" that the agent can call.
"""

from kubernetes import client, config
from kubernetes.client.rest import ApiException
import logging

logger = logging.getLogger(__name__)


def load_k8s_config():
    """
    Loads the K8s configuration.
    - In the cluster (when running as a pod): uses ServiceAccount
    - Locally (during development): uses ~/.kube/config
    """
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster config (ServiceAccount)")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local config (~/.kube/config)")


load_k8s_config()
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()



def get_pod_logs(pod_name: str, namespace: str = "default", tail: int = 100) -> str:
    """Gets the last `tail` lines of logs from a pod."""
    try:
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail,
            timestamps=True,
        )
        return logs or "(no logs)"
    except ApiException as e:
        return f"ERROR fetching logs: {e.reason}"


def describe_pod(pod_name: str, namespace: str = "default") -> dict:
    """Returns details about a pod: status, events, restarts, reason."""
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        statuses = pod.status.container_statuses or []
        return {
            "phase": pod.status.phase,
            "containers": [
                {
                    "name": cs.name,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count,
                    "state": str(cs.state),
                    "last_state": str(cs.last_state),
                }
                for cs in statuses
            ],
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason}
                for c in (pod.status.conditions or [])
            ],
        }
    except ApiException as e:
        return {"error": e.reason}


def list_pods(namespace: str = "default") -> list[dict]:
    """Returns a list of pods in the specified namespace with their statuses."""
    pods = v1.list_namespaced_pod(namespace=namespace)
    return [
        {
            "name": p.metadata.name,
            "phase": p.status.phase,
            "restarts": sum(
                cs.restart_count
                for cs in (p.status.container_statuses or [])
            ),
        }
        for p in pods.items
    ]



def delete_pod(pod_name: str, namespace: str = "default") -> str:
    """
    Deletes a pod — K8s will automatically recreate it through ReplicaSet/Deployment.
    This is equivalent to 'kubectl delete pod <name>'.
    """
    try:
        v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        return f"Pod {pod_name} deleted. K8s will recreate it automatically."
    except ApiException as e:
        return f"ERROR deleting pod: {e.reason}"


def restart_deployment(deployment_name: str, namespace: str = "default") -> str:
    """
    Performs a rolling restart of the deployment — equivalent to
    'kubectl rollout restart deployment/<name>'.
    """
    import datetime
    try:
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.datetime.utcnow().isoformat()
                        }
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(
            name=deployment_name, namespace=namespace, body=patch
        )
        return f"Deployment {deployment_name} restarted."
    except ApiException as e:
        return f"ERROR restarting deployment: {e.reason}"


def scale_deployment(deployment_name: str, replicas: int, namespace: str = "default") -> str:
    """Scales the deployment to the specified number of replicas."""
    try:
        patch = {"spec": {"replicas": replicas}}
        apps_v1.patch_namespaced_deployment_scale(
            name=deployment_name, namespace=namespace, body=patch
        )
        return f"Deployment {deployment_name} scaled to {replicas} replicas."
    except ApiException as e:
        return f"ERROR scaling deployment: {e.reason}"


def is_pod_healthy(pod_name: str, namespace: str = "default") -> bool:
    """Checks if a pod is in the Running state and ready."""
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        if pod.status.phase != "Running":
            return False
        return all(
            cs.ready
            for cs in (pod.status.container_statuses or [])
        )
    except ApiException:
        return False
