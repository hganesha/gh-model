# Sign-in

The Explorer can require sign-in with Microsoft Entra ID or any OpenID Connect provider. It is the only public part of RKP, so the login lives there. It uses the authorization code flow with PKCE, a nonce and a signed state cookie. The ID token is verified against the provider's published keys, issuer, audience and lifetime. The session is an HttpOnly, HMAC-signed cookie (8 hours). The Explorer's proxy then tells the internal API who is acting (`X-RKP-Actor`) and with which role (`X-RKP-Role`). The browser cannot set these headers, and the API accepts them only together with the Explorer's token. Reviews and map edits are recorded under the user's sign-in name instead of `local-admin`.

| Variable (Explorer) | |
|---|---|
| `RKP_ENTRA_TENANT_ID` | Entra tenant; sets the issuer to `https://login.microsoftonline.com/<tenant>/v2.0` and the user id to tenant + object id |
| `RKP_OIDC_ISSUER` | any other OIDC issuer (https) |
| `RKP_OIDC_CLIENT_ID`, `RKP_OIDC_CLIENT_SECRET` | the app registration |
| `RKP_PUBLIC_URL` | the Explorer's public origin; register `<url>/auth/callback` and `<url>/auth/signed-out` |
| `RKP_SESSION_SECRET` | at least 32 characters |
| `RKP_OIDC_ADMIN_ROLES` | optional comma-separated app roles (Entra `roles` claim) or group IDs (`groups`) that may change things; everyone else is read-only. Empty: every signed-in user can |

With neither issuer variable set, sign-in is off under `next dev`. A production build then refuses every request unless `RKP_ALLOW_ANONYMOUS=1` says another control restricts access; `deploy.sh` sets it when it deploys without sign-in, behind the IP allow-list. A half-configured sign-in refuses every request rather than run open. Read-only users can browse everything. Connecting or compiling repositories, editing the map, reviewing placements and opening Settings return 403. For Entra, prefer app roles over groups: a user in many groups gets a group-overage reference instead of the list.
