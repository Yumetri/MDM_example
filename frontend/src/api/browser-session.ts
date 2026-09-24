import { createAuthApi } from './auth-api'
import { SessionController } from './session-controller'
import { createSessionLock } from './session-lock'
import { createSessionState } from './session-state'

export function createBrowserSession(target: Window) {
  return new SessionController({
    api: createAuthApi(target.fetch.bind(target), () => target.document.cookie),
    lock: createSessionLock(target.navigator.locks),
    state: createSessionState(() => target.localStorage),
    hasCsrf: () => target.document.cookie.split(';').some((part) => part.trim().startsWith('mdm_csrf=')),
  })
}
