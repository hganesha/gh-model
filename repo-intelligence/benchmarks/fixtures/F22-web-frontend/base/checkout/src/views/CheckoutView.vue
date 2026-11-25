<template>
  <section class="checkout">
    <h1>{{ $t('checkout.title') }}</h1>
    <form @submit.prevent="placeOrder">
      <input v-model="form.email" type="email" required :placeholder="$t('checkout.email')" />
      <select v-model="form.shipping">
        <option value="standard">Standard</option>
        <option value="express">Express</option>
      </select>
      <el-input v-model="form.coupon" />
      <router-link to="/cart">Back to cart</router-link>
      <button type="submit">Place order</button>
    </form>
  </section>
</template>

<script setup lang="ts">
import { reactive } from "vue";
import { useRouter } from "vue-router";

const router = useRouter();
const form = reactive({ email: "", shipping: "standard", coupon: "" });

async function placeOrder() {
  await fetch("/api/checkout", { method: "POST", body: JSON.stringify(form) });
  router.push("/thanks");
}
</script>

<style scoped lang="scss">
@use "../../../src/styles/variables" as *;

.checkout {
  --accent: #8250df;
  padding: $gutter;
  color: var(--accent);
  border-color: var(--color-primary);
}
</style>
