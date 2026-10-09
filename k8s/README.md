# ForgeFlow on Kubernetes

Kustomize manifests for spec sections 107-112 (Kubernetes) and 108 / 158 (Argo CD).

```
k8s/
  base/                 ForgeFlow: api, orchestrator-worker, agent-worker, frontend,
                        playwright-mcp, shared volumes, HPAs, network policies
  data/<service>/       mongodb, redis, kafka, elasticsearch (single-node, in-cluster)
  overlays/local/       minikube: everything in-cluster, repos from a host folder
  overlays/atlas/       same, but MongoDB Atlas instead of the in-cluster MongoDB
  overlays/gitops/      what Argo CD deploys from git (no secrets in git)
  observability/        Prometheus + Grafana with the ForgeFlow dashboards
  argocd/               Argo CD AppProject + Application
```

## What the manifests guarantee

| Concern | How |
|---|---|
| No cluster privileges | Pods run with `automountServiceAccountToken: false`: ForgeFlow never calls the Kubernetes API. Deployments go through Argo CD. |
| Least network access (spec 72) | `default-deny` NetworkPolicy, then explicit allows: app pods -> data services, the internet on 443 / 27017 only; the browser (playwright-mcp) -> agent-worker preview ports only (no internet). |
| Non-root | ForgeFlow containers run as uid 10001, no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp. |
| Secrets (spec 71) | Only in the `forgeflow-secrets` Secret, generated from a git-ignored `secrets.env` (or created out of band for GitOps). Settings that are not secret live in the `forgeflow-config` ConfigMap. |
| Scaling (spec 112) | HPAs for `agent-worker` (1-4) and `platform-api` (1-3). Task ownership is decided by MongoDB claims, so extra workers never run a task twice. |

## Deploy to minikube (local overlay)

Requires Docker with ~5 GB free for minikube. Stop the docker compose stack first
(`docker compose --profile ci down`) if memory is tight.

```bash
# 1. Cluster with a CNI that enforces NetworkPolicy, plus metrics for the HPAs.
minikube start --cpus=4 --memory=5g --cni=calico
minikube addons enable metrics-server

# 2. Your repositories, mounted into the node (keep this running in its own terminal).
minikube mount ./repos:/mnt/forgeflow/repos --uid 10001 --gid 10001

# 3. Images, built inside minikube (no registry needed).
minikube image build -t forgeflow/python:dev -f docker/python.Dockerfile .
minikube image build -t forgeflow/frontend:dev -f docker/frontend.Dockerfile .

# 4. Secrets: copy the example and fill in FORGEFLOW_SECRET_KEY (and LLM keys if any).
cp k8s/overlays/local/secrets.env.example k8s/overlays/local/secrets.env

# 5. Deploy and open the UI.
kubectl apply -k k8s/overlays/local
kubectl -n forgeflow get pods -w
kubectl -n forgeflow port-forward svc/frontend 8080:80
```

Optional dashboards:

```bash
kubectl -n forgeflow create secret generic grafana-admin \
  --from-literal=user=admin --from-literal=password=<choose-one>
kubectl apply -k k8s/observability
kubectl -n forgeflow port-forward svc/grafana 3000:3000
```

The local overlay sets `FAKE_LLM=true`; set it to `false` in
`overlays/local/kustomization.yaml` once a provider key is in `secrets.env`.

MongoDB Atlas instead: use `overlays/atlas` and put `MONGODB_URI=mongodb+srv://...` in
its `secrets.env`; allow the cluster's egress IP in Atlas > Network Access.

## Argo CD (GitOps)

```
Jenkins (Jenkinsfile) -> images -> image tags committed to k8s/overlays/gitops
       -> Argo CD Application "forgeflow" (manual sync) -> Kubernetes -> ForgeFlow checks health
```

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n forgeflow create secret generic forgeflow-secrets --from-env-file=secrets.env
kubectl apply -f k8s/argocd/project.yaml -f k8s/argocd/application.yaml
```

The repository is private: register it in Argo CD first (Settings > Repositories, or
`argocd repo add https://github.com/IAmParthivsinh-15/ForgeFlow.git --username <you>
--password <read-only fine-grained token>`).

Sync is manual on purpose: ForgeFlow's `deployment.sync` and `deployment.rollback` are
ASK capabilities, and Argo CD only allows rollbacks while automated sync is off.

### Connect Argo CD to ForgeFlow (deployment checks and rollback)

1. Create a token for the project role (read/sync this app only):
   `argocd proj role create-token forgeflow forgeflow-connector`
2. In ForgeFlow: *Extensibility > Connectors > Connect Argo CD* with the server URL,
   the token and the application name (`forgeflow`, or your own app).
3. *Projects > (repository) > Deployment*: pick the connector, the application and an
   optional health URL (+ smoke paths).
4. *Verify deployment* reads the Argo CD status, probes the URLs and records the result.
   When unhealthy and an earlier revision exists, *Roll back* asks for your approval
   first, then asks Argo CD to roll back (never pruning resources).

## Not covered yet

- Agent tasks run inside the agent-worker pods; per-task Kubernetes Jobs (spec 107,
  "consider") are a later step.
- Multi-node clusters need ReadWriteMany storage for the repos/workspaces/artifacts
  claims, and real TLS/authentication for Elasticsearch and an ingress for the UI.
- Kafka and MongoDB here are single-node; use managed services (or Strimzi / an operator)
  for production.
