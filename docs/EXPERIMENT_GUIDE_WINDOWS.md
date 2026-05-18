# kRAG Experiment Guide — OOM Self-Healing Demo (Windows)

## What This Experiment Demonstrates

A Python worker pod leaks memory at 2MB/second until Kubernetes OOMKills it.
The crash repeats with exponential back-off, building up CrashLoopBackOff state.
Prometheus detects the restart count crossing a threshold and fires an alert.
Alertmanager forwards the alert to the kRAG webhook.
The kRAG agent — powered by a local Ollama LLM and ChromaDB RAG — investigates
the pod logs, queries its knowledge base for similar incidents, reasons about
the root cause, executes a remediation action, verifies recovery, and stores
the solved case back into ChromaDB for future reference.

**All system components are exercised end-to-end:**

| Component | Role in this experiment |
|-----------|------------------------|
| Python worker (StatefulSet) | The failing service — leaks memory, gets OOMKilled |
| Prometheus | Detects CrashLoopBackOff via kube-state-metrics |
| PrometheusRule | Fires `PodCrashLooping` alert at restart count >= 3 |
| Alertmanager | Routes firing alert to kRAG webhook |
| FastAPI server (`server.py`) | Receives webhook, queues the alert |
| LangGraph agent (`graph.py`) | Orchestrates the 6-step repair workflow |
| `tools.py` — `get_pod_logs` | Fetches crash logs from Kubernetes |
| `tools.py` — `describe_pod` | Gets pod status (OOMKilled, restart count) |
| `tools.py` — `delete_pod` | Primary remediation: force-recreate the pod |
| `tools.py` — `restart_deployment` | Alternative: rolling restart of the StatefulSet |
| ChromaDB (`rag.py`) | RAG: searches past incidents + runbooks |
| Ollama LLM (`graph.py`) | Reasons about root cause, decides action, verifies fix |
| ChromaDB store | Persists resolved incident for future learning |

---

## Why CrashLoopBackOff Does NOT Break the Experiment

Kubernetes does restart pods automatically — but with **exponential back-off delays**
(10s, 20s, 40s, 80s, 160s, up to 5 minutes). This means:

1. The pod crashes and restarts multiple times, accumulating a restart count.
2. Prometheus detects the restart count crossing a threshold and fires an alert.
3. By the time kRAG acts, the pod is still in CrashLoopBackOff (waiting for the
   next back-off period), not happily running.
4. kRAG's action (`delete_pod` or `restart_deployment`) **bypasses the back-off**
   and creates a fresh pod immediately.
5. The fresh pod has zero accumulated memory — so it survives the 30-second
   verification window (only ~60MB used vs 128Mi limit).
6. kRAG reports **SUCCESS**.

The pod will eventually OOM again (it still has the leak), but the demo has
completed its purpose: show the full AI-driven detection → reasoning → repair cycle.

---

## Prerequisites

Before starting, verify these are installed and working:

```powershell
# Check kubectl (Kubernetes CLI)
kubectl version --client

# Check docker and docker compose
docker --version
docker compose version

# Check that your cluster is running (kind, minikube, or other)
kubectl cluster-info

# Check that kube-prometheus-stack is installed (Prometheus + Alertmanager)
kubectl get pods -n monitoring

# Check that kube-state-metrics is running (needed for restart metrics)
kubectl get pods -n monitoring | Select-String "kube-state-metrics"

# Check Ollama is running locally (LLM inference engine)
# Expected output: {"status":"ok"} or similar
Invoke-RestMethod -Uri http://localhost:11434/api/tags

# Check that llama3.2 model is available in Ollama
Invoke-RestMethod -Uri http://localhost:11434/api/tags | ConvertTo-Json -Depth 5
# Look for "llama3.2" in the models list
```

If Ollama is not running, start it:
```powershell
ollama serve
# In another terminal: ollama pull llama3.2
```

---

## Architecture Diagram

