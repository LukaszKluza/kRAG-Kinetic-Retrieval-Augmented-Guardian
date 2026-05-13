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
│   ├── ollama.yaml                  # Deployment + Service Ollamy w klastrze K8s
│   ├── model-config.yaml            # ModelConfig — łączy kagent z Ollamą
│   ├── alert-rule.yaml              # PrometheusRule — reguła alertu CrashLoopBackOff
│   ├── kagent.yaml                  # Agent CRD — definicja agenta kRAG
│   └── alertmanager-receiver.yaml   # AlertmanagerConfig — webhook do krag-agenta
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
├── docker-compose.yaml      # ChromaDB + krag-agent (tryb produkcyjny)
├── Dockerfile
└── pyproject.toml
```

---

## CZĘŚĆ 1 — Wspólna infrastruktura K8s

> Wykonujesz **raz**. Po zatrzymaniu klastra i wznowieniu (`docker unpause`) nie musisz tych kroków powtarzać — stan klastra jest zachowany.

### Krok 1 — Utwórz klaster

```bash
kind create cluster --name krag
kubectl cluster-info --context kind-krag
kubectl get nodes
# Oczekiwany wynik: 1 node w statusie Ready
```

### Krok 2 — Zainstaluj kagent CLI i framework

> **Uwaga:** kagent domyślnie wymaga `OPENAI_API_KEY`. Ponieważ używamy własnego
> ModelConfig z Ollamą (Krok 5), wystarczy ustawić placeholder — klucz nie będzie używany.

```bash
# Windows (PowerShell)
$env:OPENAI_API_KEY = "placeholder"

# Linux/macOS
export OPENAI_API_KEY=placeholder

# Zainstaluj framework do klastra
kagent install --profile minimal

kubectl get pods -n kagent
# Oczekiwany wynik: kagent-controller i kagent-ui w statusie Running
```

### Krok 3 — Zainstaluj Prometheus + Alertmanager

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus prometheus-community/kube-prometheus-stack --namespace monitoring --create-namespace

kubectl get pods -n monitoring -w
# Poczekaj aż wszystkie pody będą Running (~2 min)
```

### Krok 4 — Deploy Ollamy do klastra (dla kagent)

```bash
kubectl create namespace ollama
kubectl apply -f config/ollama.yaml

kubectl get pods -n ollama -w
# Poczekaj na init container — pobiera model llama3.2 (~4.7 GB, może trwać kilka minut)
# Oczekiwany wynik: ollama-... w statusie Running
```

```bash
# Sprawdź postęp pobierania:
kubectl describe pod <NAME> -n ollama
```

### Krok 5 — Zastosuj konfigurację modelu

```bash
kubectl apply -f config/model-config.yaml

kubectl get modelconfig -n kagent
# Oczekiwany wynik: llama3-local z providerem Ollama
```

### Krok 6 — Zastosuj reguły alertów

```bash
kubectl apply -f config/alert-rule.yaml

# Weryfikacja — odpal port-forward i sprawdź w przeglądarce:
kubectl port-forward -n monitoring svc/kube-prometheus-kube-prome-prometheus 9090:9090
# http://localhost:9090 → Status → Rules → szukaj "PodCrashLooping"
```

### Krok 7 — Deploy agenta kRAG (CRD)

```bash
kubectl apply -f config/kagent.yaml

kubectl get agent -n kagent
# Oczekiwany wynik: krag-agent w statusie Ready
```

### Krok 8 — Skonfiguruj Alertmanager → webhook

Plik `config/alertmanager-receiver.yaml` jest już gotowy w projekcie. Definiuje odbiorcę,
który wysyła alerty `PodCrashLooping` do krag-agenta na porcie 8888.

**Znajdź IP hosta widoczne z klastra kind:**

```bash
# Windows / macOS (Docker Desktop) — host.docker.internal działa automatycznie, pomiń ten krok

# Linux — pobierz gateway IP sieci kind:
docker network inspect kind | grep Gateway
# Wpisz ten IP w config/alertmanager-receiver.yaml zamiast host.docker.internal
```

```bash
kubectl apply -f config/alertmanager-receiver.yaml

# Weryfikacja:
kubectl get alertmanagerconfig -n monitoring
```

### Krok 9 — Otwórz UI kagent (opcjonalne)

```bash
kubectl port-forward -n kagent svc/kagent-ui 8080:8080
# http://localhost:8080
```

---

## CZĘŚĆ 2A — Tryb deweloperski (zalecany)

> Kod działa lokalnie z hot-reload. ChromaDB w Docker, Ollama lokalnie.
> Wybierz **albo** tryb deweloperski, **albo** Docker (Część 2B) — nie oba jednocześnie.

### Pierwsze uruchomienie

