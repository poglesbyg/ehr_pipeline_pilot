#!/usr/bin/env bash
# Export the Azure prototype's deployment outputs as the environment hl7bridge reads.
# Usage:  source tools/azure_env.sh <resource-group> [deployment-name]
# Nothing secret is exported: auth is Entra ID (your `az login`, or a managed identity in Azure).

rg="${1:?usage: source tools/azure_env.sh <resource-group> [deployment-name]}"
dep="${2:-main}"
outputs="$(az deployment group show -g "$rg" -n "$dep" --query properties.outputs -o json)" || return 1

read_output() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])" <<<"$outputs"; }

export AZURE_OPENAI_ENDPOINT="$(read_output openAiEndpoint)"
export AZURE_OPENAI_DEPLOYMENT="$(read_output chatDeployment)"
export AZURE_OPENAI_EMBED_DEPLOYMENT="$(read_output embedDeployment)"
export AZURE_SEARCH_ENDPOINT="$(read_output searchEndpoint)"
export AZURE_SEARCH_INDEX="hl7bridge-evidence"
export AZURE_CLOUD="$(read_output cloud)"
unset AZURE_OPENAI_API_KEY AZURE_CLIENT_ID  # laptop: use the Azure CLI token, not a key or an identity

echo "AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT"
echo "AZURE_SEARCH_ENDPOINT=$AZURE_SEARCH_ENDPOINT"
echo "AZURE_CLOUD=$AZURE_CLOUD  (deployments: $AZURE_OPENAI_DEPLOYMENT, $AZURE_OPENAI_EMBED_DEPLOYMENT)"
