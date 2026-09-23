import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type AccessGrant, type AuthApi, type Profile } from './auth-api'
import { CompletedActionError, SessionChangedError, SessionController } from './session-controller'
import type { SessionLock } from './session-lock'
import { createSessionState } from './session-state'

const user: Profile = { id: 'one', email: 'one@example.net', name: '사용자', effective_role: 'USER' }
const grant: AccessGrant = { access_token: 'memory-only-secret', token_type: 'Bearer', expires_in: 900 }

function serialLock(): SessionLock {
  let tail = Promise.resolve()
  return {
    assertAvailable() {},
    run<T>(operation: () => Promise<T>) {
      const result = tail.then(operation)
      tail = result.then(() => undefined, () => undefined)
      return result
    },
  }
}

function fixture(lock = serialLock()) {
  let time = 0
  const api = {
    login: vi.fn<AuthApi['login']>().mockResolvedValue(grant),
    refresh: vi.fn<AuthApi['refresh']>().mockResolvedValue(grant),
    me: vi.fn<AuthApi['me']>().mockResolvedValue(user),
    logout: vi.fn<AuthApi['logout']>().mockResolvedValue(undefined),
    requestRegistration: vi.fn<AuthApi['requestRegistration']>().mockResolvedValue({ message: '안내' }),
    completeRegistration: vi.fn<AuthApi['completeRegistration']>().mockResolvedValue(grant),
    requestPasswordReset: vi.fn<AuthApi['requestPasswordReset']>().mockResolvedValue({ message: '안내' }),
    completePasswordReset: vi.fn<AuthApi['completePasswordReset']>().mockResolvedValue(undefined),
    changePassword: vi.fn<AuthApi['changePassword']>().mockResolvedValue(undefined),
  }
  const state = createSessionState(() => localStorage)
  const sleep = vi.fn<(milliseconds: number) => Promise<void>>().mockResolvedValue(undefined)
  const hasCsrf = vi.fn(() => true)
  const controller = new SessionController({ api, state, lock, sleep, hasCsrf, now: () => time })
  return { api, state, controller, sleep, hasCsrf, advance: (ms: number) => { time += ms } }
}

beforeEach(() => localStorage.clear())