```bash
# 1. Pobierz modele Ollama (raz)
ollama pull llama3.2
ollama pull nomic-embed-text

# 2. Zainstaluj zależności Python (raz)
uv sync

# 3. Uruchom ChromaDB w tle
docker-compose up chromadb -d

# 4. Załaduj runbooki do bazy (raz — lub po dodaniu nowych runbooków)
uv run python src/data/ingest_docs.py

# 5. Uruchom serwer webhook
# Windows (PowerShell):
$env:PYTHONPATH = "src"
uv run uvicorn api.server:app --host 0.0.0.0 --port 8888 --reload

# Linux/macOS:
PYTHONPATH=src uv run uvicorn api.server:app --host 0.0.0.0 --port 8888 --reload
```

### Kolejne uruchomienia

```bash
docker-compose up chromadb -d

# Windows (PowerShell):
$env:PYTHONPATH = "src"
uv run uvicorn api.server:app --host 0.0.0.0 --port 8888 --reload

# Linux/macOS:
PYTHONPATH=src uv run uvicorn api.server:app --host 0.0.0.0 --port 8888 --reload
```

### Zatrzymanie

```bash
# Ctrl+C w terminalu z uvicorn
docker-compose stop chromadb
```

> Po zatrzymaniu port-forwardingów w konsolach, musisz odpalić odpowienie komendy ponownie przy restarcie.


### Restart agenta (po zmianie kodu)

Hot-reload (`--reload`) restartuje serwer automatycznie przy każdej zmianie pliku.
Jeśli trzeba ręcznie: Ctrl+C, a następnie ponowne uruchomienie serwera.

---

## CZĘŚĆ 2B — Tryb produkcyjny (Docker Compose)

> Cały stack (ChromaDB + krag-agent) w kontenerach.
> Wybierz **albo** ten tryb, **albo** deweloperski (Część 2A) — nie oba jednocześnie.

### Pierwsze uruchomienie

```bash
# Zbuduj i uruchom kontenery
docker-compose up --build -d

# Załaduj runbooki do bazy (raz)
docker exec -it krag-agent uv run python src/data/ingest_docs.py
```

### Kolejne uruchomienia

```bash
docker-compose up -d
```

### Zatrzymanie

```bash
docker-compose down
```

### Restart po zmianie kodu

```bash
docker-compose up --build -d
```

### Restart tylko agenta (bez przebudowy)

```bash
docker-compose restart krag-agent
```

---

## Klaster K8s — cykl życia

```bash
# Zatrzymaj klaster (dane zachowane, nie trzeba ponownie konfigurować)
docker pause krag-control-plane

# Wznów klaster
docker unpause krag-control-plane

# Sprawdź stan po wznowieniu
kubectl get pods --all-namespaces

# Usuń klaster całkowicie (wymaga ponownego przejścia przez Część 1)
kind delete cluster --name krag
```

---

## Testowanie działania

### Test manualny — zasymuluj crashujący pod

```bash
kubectl run crash-test --image=busybox --restart=Always -- /bin/sh -c "exit 1"

kubectl get pods -w
# Oczekiwany wynik: crash-test w CrashLoopBackOff
```

### Wywołanie agenta bezpośrednio (kagent CLI)

```bash
# Windows Linux/macOS
kagent invoke --agent krag-agent -t "Pod crash-test w namespace default ciągle się restartuje. Przeanalizuj i napraw."
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
| Ollama (K8s) nie startuje | `kubectl logs -n ollama -l name=ollama -c model-puller` |
| ModelConfig nie Ready | `kubectl describe modelconfig llama3-local -n kagent` |
| Prometheus nie widzi alertu | `kubectl describe prometheusrule krag-pod-alert -n monitoring` |
| Alertmanager nie wysyła webhooków | `kubectl describe alertmanagerconfig krag-receiver -n monitoring` |
| kagent UI niedostępne | `kubectl get svc -n kagent` |
| ChromaDB niedostępne | `docker logs krag-chroma` |
| krag-agent (webhook) błędy | `docker logs krag-agent` |

### Szybki health check całego stacka

```bash
echo "=== kagent ===" && kubectl get pods -n kagent
echo "=== ollama ===" && kubectl get pods -n ollama
echo "=== monitoring ===" && kubectl get pods -n monitoring
echo "=== agent CRD ===" && kubectl get agent -n kagent
echo "=== model ===" && kubectl get modelconfig -n kagent
```

---

## Znane problemy (do naprawienia)

| Problem | Plik | Opis |
|---------|------|------|
| Brakujące zależności | `pyproject.toml` | Brak `langgraph`, `uvicorn`, `requests` jako bezpośrednich zależności |
| Wersja Pythona w Dockerfile | `Dockerfile` | Używa `python:3.11-slim`, projekt wymaga `>=3.13` |
