# Deploy to Azure

`deploy/azure/deploy.sh` puts the API and the Explorer on Azure with a small footprint. Run it from `repo-intelligence/`, signed in with `az login`:

~~~bash
deploy/azure/deploy.sh -g rkp --entra --providers-file ../.rkp-store/providers.env --github-token-from-gh
~~~

| Piece | Azure resource | Size |
|---|---|---|
| Explorer | Container App, public HTTPS, Entra/OIDC sign-in and/or IP allow-list, scales to zero | 0.25 vCPU, 0.5 GiB |
| API | Container App, internal ingress only, one replica; compile steps run in a Landlock/seccomp-confined worker | 0.5 vCPU, 1 GiB |
| Projection | PostgreSQL Flexible Server, reachable from Azure services only, TLS | Burstable B1ms, 32 GB |
| Object store, git mirrors, `providers.env` | Azure Files share mounted at `/data` | Standard LRS |
| Images | Container Registry | Basic |
| Identity | One user-assigned managed identity: pulls images, signs in to Azure model deployments and Key Vault | |
| Credentials | Key Vault (RBAC only): tokens from GitHub device sign-in | Standard |

Expect roughly USD 40–60 a month, mostly the API replica and PostgreSQL. Model calls are billed separately by use.

- **Access.** With `--entra` (or `--oidc-issuer`, see [Sign-in](sign-in.md)) people sign in and the Explorer is open to the internet unless you also pass `--allow-ip`. Without sign-in, only your current public IP may open it (the script asks a public IP-lookup service once); add addresses with `--allow-ip CIDR`, or open it with `--allow-any`. The API is never exposed outside the environment.
- **Entra app registration.** `--entra` creates or updates `<prefix>-explorer-<group>` in your current tenant: redirect URIs `<url>/auth/callback` and `<url>/auth/signed-out`, app roles `RKP.Admin` and `RKP.Viewer`, user assignment required, and `RKP.Admin` for you. Give others access under Enterprise applications → the app → Users and groups. `az group delete` does not remove the registration; delete it with `az ad app delete`.
- **Secrets.** Every run generates new PostgreSQL passwords, a new Explorer-to-API token and, with sign-in, a new client secret and session key (everyone signs in again), and passes them as Container App secrets; nothing is written locally. Data is kept across runs.
- **Models.** `--providers-file` uploads a `providers.env` (the same file used locally). `azure-cli` sign-in becomes `managed-identity`, and the identity is granted "Cognitive Services User" on each Azure AI resource named in the file.
- **Repositories.** The container has no `gh`, so the GitHub CLI connection is hidden there. Sign in to GitHub from the web app instead:
  1. Create a GitHub OAuth App once (GitHub → Settings → Developer settings → **OAuth Apps** → New, not GitHub Apps). Use the Explorer URL as both homepage and callback URL; device sign-in never redirects, but the form requires one. Tick **Enable Device Flow** and click **Update application** to save it. The client ID is not a secret, and no client secret is needed.
  2. In the Explorer, Settings → Connections → Add connection: Authentication **GitHub device sign-in**, the client ID, credential reference empty. **Save settings** (the sign-in buttons stay disabled until the connection is saved).
  3. **Start device sign-in**, open the GitHub link and enter the code. GitHub asks for the `repo` scope: read and write access to your repositories, the narrowest OAuth scope that covers private ones. Click **Grant** next to any organization whose repositories you want to compile, then **Authorize**.
  4. **Check authorization**. RKP asks GitHub at most every 5 seconds, so a check right after approving can still say pending; check again. The token is stored in the deployment's Key Vault by the app's identity and never shown again. **Test connection** should then say *connected* and count your repositories.

  If GitHub refuses, Settings says why: *Enable Device Flow* is not saved on the app, or the client ID is wrong. Only the app's identity can read the vault, so `az keyvault secret list` with your own account is refused; that is intended. To sign in again, or to change account, run **Start device sign-in** again and the stored token is replaced.

  For read-only access, a **GitHub App** connection with only *Contents: Read* and *Metadata: Read* also works. On Azure, its private key must go into the Key Vault: grant yourself *Key Vault Secrets Officer* on the vault first, then use a `vault:<name>` reference. This has not been checked live. Alternatively `--github-token-from-gh` mounts your current `gh` token for a GitHub token connection with credential reference `managed:github-token`. Local paths cannot be compiled in the hosted API.
- **Images** are built in the registry (`az acr build`), so local Docker is not needed. `Dockerfile` and `explorer/Dockerfile` also build locally.
- **Region.** Everything goes to `-l` (default eastus2). If PostgreSQL fails with `SkuNotAvailable`, the small tier is not offered to your subscription there; pass `--db-location centralus` (or another region) for the database only.
- **Remove everything** with `az group delete -n rkp`. Key Vault keeps a deleted vault for 7 days; to redeploy into the same group sooner, purge it first (`az keyvault purge -n <vault name>`).

The API runs against the hosted database through `RKP_DATABASE_URL` (owner connection, used to apply the schema) and `RKP_APP_DB_PASSWORD` (the restricted role it then connects as, so row-level security still applies). `RKP_API_HOST=0.0.0.0` makes it listen inside the container.
