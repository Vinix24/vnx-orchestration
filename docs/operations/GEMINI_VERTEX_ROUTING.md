# Gemini Vertex AI Routing — Operator Runbook

Use this guide when the Gemini API quota is exhausted (or to pre-emptively route
reviews through a higher-tier quota) via Vertex AI.

## When to Use

- **Quota exhausted**: Gemini API daily quota hit; gate runs returning 429 errors.
- **Pre-emptive**: High-volume PR batch where API-tier free allowance is insufficient.
- **Enterprise setup**: Prefer service-account auth over personal OAuth tokens in CI.

## Prerequisites

1. GCP project with **Vertex AI API** enabled.
2. A service account with the **AI Platform User** (`roles/aiplatform.user`) role
   (or `Vertex AI User` in the GCP console).
3. A JSON key file downloaded for that service account.

## Setup

### 1. Create and download the service-account key

```bash
gcloud iam service-accounts create vnx-vertex \
  --display-name "VNX Vertex AI Gate Runner"

gcloud projects add-iam-policy-binding <project-id> \
  --member="serviceAccount:vnx-vertex@<project-id>.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

gcloud iam service-accounts keys create ~/vnx-vertex-sa.json \
  --iam-account vnx-vertex@<project-id>.iam.gserviceaccount.com
```

### 2. Set environment variables

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export GOOGLE_CLOUD_PROJECT=<project-id>          # optional — picked up by gcloud automatically
export VNX_VERTEX_PROJECT=<project-id>            # explicit override for the runner
export VNX_GEMINI_ROUTING=vertex                  # activate Vertex path
```

Optional overrides (defaults shown):

```bash
export VNX_VERTEX_REGION=us-central1              # Vertex AI region
export VNX_VERTEX_MODEL=gemini-2.5-pro            # model identifier
export VNX_GEMINI_MAX_PROMPT_BYTES=100000         # max bytes of file content inlined in prompt
```

### 3. Activate the service-account in gcloud

```bash
gcloud auth activate-service-account \
  --key-file="$GOOGLE_APPLICATION_CREDENTIALS"
```

After activation, `gcloud auth print-access-token` returns an SA-derived bearer
token that `vertex_ai_runner.py` uses for the Vertex AI REST call.

## Verify the Setup

```bash
# Verify gcloud uses the service account
gcloud auth print-access-token            # must return a token, not an error

# Verify gemini CLI is unaffected (Vertex path does not touch it)
gemini --version                          # still works; routing is transparent

# Dry-run a gate (VNX internal command)
python3 scripts/gate_runner.py --gate gemini_review --dry-run
```

## How the Routing Works

```
VNX_GEMINI_ROUTING=vertex
        │
        ▼
scripts/gate_runner.py (line 83)
  using_vertex = True
        │
        ▼
GateRunner._run_vertex_path()
        │
        ▼
vertex_ai_runner.run_vertex_ai()
  1. _get_vertex_project()  → VNX_VERTEX_PROJECT or gcloud config get-value project
  2. _get_gcloud_token()    → gcloud auth print-access-token (picks up GOOGLE_APPLICATION_CREDENTIALS)
  3. _build_vertex_url()    → https://{region}-aiplatform.googleapis.com/...
  4. urllib.request.urlopen → REST POST to Vertex AI generateContent
        │
        ▼
gate_artifacts.materialize_artifacts()   (same pipeline as CLI path)
```

When `VNX_GEMINI_ROUTING` is **unset** or set to `oauth`, the gate uses the
`gemini` CLI subprocess unchanged — backward-compatible.

## Cost Note

Vertex AI is billed per token at pay-as-you-go rates. Unlike the Gemini API
Developer tier, there is no free daily allowance. Confirm billing is enabled on
the GCP project before routing production gates.

Reference pricing: [cloud.google.com/vertex-ai/generative-ai/pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing)

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `PERMISSION_DENIED` | SA missing `roles/aiplatform.user` | Re-run `add-iam-policy-binding` above |
| `PROJECT_NOT_FOUND` | `VNX_VERTEX_PROJECT` wrong or project does not exist | Verify with `gcloud projects describe <id>` |
| `VNX_VERTEX_PROJECT not set` | Neither env var nor gcloud default set | Export `VNX_VERTEX_PROJECT` |
| `Failed to get gcloud access token` | `gcloud` not authenticated or SA key not activated | Run `gcloud auth activate-service-account --key-file=...` |
| Quota still exhausted | Vertex quota per-region also capped | Try a different `VNX_VERTEX_REGION` |
| Gate reads from CLI not Vertex | `VNX_GEMINI_ROUTING` not exported in the shell running the dispatcher | Check `echo $VNX_GEMINI_ROUTING` in the same shell |

## Reference

- Implementation: `scripts/lib/vertex_ai_runner.py`
- Routing check: `scripts/gate_runner.py` (line 83, `using_vertex` flag)
- Tests: `tests/test_gemini_vertex_routing.py`, `tests/test_gate_runner_vertex.py`
