#!/usr/bin/env bash
set -euo pipefail

export SUBSCRIPTION_ID='3184d291-4c7d-4742-9701-672b368b3768'
export TENANT_ID='bc9cec50-5d19-44cf-ac3c-0be172cddbd4'
export KV_NAME='mathlib-kv'

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "ERROR: missing env var: $name" >&2
    exit 1
  fi
}

print_header() {
  echo
  echo "================================================================================"
  echo "$1"
  echo "================================================================================"
}

list_federated_credentials() {
  local app_object_id="$1"
  az rest \
    --method get \
    --uri "https://graph.microsoft.com/beta/applications/${app_object_id}/federatedIdentityCredentials"
}

federated_subject_or_expression_query() {
  echo "value[].{name:name,issuer:issuer,subjectOrExpression:subject || claimsMatchingExpression.value,audiences:join(',', audiences)}"
}

list_relevant_rbac_assignments() {
  local sp_object_id="$1"
  az role assignment list \
    --all \
    --assignee-object-id "$sp_object_id" \
    --include-inherited \
    --query "[?starts_with(scope, '$KV_ID') || scope=='$RESOURCE_GROUP_ID' || scope=='$SUBSCRIPTION_SCOPE'].{role:roleDefinitionName,scope:scope}" \
    -o table
}

list_relevant_rbac_assignments_tsv() {
  local sp_object_id="$1"
  az role assignment list \
    --all \
    --assignee-object-id "$sp_object_id" \
    --include-inherited \
    --query "[?starts_with(scope, '$KV_ID') || scope=='$RESOURCE_GROUP_ID' || scope=='$SUBSCRIPTION_SCOPE'].[roleDefinitionName,scope]" \
    -o tsv
}

role_grants_secret_values() {
  case "$1" in
    "Key Vault Administrator"|"Key Vault Secrets Officer"|"Key Vault Secrets User")
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

print_all_secret_names() {
  if [ -n "$ALL_SECRET_NAMES" ]; then
    printf '%s\n' "$ALL_SECRET_NAMES"
  else
    echo "(no secrets found in vault)"
  fi
}

# This is a best-effort summary from RBAC role names and scopes, not a full
# authorization evaluation. The raw assignments are printed separately.
print_inferred_secret_access_from_rbac() {
  local sp_object_id="$1"
  local assignments
  local role
  local scope
  local has_all_secrets=0
  local secret_names=""

  assignments="$(list_relevant_rbac_assignments_tsv "$sp_object_id")"
  if [ -z "$assignments" ]; then
    echo "(no relevant RBAC assignments)"
    return
  fi

  while IFS=$'\t' read -r role scope; do
    [ -n "${role:-}" ] || continue

    if ! role_grants_secret_values "$role"; then
      continue
    fi

    case "$scope" in
      "$SUBSCRIPTION_SCOPE"|"$RESOURCE_GROUP_ID"|"$KV_ID")
        has_all_secrets=1
        ;;
      "$KV_ID"/secrets/*)
        secret_names="${secret_names}${scope##*/}"$'\n'
        ;;
    esac
  done <<< "$assignments"

  if [ "$has_all_secrets" -eq 1 ]; then
    print_all_secret_names
    return
  fi

  if [ -n "$secret_names" ]; then
    printf '%s' "$secret_names" | sort -u
  else
    echo "(none inferred from RBAC roles)"
  fi
}

print_secret_access_summary() {
  local sp_object_id="$1"
  local secret_permissions

  if [ "$KV_RBAC" = "true" ]; then
    print_inferred_secret_access_from_rbac "$sp_object_id"
    return
  fi

  secret_permissions="$(az keyvault show --name "$KV_NAME" \
    --query "properties.accessPolicies[?objectId=='$sp_object_id'].permissions.secrets[]" \
    -o tsv)"

  if [ -z "$secret_permissions" ]; then
    echo "(no secret access policy)"
    return
  fi

  echo "permissions: $(printf '%s' "$secret_permissions" | tr '\n' ',' | sed 's/,$//')"
  case "$secret_permissions" in
    *get*|*list*)
      print_all_secret_names
      ;;
    *)
      echo "(policy present, but no get/list secret permission)"
      ;;
  esac
}

require_env SUBSCRIPTION_ID
require_env TENANT_ID
require_env KV_NAME

az account set --subscription "$SUBSCRIPTION_ID"

print_header "AZURE ACCOUNT"
az account show --query "{subscriptionId:id,tenantId:tenantId,name:name,user:user.name}" -o yaml

print_header "KEY VAULT"
KV_ID="$(az keyvault show --name "$KV_NAME" --query id -o tsv)"
KV_RBAC="$(az keyvault show --name "$KV_NAME" --query properties.enableRbacAuthorization -o tsv)"
RESOURCE_GROUP_ID="${KV_ID%/providers/Microsoft.KeyVault/vaults/*}"
SUBSCRIPTION_SCOPE="/subscriptions/$SUBSCRIPTION_ID"
ALL_SECRET_NAMES="$(az keyvault secret list --vault-name "$KV_NAME" --query "[].name" -o tsv | sort)"
az keyvault show --name "$KV_NAME" \
  --query "{name:name,id:id,tenantId:properties.tenantId,enableRbacAuthorization:properties.enableRbacAuthorization}" \
  -o yaml

while IFS=$'\t' read -r APP_DISPLAY_NAME CLIENT_ID APP_OBJ_ID; do
  print_header "APP: ${APP_DISPLAY_NAME:-<no display name>} ($CLIENT_ID)"

  echo "-- app registration"
  printf 'displayName: %s\n' "${APP_DISPLAY_NAME:-}"
  printf 'appId: %s\n' "$CLIENT_ID"
  printf 'id: %s\n' "$APP_OBJ_ID"

  SP_OBJ_ID="$(az ad sp list --filter "appId eq '$CLIENT_ID'" --query "[0].id" -o tsv)"

  echo
  echo "-- service principal"
  if [ -n "$SP_OBJ_ID" ]; then
    az ad sp show --id "$CLIENT_ID" \
      --query "{displayName:displayName,appId:appId,id:id,servicePrincipalType:servicePrincipalType,accountEnabled:accountEnabled}" \
      -o yaml
  else
    echo "(no service principal found)"
  fi

  echo
  echo "-- federated credentials"
  FEDERATED_CREDENTIALS="$(list_federated_credentials "$APP_OBJ_ID" \
    --query "$(federated_subject_or_expression_query)" -o table)"
  if [ -n "$FEDERATED_CREDENTIALS" ]; then
    printf '%s\n' "$FEDERATED_CREDENTIALS"
  else
    echo "(none)"
  fi

  if [ -z "$SP_OBJ_ID" ]; then
    continue
  fi

  echo
  echo "-- mathlib-kv RBAC assignments"
  list_relevant_rbac_assignments "$SP_OBJ_ID"

  echo
  echo "-- mathlib-kv secret access (inferred)"
  print_secret_access_summary "$SP_OBJ_ID"
done < <(
  az ad app list --all \
    --query "[].[displayName, appId, id]" \
    -o tsv
)
