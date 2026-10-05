import { ref, getCurrentScope, onScopeDispose } from 'vue'
import { ApiError, Submission, errorMessage } from '../api/client'
export function useAction() {
  let active = true
  if (getCurrentScope())
    onScopeDispose(() => {
      active = false
    })
  const pending = ref(false),
    error = ref(''),
    requestId = ref(''),
    success = ref('')
  async function perform<T>(fn: () => Promise<T>, done?: (value: T) => void | Promise<void>) {
    if (pending.value) return
    pending.value = true
    error.value = ''
    requestId.value = ''
    success.value = ''
    try {
      const result = await fn()
      if (active) await done?.(result)
      return result
    } catch (e) {
      error.value = errorMessage(e)
      if (e instanceof ApiError) requestId.value = e.requestId
    } finally {
      pending.value = false
    }
  }
  const submission = new Submission()
  return { pending, error, requestId, success, perform, submission, active: () => active }
}
