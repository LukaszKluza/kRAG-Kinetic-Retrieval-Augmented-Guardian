### Tworzenie przestrzeni nazw dla środowiska produkcyjnego
```shell
kubectl create namespace prod
```
### Nakładanie limitów CPU/RAM na poszczególne serwisy

```shell
kubectl apply -f config/frontend_limits.yaml
kubectl apply -f config/backend_limits.yaml
kubectl apply -f config/cache_limits.yaml
```

##### Zarządzanie Zasobami
Komenda do szybkiego czyszczenia środowiska wewnątrz namespace prod.

```shell
kubectl delete deployments,services --all -n prod
```

---

### Instalacja Metrics Server

```shell
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

#### ⚠️ Ważna Konfiguracja: Metrics Server

Aby poprawnie skonfigurować **Metrics Server** w środowiskach, gdzie certyfikaty Kubelet nie są podpisane przez zaufane CA (np. lokalne klastry deweloperskie, Minikube, Bare-metal), należy ręcznie zmodyfikować parametry deploymentu.

##### 🛠️ Krok 1: Otwarcie edycji zasobu
Wykonaj poniższe polecenie w terminalu, aby edytować deployment bezpośrednio na klastrze:

```bash
kubectl edit deployment metrics-server -n kube-system
```

##### 🛠️ Krok 2: Modyfikacja sekcji `args`

W otwartym edytorze odszukaj ścieżkę `spec.template.spec.containers` i przejdź do listy argumentów (`args`). Musisz tam dopisać flagę `--kubelet-insecure-tls`.

> [!CAUTION]
> **Pamiętaj o wcięciach:** Kubernetes (YAML) nie wybacza błędów w strukturze. Używaj wyłącznie **spacji**, nigdy tabulatorów!

#### 📝 Przykład poprawnej struktury:

```yaml
      containers:
      - name: metrics-server
        args:
          - --cert-dir=/tmp
          - --secure-port=4443
          - --kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname
          - --kubelet-use-node-status-port
          - --metric-resolution=15s
          # --- DODAJ TĘ LINIĘ PONIŻEJ ---
          - --kubelet-insecure-tls
```

---


##### Sprawdzanie zużycia zasobów w namespace prod
```shell
kubectl top pods -n prod
```

##### Szczegółowa diagnostyka konkretnego podu
```shell
kubectl top pod api-service-f4877cc4f-ljwml -n prod
kubectl describe pod api-service-f4877cc4f-ljwml -n prod
```

##### Weryfikacja etykiet (ważne dla selektorów w Chaos Mesh)
```shell
kubectl get pods -n prod --show-labels
kubectl get pods -n prod -l app=api-service
```

##### Dostęp do Panelu Grafana
Przekierowanie portu, aby uzyskać dostęp do wizualizacji metryk.

```shell
kubectl port-forward -n monitoring svc/kube-prometheus-grafana 3000:80
```


> [!INFO]
> **User**: admin
> **Password**: OvsKTFIjO7TwwQbeoLgY5P3Yjbd5Sk5HTQA88JMy

---

#### Chaos-mesh

##### Przygotowanie namespace

```shell
kubectl create namespace chaos-mesh
[OPT] curl.exe -sSL https://raw.githubusercontent.com/chaos-mesh/chaos-mesh/v2.8.1/install.sh -o config/install.sh
[OPT] bash install.sh --local kind
[OPT] bash install.sh --template --runtime containerd > config/chaos-mesh.yaml
kubectl apply -f https://mirrors.chaos-mesh.org/v2.8.1/crd.yaml --server-side
kubectl apply -f config/chaos-mesh.yaml --server-side
kubectl get pods -n chaos-mesh
```


##### Wykonywanie Testów Obciążeniowych (Stress)


##### Dostęp do Chaos Mesh'a
```shell
kubectl port-forward -n chaos-mesh svc/chaos-dashboard 2333:2333
```

##### Odpalenie scenariusza
```shell
kubectl apply -f intensive-load.yaml
```

##### Usunięcia scenariusza po wykonaniu
```
kubectl delete stresschaos intensive-load-test -n prod
```


> [!EXAMPLE]
> Przykładowe Query dla Prometheus's, wylicza ono średnie zużycie procesowa przez pod'y frontendowe w ciągu ostatniej minuty.
```shell
(
  avg by (pod) (rate(container_cpu_usage_seconds_total{namespace="prod", container="nginx"}[1m]))
  /
  avg by (pod) (kube_pod_container_resource_limits{namespace="prod", container="nginx", resource="cpu"})
) * 100
```