```
  [Kubernetes cluster — kind/minikube]
  ┌─────────────────────────────────────────────┐
  │  krag-demo namespace                         │
  │  ┌──────────────────────────────────────┐   │
  │  │  StatefulSet: crasher                │   │
  │  │  Pod: crasher-0                      │   │
  │  │  Leaks 2MB/sec → OOMKill at ~50s    │   │
  │  │  CrashLoopBackOff builds up          │   │
  │  └──────────────────────────────────────┘   │
  │  ┌──────────────────────────────────────┐   │
  │  │  AlertmanagerConfig: krag-receiver   │   │
  │  │  (must be in krag-demo namespace)    │   │
  │  └──────────────────────────────────────┘   │
  │                                              │
  │  monitoring namespace                        │
  │  ┌─────────────┐    ┌─────────────────────┐ │
  │  │ Prometheus  │───>│ PrometheusRule       │ │
  │  │             │    │ restarts >= 3        │ │
  │  │             │    │ → PodCrashLooping    │ │
  │  └─────────────┘    └──────────┬──────────┘ │
  │  ┌──────────────────────────┐  │             │
  │  │ Alertmanager             │<─┘             │
  │  └──────────┬───────────────┘                │
  └─────────────┼───────────────────────────────-┘
                │ POST /webhook
                │ http://<HOST_IP>:8888/webhook
                │ (detected in Step 0)
                ▼
  [Windows host — running kRAG and Ollama]
  ┌──────────────────────────────────────────────┐
  │  kRAG — uvicorn on port 8888                 │
  │  ┌────────────────────────────────────────┐  │
  │  │  FastAPI server.py                     │  │
  │  │  → queues alert → run_agent()          │  │
  │  │                                        │  │
  │  │  LangGraph graph.py                    │  │
  │  │  1. fetch_logs   ──► Kubernetes API    │  │
  │  │  2. query_rag    ──► ChromaDB          │  │
  │  │  3. reason       ──► Ollama LLM        │  │
  │  │  4. execute      ──► Kubernetes API    │  │
  │  │  5. verify       ──► Kubernetes API    │  │
  │  │                  ──► Ollama LLM        │  │
  │  │  6. store_memory ──► ChromaDB          │  │
  │  └────────────────────────────────────────┘  │
  │                                              │
  │  ChromaDB — Docker container, port 8000      │
  │  krag-chroma: incident history + runbooks    │
  │                                              │
  │  Ollama — port 11434                         │
  │  llama3.2 + nomic-embed-text                 │
  └──────────────────────────────────────────────┘
```

---

## Step 0 — One-Time Setup (Run Once Per Machine)

These commands configure your environment so that pods inside the Kubernetes cluster
can reach the kRAG server running on your Windows host. Run them once — they persist
across experiment runs.

### 0a — Detect the Windows Host IP (as seen from inside the cluster)

Docker Desktop on Windows exposes the host at a special IP address that pods
inside the kind cluster can route to. This IP is **not** `127.0.0.1` (that's
the container itself) and **not** `host.docker.internal` (that DNS name only
works from Docker containers, not from Kubernetes pods).

Run this script to detect the correct IP and update the experiment file automatically:

```powershell
cd "C:\Users\Mateusz\Desktop\kRAG-Kinetic-Retrieval-Augmented-Guardian"

# Find the control-plane node name (works for any cluster name)
$cpNode = kubectl get nodes -o jsonpath='{.items[0].metadata.name}'
Write-Host "Control-plane node: $cpNode"

# Resolve the Windows host IPv4 as seen from inside Docker Desktop
$ahosts = docker exec $cpNode getent ahostsv4 host.docker.internal
$hostIP = ($ahosts -split '\s+' | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1)
Write-Host "Detected Windows host IP: $hostIP"

# Update the experiment YAML with the correct IP
(Get-Content experiments\oom-selfheal-experiment.yaml) `
    -replace '(\d+\.\d+\.\d+\.\d+)(?=:8888)', $hostIP |
    Set-Content experiments\oom-selfheal-experiment.yaml

Write-Host "Updated experiments\oom-selfheal-experiment.yaml with IP: $hostIP"
```

Expected output:
```
Control-plane node: krag-control-plane
Detected Windows host IP: 192.168.65.254
Updated experiments\oom-selfheal-experiment.yaml with IP: 192.168.65.254
```

Verify the URL was updated correctly:
```powershell
Select-String "url:" experiments\oom-selfheal-experiment.yaml
# Expected: - url: "http://192.168.65.254:8888/webhook"
```

### 0b — Open Windows Firewall for Port 8888

Kubernetes pods send the webhook from inside Docker Desktop. Windows Firewall
blocks inbound connections from Docker's virtual network by default.

```powershell
# Run as Administrator
New-NetFirewallRule `
    -DisplayName "kRAG Webhook (Docker Desktop)" `
    -Direction Inbound `
    -LocalPort 8888 `
    -Protocol TCP `
    -Action Allow `
    -ErrorAction SilentlyContinue

