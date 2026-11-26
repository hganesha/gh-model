# shop-web

The storefront (React and React Router), the checkout (Vue) and the back office
(Angular) of a small shop, with a mock API for local development.

The package manifest lists no dependencies on purpose: the fixture tests the
web experience pack, and the cross-fixture dependency counts in the test suite
stay as they are.

Each front end has its own router. The checkout declares it in
`checkout/src/router/index.ts` and the back office in
`backoffice/src/app/app-routing.module.ts`, both from a `routes` array; the
storefront's routes are in `src/App.tsx`, mounted under the `BrowserRouter` in
`src/main.tsx`. Because the package holds more than one router, the web pack
keys the checkout's and the back office's screens by `app` as well as by path
(`checkout/src/router`, `backoffice/src/app`: the two routers share the label
`routes`, so each is named by its file's directory). The storefront's routes
show no router in their own file, so their keys stay path only.
