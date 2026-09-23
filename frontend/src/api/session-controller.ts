import {
  ApiError, type AccessGrant, type AuthApi, type LoginInput, type Profile,
  type RegistrationInput, type PasswordResetInput, type PasswordChangeInput,
} from './auth-api'
import { UnsupportedBrowserError, type SessionLock } from './session-lock'
import { StorageUnavailableError, type SessionChange, type SessionChangeKind, type SessionState } from './session-state'

export class SessionChangedError extends Error {
  constructor() {
    super('다른 탭에서 로그인 상태가 변경되어 요청을 취소했습니다. 현재 계정을 확인한 뒤 다시 제출해 주세요.')
    this.name = 'SessionChangedError'
  }
}

export class CompletedActionError extends Error {
  constructor(readonly action: 'login' | 'registration' | 'password-change') {
    const label = { login: '로그인은', registration: '가입은', 'password-change': '비밀번호 변경은' }[action]
    super(`${label} 완료됐지만 로그인 상태를 확인하지 못했습니다. 세션 확인을 다시 시도하거나 로그인해 주세요.`)
    this.name = 'CompletedActionError'
  }
}

export interface SessionSnapshot {
  status: 'loading' | 'authenticated' | 'anonymous' | 'blocked'
  profile: Profile | null
  notice: string | null
  revision: number
}

interface Dependencies {
  api: AuthApi
  lock: SessionLock
  state: SessionState
  hasCsrf: () => boolean
  now?: () => number
  sleep?: (milliseconds: number) => Promise<void>
}

export class SessionController {
  private snapshot: SessionSnapshot = { status: 'loading', profile: null, notice: null, revision: 0 }
  private listeners = new Set<() => void>()
  private credential: { token: string; expiresAt: number } | null = null
  private observed = false
  private marker: SessionChange | null = null
  private restoreNeeded = false
  private cancellationNotice: string | null = null
  private initializing: Promise<void> | null = null
  private synchronizing: Promise<void> | null = null
  private readonly now: () => number
  private readonly sleep: (milliseconds: number) => Promise<void>

