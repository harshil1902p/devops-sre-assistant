#!/usr/bin/env bash
# Tear down Azure resources so nothing keeps billing.
#   scripts/teardown.sh apps   # remove only the hourly-priced parts: Container App + environment + registry + App Insights
#   scripts/teardown.sh all    # delete the whole resource group, purge soft-deleted Key Vault / Azure OpenAI, remove the Entra app
set -euo pipefail
RG=rg-sre-assistant
MODE=${1:-apps}

if [[ "$MODE" == "apps" ]]; then
  az containerapp delete -g "$RG" -n ca-sre-agent-api --yes 2>/dev/null || true
  for type in Microsoft.App/managedEnvironments Microsoft.ContainerRegistry/registries Microsoft.Insights/components; do
    for id in $(az resource list -g "$RG" --resource-type "$type" --query "[].id" -o tsv); do
      echo "deleting $id"; az resource delete --ids "$id"
    done
  done
  echo "Done. Redeploy later with: deployRegistry=true deployApps=true (see README)."
elif [[ "$MODE" == "all" ]]; then
  read -r -p "Delete resource group $RG and everything in it? [y/N] " ok; [[ "$ok" == "y" ]] || exit 1
  kv=$(az keyvault list -g "$RG" --query "[0].name" -o tsv || true)
  aoai=$(az cognitiveservices account list -g "$RG" --query "[0].{n:name,l:location}" -o tsv || true)
  az group delete -n "$RG" --yes
  # Soft delete keeps names reserved (and Azure OpenAI quota allocated) until purged.
  [[ -n "$kv" ]] && az keyvault purge -n "$kv" || true
  if [[ -n "$aoai" ]]; then read -r n l <<<"$aoai"; az cognitiveservices account purge -n "$n" -g "$RG" -l "$l" || true; fi
  app=$(az ad app list --display-name sre-assistant-api --query "[0].id" -o tsv || true)
  [[ -n "$app" ]] && az ad app delete --id "$app" || true
  echo "Budget alert is subscription-level: az consumption budget delete --budget-name budget-sre-assistant"
else
  echo "usage: $0 apps|all"; exit 2
fi
