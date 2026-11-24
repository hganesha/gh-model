#!/usr/bin/env bash
# Deploy RKP (API + Explorer) to Azure Container Apps with a small footprint.
#
#   deploy/azure/deploy.sh [options]
#
#   -g, --resource-group NAME   resource group to create or update      (default: rkp)
#   -l, --location REGION       Azure region                            (default: eastus2)
#       --db-location REGION    region for PostgreSQL only, when its small tier is not offered to
#                               your subscription in --location (error SkuNotAvailable)
#   -p, --prefix PREFIX         resource name prefix, 2-8 characters    (default: rkp)
#       --entra                 sign in with Microsoft Entra ID (your current az tenant). Creates or
#                               updates the app registration "<prefix>-explorer-<group>" with the
#                               roles RKP.Admin and RKP.Viewer, requires users to be assigned one,
#                               and assigns you RKP.Admin. Needs rights to create app registrations.
#       --oidc-issuer URL       sign in with another OIDC provider instead; also --oidc-client-id ID,
#                               and the client secret in the environment as RKP_OIDC_CLIENT_SECRET.
#                               Register <explorer URL>/auth/callback and /auth/signed-out with it.
#       --oidc-admin-roles LIST roles or group IDs that may change things (default with --entra:
#                               RKP.Admin; otherwise every signed-in user)
#       --allow-ip CIDR         who may open the Explorer; repeatable   (default: your current public
#                               IP, or anyone when sign-in is on)
#       --allow-any             open the Explorer to the internet
#       --providers-file FILE   decision models: a providers.env, as used locally. `azure-cli`
#                               sign-in is switched to the app's managed identity, which is
#                               granted "Cognitive Services User" on the Azure AI resources named
#                               in the file's endpoints.
#       --github-token-from-gh  give the API your `gh auth token` as credential managed:github-token
#
# Needs: az (signed in, Owner or User Access Administrator on the resource group), git, openssl.
# No local Docker: images are built in the registry with `az acr build`.
# Every run rotates the database passwords, the Explorer-to-API token and, with sign-in, the client
# secret and session key (everyone signs in again); data is kept.
# Remove everything with:  az group delete -n <resource-group>  (and, with --entra, the app registration;
# the Key Vault stays soft-deleted for 7 days: az keyvault purge -n <name> before redeploying to that group)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "$here/../.." && pwd)"

group=rkp location=eastus2 db_location="" prefix=rkp providers="" github=0 allow_any=0
entra=0 oidc_issuer="" oidc_client_id="" admin_roles="" admin_roles_set=0
allowed=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group) group="$2"; shift 2 ;;
    -l|--location) location="$2"; shift 2 ;;
    --db-location) db_location="$2"; shift 2 ;;
    -p|--prefix) prefix="$2"; shift 2 ;;
    --entra) entra=1; shift ;;
    --oidc-issuer) oidc_issuer="$2"; shift 2 ;;
    --oidc-client-id) oidc_client_id="$2"; shift 2 ;;
    --oidc-admin-roles) admin_roles="$2"; admin_roles_set=1; shift 2 ;;
    --allow-ip) allowed+=("$2"); shift 2 ;;
    --allow-any) allow_any=1; shift ;;
    --providers-file) providers="$2"; shift 2 ;;
    --github-token-from-gh) github=1; shift ;;
    -h|--help) sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
secret() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32; }

az account show --query name -o tsv >/dev/null || { echo "Sign in first: az login" >&2; exit 1; }
[[ -z "$providers" || -f "$providers" ]] || { echo "No such file: $providers" >&2; exit 1; }
if [[ $entra -eq 1 && -n "$oidc_issuer" ]]; then echo "Use --entra or --oidc-issuer, not both." >&2; exit 2; fi
if [[ -n "$oidc_issuer" ]]; then
  [[ -n "$oidc_client_id" && -n "${RKP_OIDC_CLIENT_SECRET:-}" ]] || { echo "--oidc-issuer needs --oidc-client-id and RKP_OIDC_CLIENT_SECRET in the environment." >&2; exit 2; }
fi
sign_in=0; [[ $entra -eq 1 || -n "$oidc_issuer" ]] && sign_in=1
[[ $entra -eq 1 && $admin_roles_set -eq 0 ]] && admin_roles="RKP.Admin"

