# kRAG — Instrukcja uruchomienia

> **Stack:** kind · kubectl · Helm · Ollama · kagent · Prometheus · Alertmanager  
> **Cel:** lokalny klaster K8s z autonomicznym agentem SRE reagującym na alerty

---

## Wymagania wstępne

| Narzędzie | Min. wersja | Instalacja |
|-----------|-------------|-----------|
| Docker Desktop | najnowsza | [docs.docker.com/desktop](https://docs.docker.com/desktop/) |
| kubectl | v1.28+ | [kubernetes.io/docs/tasks/tools](https://kubernetes.io/docs/tasks/tools/) |
| kind | v0.31.0 | [kind.sigs.k8s.io](https://kind.sigs.k8s.io/docs/user/quick-start/) |
| Helm | v3.x | [helm.sh](https://helm.sh/docs/intro/quickstart/) |
| Ollama (lokalnie) | najnowsza | [ollama.com](https://ollama.com) |
| kagent CLI | najnowsza | [kagent.dev](https://kagent.dev/docs/kagent/getting-started/quickstart) |
| Python | 3.13+ | [python.org](https://www.python.org/downloads/) |
| uv | najnowsza | `pip install uv` lub [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |

> **RAM:** Docker Desktop musi mieć przydzielone min. **12 GB RAM** (Settings → Resources).

---

## Struktura plików projektu

```
kRAG/
├── config/
│   ├── ollama.yaml          # Deployment + Service Ollamy w klastrze K8s
│   ├── model-config.yaml    # ModelConfig — łączy kagent z Ollamą
│   ├── alert-rule.yaml      # PrometheusRule — reguła alertu CrashLoopBackOff
│   └── kagent.yaml          # Agent CRD — definicja agenta kRAG
├── src/
│   ├── agent/
│   │   ├── graph.py         # LangGraph — główna logika agenta
│   │   ├── tools.py         # Narzędzia Kubernetes (python-kubernetes)
│   │   ├── rag.py           # ChromaDB — pamięć długoterminowa agenta
│   │   └── prompts.py       # Szablony promptów dla LLM
│   ├── api/
│   │   └── server.py        # FastAPI — endpoint /webhook dla Alertmanagera
│   └── data/
│       └── ingest_docs.py   # Skrypt ładujący runbooki do ChromaDB
├── docker-compose.yaml      # ChromaDB + krag-agent (webhook server)
├── Dockerfile
└── pyproject.toml
```

---

## Uruchomienie od zera (kolejność ma znaczenie)

### Krok 1 — Utwórz klaster

```bash
kind create cluster --name krag
kubectl cluster-info --context kind-krag
kubectl get nodes
# Oczekiwany wynik: 1 node w statusie Ready
```

### Krok 2 — Zainstaluj kagent

```bash
# Linux/macOS
curl https://raw.githubusercontent.com/kagent-dev/kagent/refs/heads/main/scripts/get-kagent | bash

# Windows — pobierz ręcznie z GitHub Releases:
# https://github.com/kagent-dev/kagent/releases
# Umieść kagent.exe w katalogu projektu (bin/) lub w PATH

# Zainstaluj framework do klastra (profil minimalny — bez domyślnych agentów)
kagent install --profile minimal

# Sprawdź że pody kagent działają
kubectl get pods -n kagent
# Oczekiwany wynik: kagent-controller i kagent-ui w statusie Running
```

### Krok 3 — Zainstaluj Prometheus + Alertmanager

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace

# Poczekaj aż wszystkie pody będą Running (~2 min)
kubectl get pods -n monitoring -w
```

### Krok 4 — Deploy Ollamy do klastra

```bash
kubectl create namespace ollama
kubectl apply -f config/ollama.yaml

# Poczekaj na init container (pobiera model llama3.2 ~4.7 GB — może trwać kilka minut)
kubectl get pods -n ollama -w
# Oczekiwany wynik: ollama-... w statusie Running
```

> Sprawdź postęp pobierania modelu:
> ```bash
> kubectl logs -n ollama -l name=ollama -c model-puller
> ```

### Krok 5 — Zastosuj konfigurację modelu

```bash
kubectl apply -f config/model-config.yaml

# Sprawdź czy ModelConfig jest gotowy
kubectl get modelconfig -n kagent
# Oczekiwany wynik: llama3-local z providerem Ollama
```

### Krok 6 — Zastosuj reguły alertów

```bash
kubectl apply -f config/alert-rule.yaml

# Sprawdź że Prometheus widzi regułę
kubectl port-forward -n monitoring svc/kube-prometheus-kube-prome-prometheus 9090:9090
# Otwórz http://localhost:9090 → Status → Rules → szukaj "PodCrashLooping"
```

### Krok 7 — Deploy agenta kRAG

```bash
kubectl apply -f config/kagent.yaml

# Sprawdź status agenta
kubectl get agent -n kagent
# Oczekiwany wynik: krag-agent w statusie Ready
```

### Krok 8 — Otwórz UI kagent (opcjonalne)

```bash
kubectl port-forward -n kagent svc/kagent-ui 8080:8080
# Otwórz http://localhost:8080
```

### Krok 9 — Uruchamienie Dockerem bazy oraz agenta

```bash
docker-compose up --build

# Uruchomienie skryptu zasilającego bazę wewnątrz kontenera (jeśli jeszcze nie był)
docker exec -it krag-agent uv run python src/data/ingest_docs.py
```

### Krok 10 — Uruchomienie Agenta (Tryb Deweloperski)

> **Wymaganie:** Ollama musi działać lokalnie (`ollama serve`).

```bash
# Pobierz modele potrzebne do działania agenta
ollama pull llama3.2
ollama pull nomic-embed-text   # model embeddingów — wymagany przez rag.py

# Zainstaluj zależności Python przez uv (projekt NIE używa pip/requirements.txt)
uv sync

# Załaduj runbooki do ChromaDB (uruchom raz przed pierwszym startem agenta)
uv run python src/data/ingest_docs.py

# Uruchom serwer webhook
# Windows (PowerShell):
$env:PYTHONPATH = "src"
uv run uvicorn api.server:app --host 0.0.0.0 --port 8888 --reload

# Linux/macOS:
PYTHONPATH=src uv run uvicorn api.server:app --host 0.0.0.0 --port 8888 --reload
```

### Krok 11 — Skonfiguruj Alertmanager → webhook (integracja)

Aby Alertmanager automatycznie wysyłał alerty do krag-agenta, utwórz receiver:

```yaml
# alertmanager-receiver.yaml
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: krag-receiver
  namespace: monitoring
  labels:
    alertmanagerConfig: krag
spec:
  route:
    receiver: krag-webhook
    matchers:
      - name: alertname
        value: PodCrashLooping
  receivers:
    - name: krag-webhook
      webhookConfigs:
        # Jeśli krag-agent działa w Docker Compose na hoście:
        # W kind użyj IP hosta (sprawdź: docker inspect krag-control-plane | grep Gateway)
        - url: "http://<HOST_IP>:8888/webhook"
```

```bash
kubectl apply -f alertmanager-receiver.yaml
```

> **Uwaga:** Adres IP hosta w klastrze kind sprawdzisz przez:
> ```bash
> docker inspect krag-control-plane | findstr Gateway   # Windows
> docker inspect krag-control-plane | grep Gateway      # Linux/macOS
> ```

---

## Testowanie działania

### Test manualny — zasymuluj crashujący pod

```bash
# Utwórz pod który zawsze się wysypuje
kubectl run crash-test --image=busybox \
  --restart=Always -- /bin/sh -c "exit 1"

# Obserwuj crashowanie
kubectl get pods -w
# Oczekiwany wynik: crash-test w CrashLoopBackOff
```

### Wywołanie agenta

```bash
# Windows
.\bin\kagent.exe invoke --agent krag-agent `
  -t "Pod crash-test w namespace default ciągle się restartuje. Przeanalizuj i napraw."

# Linux/macOS
kagent invoke --agent krag-agent \
  -t "Pod crash-test w namespace default ciągle się restartuje. Przeanalizuj i napraw."
```

### Test webhooka (bez Alertmanagera)

```bash
curl -X POST http://localhost:8888/test \
  -H "Content-Type: application/json" \
  -d '{"pod": "crash-test", "namespace": "default", "alertname": "PodCrashLooping", "description": "test"}'
```

### Sprzątanie po testach

```bash
kubectl delete pod crash-test
```

---

## Diagnostyka — co sprawdzić gdy coś nie działa

| Problem | Polecenie diagnostyczne |
|---------|------------------------|
| Agent nie odpowiada | `kubectl logs -n kagent deployment/kagent-controller` |
| Ollama nie startuje | `kubectl logs -n ollama -l name=ollama -c model-puller` |
| ModelConfig nie Ready | `kubectl describe modelconfig llama3-local -n kagent` |
| Prometheus nie widzi alertu | `kubectl describe prometheusrule krag-pod-alert -n monitoring` |
| kagent UI niedostępne | `kubectl get svc -n kagent` |
| ChromaDB niedostępne | `docker logs krag-chroma` |
| krag-agent (webhook) błędy | `docker logs krag-agent` |

### Szybki health check całego stacka

```bash
echo "=== kagent ===" && kubectl get pods -n kagent
echo "=== ollama ===" && kubectl get pods -n ollama
echo "=== monitoring ===" && kubectl get pods -n monitoring
echo "=== agent ===" && kubectl get agent -n kagent
echo "=== model ===" && kubectl get modelconfig -n kagent
```

---

## Zatrzymanie i wznowienie

```bash
# Zatrzymaj klaster (nie kasuje danych)
docker pause krag-control-plane

# Wznów klaster
docker unpause krag-control-plane

# Lub całkowite usunięcie klastra
kind delete cluster --name krag
```

---

## Znane problemy (do naprawienia)

| Problem | Plik | Opis |
|---------|------|------|
| Brakujące zależności | `pyproject.toml` | Brak `langgraph`, `uvicorn`, `requests` — `uv sync` może nie zainstalować ich jako bezpośrednich zależności |
| Wersja Pythona w Dockerfile | `Dockerfile` | Używa `python:3.11-slim`, podczas gdy projekt wymaga `>=3.13` |
