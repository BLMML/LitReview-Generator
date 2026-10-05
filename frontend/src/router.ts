import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'
import { useSession } from './stores/session'
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/projects' },
    { path: '/login', component: () => import('./pages/LoginPage.vue') },
    { path: '/projects', component: () => import('./pages/ProjectsPage.vue') },
    { path: '/projects/:id', component: () => import('./pages/WorkspacePage.vue') },
    { path: '/reviews/:id', component: () => import('./pages/ReviewPage.vue') },
    { path: '/:pathMatch(.*)*', component: () => import('./pages/NotFoundPage.vue') },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
router.beforeEach(async (to) => {
  const session = useSession()
  try {
    await session.init()
  } catch {
    if (to.path !== '/login') return { path: '/login' }
    return
  }
  if (!session.user && to.path !== '/login') return { path: '/login', query: { next: to.fullPath } }
  if (session.user && to.path === '/login') return '/projects'
})
router.afterEach(async () => {
  await nextTick()
  document.querySelector<HTMLElement>('#main')?.focus()
})
