import { describe, expect, it, vi } from 'vitest'
import { createSessionLock, UnsupportedBrowserError } from './session-lock'

describe('session mutation lock', () => {
  it('rejects unsupported browsers before executing an authentication operation', async () => {
    const operation = vi.fn()
    const lock = createSessionLock(undefined)

    await expect(lock.run(operation)).rejects.toBeInstanceOf(UnsupportedBrowserError)
    expect(operation).not.toHaveBeenCalled()
  })

  it('reads cookies only after acquiring the shared exclusive lock and holds it until completion', async () => {
    let grant!: () => void
    let complete!: () => void
    const available = new Promise<void>((resolve) => { grant = resolve })
    const response = new Promise<void>((resolve) => { complete = resolve })
    const request = vi.fn(async (_name: string, _options: LockOptions, callback: () => Promise<unknown>) => {
      await available
      return callback()
    })
    const locks = { request } as unknown as Pick<LockManager, 'request'>
    const readCookie = vi.fn(() => 'latest-cookie')
    const lock = createSessionLock(locks)
    const finished = vi.fn()
    const pending = lock.run(async () => {
      const cookie = readCookie()
      await response
      return cookie
    }).then(finished)

    expect(readCookie).not.toHaveBeenCalled()
    grant()
    await vi.waitFor(() => expect(readCookie).toHaveBeenCalledOnce())
    expect(finished).not.toHaveBeenCalled()
    expect(request).toHaveBeenCalledWith('mdm-session-mutation', { mode: 'exclusive' }, expect.any(Function))
    complete()
    await pending
    expect(finished).toHaveBeenCalledWith('latest-cookie')
  })
})
