<script setup lang="ts">
defineProps<{ message?: string; requestId?: string }>()
</script>
<template>
  <div v-if="message" class="notice danger" role="alert">
    <strong>操作未完成</strong>
    <p>{{ message }}</p>
    <small v-if="requestId">请求编号 {{ requestId }}</small
    ><slot />
  </div>
</template>
