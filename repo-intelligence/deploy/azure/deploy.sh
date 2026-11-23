#!/usr/bin/env bash
# Deploy RKP (API + Explorer) to Azure Container Apps with a small footprint.
#
#   deploy/azure/deploy.sh [options]
#
#   -g, --resource-group NAME   resource group to create or update      (default: rkp)
#   -l, --location REGION       Azure region                            (default: eastus2)
#   -p, --prefix PREFIX         resource name prefix, 2-8 characters    (default: rkp)
#       --allow-ip CIDR         who may open the Explorer; repeatable   (default: your current public IP)
#       --allow-any             open the Explorer to the internet (it has no sign-in of its own)
#       --providers-file FILE   decision models: a providers.env, as used locally. `azure-cli`
#                               sign-in is switched to the app's managed identity, which is
#                               granted "Cognitive Services User" on the Azure AI resources named
#                               in the file's endpoints.
#       --github-token-from-gh  give the API your `gh auth token` as credential managed:github-token
#
# Needs: az (signed in, Owner or User Access Administrator on the resource group), git, openssl.
# No local Docker: images are built in the registry with `az acr build`.
# Every run rotates the database passwords and the Explorer-to-API token; data is kept.
# Remove everything with:  az group delete -n <resource-group>
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "$here/../.." && pwd)"

group=rkp location=eastus2 prefix=rkp providers="" github=0 allow_any=0
allowed=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group) group="$2"; shift 2 ;;
    -l|--location) location="$2"; shift 2 ;;
    -p|--prefix) prefix="$2"; shift 2 ;;
    --allow-ip) allowed+=("$2"); shift 2 ;;
    --allow-any) allow_any=1; shift ;;
    --providers-file) providers="$2"; shift 2 ;;
    --github-token-from-gh) github=1; shift ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
secret() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32; }

az account show --query name -o tsv >/dev/null || { echo "Sign in first: az login" >&2; exit 1; }
[[ -z "$providers" || -f "$providers" ]] || { echo "No such file: $providers" >&2; exit 1; }

if [[ $allow_any -eq 0 && ${#allowed[@]} -eq 0 ]]; then
  # One request to a public "what is my IP" service, only to fill the default allow-list.
  ip="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Could not find your public IP. Pass --allow-ip CIDR or --allow-any." >&2; exit 1; }
  allowed=("$ip/32")
fi
if [[ $allow_any -eq 1 ]]; then
  allowed=()
  echo "Warning: the Explorer will be reachable by anyone with its URL." >&2
fi
allowed_json="$(printf '%s\n' "${allowed[@]+"${allowed[@]}"}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"

pg_password="$(secret)" app_password="$(secret)" api_token="$(secret)" github_token=""
[[ $github -eq 1 ]] && github_token="$(gh auth token)"

params=(prefix="$prefix" location="$location" allowedIpRanges="$allowed_json"
        postgresAdminPassword="$pg_password" appDbPassword="$app_password" apiToken="$api_token" githubToken="$github_token")

step "Resource group $group in $location"
az group create -n "$group" -l "$location" -o none

step "Infrastructure (registry, storage, PostgreSQL, Container Apps environment)"
out="$(az deployment group create -g "$group" -n rkp-infra -f "$here/main.bicep" \
  -p "${params[@]}" deployApps=false --query properties.outputs -o json)"
get() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])" <<<"$out"; }
registry="$(get registryName)" storage="$(get storageAccount)" share="$(get shareName)" principal="$(get identityPrincipalId)"

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
  -p "${params[@]}" deployApps=true apiImageTag="$tag" explorerImageTag="$tag" --query properties.outputs -o json)"
url="$(get explorerUrl)"

step "Waiting for the Explorer to answer"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url/api/rkp/repositories" || true)"
  [[ "$code" == 200 ]] && break
  sleep 10
done
[[ "${code:-}" == 200 ]] && echo "API reachable through the Explorer." || echo "Not answering yet (last HTTP status ${code:-none}); check: az containerapp logs show -g $group -n $prefix-api"

cat <<EOF

Explorer: $url
Allowed:  ${allowed[*]:-anyone}
Remove:   az group delete -n $group
EOF
if [[ $github -eq 1 ]]; then
  echo "GitHub: in Settings, add a connection of type GitHub token with credential reference managed:github-token."
fi
