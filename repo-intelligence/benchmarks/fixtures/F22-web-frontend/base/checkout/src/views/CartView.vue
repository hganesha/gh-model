<template>
  <section>
    <h1>Your cart</h1>
    <ul>
      <li v-for="item in items" :key="item.id">{{ item.name }}</li>
    </ul>
    <router-link to="/checkout">Check out</router-link>
  </section>
</template>

<script setup lang="ts">
import { ref } from "vue";

const items = ref([{ id: 1, name: "Notebook" }]);
</script>