Write-Host "Firewall rule created (or already exists)"
```

To verify the rule exists:
```powershell
Get-NetFirewallRule -DisplayName "kRAG Webhook (Docker Desktop)" | Select-Object DisplayName, Enabled, Direction
```

### 0c — Patch Alertmanager to Discover Cross-Namespace Configs

By default, the Prometheus Operator only looks for `AlertmanagerConfig` resources
in the same namespace as Alertmanager (`monitoring`). This patch tells it to
discover configs from **all** namespaces — which is required because our config
lives in `krag-demo`.

```powershell
kubectl patch alertmanager kube-prometheus-kube-prome-alertmanager `
    -n monitoring `
    --type=merge `
    -p '{"spec":{"alertmanagerConfigSelector":{},"alertmanagerConfigNamespaceSelector":{}}}'
```

Expected output:
```
alertmanager.monitoring.coreos.com/kube-prometheus-kube-prome-alertmanager patched
```

Verify Alertmanager restarted and picked up the change:
```powershell
kubectl rollout status statefulset/alertmanager-kube-prometheus-kube-prome-alertmanager -n monitoring
# Expected: statefulset rolling update complete 1 pods at revision ...
```

---

## Step 1 — Start kRAG Services

kRAG runs directly on your machine with `uv` (not in Docker).
You need **two services running before the experiment**: ChromaDB and kRAG itself.

### 1a — Start ChromaDB (Docker)

ChromaDB is the vector database. Start only the ChromaDB container:

```powershell
cd "C:\Users\Mateusz\Desktop\kRAG-Kinetic-Retrieval-Augmented-Guardian"

# Start only the ChromaDB container (not the full compose stack)
docker run -d --name krag-chroma -p 8000:8000 `
    -e IS_PERSISTENT=TRUE `
    -e PERSIST_DIRECTORY=/chroma/data `
    -v "${PWD}/chroma_data:/chroma/data" `
    chromadb/chroma:latest

# Verify it is running
docker ps | Select-String "chroma"

# Test ChromaDB is responding
Invoke-RestMethod -Uri http://localhost:8000/api/v1/heartbeat
# Expected: {"nanosecond heartbeat": <number>}
```

If ChromaDB is already running (from a previous session):
```powershell
docker start krag-chroma
```

### 1b — Start kRAG (uvicorn — THIS IS YOUR LOG TERMINAL)

Open a **dedicated PowerShell terminal** for kRAG and leave it open.
All kRAG log output — including the full LLM prompts, responses, and actions —
will print directly here. **This is the most important terminal in the experiment.**

```powershell
cd "C:\Users\Mateusz\Desktop\kRAG-Kinetic-Retrieval-Augmented-Guardian"

# Start the kRAG FastAPI server with live reload
uv run uvicorn api.server:app --host 0.0.0.0 --port 8888 --reload
```

Expected startup output:
```
INFO:     Will watch for changes in these directories: [...]
INFO:     Uvicorn running on http://0.0.0.0:8888 (Press CTRL+C to quit)
INFO:     Started reloader process [XXXX] using WatchFiles
INFO     Loaded local config (~/.kube/config)
INFO:     Application startup complete.
```

**Leave this terminal open and visible throughout the experiment.**

Test that kRAG is responding (from any other terminal):
```powershell
Invoke-RestMethod -Uri http://localhost:8888/health
# Expected: {"status":"ok","service":"krag-webhook"}
```

> **Optional — save logs to a file:** If you want to search through the logs
> after the experiment, restart kRAG with `Tee-Object` to write output to a file
> at the same time:
> ```powershell
> uv run uvicorn api.server:app --host 0.0.0.0 --port 8888 --reload 2>&1 | Tee-Object -FilePath krag-experiment.log
> ```
> You can then filter the saved file with the commands in the **Additional Monitoring** section.

**If kRAG fails to start**, common causes:
- `ModuleNotFoundError`: run `uv sync` first to install dependencies
- Kubeconfig not found: ensure `~/.kube/config` exists and your cluster is running
- Ollama not reachable: ensure `ollama serve` is running on port 11434
- ChromaDB not reachable: ensure ChromaDB container is started (step 1a above)
- Port 8888 already in use: stop anything else on that port

---

## Step 2 — Apply the Prometheus Alert Rule

This creates the `PodCrashLooping` rule that triggers the alert chain:

```powershell
kubectl apply -f config/alert-rule.yaml
```

Expected output:
```
prometheusrule.monitoring.coreos.com/krag-demo-alerts created
```