describe('in-memory browser session', () => {
  it('restores using refresh and publishes only the profile to UI subscribers', async () => {
    const { controller, api } = fixture()
    await controller.initialize()
    expect(api.refresh).toHaveBeenCalledOnce()
    expect(api.me).toHaveBeenCalledWith(grant.access_token)
    expect(controller.getSnapshot()).toMatchObject({ status: 'authenticated', profile: user })
    expect(JSON.stringify(controller.getSnapshot())).not.toContain(grant.access_token)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('does not refresh without a CSRF cookie or periodically while idle', async () => {
    const f = fixture()
    f.hasCsrf.mockReturnValue(false)
    await f.controller.initialize()
    expect(f.controller.getSnapshot().status).toBe('anonymous')
    expect(f.api.refresh).not.toHaveBeenCalled()
    await f.controller.login({ email: user.email, password: 'password' })
    f.advance(900_000)
    expect(f.api.refresh).not.toHaveBeenCalled()
    await f.controller.loadProfile()
    expect(f.api.refresh).toHaveBeenCalledOnce()
  })

  it('coalesces same-tab startup and reuses an unexpired access token', async () => {
    const f = fixture()
    await Promise.all([f.controller.initialize(), f.controller.initialize()])
    await f.controller.loadProfile()
    expect(f.api.refresh).toHaveBeenCalledOnce()
  })

  it('retries a refresh conflict once after Retry-After, then surfaces a second conflict', async () => {
    const f = fixture()
    f.api.refresh.mockRejectedValue(new ApiError(409, 'refresh_conflict', 1))
    await expect(f.controller.initialize()).rejects.toMatchObject({ code: 'refresh_conflict' })
    expect(f.sleep).toHaveBeenCalledExactlyOnceWith(1000)
    expect(f.api.refresh).toHaveBeenCalledTimes(2)
  })

  it('keeps existing authentication on a CSRF failure without automatic resubmission', async () => {
    const f = fixture()
    await f.controller.initialize()
    f.api.logout.mockRejectedValue(new ApiError(403, 'CSRF_VALIDATION_FAILED'))
    await expect(f.controller.logout()).rejects.toMatchObject({ code: 'CSRF_VALIDATION_FAILED' })
    expect(f.controller.getSnapshot().profile).toEqual(user)
    expect(f.api.logout).toHaveBeenCalledOnce()
    expect(f.state.read()).toBeNull()
  })

  it('clears other tabs on logout without sending access tokens between tabs', async () => {
    const lock = serialLock()
    const a = fixture(lock)
    const b = fixture(lock)
    await a.controller.initialize()
    await b.controller.initialize()
    await a.controller.logout()
    await b.controller.synchronize()
    expect(a.controller.getSnapshot().status).toBe('anonymous')
    expect(b.controller.getSnapshot().status).toBe('anonymous')
    expect(b.controller.getSnapshot().notice).not.toContain('요청을 취소')
    expect(b.api.refresh).toHaveBeenCalledOnce()
    expect(JSON.stringify(localStorage)).not.toMatch(/memory-only-secret|one@example.net/)
  })

  it('cancels a queued logout when another tab changes account, even without a storage event', async () => {
    const lock = serialLock()
    const a = fixture(lock)
    const b = fixture(lock)
    await a.controller.initialize()
    await b.controller.initialize()
    let release!: () => void
    const hold = lock.run(() => new Promise<void>((resolve) => { release = resolve }))
    await Promise.resolve()
    const login = a.controller.login({ email: 'two@example.net', password: 'password' })
    const logout = b.controller.logout()
    const rejected = expect(logout).rejects.toBeInstanceOf(SessionChangedError)
    release()
    await hold
    await login
    await rejected
    expect(b.api.logout).not.toHaveBeenCalled()
    await b.controller.synchronize()
    expect(b.controller.getSnapshot().status).toBe('authenticated')
    expect(b.controller.getSnapshot().notice).toBe(new SessionChangedError().message)
  })

  it('rechecks the latest session after another tab logs in', async () => {
    const f = fixture()
    await f.controller.initialize()
    f.state.publish('signed-in')
    f.api.me.mockResolvedValue({ ...user, id: 'two', name: '다른 사용자' })
    await f.controller.synchronize()
    expect(f.api.refresh).toHaveBeenCalledTimes(2)
    expect(f.controller.getSnapshot().profile?.id).toBe('two')
  })

  it('still restores the changed session after a foreground request already detected the marker', async () => {
    const f = fixture()
    await f.controller.initialize()
    f.state.publish('signed-in')
    await expect(f.controller.logout()).rejects.toBeInstanceOf(SessionChangedError)
    expect(f.api.logout).not.toHaveBeenCalled()
    await f.controller.synchronize()
    expect(f.api.refresh).toHaveBeenCalledTimes(2)
    expect(f.controller.getSnapshot().status).toBe('authenticated')
  })

  it('blocks all authentication operations if storage becomes unavailable', async () => {
    const f = fixture()
    await f.controller.initialize()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    await expect(f.controller.logout()).rejects.toThrow()
    expect(f.api.logout).not.toHaveBeenCalled()
    expect(f.controller.getSnapshot()).toMatchObject({ status: 'blocked', profile: null })
    await expect(f.controller.requestRegistration(user.email)).rejects.toThrow()
    expect(f.api.requestRegistration).not.toHaveBeenCalled()
  })

  it('restores all tabs with the new session after a password change and cancels queued requests', async () => {
    const lock = serialLock()
    const a = fixture(lock)
    const b = fixture(lock)
    await a.controller.initialize()
    await b.controller.initialize()
    const change = a.controller.changePassword({ current_password: 'old-password', new_password: 'new-password' })
    const pending = expect(b.controller.logout()).rejects.toBeInstanceOf(SessionChangedError)
    await change
    await pending
    await b.controller.synchronize()
    expect(a.api.changePassword).toHaveBeenCalledWith(grant.access_token, {
      current_password: 'old-password', new_password: 'new-password',
    })
    expect(a.api.refresh).toHaveBeenCalledTimes(2)
    expect(b.api.refresh).toHaveBeenCalledTimes(2)
    expect(b.api.logout).not.toHaveBeenCalled()
    expect(a.controller.getSnapshot().status).toBe('authenticated')
    expect(b.controller.getSnapshot().status).toBe('authenticated')
    expect(JSON.stringify(localStorage)).not.toMatch(/password|memory-only-secret/)
  })

  it('keeps the session and does not broadcast a rejected password change', async () => {
    const f = fixture()
    await f.controller.initialize()
    f.api.changePassword.mockRejectedValue(new ApiError(400, 'INVALID_CURRENT_PASSWORD'))
    await expect(f.controller.changePassword({ current_password: 'wrong', new_password: 'new' }))
      .rejects.toMatchObject({ code: 'INVALID_CURRENT_PASSWORD' })
    expect(f.state.read()).toBeNull()
    expect(f.api.refresh).toHaveBeenCalledOnce()
    expect(f.controller.getSnapshot().profile).toEqual(user)
  })

  it('reports registration completion separately from a failed profile lookup', async () => {
    const f = fixture()
    f.hasCsrf.mockReturnValue(false)
    await f.controller.initialize()
    f.api.me.mockRejectedValueOnce(new ApiError(503, 'SERVICE_UNAVAILABLE'))
    await expect(f.controller.completeRegistration({ token: 'link', name: '사용자', password: 'password' }))
      .rejects.toBeInstanceOf(CompletedActionError)
    f.hasCsrf.mockReturnValue(true)
    await f.controller.initialize()
    expect(f.api.completeRegistration).toHaveBeenCalledOnce()
    expect(f.controller.getSnapshot().status).toBe('authenticated')
  })

  it('never reports a completed password change as a rejected password on recovery failure', async () => {
    const f = fixture()
    await f.controller.initialize()
    f.api.refresh.mockRejectedValueOnce(new ApiError(0, 'NETWORK_ERROR'))
    await expect(f.controller.changePassword({ current_password: 'old', new_password: 'new' }))
      .rejects.toMatchObject({ name: 'CompletedActionError', action: 'password-change' })
    expect(f.api.changePassword).toHaveBeenCalledOnce()
    expect(f.controller.getSnapshot().notice).toContain('비밀번호 변경은 완료')
  })
})