if [[ $sign_in -eq 1 && $allow_any -eq 0 && ${#allowed[@]} -eq 0 ]]; then
  allow_any=1  # sign-in protects the Explorer; narrow it further with --allow-ip
fi
if [[ $allow_any -eq 0 && ${#allowed[@]} -eq 0 ]]; then
  # One request to a public "what is my IP" service, only to fill the default allow-list.
  ip="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Could not find your public IP. Pass --allow-ip CIDR or --allow-any." >&2; exit 1; }
  allowed=("$ip/32")
fi
if [[ $allow_any -eq 1 ]]; then
  allowed=()
  [[ $sign_in -eq 1 ]] || echo "Warning: the Explorer has no sign-in; anyone with its URL can use it." >&2
fi
allowed_json="$(printf '%s\n' "${allowed[@]+"${allowed[@]}"}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"

pg_password="$(secret)" app_password="$(secret)" api_token="$(secret)" github_token=""
[[ $github -eq 1 ]] && github_token="$(gh auth token)"

params=(prefix="$prefix" location="$location" postgresLocation="${db_location:-$location}" allowedIpRanges="$allowed_json"
        postgresAdminPassword="$pg_password" appDbPassword="$app_password" apiToken="$api_token" githubToken="$github_token")
sign_in_params=()

step "Resource group $group in $location"
az group create -n "$group" -l "$location" -o none

step "Infrastructure (registry, storage, PostgreSQL, Container Apps environment)"
out="$(az deployment group create -g "$group" -n rkp-infra -f "$here/main.bicep" \
  -p "${params[@]}" deployApps=false --query properties.outputs -o json)"
get() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])" <<<"$out"; }
registry="$(get registryName)" storage="$(get storageAccount)" share="$(get shareName)" principal="$(get identityPrincipalId)"
url="$(get explorerUrl)"

if [[ $entra -eq 1 ]]; then
  app_name="$prefix-explorer-$group"
  step "Entra app registration $app_name"
  tenant="$(az account show --query tenantId -o tsv)"
  redirects=("$url/auth/callback" "$url/auth/signed-out")
  app_id="$(az ad app list --display-name "$app_name" --query '[0].appId' -o tsv)"
  if [[ -z "$app_id" ]]; then
    roles="$(python3 -c 'import json,uuid; print(json.dumps([
      {"allowedMemberTypes":["User"],"value":"RKP.Admin","displayName":"RKP administrator","description":"Connect repositories, edit the business map and change settings.","isEnabled":True,"id":str(uuid.uuid4())},
      {"allowedMemberTypes":["User"],"value":"RKP.Viewer","displayName":"RKP viewer","description":"Read everything, change nothing.","isEnabled":True,"id":str(uuid.uuid4())}]))')"
    roles_file="$(mktemp)"; printf '%s' "$roles" >"$roles_file"
    app_id="$(az ad app create --display-name "$app_name" --sign-in-audience AzureADMyOrg \
      --web-redirect-uris "${redirects[@]}" --app-roles "@$roles_file" --query appId -o tsv)"
    rm -f "$roles_file"
    echo "  created $app_id"
  else
    az ad app update --id "$app_id" --web-redirect-uris "${redirects[@]}" -o none
    echo "  updated redirect URIs of $app_id"
  fi
  sp_id="$(az ad sp list --filter "appId eq '$app_id'" --query '[0].id' -o tsv)"
  [[ -n "$sp_id" ]] || sp_id="$(az ad sp create --id "$app_id" --query id -o tsv)"
  # Only users (or groups) assigned RKP.Admin or RKP.Viewer can sign in.
  az ad sp update --id "$sp_id" --set appRoleAssignmentRequired=true -o none
  admin_role_id="$(az ad app show --id "$app_id" --query "appRoles[?value=='RKP.Admin'].id | [0]" -o tsv)"
  me="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
  if [[ -n "$me" && -n "$admin_role_id" ]]; then
    assigned="$(az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$sp_id/appRoleAssignedTo" \
      --query "value[?principalId=='$me' && appRoleId=='$admin_role_id'] | length(@)" -o tsv)"
    if [[ "$assigned" == 0 ]]; then
      az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$sp_id/appRoleAssignedTo" \
        --body "{\"principalId\":\"$me\",\"resourceId\":\"$sp_id\",\"appRoleId\":\"$admin_role_id\"}" -o none
      echo "  you are assigned RKP.Admin"
    fi
  fi
  # A fresh client secret every run, like the other secrets; it replaces the previous one.
  client_secret="$(az ad app credential reset --id "$app_id" --display-name "rkp-deploy" --years 1 --query password -o tsv --only-show-errors)"
  sign_in_params=(entraTenantId="$tenant" oidcClientId="$app_id" oidcClientSecret="$client_secret")
elif [[ -n "$oidc_issuer" ]]; then
  sign_in_params=(oidcIssuer="$oidc_issuer" oidcClientId="$oidc_client_id" oidcClientSecret="$RKP_OIDC_CLIENT_SECRET")
fi
if [[ $sign_in -eq 1 ]]; then
  sign_in_params+=(sessionSecret="$(secret)$(secret)" oidcAdminRoles="$admin_roles")
fi

tag="$(git -C "$app_root" rev-parse --short HEAD 2>/dev/null || echo manual)-$(date +%Y%m%d%H%M%S)"
step "Images rkp-api:$tag and rkp-explorer:$tag (built in $registry)"
az acr build -r "$registry" -t "rkp-api:$tag" "$app_root" -o none
az acr build -r "$registry" -t "rkp-explorer:$tag" "$app_root/explorer" -o none

if [[ -n "$providers" ]]; then
  step "Decision models from $providers"
  converted="$(mktemp)"; trap 'rm -f "$converted"' EXIT
  sed -E 's/^(RKP_[A-Z_]+_AUTH)=azure-cli[[:space:]]*$/\1=managed-identity/' "$providers" >"$converted"
  key="$(az storage account keys list -g "$group" -n "$storage" --query '[0].value' -o tsv)"
  az storage file upload --account-name "$storage" --account-key "$key" --share-name "$share" \
    --source "$converted" --path providers.env --only-show-errors -o none
  # Grant the app's identity use of each Azure AI resource the file points at.
  for account in $(grep -Eo 'https://[a-z0-9-]+\.(services\.ai|openai|cognitiveservices)\.azure\.com' "$providers" | sed -E 's#https://([^.]+)\..*#\1#' | sort -u); do
    id="$(az cognitiveservices account list --query "[?name=='$account'].id | [0]" -o tsv)"
    if [[ -z "$id" ]]; then echo "  $account: not found in this subscription; grant access yourself"; continue; fi
    az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
      --role "Cognitive Services User" --scope "$id" -o none --only-show-errors
    echo "  $account: Cognitive Services User granted"
  done
fi

step "Container apps"
out="$(az deployment group create -g "$group" -n rkp-apps -f "$here/main.bicep" \
  -p "${params[@]}" "${sign_in_params[@]+"${sign_in_params[@]}"}" deployApps=true apiImageTag="$tag" explorerImageTag="$tag" --query properties.outputs -o json)"

step "Waiting for the Explorer to answer"
# With sign-in, an unauthenticated call is answered 401 by the Explorer itself; the API is checked below.
expect=200; [[ $sign_in -eq 1 ]] && expect=401
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url/api/rkp/repositories" || true)"
  [[ "$code" == "$expect" ]] && break
  sleep 10
done
if [[ "${code:-}" == "$expect" ]]; then
  [[ $sign_in -eq 1 ]] && echo "Explorer is up and asks for sign-in." || echo "API reachable through the Explorer."
else
  echo "Not answering yet (last HTTP status ${code:-none}); check: az containerapp logs show -g $group -n $prefix-explorer (and $prefix-api)"
fi

cat <<EOF

Explorer: $url
Sign-in:  $([[ $entra -eq 1 ]] && echo "Microsoft Entra ID (app $app_name; assign people RKP.Admin or RKP.Viewer under Enterprise applications)" || { [[ -n "$oidc_issuer" ]] && echo "$oidc_issuer" || echo "none"; })
Allowed:  ${allowed[*]:-anyone}
Remove:   az group delete -n $group
EOF
if [[ $github -eq 1 ]]; then
  echo "GitHub: in Settings, add a connection of type GitHub token with credential reference managed:github-token."
else
  echo "GitHub: create a GitHub OAuth App with Device Flow enabled (github.com/settings/developers), then in"
  echo "        Settings add a connection 'GitHub device sign-in' with its client ID, save, and start device sign-in."
fi
