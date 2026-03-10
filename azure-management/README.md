# Azure Management Notes

`check-app-registrations.sh` is an inventory script for Entra app registrations related to the `mathlib-kv` Key Vault.

It reports, for each app registration in the tenant:
- the app registration identity
- the corresponding service principal, if one exists
- federated identity credentials
- RBAC assignments relevant to `mathlib-kv`
- an inferred summary of which Key Vault secrets the service principal can access

## Usage

Prerequisites:
- Azure CLI installed
- an active Azure login for the target tenant
- permission to read Entra applications, service principals, Key Vault metadata, and role assignments

Typical usage:

```bash
az login
./azure-management/check-app-registrations.sh
```

The script sets the subscription with `az account set --subscription ...`, but it does not perform `az login` itself.

## Output interpretation

The secret-access section is a best-effort summary.

For RBAC-enabled vaults, the script infers secret access from a small set of Key Vault data-plane roles and their scopes:
- `Key Vault Administrator`
- `Key Vault Secrets Officer`
- `Key Vault Secrets User`

This is useful for inspection, but it is not a full effective-permissions evaluator. The raw RBAC assignments are printed separately and should be treated as the source material.

## Sensitivity

The output does not include secret values, private keys, client secrets, or tokens.

It does include security-sensitive metadata such as:
- subscription and tenant identifiers
- app and service principal identifiers
- federated credential subjects / expressions
- Key Vault RBAC scopes
- Key Vault secret names
