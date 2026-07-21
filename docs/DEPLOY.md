# Cloud Run Deployment Guide

One-time GCP setup for the canvassing planner. After this is done every push to
`main` builds, publishes to ghcr.io, and deploys to Cloud Run automatically.

This is a single service (FastAPI serves both the API and `index.html` — no
separate frontend build, no database).

**Status: this setup has already been run** (project `canvassing-planner-prod`,
service account, Workload Identity Federation, Artifact Registry repo). This
doc is here so it can be reproduced or audited later.

---

## 1. Create a GCP project

```bash
PROJECT_ID=canvassing-planner-prod
gcloud projects create $PROJECT_ID --name="Canvassing Planner"
gcloud billing projects link $PROJECT_ID --billing-account=YOUR_BILLING_ACCOUNT_ID
gcloud config set project $PROJECT_ID
```

## 2. Enable APIs

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com
```

## 3. Create the Artifact Registry repo

```bash
REGION=europe-west2
gcloud artifacts repositories create canvassing-planner \
  --repository-format=docker \
  --location=$REGION
```

## 4. Create a service account for CI/CD

```bash
gcloud iam service-accounts create canvassing-deployer \
  --display-name="Canvassing Planner Deployer"

SA_EMAIL="canvassing-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/run.admin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/artifactregistry.writer"

gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/iam.serviceAccountUser"
```

No Cloud SQL / runtime service account step is needed — this app has no database.

## 5. Set up Workload Identity Federation

Keyless auth — GitHub Actions authenticates to GCP without a stored service account key.

```bash
gcloud iam workload-identity-pools create github-pool \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --workload-identity-pool=github-pool \
  --location=global \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='StuartJAtkinson/canvassing-planner'"

PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/StuartJAtkinson/canvassing-planner"

gcloud iam workload-identity-pools providers describe github-provider \
  --workload-identity-pool=github-pool --location=global --format="value(name)"
```

## 6. Set GitHub secrets and variables

Go to **Settings → Secrets and variables → Actions**.

### Secrets

| Name | Value |
|------|-------|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | The `projects/.../providers/github-provider` string from step 5 |
| `GCP_SERVICE_ACCOUNT` | `canvassing-deployer@canvassing-planner-prod.iam.gserviceaccount.com` |

### Variables

| Name | Value |
|------|-------|
| `GCP_PROJECT_ID` | `canvassing-planner-prod` — **setting this activates the deploy job** |
| `GCP_REGION` | `europe-west2` |

## 7. First deploy

Push to `main`. The CI `deploy` job builds the image, pushes it to Artifact
Registry, and deploys it to Cloud Run as `canvassing-planner`, printing the
service URL in the "Print URL" step.

## 8. Verify

```bash
gcloud run services describe canvassing-planner --region=europe-west2 --format='value(status.url)'
```

---

## Custom domain: canvas.stuartjatkinson.co.uk

```bash
gcloud run domain-mappings create \
  --service=canvassing-planner \
  --domain=canvas.stuartjatkinson.co.uk \
  --region=europe-west2

# Print the DNS records to add at the domain registrar
gcloud run domain-mappings describe \
  --domain=canvas.stuartjatkinson.co.uk \
  --region=europe-west2 \
  --format="value(status.resourceRecords)"
```

Add the printed CNAME (or A/AAAA) records at the registrar, then wait for DNS
to propagate. `gcloud run domain-mappings describe ... --format="value(status.conditions)"`
shows when the certificate has provisioned.

---

## Notes

**Address data**: `data/uprn.sqlite` (~2GB, the OS Open UPRN address database)
is not shipped in the container — it's gitignored and too large for a
lightweight image. The app already degrades gracefully without it, falling
back to estimated address counts (`addr_source: "estimated"`) derived from
residential street length. Real UPRN-backed counts stay local-only for now; a
future upgrade could mount the database from Cloud Storage or Cloud SQL at
startup.

**OSM/elevation cache**: osmnx's on-disk request cache and the in-memory
graph/elevation caches are not persisted — every cold start refetches from
Overpass/open-elevation.com. Fine for occasional/demo use; set
`--min-instances=1` if latency on first request matters.

**Cold starts**: `--min-instances=0` (free when idle). First request after a
period of inactivity is slower — the street-graph/elevation fetches add to
normal Cloud Run cold-start time.
