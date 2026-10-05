<script setup lang="ts">
import { watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useSession } from './stores/session'
import { useAction } from './composables/action'
import { confirmDiscardDraft } from './composables/draftGuard'
import { useWorkspace } from './stores/workspace'
const session = useSession(),
  router = useRouter(),
  route = useRoute(),
  action = useAction()
const workspace = useWorkspace()
watch(
  () => session.user,
  (user) => {
    if (!user) workspace.reset()
    if (!user && session.initialized && route.path !== '/login')
      void router.push({ path: '/login', query: { next: route.fullPath } })
  },
)
async function logout() {
  if (!confirmDiscardDraft()) return
  await action.perform(
    () => session.logout(),
    () => router.push('/login').then(() => {}),
  )
}
</script>
<template>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <header class="masthead">
    <RouterLink class="brand" to="/projects"
      ><span class="brand-mark" aria-hidden="true">Lr<span>↗</span></span
      ><span>文献手稿<small>LITREVIEW / RESEARCH WORKBENCH</small></span></RouterLink
    >
    <nav aria-label="主导航">
      <RouterLink v-if="session.user" to="/projects">项目档案</RouterLink
      ><span class="edition">研究 · 整理 · 回看</span>
    </nav>
    <div v-if="session.user" class="session">
      <span>{{ session.user.display_name }}</span
      ><button class="quiet" :disabled="action.pending.value" @click="logout">退出登录</button>
    </div>
    <span v-else class="stamp">FIELD NOTES / 01</span>
  </header>
  <p v-if="action.error.value" class="notice danger" role="alert">{{ action.error.value }}</p>
  <main id="main" tabindex="-1">
    <RouterView v-if="route.path === '/login' || session.user" :key="route.path" />
  </main>
  <footer class="site-footer">
    <span>从原文出发，让每一处引用都有来处。</span><span>LITREVIEW GENERATOR · 工作手稿</span>
  </footer>
</template>