**Verify Prometheus picked up the rule** (wait 1-2 minutes after applying):

```powershell
# Start port-forwarding to Prometheus in a separate terminal
kubectl port-forward -n monitoring svc/kube-prometheus-kube-prome-prometheus 9090:9090
```

Then open in browser: **http://localhost:9090/rules**

Look for the group `krag-demo.rules` with rule `PodCrashLooping`.
Status should be `inactive` (no pods running yet).

If the rule does NOT appear after 2 minutes, the `release: kube-prometheus`
label may not match your Prometheus Operator selector. Check:
```powershell
kubectl get prometheus -n monitoring -o yaml | Select-String -Pattern "ruleSelector" -Context 0,5
```
Then edit `config/alert-rule.yaml` labels to match.

**Verify the Alertmanager webhook config is applied (lives in krag-demo namespace):**
```powershell
kubectl get alertmanagerconfig -n krag-demo
# Should show: krag-receiver
# Note: if the experiment YAML has not been applied yet, run Step 4 first,
# then come back to verify here.
```

---

## Step 3 — Open Monitoring Terminals

Open 5 PowerShell terminals in the project directory. Arrange them side-by-side
(Windows Terminal with tabs works great here).

### Terminal A — Pod Status Watcher
```powershell
# Watches pod status in real-time (updates on every change)
kubectl get pods -n krag-demo -w

# Alternative (clears and refreshes every 3 seconds):
while ($true) { Clear-Host; kubectl get pods -n krag-demo -o wide; Write-Host ""; Write-Host "Updated: $(Get-Date -Format 'HH:mm:ss')"; Start-Sleep 3 }
```

### Terminal B — Pod Logs (memory leak in action)
```powershell
# Follow live logs from the crashing pod
kubectl logs -n krag-demo crasher-0 -f

# Run this AFTER the pod crashes to see its final log output:
kubectl logs -n krag-demo crasher-0 --previous
```

### Terminal C — kRAG Agent Logs (THE MOST IMPORTANT WINDOW)

**This is the uvicorn terminal from Step 1b** — the one where you ran
`uv run uvicorn api.server:app ...`. All kRAG log output prints there directly.

Keep that terminal visible. You will see the full LLM prompt, the model's
reasoning, the chosen action, execution result, and verification — all in
chronological order as the experiment unfolds.

### Terminal D — Prometheus UI (alert status)
```powershell
kubectl port-forward -n monitoring svc/kube-prometheus-kube-prome-prometheus 9090:9090
# Keep this running. Open in browser: http://localhost:9090/alerts
```

### Terminal E — Alertmanager UI (webhook routing status)
```powershell
kubectl port-forward -n monitoring svc/kube-prometheus-kube-prome-alertmanager 9093:9093
# Keep this running. Open in browser: http://localhost:9093/#/alerts
```

---

## Step 4 — Deploy the Experiment

In a new terminal (or any available terminal), run:

```powershell
kubectl apply -f experiments/oom-selfheal-experiment.yaml
```

Expected output:
```
namespace/krag-demo created
resourcequota/krag-demo-quota created
statefulset.apps/crasher created
service/crasher created
alertmanagerconfig.monitoring.coreos.com/krag-receiver created
```

**Immediately switch to Terminal A** — you should see `crasher-0` appear in state `ContainerCreating`, then transition to `Running`.

---

## Step 5 — Watch the Experiment Unfold

The experiment follows this timeline automatically. You do not need to trigger anything.

### T+0:00 — Pod Starts (Terminal A)
```
NAME        READY   STATUS    RESTARTS   AGE
crasher-0   1/1     Running   0          5s
```

### T+0:00 — Memory Leak Begins (Terminal B)
```
==================================================
  kRAG OOM Demo Worker v1.0
  Intentional memory leak — experiment target
==================================================
PID         : 1
Start time  : 2024-01-15 14:30:00 UTC
Leak rate   : 2 MB/second
Memory limit: 128 MiB (will OOMKill at ~50-60s)

Starting leaky work loop...
WARNING: This pod WILL be OOMKilled intentionally!

[14:30:01] [MEMLEAK] Memory used: ~2MB | Elapsed: 1s | Status: RUNNING
[14:30:02] [MEMLEAK] Memory used: ~4MB | Elapsed: 2s | Status: RUNNING
[14:30:03] [MEMLEAK] Memory used: ~6MB | Elapsed: 3s | Status: RUNNING
[14:30:10] [MEMLEAK] Memory used: ~22MB | Elapsed: 11s | Status: RUNNING
[14:30:20] [MEMLEAK] Memory used: ~42MB | Elapsed: 21s | Status: RUNNING
[14:30:30] [MEMLEAK] Memory used: ~62MB | Elapsed: 31s | Status: RUNNING
[14:30:40] [MEMLEAK] Memory used: ~82MB | Elapsed: 41s | Status: RUNNING
[14:30:50] [MEMLEAK] Memory used: ~102MB | Elapsed: 51s | Status: RUNNING
# (no more output — OOMKill is abrupt, the process gets SIGKILL)
```

