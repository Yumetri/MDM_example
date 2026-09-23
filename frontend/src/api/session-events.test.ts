import { describe, expect, it, vi } from 'vitest'
import { connectSessionEvents } from './session-events'
import { SESSION_STATE_KEY } from './session-state'

describe('browser session events', () => {
  it('rechecks persisted state on cross-tab changes, focus, and history restoration; removes listeners on cleanup', () => {
    const synchronize = vi.fn<() => Promise<void>>().mockResolvedValue(undefined)
    const disconnect = connectSessionEvents({ synchronize }, window)
    window.dispatchEvent(new StorageEvent('storage', { key: 'unrelated' }))
    expect(synchronize).not.toHaveBeenCalled()
    window.dispatchEvent(new StorageEvent('storage', { key: SESSION_STATE_KEY }))
    window.dispatchEvent(new Event('focus'))
    window.dispatchEvent(new Event('pageshow'))
    expect(synchronize).toHaveBeenCalledTimes(3)
    disconnect()
    window.dispatchEvent(new Event('focus'))
    expect(synchronize).toHaveBeenCalledTimes(3)
  })

  it('handles storage clearing and contains errors already reported by the controller', async () => {
    const synchronize = vi.fn<() => Promise<void>>().mockRejectedValue(new Error('already presented'))
    const disconnect = connectSessionEvents({ synchronize }, window)
    window.dispatchEvent(new StorageEvent('storage', { key: null }))
    await Promise.resolve()
    expect(synchronize).toHaveBeenCalledOnce()
    disconnect()
  })
})
