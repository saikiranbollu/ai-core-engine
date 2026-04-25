# AICE Test Environment

## Deploy

```bash
# 1. Login to OpenShift via kubectl
kubectl config set-cluster openshift --server=https://api.eu-de-7.icp.infineon.com:6443 --insecure-skip-tls-verify=true
kubectl config set-credentials openshift-user --token=<YOUR_TOKEN>
kubectl config set-context openshift --cluster=openshift --user=openshift-user
kubectl config use-context openshift

# 2. Apply all test resources
kubectl apply -k mcp/k8s/test/
```

## Neo4j Ontology Setup

The `test-init-neo4j` job creates graph indexes based on the `ONTOLOGY_PROFILE` env var.
By default it creates indexes for **both** profiles. To target a specific ontology,
edit `init-neo4j.yaml` before applying:

```yaml
- name: ONTOLOGY_PROFILE
  value: "mcal"    # mcal | illd | both (default)
```

Or re-run the init job for a different profile after the initial deploy:

```bash
# Delete the old job and re-create with desired profile
kubectl delete job test-init-neo4j -n mcswai --ignore-not-found

kubectl create job test-init-neo4j -n mcswai \
  --image=neo4j:4.4.48 \
  -- /bin/bash -c "echo 'Use kubectl apply -k with edited ONTOLOGY_PROFILE instead'"

# Preferred: edit init-neo4j.yaml, then re-apply
kubectl apply -k mcp/k8s/test/
```

Shared indexes (`NodeSet`, `ApprovedPattern`) are always created regardless of profile.

## External Secrets

Before deploying, patch the external secrets with real credentials:

```bash
kubectl patch secret test-aice-external-secrets -n mcswai -p \
  '{"stringData":{"ifx-username":"<REAL>","ifx-password":"<REAL>","jama-api-key":"<REAL>","jama-api-secret":"<REAL>"}}'
kubectl rollout restart deployment/test-aice-mcp-server -n mcswai
```

## Routes

| Service | URL | Port |
|---------|-----|------|
| MCP Server | https://test-mcp-mcswai.eu-de-7.icp.infineon.com/mcp | 8000 |
| Neo4j Browser | https://neo4j-ui-mcswai-test.icp.infineon.com | 7474 |
| Neo4j Bolt (edge) | https://bolt-edge-neo4j-mcswai-test.icp.infineon.com | 7687 |
| Neo4j Bolt (passthrough) | https://bolt-passthrough-neo4j-mcswai-test.icp.infineon.com | 7687 |
| Qdrant REST | https://qdrant-mcswai-test.icp.infineon.com | 6333 |

**Internal services** (cluster-only, no external route):

| Service | Cluster DNS | Port |
|---------|-------------|------|
| Qdrant gRPC | test-qdrant:6334 | 6334 |
| Redis | test-redis:6379 | 6379 |
| PostgreSQL | test-postgres:5432 | 5432 |

## Cleanup

```bash
kubectl delete all,secret,configmap,pvc,networkpolicy,route -l app.kubernetes.io/part-of=aice-test-env -n mcswai
```