### T+~1:00 — First OOMKill (Terminal A)
```
NAME        READY   STATUS      RESTARTS   AGE
crasher-0   0/1     OOMKilled   0          58s
# → Kubernetes schedules restart with 10s back-off
crasher-0   0/1     CrashLoopBackOff   1   70s
crasher-0   1/1     Running            1   80s
# Pod starts again, leaks again...
```

Check the crash reason at any point:
```powershell
kubectl describe pod crasher-0 -n krag-demo
```
Look for `Last State: Terminated | Reason: OOMKilled`.

To see logs from the previous (crashed) container:
```powershell
kubectl logs -n krag-demo crasher-0 --previous
```

### T+~4:00 — CrashLoopBackOff Builds Up, Alert Fires (Terminal D)

After 3 restarts (~4 minutes), Prometheus evaluates the rule and it enters PENDING.
After 1 more minute, the alert becomes FIRING.

**In Prometheus UI (http://localhost:9090/alerts):**
```
PodCrashLooping   FIRING
Labels:    alertname="PodCrashLooping" namespace="krag-demo" pod="crasher-0"
           container="memory-worker" severity="critical"
Value:     3 (restart count)
```

**In Alertmanager UI (http://localhost:9093/#/alerts):**
```
PodCrashLooping [1 active]
  pod=crasher-0, namespace=krag-demo, severity=critical
  → routed to: krag-webhook → http://<HOST_IP>:8888/webhook
```

### T+~5:00 — kRAG Receives Alert (Terminal C)
```
INFO krag.server: Received 1 alerts, 1 firing
INFO krag.server: Queuing repair for: PodCrashLooping / crasher-0
```

### T+~5:00 — kRAG Node 1: Fetch Logs
```
INFO src.agent.graph: [krag] START — alert: PodCrashLooping / pod: crasher-0
INFO src.agent.graph: [fetch_logs] Pod: crasher-0 / namespace: krag-demo
```

### T+~5:05 — kRAG Node 2: Query RAG (ChromaDB)
```
INFO src.agent.graph: [query_rag] Searching for similar incidents for: PodCrashLooping ...
INFO src.agent.graph: [query_rag] Found: 0 incidents, 1 runbooks
```

(On first run, ChromaDB has no past incidents. It may find OOM runbooks if previously ingested.)

### T+~5:10 — kRAG Node 3: LLM Reasoning (THE KEY PART)
```
INFO src.agent.graph: [reason] Sending prompt to LLM...
INFO src.agent.graph: [reason] === PROMPT SENT TO LLM ===
You are an autonomous SRE agent. You are analyzing an incident in the Kubernetes cluster.
...
## Alert:
- Type: PodCrashLooping
- Pod: crasher-0
- Namespace: krag-demo
- Description: Pod crasher-0 in namespace krag-demo has restarted 3 times...
...
## Pod Logs (last 100 lines):
[14:30:50] [MEMLEAK] Memory used: ~102MB | Elapsed: 51s | Status: RUNNING
...
## Pod Status:
{"phase": "Running", "containers": [{"restart_count": 3, "last_state": "OOMKilled"}]}
...
=== END PROMPT ===
```

**Waiting for LLM response (30 seconds to 5 minutes depending on your hardware)...**

```
INFO src.agent.graph: [reason] === LLM RAW RESPONSE ===
{
  "root_cause": "The pod crasher-0 is being OOMKilled due to a memory leak.
                 The memory usage grows continuously at ~2MB/second until it
                 hits the 128Mi container limit, causing the kernel to kill
                 the process. This results in CrashLoopBackOff.",
  "action": "delete_pod",
  "target": "crasher-0",
  "namespace": "krag-demo",
  "reasoning": "Deleting the pod forces Kubernetes to recreate it via the
                StatefulSet controller. The fresh pod starts with zero
                accumulated memory, allowing it to run normally until the
                leak fills up again. This buys time while the underlying
                memory leak is investigated and fixed."
}
=== END RESPONSE ===

INFO src.agent.graph: [reason] === ACTION PLAN ===
  root_cause : The pod crasher-0 is being OOMKilled due to a memory leak...
  action     : delete_pod
  target     : crasher-0
  namespace  : krag-demo
  reasoning  : Deleting the pod forces Kubernetes to recreate it...
=== END PLAN ===
```

### T+~6:xx — kRAG Node 4: Execute Action
```
INFO src.agent.graph: [execute] Executing: delete_pod on crasher-0 (krag-demo)
INFO src.agent.graph: [execute] Result: Pod crasher-0 deleted. K8s will recreate it automatically.
```

**In Terminal A — pod is deleted and immediately recreated:**
```
NAME        READY   STATUS              RESTARTS   AGE
crasher-0   0/1     Terminating         3          6m02s
crasher-0   0/1     ContainerCreating   0          2s
crasher-0   1/1     Running             0          7s
```

Notice: `RESTARTS: 0` — this is a brand new pod, restart counter reset by StatefulSet.

### T+~6:xx — kRAG Node 5: Verify (30-second wait)
```
INFO src.agent.graph: [verify] Waiting 30s for stabilization...
```

**During this 30 seconds** — watch Terminal B. The fresh pod runs normally:
```
==================================================
  kRAG OOM Demo Worker v1.0
  Intentional memory leak — experiment target
==================================================
PID         : 1
Start time  : 2024-01-15 14:36:07 UTC
...
[14:36:08] [MEMLEAK] Memory used: ~2MB | Elapsed: 1s | Status: RUNNING
[14:36:09] [MEMLEAK] Memory used: ~4MB | Elapsed: 2s | Status: RUNNING
[14:36:10] [MEMLEAK] Memory used: ~6MB | Elapsed: 3s | Status: RUNNING
```

Only ~60MB used after 30 seconds — well under the 128Mi limit. Pod is healthy.

### T+~7:00 — kRAG Verification Passes
```
INFO src.agent.graph: [verify] Asking LLM to confirm recovery...
INFO src.agent.graph: [verify] === LLM VERIFICATION RESPONSE ===
{
  "success": true,
  "reason": "The pod crasher-0 is now in Running state with 0 restarts.
             The delete_pod action successfully created a fresh instance
             via the StatefulSet controller. Memory usage is within normal
             range. The immediate crisis is resolved.",
  "next_action": null
}
=== END ===

INFO src.agent.graph: [verify] === VERIFICATION RESULT ===
  pod_healthy (k8s): True
  llm_success      : True
  reason           : The pod crasher-0 is now in Running state...
  next_action      : None
=== END RESULT ===
```

### T+~7:10 — kRAG Node 6: Store in ChromaDB
```
INFO src.agent.graph: [store_memory] === LLM SUMMARY RESPONSE ===
{
  "problem": "Pod crasher-0 in krag-demo was OOMKilled repeatedly due to a
              memory leak, causing CrashLoopBackOff with 3+ restarts.",
  "solution": "Deleted the pod to force StatefulSet recreation. Fresh pod
               starts with clean memory state, bypassing CrashLoopBackOff
               back-off delay. Pod immediately returned to healthy Running state."
}
=== END ===

INFO src.agent.graph: [store_memory] === INCIDENT STORED IN CHROMADB ===
  doc_id  : incident_20240115_143710
  problem : Pod crasher-0 in krag-demo was OOMKilled repeatedly...
  solution: Deleted the pod to force StatefulSet recreation...
=== END ===

INFO src.agent.graph: [krag] END — success: True
```

**The experiment is complete.**

---

## Step 6 — Confirm Resolution with Kubernetes Commands

After kRAG reports success, run these to confirm the cluster state:

```powershell
# Pod should be Running with 0 restarts
kubectl get pods -n krag-demo

# Confirm it's actually healthy (Running + Ready)
kubectl describe pod crasher-0 -n krag-demo | Select-String -Pattern "Status:|Ready:|Restart|OOM"

# Check events for the namespace (shows delete + recreate)
kubectl get events -n krag-demo --sort-by='.lastTimestamp'
```

Expected output:
```
NAME        READY   STATUS    RESTARTS   AGE
crasher-0   1/1     Running   0          2m
```

Events will show:
```
REASON    MESSAGE
Killing   Stopping container memory-worker
Pulled    Successfully pulled image "python:3.11-slim"
Created   Created container memory-worker
Started   Started container memory-worker
```

---

## Additional Monitoring Commands

### Check past crash logs (OOMKill evidence)
```powershell
# Logs from before the latest restart
kubectl logs -n krag-demo crasher-0 --previous

# List all containers and their crash history
kubectl describe pod crasher-0 -n krag-demo
```

### Check Prometheus metrics directly
```powershell
# Query restart count for crasher-0 (in Prometheus browser UI or via API)
# Open http://localhost:9090 and paste this PromQL:
# kube_pod_container_status_restarts_total{namespace="krag-demo", pod="crasher-0"}

# Or via PowerShell:
$query = 'kube_pod_container_status_restarts_total{namespace="krag-demo"}'
$encoded = [uri]::EscapeDataString($query)
Invoke-RestMethod -Uri "http://localhost:9090/api/v1/query?query=$encoded" | ConvertTo-Json -Depth 5
```

### Check ChromaDB for stored incidents
```powershell
# List all ChromaDB collections
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/collections" | ConvertTo-Json

# Count incidents stored by kRAG
$col = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/collections/krag_incidents"
Write-Host "Stored incidents: $($col.id)"
```

### Filter kRAG logs (if you used Tee-Object in Step 1b)

```powershell
# See only the alert receipt and queuing
Select-String "Received|Queuing|START|END|success" krag-experiment.log

# See the full LLM conversation (prompt + response) — shows the AI reasoning
Select-String "PROMPT|RESPONSE|PLAN|RESULT" krag-experiment.log -Context 0,30

# See only errors
Select-String "ERROR|WARNING|Exception" krag-experiment.log

# See timestamps of key workflow steps
Select-String "\[krag\]|\[reason\]|\[execute\]|\[verify\]|\[store" krag-experiment.log
```

### Test kRAG manually without Prometheus (instant trigger)
```powershell
# Send a test alert directly to kRAG (bypasses Prometheus/Alertmanager entirely)
# This is useful to verify kRAG is working before the experiment
$body = @{
    alertname = "PodCrashLooping"
    pod       = "crasher-0"
    namespace = "krag-demo"
    severity  = "critical"
    description = "Manual test: Pod crasher-0 restarted 3 times due to OOMKill"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8888/test" -Method Post `
    -ContentType "application/json" -Body $body
```

This endpoint runs the full kRAG workflow synchronously and returns the result immediately.

---

## If Something Goes Wrong

### Problem: Prometheus rule not appearing at http://localhost:9090/rules

The `release: kube-prometheus` label may not match your installation.
Check what selector your Prometheus uses:
```powershell
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}' | ConvertTo-Json
```
Then edit `config/alert-rule.yaml` labels to match, and re-apply.

### Problem: Alert fires but kRAG does not receive it

**Check 1 — AlertmanagerConfig is applied in krag-demo:**
```powershell
kubectl get alertmanagerconfig -n krag-demo krag-receiver -o yaml
```
It should show `url: "http://<HOST_IP>:8888/webhook"` with the IP from Step 0.

**Check 2 — Alertmanager was patched to discover cross-namespace configs:**
```powershell
kubectl get alertmanager kube-prometheus-kube-prome-alertmanager -n monitoring -o jsonpath='{.spec.alertmanagerConfigSelector}' | ConvertTo-Json
# Expected: {} (empty = match all)
```
If not patched, re-run Step 0c.

**Check 3 — Webhook URL is reachable from inside the cluster:**
```powershell
# Find the host IP that was used in the experiment YAML
Select-String "url:" experiments\oom-selfheal-experiment.yaml

# Test reachability from a pod inside the cluster
$hostIP = (Select-String 'url: "http://(\d+\.\d+\.\d+\.\d+)' experiments\oom-selfheal-experiment.yaml).Matches.Groups[1].Value
kubectl run curl-test --image=curlimages/curl --restart=Never --rm -it -- `
    curl -s "http://${hostIP}:8888/health"
# Expected: {"status":"ok","service":"krag-webhook"}
```

If the curl fails with "Connection refused", re-run Step 0b (firewall rule).
If the curl fails with "No route to host", re-run Step 0a (wrong IP was used).

**Check 4 — Send webhook manually to confirm kRAG is working:**
```powershell
$body = '{"alerts":[{"labels":{"alertname":"PodCrashLooping","pod":"crasher-0","namespace":"krag-demo","severity":"critical"},"annotations":{"description":"test"},"status":"firing"}],"version":"4","groupKey":"test"}'
Invoke-RestMethod -Uri http://localhost:8888/webhook -Method Post -ContentType "application/json" -Body $body
```

### Problem: LLM not responding / kRAG hangs at "Sending prompt to LLM..."

```powershell
# Check Ollama is running
Invoke-RestMethod -Uri http://localhost:11434/api/tags

# Pull the model if missing
ollama pull llama3.2

# Test LLM directly (kRAG uses localhost:11434, same as your terminal)
$body = '{"model":"llama3.2","prompt":"Say hello","stream":false}'
Invoke-RestMethod -Uri http://localhost:11434/api/generate -Method Post -ContentType "application/json" -Body $body
# This can take 30 seconds to several minutes depending on your hardware
```

### Problem: Pod recovers before kRAG can verify

If Ollama is very slow (> 3 minutes per LLM call), the pod may OOM again before
verification. This manifests as `success: False` and a retry.

kRAG will retry up to 2 times (MAX_RETRIES = 2 in graph.py). On each retry
it fetches fresh logs, re-reasons, and re-executes. You will see:
```
[edge] Retry 1/2
[reason] Sending prompt to LLM...
```

If all retries fail, the agent gives up and logs:
```
[edge] Exceeded MAX_RETRIES — giving up
[krag] END — success: False
```

This is expected behaviour for cases where the underlying problem cannot be
solved by the available actions (delete, restart, scale).

### Problem: kRAG reports success but pod is still crashing

This is actually correct: kRAG declares success at T+30s after the action,
when the fresh pod has only used ~60MB (well under 128Mi). The pod will
eventually OOM again because the memory leak is still in the code. kRAG has
done its job (bought time, logged the incident), but the root fix must come
from a developer fixing the leak.

---

## Step 7 — Cleanup

```powershell
# Remove all experiment resources from Kubernetes
kubectl delete -f experiments/oom-selfheal-experiment.yaml

# Remove the Prometheus alert rule
kubectl delete -f config/alert-rule.yaml

# Verify krag-demo namespace is gone
kubectl get namespace krag-demo
# Expected: Error from server (NotFound): namespaces "krag-demo" not found

# Stop kRAG: press Ctrl+C in the uvicorn terminal (Step 1b)

# Stop ChromaDB container
docker stop krag-chroma

# Optional: delete the log file if you used Tee-Object in Step 1b
Remove-Item krag-experiment.log -ErrorAction SilentlyContinue
```

---

## Run the Experiment Again (Second Run)

On the second run, ChromaDB already contains the incident from the first run.
You will see kRAG's RAG step return past incidents:

```
[query_rag] Found: 1 incidents, 1 runbooks
```

The LLM prompt will include the past incident as context, and you will see
the model reference it in its reasoning:

```json
{
  "root_cause": "OOMKill due to memory leak (seen before in incident_20240115_143710)",
  "action": "delete_pod",
  "reasoning": "Previously resolved by pod deletion. Applying same strategy."
}
```

This demonstrates the **learning loop**: kRAG gets better with each incident.

---

## Summary of Log Locations

| What you want to see | Where to look |
|---------------------|---------------|
| Pod failing (memory leak) | `kubectl logs -n krag-demo crasher-0 -f` |
| Pod killed (OOMKill evidence) | `kubectl logs -n krag-demo crasher-0 --previous` |
| Pod restart/recovery | `kubectl get pods -n krag-demo -w` |
| Alert firing | http://localhost:9090/alerts |
| Alert routed to kRAG | http://localhost:9093/#/alerts |
| kRAG receives alert | uvicorn terminal → "Received 1 alerts" |
| kRAG fetches K8s logs | uvicorn terminal → "[fetch_logs]" |
| RAG knowledge search | uvicorn terminal → "[query_rag] Found" |
| LLM prompt (full text) | uvicorn terminal → "=== PROMPT SENT TO LLM ===" |
| LLM reasoning response | uvicorn terminal → "=== LLM RAW RESPONSE ===" |
| Action plan decided | uvicorn terminal → "=== ACTION PLAN ===" |
| Remediation executed | uvicorn terminal → "[execute] Executing:" |
| Recovery verification | uvicorn terminal → "=== VERIFICATION RESULT ===" |
| Incident stored in RAG | uvicorn terminal → "=== INCIDENT STORED IN CHROMADB ===" |
| Final outcome | uvicorn terminal → "[krag] END — success: True/False" |
