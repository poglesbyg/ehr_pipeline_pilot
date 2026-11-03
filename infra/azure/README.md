# Azure prototype: Azure OpenAI + Azure AI Search, Entra ID only, one private endpoint

This deploys the production pattern from `docs/DESIGN_WALKTHROUGH.md §7` at prototype size and
runs the pilot's own evaluation against it:

- **Azure OpenAI**: chat and embeddings deployments, **API keys disabled** (Entra ID only).
- **Azure AI Search** (Basic), keys disabled. The pilot uses it for hybrid keyword and vector
  search, with the patient and 38 USC 7332 filters applied **before** the vector search.
- **A user-assigned managed identity**, with the least roles the pilot needs.
- **Phase B:** Azure OpenAI with **public access disabled** behind a **private endpoint**, a
  **private DNS zone linked to the VNet**, and the eval running *inside* the VNet as a Container
  Apps job using the managed identity.

All of it is one Bicep file (`main.bicep`) and it works in commercial Azure and Azure Government.
Data is synthetic only.

> **Use a personal subscription.** Don't deploy this into an employer's or university
> subscription. Government subscriptions require eligibility; a commercial pay-as-you-go or
> free-trial subscription is the right place for this prototype.

## Cost

Rough figures at the time of writing; check the Azure pricing pages before you start. For an
afternoon that you tear down the same day, expect a few dollars:

| Resource | Billing |
|---|---|
| AI Search, Basic | Hourly (~$75/month if left running). **The main cost: delete it when done** |
| Azure OpenAI | Per token. One eval run is cents |
| Container Apps job, Container Registry Basic, Log Analytics, private endpoint | Cents per day at this size |

## Prerequisites

- Azure CLI (`az version`), signed in, with **Owner** or **User Access Administrator** on the
  resource group so the template can create role assignments.
- Quota for the models in your region (Azure OpenAI → Quotas). Defaults: `gpt-4o-mini`
  (`2024-07-18`) and `text-embedding-3-small` (`1`), 10K TPM each. Override with
  `-p chatModel=... chatModelVersion=...` if your region offers different versions.

## Phase A: Entra-only services, eval from your laptop (≈20 min)

```bash
az login
az account set --subscription "<your personal subscription>"
az account show --query "{subscription:name, cloud:environmentName}" -o table   # confirm before creating anything
```

```bash
az group create -n rg-hl7bridge-proto -l eastus2
```

```bash
az deployment group create -g rg-hl7bridge-proto -f infra/azure/main.bicep \
  -p developerPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
  --query properties.outputs -o table
```

```bash
source tools/azure_env.sh rg-hl7bridge-proto
```

Role assignments take a few minutes to propagate. A `403` right after deployment is RBAC
propagation, not a bug: wait and retry. Then run the same gold sets as CI, now on Azure:

```bash
python3 -m hl7bridge eval --provider azure-openai --index azure-search
```

```bash
python3 -m hl7bridge eval --provider azure-openai --index azure-search --gold eval/gold-synthea.json
```

Each report records the served model version (e.g. `gpt-4o-mini-2024-07-18`). Compare it
with the extractive, Ollama and Azure results side by side; this is the regression run you'd
repeat when Microsoft retires a model version.

## Phase B: private endpoint, eval from inside the VNet (≈30 min)

```bash
az deployment group create -g rg-hl7bridge-proto -f infra/azure/main.bicep \
  -p privateNetworking=true developerPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
  --query properties.outputs -o table
```

**Watch it break from your laptop first.** Azure OpenAI now refuses public traffic:

```bash
python3 -m hl7bridge ask --provider azure-openai "potassium"
```

The provider fails and the assistant falls back to the extractive baseline. The `provider` line
says `extractive (fallback: ...)`, which is the designed degraded mode.

**See the DNS split.** From your laptop the hostname still resolves publicly. Inside the VNet,
the linked private zone answers with the private endpoint's `10.40.1.x` address:

```bash
python3 -c "import socket,urllib.parse,os; h=urllib.parse.urlsplit(os.environ['AZURE_OPENAI_ENDPOINT']).hostname; print(h, '->', socket.gethostbyname(h))"
```

```bash
az network private-endpoint show -g rg-hl7bridge-proto -n hl7b-aoai-pe --query "customDnsConfigs[].{fqdn:fqdn, ip:ipAddresses[0]}" -o table
```

Build the pilot's image in Azure (no local Docker needed), then deploy the runner job:

```bash
az acr build -r "$(az deployment group show -g rg-hl7bridge-proto -n main --query properties.outputs.registryName.value -o tsv)" -t hl7bridge:latest .
```

```bash
az deployment group create -g rg-hl7bridge-proto -f infra/azure/main.bicep \
  -p privateNetworking=true deployRunner=true developerPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
  --query properties.outputs -o table
```

```bash
az containerapp job start -g rg-hl7bridge-proto -n hl7b-eval
```

```bash
az containerapp job execution list -g rg-hl7bridge-proto -n hl7b-eval --query "[0].{name:name, status:properties.status}" -o table
```

The job's output (the eval report) lands in Log Analytics. In the portal, open the
`hl7b-logs` workspace and run:

```kusto
ContainerAppConsoleLogs_CL | where ContainerJobName_s == "hl7b-eval" | project TimeGenerated, Log_s | order by TimeGenerated asc
```

If that returns nothing, the column names differ in your workspace; start from
`ContainerAppConsoleLogs_CL | take 50` and filter on the job name you see there.

A passing run proves the whole chain inside the boundary: the managed identity gets tokens
from the platform, private DNS resolves Azure OpenAI to its private endpoint, keys are never
used, and the patient and 7332 filters run inside AI Search.

## Tear down

```bash
az group delete -n rg-hl7bridge-proto --yes --no-wait
```

Azure OpenAI accounts are soft-deleted and keep their name reserved for a while. Purging frees
it. Run `az cognitiveservices account list-deleted` to find the account's name and location,
then `az cognitiveservices account purge` with those values.

## Azure Government

```bash
az cloud set --name AzureUSGovernment && az login
```

Deploy to a Government region (e.g. `usgovvirginia`) with `-p deploymentSku=Standard`.
Global standard deployments aren't offered in Government, and the model list and versions
differ, so check the Government model catalog first. The template switches the private DNS
zone (`privatelink.openai.azure.us`) and the Search domain (`search.azure.us`) automatically.
`tools/azure_env.sh` sets `AZURE_CLOUD=usgovernment` so tokens request the Government audiences.

## What maps to what

| Control (DESIGN_WALKTHROUGH §7) | Where |
|---|---|
| Keys disabled; Entra ID only | `disableLocalAuth: true` on both services; `hl7bridge/azure_auth.py` |
| Least-privilege roles | OpenAI User + Search Index Data Contributor + Search Service Contributor for the runner, which both ingests and queries. A production split gives the app *Search Index Data Reader* and only the ingestion job Contributor |
| Private endpoint + private DNS linked to the VNet | `openAiEndpoint`, `openAiDns`, `openAiDnsLink` |
| Patient filter before vector search | `vectorFilterMode: preFilter` in `hl7bridge/ai/azure_search.py` |
| Second scope check after retrieval | unchanged: `ClinicalAssistant._verify_scope` |
| Degrade when generation is unavailable | the extractive fallback you see in Phase B from the laptop |
| Not built here | Azure Policy assignments (deny public network access, allowed regions), App Service front end, AI Search private endpoint, diagnostic settings to a SIEM |
