"""The web pack's vocabulary: platform terms and the constructs it looks for.

Kept apart from the pack definition so that the scanners can name terms and
constructs without importing the package that lists them.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind
from repo_intel.platforms import Construct, Term

K = EntityKind

TERMS = (
    Term("web:Route", K.SCREEN, "lossy", "a route in router configuration (React Router, Vue Router, Angular) or a file route (Next.js, Nuxt); "
         "name is the full route path; the component, guards, layouts and lazy loading live in attributes"),
    Term("web:Page", K.SCREEN, "lossy", "a static HTML document; name is its path from the repository root"),
    Term("web:View", K.SCREEN, "lossy", "a component module or template fragment with forms or navigation links whose route, if any, is declared "
         "in another file; name is its path from the repository root. A route that imports it CONTAINS it"),
    Term("web:Form", K.FORM, "lossy", "a <form> element or a form-library form (Formik, react-hook-form, vee-validate, Angular reactive forms)"),
    Term("web:Control", K.UI_CONTROL, "lossy", "an input, select, textarea, button or link, native or from a component library; "
         "the binding, type and validation live in attributes"),
    Term("web:CssVariable", K.STYLE_TOKEN, "lossy", "a CSS custom property (--name); system is the stylesheet path from the repository root"),
    Term("web:ScssVariable", K.STYLE_TOKEN, "lossy", "a top-level SCSS ($name) or LESS (@name) variable; system is the stylesheet path from the repository root"),
    Term("web:ThemeToken", K.STYLE_TOKEN, "lossy", "a leaf of an exported theme or design-token object, by its literal key path; system is the module path from the repository root"),
    Term("web:Role", K.ROLE, "exact", "a role name a route (roles, meta.roles, data.roles), route guard or guard component lists literally; "
         "realm is 'web'. The guard itself is not the role: its name stays in the screen's `guards` attribute"),
    Term("web:ModelField", K.FIELD, "lossy", "a field of a form's model, declared with literal fields in the same file (a TypeScript interface "
         "or type alias, a Zod or Yup object schema); table is '<file path>#<model name>', name the field's dotted path. "
         "Types, optionality and validation stay in the source"),
)


def _c(id: str, description: str, modelled_as: str) -> Construct:
    return Construct(id, description, modelled_as)


CONSTRUCTS = (
    # Routing
    _c("web:react-router-route", "a <Route> element (React Router)", "entity:Screen"),
    _c("web:route-object", "a route object in a routes array (createBrowserRouter, useRoutes, Vue Router, Angular Routes)", "entity:Screen"),
    _c("web:nextjs-app-page", "a Next.js app/ router page file", "entity:Screen"),
    _c("web:nextjs-pages-route", "a Next.js pages/ router file", "entity:Screen"),
    _c("web:nextjs-layout", "a Next.js app/ router layout file", "unmapped"),
    _c("web:nuxt-page", "a Nuxt pages/ file with definePageMeta", "entity:Screen"),
    _c("web:static-page", "a static HTML document", "entity:Screen"),
    _c("web:view", "a component module or template with forms or links, routed elsewhere", "entity:Screen"),
    _c("web:lazy-route", "a lazily loaded route (lazy(), loadChildren, loadComponent, import())", "attribute:lazy"),
    _c("web:layout-route", "a route that renders a layout around child routes", "attribute:layouts"),
    _c("web:redirect-route", "a route that only redirects (redirect, redirectTo, <Navigate>)", "unmapped"),
    _c("web:dynamic-route", "a route whose path is not a literal, or whose prefix the file does not show", "unmapped"),
    _c("web:route-spread", "routes spread in from another module (...otherRoutes)", "unmapped"),
    _c("web:route-guard", "a route guard (canActivate, beforeEnter, requiresAuth, a guard wrapper component)", "attribute:guards"),
    _c("web:route-role", "a literal role a guard requires", "relation:REQUIRES_ROLE"),
    _c("web:role-check", "an in-component role check (hasRole('x'), isInRole)", "unmapped"),
    # Forms and controls
    _c("web:form", "a <form> element", "entity:Form"),
    _c("web:library-form", "a form component from a library (Form, el-form, v-form)", "entity:Form"),
    _c("web:form-library", "a form-state library (react-hook-form, Formik, vee-validate, Angular forms)", "attribute:form_library"),
    _c("web:input", "an <input> element", "entity:UIControl"),
    _c("web:select", "a <select> element", "entity:UIControl"),
    _c("web:textarea", "a <textarea> element", "entity:UIControl"),
    _c("web:button", "a <button> or submit input", "entity:UIControl"),
    _c("web:link", "a link (<a href>, <Link to>, <router-link>, routerLink)", "entity:UIControl"),
    _c("web:library-control", "an input, select, button or text field from a component library", "entity:UIControl"),
    _c("web:form-action", "a form action attribute", "relation:SUBMITS_TO"),
    _c("web:submit-handler", "a submit handler (onSubmit, @submit, (ngSubmit))", "relation:SUBMITS_TO"),
    _c("web:binding", "a control binding (v-model, formControlName, ngModel, register(), name)", "attribute:binds"),
    _c("web:form-model", "a form's model type or schema (useForm<T>(), zodResolver(schema), validationSchema, a typed v-model or ngModel variable)", "relation:BINDS_TO"),
    _c("web:client-validation", "client-side validation on a control (required, pattern, min, max, Validators)", "attribute:validation"),
    _c("web:programmatic-navigation", "navigation in code (navigate(), router.push(), router.navigate(), <Navigate>)", "relation:NAVIGATES_TO"),
    # Styling
    _c("web:css-custom-property", "a CSS custom property declaration", "entity:StyleToken"),
    _c("web:scss-variable", "a top-level SCSS variable", "entity:StyleToken"),
    _c("web:less-variable", "a top-level LESS variable", "entity:StyleToken"),
    _c("web:theme-object", "an exported theme or token object", "entity:StyleToken"),
    _c("web:tailwind-config", "a Tailwind configuration theme", "entity:StyleToken"),
    _c("web:token-reference", "a reference to a style token (var(--x), $x, @x, theme.x)", "relation:STYLED_BY"),
    _c("web:stylesheet-import", "a stylesheet import or link (import './x.css', <link rel=stylesheet>, @use, @import); "
       "an @import, @use or @forward is also Artifact DEPENDS_ON Artifact", "attribute:stylesheets"),
    _c("web:css-module", "a CSS module import (import styles from './x.module.css')", "attribute:stylesheets"),
    _c("web:styled-component", "a CSS-in-JS styled component (styled-components, emotion styled)", "unmapped"),
    _c("web:css-in-js", "a css`` template or css prop (emotion, styled-components, makeStyles)", "unmapped"),
    _c("web:tailwind", "Tailwind directives (@tailwind, @apply)", "unmapped"),
    _c("web:scss-mixin", "an SCSS mixin or include", "unmapped"),
    _c("web:media-query", "a media query", "unmapped"),
    _c("web:font-face", "an @font-face rule", "unmapped"),
    _c("web:keyframes", "an @keyframes animation", "unmapped"),
    # Everything else a front end does
    _c("web:i18n-key", "a translation key (t('x'), $t('x'), | translate, i18nKey)", "unmapped"),
    _c("web:aria", "an ARIA role or aria-* attribute", "unmapped"),
    _c("web:custom-element", "a custom element definition (customElements.define)", "unmapped"),
    _c("web:iframe", "an <iframe>", "unmapped"),
    _c("web:service-worker", "a service worker registration", "unmapped"),
    _c("web:web-worker", "a web worker (new Worker)", "unmapped"),
    _c("web:websocket", "a WebSocket or EventSource connection", "unmapped"),
    _c("web:browser-storage", "localStorage, sessionStorage or document.cookie", "unmapped"),
    _c("web:raw-html", "raw HTML injection (dangerouslySetInnerHTML, v-html, [innerHTML])", "unmapped"),
    _c("web:inline-script", "an inline <script> in HTML", "unmapped"),
    _c("web:external-script", "a <script src> in HTML", "unmapped"),
    _c("web:portal", "a portal (createPortal, <Teleport>)", "unmapped"),
    _c("web:state-store", "a client state store (Redux createSlice/configureStore, Pinia defineStore, NgRx createReducer)", "unmapped"),
    _c("web:data-fetching-hook", "a data-fetching hook (useQuery, useSWR, useMutation)", "unmapped"),
    _c("web:angular-template-url", "an Angular component templateUrl (template in another file)", "unmapped"),
)
