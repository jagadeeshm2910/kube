# kube

GitOps repository for a self-healing WordPress + MySQL stack on Kubernetes, managed by ArgoCD and monitored by KARA (Kubernetes Autonomous Remediation Agents).

---

## Architecture

```
GitHub (this repo)
       │
       │  poll every 3 min
       ▼
   ArgoCD
   ├── wordpress app  →  wp-dev namespace  (WordPress + MySQL + HPA)
   ├── kara app       →  kara namespace    (Watcher + Investigator + Analyst)
   └── monitoring app →  monitoring namespace (kube-prometheus-stack)

   KARA agents (running in kara namespace)
   ├── watcher     → streams K8s pod events cluster-wide
   ├── investigator → collects logs, events, deployment info on failures
   └── analyst     → receives investigation reports for diagnosis
```

---

## Repository Structure

```
kube/
├── app-cd.yaml          # ArgoCD Application for WordPress stack
├── kara-cd.yaml         # ArgoCD Application for KARA agents
├── monitoring-cd.yaml   # ArgoCD Application for Prometheus/Grafana
├── sealed-mysql-secret.yaml  # Encrypted MySQL credentials (Sealed Secrets)
├── k8s-app1/            # WordPress stack manifests
│   ├── wordpress.yaml       # WordPress Deployment + NodePort Service
│   ├── db-stateful.yaml     # MySQL StatefulSet + Headless Service
│   ├── hpa.yaml             # HorizontalPodAutoscaler (CPU 75%, 1–5 replicas)
│   └── scerets.yaml         # Plaintext secret (demo only, do not use in prod)
├── kara/                # KARA self-healing agent manifests
│   ├── namespace.yaml
│   ├── rbac.yaml            # ServiceAccounts + ClusterRoles
│   ├── analyst-deployment.yaml
│   ├── investigator-deployment.yaml
│   └── watcher-deployment.yaml
└── monitoring/
    └── values.yaml      # Helm values for kube-prometheus-stack
```

---

## How It Works

### Layer 1 — GitOps drift correction (ArgoCD)

ArgoCD polls this repo every 3 minutes. If the live cluster state drifts from what is in git (e.g. someone runs `kubectl edit` manually), ArgoCD automatically reverts it.

- `selfHeal: true` — reverts any manual cluster changes back to git state
- `prune: true` — removes resources that are deleted from git
- `retry` with exponential backoff — survives transient API errors

### Layer 2 — Pod health enforcement (Kubernetes probes)

Both WordPress and MySQL have liveness and readiness probes so Kubernetes can detect and automatically restart unhealthy pods.

| Pod | Probe type | Check |
|---|---|---|
| WordPress | `startupProbe` | GET `/wp-login.php` — allows 300s for boot |
| WordPress | `livenessProbe` | GET `/wp-login.php` every 20s — restarts if hung |
| WordPress | `readinessProbe` | GET `/wp-login.php` every 10s — removes from load balancer if not ready |
| MySQL | `livenessProbe` | `mysqladmin ping` every 20s — restarts if DB process hangs |
| MySQL | `readinessProbe` | `mysqladmin ping` every 10s — gates WordPress connectivity |

### Layer 3 — Autonomous incident detection (KARA)

KARA is a three-agent pipeline that detects pod failures cluster-wide and collects full diagnostics automatically.

```
Pod fails in wp-dev (or any namespace)
        │
        ▼
  watcher-agent (port 8080)
  - Streams K8s Watch API events in real time
  - Detects: CrashLoopBackOff, OOMKilled, ImagePullBackOff,
             ErrImagePull, Failed, HighRestartCount (≥5)
  - Deduplicates: 300s cooldown per pod/reason
        │
        │  POST /investigate
        ▼
  investigator-agent (port 8081, 2 replicas)
  - Fetches pod info, container statuses
  - Collects last 200 lines of current + previous container logs
  - Retrieves up to 50 Kubernetes events for the pod
  - Resolves pod → ReplicaSet → Deployment config (limits, replicas)
  - Generates human-readable summary + recommendations
        │
        │  POST /report (async, non-blocking)
        ▼
  analyst-agent (port 8082)
  - Receives full InvestigationReport
  - Logs structured incident summary (JSON, Splunk/Datadog compatible)
  - Ready for LLM root-cause analysis (stub in analyst/main.py)
```

---

## Prerequisites

- Kubernetes cluster (tested on Rancher Desktop / k3s)
- ArgoCD installed in the `argocd` namespace
- Sealed Secrets controller installed (for MySQL credentials)
- KARA images built and loaded into the cluster (see below)

---

## Setup Steps

### 1. Install ArgoCD (if not already running)

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

Get the initial admin password:

```bash
kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath="{.data.password}" | base64 -d
```

Access the UI at `https://<NODE-IP>:31846` (NodePort) or via port-forward:

```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443
```

### 2. Install Sealed Secrets controller

```bash
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/latest/download/controller.yaml
```

Apply the sealed MySQL secret:

```bash
kubectl create namespace wp-dev
kubectl apply -f sealed-mysql-secret.yaml
```

### 3. Build KARA images

Clone the KARA source repo and build the three images into the containerd `k8s.io` namespace (so k3s can pull them with `imagePullPolicy: IfNotPresent`):

```bash
cd /path/to/KARA

rdctl shell nerdctl build --namespace k8s.io \
  -f docker/analyst.Dockerfile -t kara/analyst-agent:latest .

rdctl shell nerdctl build --namespace k8s.io \
  -f docker/investigator.Dockerfile -t kara/investigator-agent:latest .

rdctl shell nerdctl build --namespace k8s.io \
  -f docker/watcher.Dockerfile -t kara/watcher-agent:latest .
```

### 4. Register ArgoCD Applications

Apply all three ArgoCD Application CRDs. ArgoCD will deploy each in the correct order:

```bash
kubectl apply -f kara-cd.yaml       -n argocd
kubectl apply -f app-cd.yaml        -n argocd
kubectl apply -f monitoring-cd.yaml -n argocd
```

### 5. Verify

```bash
# ArgoCD apps
kubectl get app -n argocd

# WordPress stack
kubectl get pods,svc -n wp-dev

# KARA agents
kubectl get pods -n kara

# Watch KARA detect a failure in real time
kubectl logs -f deployment/watcher-agent -n kara
kubectl logs -f deployment/investigator-agent -n kara
```

---

## Accessing WordPress

WordPress is exposed on NodePort `30080`. Get your node IP and open:

```
http://<NODE-IP>:30080
```

---

## Secrets Management

MySQL credentials are stored as a `SealedSecret` (`sealed-mysql-secret.yaml`). The Sealed Secrets controller decrypts this at runtime using the cluster's private key. The plaintext secret in `k8s-app1/scerets.yaml` is for **demo purposes only** — do not commit real credentials.

To re-seal secrets for a different cluster:

```bash
kubeseal --cert pub-cert.pem --format yaml < plain-secret.yaml > sealed-secret.yaml
```

---

## Monitoring

Prometheus + Grafana are deployed via the `monitoring` ArgoCD Application using the `kube-prometheus-stack` Helm chart (v56.6.2). KARA agents emit structured JSON logs compatible with log aggregation pipelines (Splunk, Datadog, ELK).

