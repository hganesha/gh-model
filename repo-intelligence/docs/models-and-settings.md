# Business map and settings

The **Business map** and **Settings** entries are workspace tools. The API stores one active map per tenant and keeps revision history. Edit the map in place: stages are columns, capabilities are cards inside them, and **Starter capabilities** opens a searchable picker. **Lanes / Canvas** switches the view: on the canvas, drag a capability by its handle (or focus the handle and use the arrow keys) to put it anywhere; dropping it in another stage's column moves it to that stage. The arrangement is saved with the map and does not rerun placement. Assign a component by dragging it onto a capability, by choosing one of its suggested capabilities, or with its capability selector and **Place** or **Move** button. Dependencies between capabilities are drawn as arrows, dashed where a link is a candidate. The map reads the latest snapshot on each repository’s default ref when available. It flags orphaned placements, preserves confirmed edits across compiles, and uses candidate grades for links across repositories.

**Run placement** uses a local lexical shortlist and the configured decision model. With no model configured or allowed, nothing is placed automatically; the map says so and each component shows its closest capabilities for one-click placement. Model egress starts disabled. To use a hosted model, configure its server-side credentials, choose a model in Settings, and explicitly allow that provider. Turning off automatic placement keeps accepted model results in the review tray. The threshold of 0.88 gave 7 correct and no wrong placements on the 15-component benchmark with Microsoft-Decision-1.1; treat it as a starting point, not a measured accuracy. Every Azure model kind has been run live; Microsoft-Decision-1.1 is the recommended default because gpt-4.1-mini made a confident wrong placement and the reranker's scores need their own threshold (see the benchmark). Install the `azure` extra for Azure CLI authentication to a hosted decision model. The [placement benchmark](../benchmarks/results/business-placement-v1.md) records shortlist quality and the remaining live measurements.

A repository can suggest a placement with `.rkp/business.yaml`. Keys are unique component names within the repository. Use the capability ID shown on the map card.

~~~yaml
placements:
  orders-api: your-capability-id
~~~

## Connections

GitHub CLI remains available by default. Settings also supports GitHub App, token and device-flow connections, GitLab tokens, and plain Git over HTTPS or SSH. Add credentials to the OS keychain before entering the reference in Settings.

~~~bash
.venv/bin/python -m keyring set repo-intelligence github-read
~~~

Use `keychain:github-read` in the credential reference field. On a hosted single-administrator instance, mount the secret-manager values as files under `RKP_MANAGED_SECRET_DIR` and use `managed:<name>`. For SSH, set up the host key for the API’s operating-system user. For GitHub device sign-in, enable device flow on the OAuth application and enter its client ID in Settings; leave the credential reference empty and the token goes to the OS keychain, or to Key Vault (`vault:`) when `RKP_KEY_VAULT_URL` is set, as on Azure (see [Deploy to Azure](deploy-azure.md)). For an App, provide the App ID, installation ID, private key reference, and webhook secret reference.

Webhooks use `/v1/webhooks/<connection-id>`. Set the repository’s trigger to **On push** and specify its tracked refs. The local API binds to loopback. A provider needs a reachable HTTPS endpoint for live delivery. The local development token represents one workspace administrator. Sharing service credentials with additional users requires per-principal repository authorization under GI-022.
