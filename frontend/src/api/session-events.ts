import { SESSION_STATE_KEY } from './session-state'

/** Events are hints: the controller rereads the marker rather than trusting event payloads. */
export function connectSessionEvents(controller: { synchronize(): Promise<void> }, target: Window): () => void {
  const synchronize = () => { void controller.synchronize().catch(() => { /* Controller presents the error. */ }) }
  const storage = (event: StorageEvent) => {
    if (event.key === SESSION_STATE_KEY || event.key === null) synchronize()
  }
  const visible = () => { if (target.document.visibilityState === 'visible') synchronize() }
  target.addEventListener('storage', storage)
  target.addEventListener('focus', synchronize)
  target.addEventListener('pageshow', synchronize)
  target.document.addEventListener('visibilitychange', visible)
  return () => {
    target.removeEventListener('storage', storage)
    target.removeEventListener('focus', synchronize)
    target.removeEventListener('pageshow', synchronize)
    target.document.removeEventListener('visibilitychange', visible)
  }
}