  constructor(private readonly deps: Dependencies) {
    this.now = deps.now ?? Date.now
    this.sleep = deps.sleep ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)))
  }

  getSnapshot = (): SessionSnapshot => this.snapshot
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  private update(values: Partial<SessionSnapshot>) {
    this.snapshot = { ...this.snapshot, ...values }
    this.listeners.forEach((listener) => listener())
  }

  private clear(notice: string | null = null) {
    this.credential = null
    this.cancellationNotice = null
    this.update({ status: 'anonymous', profile: null, notice, revision: this.snapshot.revision + 1 })
  }

  private available() {
    this.deps.lock.assertAvailable()
    this.deps.state.assertAvailable()
  }

  private observe(): boolean {
    const current = this.deps.state.read()
    const changed = this.observed && (current?.id ?? null) !== (this.marker?.id ?? null)
    this.marker = current
    this.observed = true
    if (changed) {
      this.clear(current?.kind === 'signed-in'
        ? '다른 탭의 로그인 상태를 확인하고 있습니다.'
        : '다른 탭에서 로그아웃하거나 비밀번호를 재설정했습니다. 다시 로그인해 주세요.')
      this.restoreNeeded = current?.kind === 'signed-in'
      if (this.restoreNeeded) this.update({ status: 'loading' })
    }
    return changed
  }

  private publish(kind: SessionChangeKind) {
    this.marker = this.deps.state.publish(kind)
    this.observed = true
    this.restoreNeeded = false
    this.clear()
    if (kind === 'signed-in') this.update({ status: 'loading' })
  }

  private failed(error: unknown): never {
    if (error instanceof SessionChangedError) this.cancellationNotice = error.message
    if (error instanceof StorageUnavailableError || error instanceof UnsupportedBrowserError) {
      this.credential = null
      this.update({ status: 'blocked', profile: null, notice: error.message })
    } else if (error instanceof ApiError && (error.code === 'INVALID_SESSION' || error.code === 'INVALID_ACCESS_TOKEN')) {
      this.clear(error.message)
    } else {
      this.update({
        status: this.snapshot.status === 'loading' ? 'anonymous' : this.snapshot.status,
        notice: error instanceof ApiError || error instanceof SessionChangedError || error instanceof CompletedActionError
          ? error.message : '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.',
      })
    }
    throw error
  }

  private remember(grant: AccessGrant, started: number) {
    // Use request start to avoid extending validity by network response latency.
    this.credential = { token: grant.access_token, expiresAt: started + grant.expires_in * 1000 }
  }

  private async refreshLocked(): Promise<void> {
    const started = this.now()
    let grant: AccessGrant
    try { grant = await this.deps.api.refresh() } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409 || error.code !== 'refresh_conflict') throw error
      await this.sleep((error.retryAfterSeconds ?? 1) * 1000)
      // A fresh transport call reads the current CSRF cookie; no stale request is replayed.
      grant = await this.deps.api.refresh()
    }
    this.remember(grant, started)
  }

  private async profileLocked(preserveCancellation = false) {
    if (!this.credential || this.now() >= this.credential.expiresAt) await this.refreshLocked()
    const profile = await this.deps.api.me(this.credential!.token)
    this.restoreNeeded = false
    if (!preserveCancellation) this.cancellationNotice = null
    this.update({ status: 'authenticated', profile, notice: this.cancellationNotice })
  }

  initialize(): Promise<void> {
    if (this.initializing) return this.initializing
    const operation = async () => {
      try {
        this.available()
        await this.deps.lock.run(async () => {
          this.available()
          this.observe()
          if (!this.deps.hasCsrf()) { this.clear(); return }
          await this.refreshLocked()
          await this.profileLocked()
        })
      } catch (error) {
        if (error instanceof ApiError && error.code === 'INVALID_SESSION') { this.clear(); return }
        this.failed(error)
      }
    }
    this.initializing = operation().finally(() => { this.initializing = null })
    return this.initializing
  }

  async synchronize(): Promise<void> {
    try {
      this.available()
      this.observe()
      if (this.synchronizing) return this.synchronizing
      if (!this.restoreNeeded || this.marker?.kind !== 'signed-in') return
      this.synchronizing = this.deps.lock.run(async () => {
        this.available()
        this.observe()
        if (this.marker?.kind !== 'signed-in') return
        await this.refreshLocked()
        await this.profileLocked(true)
      }).finally(() => { this.synchronizing = null })
      await this.synchronizing
    } catch (error) { this.failed(error) }
  }

  private async locked<T>(operation: () => Promise<T>): Promise<T> {
    try {
      this.available()
      if (this.observe()) throw new SessionChangedError()
      const expected = this.marker?.id ?? null
      return await this.deps.lock.run(async () => {
        this.available()
        this.observe()
        if (expected !== (this.marker?.id ?? null)) throw new SessionChangedError()
        return operation()
      })
    } catch (error) { return this.failed(error) }
  }

  private async afterCompletion(action: CompletedActionError['action'], operation: () => Promise<void>) {
    try { await operation() } catch (error) {
      try { this.failed(error) } catch { /* Preserve the classified session state. */ }
      const completed = new CompletedActionError(action)
      this.update({ notice: completed.message })
      throw completed
    }
  }

  async login(input: LoginInput): Promise<void> {
    await this.locked(async () => {
      const started = this.now()
      const grant = await this.deps.api.login(input)
      await this.afterCompletion('login', async () => {
        this.publish('signed-in')
        this.remember(grant, started)
        await this.profileLocked()
      })
    })
  }

  async completeRegistration(input: RegistrationInput): Promise<void> {
    await this.locked(async () => {
      const started = this.now()
      const grant = await this.deps.api.completeRegistration(input)
      await this.afterCompletion('registration', async () => {
        this.publish('signed-in')
        this.remember(grant, started)
        await this.profileLocked()
      })
    })
  }

  async logout(): Promise<void> {
    await this.locked(async () => {
      await this.deps.api.logout()
      this.publish('signed-out')
    })
  }

  async completePasswordReset(input: PasswordResetInput): Promise<void> {
    await this.locked(async () => {
      await this.deps.api.completePasswordReset(input)
      this.publish('signed-out')
    })
  }

  async loadProfile(): Promise<void> { await this.locked(() => this.profileLocked()) }

  async changePassword(input: PasswordChangeInput): Promise<void> {
    await this.locked(async () => {
      if (!this.credential || this.now() >= this.credential.expiresAt) await this.refreshLocked()
      await this.deps.api.changePassword(this.credential!.token, input)
      await this.afterCompletion('password-change', async () => {
        this.publish('signed-in')
        await this.refreshLocked()
        await this.profileLocked()
      })
    })
  }

  async requestRegistration(email: string) {
    try { this.available() }
    catch (error) { return this.failed(error) }
    // Email request failures belong to the submitting form, not the shared session.
    return this.deps.api.requestRegistration(email)
  }

  async requestPasswordReset(email: string) {
    try { this.available() }
    catch (error) { return this.failed(error) }
    return this.deps.api.requestPasswordReset(email)
  }
}
