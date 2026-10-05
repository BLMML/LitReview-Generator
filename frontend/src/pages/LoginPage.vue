<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSession } from '../stores/session'
import { useAction } from '../composables/action'
import ErrorNotice from '../components/ErrorNotice.vue'
const username = ref(''),
  password = ref(''),
  show = ref(false),
  session = useSession(),
  action = useAction(),
  route = useRoute(),
  router = useRouter()
async function submit() {
  await action.perform(
    () => session.login(username.value.trim(), password.value),
    () => {
      password.value = ''
      const next = String(route.query.next ?? '/projects')
      return router
        .push(
          next.startsWith('/') && !next.startsWith('//') && !next.startsWith('/login')
            ? next
            : '/projects',
        )
        .then(() => {})
    },
  )
}
</script>
<template>
  <div class="login-layout page">
    <section class="login-story">
      <p class="eyebrow">A WORKSPACE FOR THINKING</p>
      <h1>读过的论文，<br />成为<span class="scribble">有据可循</span><br />的手稿。</h1>
      <p class="lede">整理文献，辨认主题，回看证据。<br />把研究过程留在同一张工作台上。</p>
      <div class="notebook-art" aria-hidden="true">
        <div class="tape"></div>
        <span class="art-label">研究笔记 / WORK IN PROGRESS</span>
        <div class="art-line">论文 → 证据 → 综述</div>
        <svg viewBox="0 0 260 70">
          <path
            d="M8 29 Q60 8 119 28 T247 25 M191 11l57 14-32 25"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          />
        </svg>
        <p>原文页码 · 历史版本 · 人工复核</p>
      </div>
    </section>
    <section class="login-sheet sheet">
      <div class="tape"></div>
      <p class="eyebrow">01 / OPEN YOUR NOTEBOOK</p>
      <h2>进入工作台</h2>
      <p class="muted">使用管理员为你创建的账号登录。</p>
      <p v-if="session.expired" class="notice warning">登录已过期，请重新登录。</p>
      <ErrorNotice :message="action.error.value" :request-id="action.requestId.value" />
      <form @submit.prevent="submit">
        <label for="username">用户名</label
        ><input
          id="username"
          v-model="username"
          required
          maxlength="150"
          autocomplete="username"
        /><label for="password">密码</label>
        <div class="password-row">
          <input
            id="password"
            v-model="password"
            required
            maxlength="256"
            :type="show ? 'text' : 'password'"
            autocomplete="current-password"
          /><button type="button" class="quiet" :aria-pressed="show" @click="show = !show">
            {{ show ? '隐藏' : '显示' }}
          </button>
        </div>
        <button class="primary wide" :disabled="action.pending.value">
          {{ action.pending.value ? '正在登录…' : '登录，打开手稿 →' }}
        </button>
      </form>
      <p class="footnote">你的项目和原文仅在登录后可访问。</p>
    </section>
  </div>
</template>
