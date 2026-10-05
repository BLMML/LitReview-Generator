import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api/resources'
import { ApiError, clearCsrf, refreshCsrf, unauthorizedHandler } from '../api/client'
import type { User } from '../types/contracts'
export const useSession = defineStore('session', () => {
  const user = ref<User | null>(null),
    initialized = ref(false),
    expired = ref(false)
  let flight: Promise<void> | null = null
  unauthorizedHandler(() => {
    user.value = null
    expired.value = initialized.value
    clearCsrf()
  })
  async function init() {
    if (initialized.value) return
    if (!flight)
      flight = (async () => {
        await refreshCsrf()
        try {
          user.value = (await api.me()).data
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 401)) throw e
        }
        initialized.value = true
      })().finally(() => {
        flight = null
      })
    await flight
  }
  async function login(username: string, password: string) {
    await refreshCsrf()
    const response = await api.login(username, password)
    await refreshCsrf()
    user.value = response.data
    initialized.value = true
    expired.value = false
  }
  async function logout() {
    await api.logout()
    user.value = null
    clearCsrf()
    expired.value = false
  }
  return { user, initialized, expired, init, login, logout }
})
