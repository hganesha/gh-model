import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import CartView from "../views/CartView.vue";

const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/cart" },
  { path: "/cart", name: "cart", component: CartView },
  {
    path: "/checkout",
    name: "checkout",
    component: () => import("../views/CheckoutView.vue"),
    meta: { requiresAuth: true, roles: ["customer"] },
  },
  { path: "/thanks", name: "thanks", component: () => import("../views/ThanksView.vue") },
];

export default createRouter({
  history: createWebHistory(),
  routes,
});
