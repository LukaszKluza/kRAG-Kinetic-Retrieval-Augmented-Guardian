from kubernetes import client, config
from kubernetes.client.rest import ApiException
import logging

logger = logging.getLogger(__name__)


def load_k8s_config():
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
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        statuses = pod.status.container_statuses or []
        owners = [
            {"kind": ref.kind, "name": ref.name}
            for ref in (pod.metadata.owner_references or [])
        ]
        return {
            "phase": pod.status.phase,
            "owner_workload": owners[0] if owners else None,
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
    try:
        v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        return f"Pod {pod_name} deleted. K8s will recreate it automatically."
    except ApiException as e:
        return f"ERROR deleting pod: {e.reason}"


def restart_deployment(deployment_name: str, namespace: str = "default") -> str:
    import datetime
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
    try:
        apps_v1.patch_namespaced_deployment(
            name=deployment_name, namespace=namespace, body=patch
        )
        return f"Deployment {deployment_name} restarted."
    except ApiException:
        pass
    try:
        apps_v1.patch_namespaced_stateful_set(
            name=deployment_name, namespace=namespace, body=patch
        )
        return f"StatefulSet {deployment_name} restarted (pods will be recreated with stable names)."
    except ApiException as e:
        return f"ERROR restarting {deployment_name}: {e.reason}"


def scale_deployment(deployment_name: str, replicas: int, namespace: str = "default") -> str:
    try:
        patch = {"spec": {"replicas": replicas}}
        apps_v1.patch_namespaced_deployment_scale(
            name=deployment_name, namespace=namespace, body=patch
        )
        return f"Deployment {deployment_name} scaled to {replicas} replicas."
    except ApiException as e:
        return f"ERROR scaling deployment: {e.reason}"


def is_pod_healthy(pod_name: str, namespace: str = "default") -> bool:
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
