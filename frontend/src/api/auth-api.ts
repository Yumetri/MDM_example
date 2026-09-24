export interface AccessGrant {
  access_token: string
  token_type: 'Bearer'
  expires_in: 900
}

export interface Profile {
  id: string
  email: string
  name: string
  effective_role: 'USER' | 'ADMIN' | 'SUPER_ADMIN'
}

export interface LoginInput { email: string; password: string }
export interface RegistrationInput { token: string; name: string; password: string }
export interface PasswordResetInput { token: string; new_password: string }
export interface PasswordChangeInput { current_password: string; new_password: string }

const messages: Record<string, string> = {
  INVALID_CREDENTIALS: '이메일 또는 비밀번호를 확인해 주세요.',
  INVALID_ACCESS_TOKEN: '로그인 정보가 만료되었습니다. 다시 로그인해 주세요.',
  INVALID_SESSION: '유효한 로그인 세션이 없습니다. 다시 로그인해 주세요.',
  INVALID_REGISTRATION_TOKEN: '유효하지 않거나 만료된 가입 인증 링크입니다. 가입을 다시 신청해 주세요.',
  INVALID_PASSWORD_RESET_TOKEN: '유효하지 않거나 만료된 비밀번호 재설정 링크입니다. 재설정을 다시 신청해 주세요.',
  INVALID_CURRENT_PASSWORD: '현재 비밀번호를 확인해 주세요.',
  EMAIL_DOMAIN_NOT_ALLOWED: '현재 가입이 허용된 이메일 도메인이 아닙니다.',
  PASSWORD_POLICY_VIOLATION: '새 비밀번호는 NFC 정규화 후 8~128자여야 합니다.',
  VALIDATION_ERROR: '입력한 내용을 확인해 주세요.',
  ORIGIN_NOT_ALLOWED: '이 주소에서는 인증 요청을 처리할 수 없습니다. 서비스 주소를 확인해 주세요.',
  CSRF_VALIDATION_FAILED: '로그인 상태를 확인할 수 없습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.',
  RATE_LIMIT_EXCEEDED: '요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.',
  refresh_conflict: '다른 탭에서 로그인 정보를 갱신하고 있습니다. 잠시 후 다시 시도해 주세요.',
  AUTH_PASSWORD_HASH_UNAVAILABLE: '인증 처리가 지연되고 있습니다. 잠시 후 다시 시도해 주세요.',
  SERVICE_UNAVAILABLE: '현재 이 기능을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.',
  NETWORK_ERROR: '서버 응답을 확인하지 못했습니다. 연결 상태를 확인해 주세요.',
  INVALID_RESPONSE: '서버 응답을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.',
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfterSeconds: number | null = null,
    readonly fields: string[] = [],
  ) {
    super(Object.hasOwn(messages, code) ? messages[code] : '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.')
    this.name = 'ApiError'
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function accessGrant(value: unknown): AccessGrant {
  if (record(value) && typeof value.access_token === 'string' && value.access_token.length > 0
    && value.token_type === 'Bearer' && value.expires_in === 900) {
    return { access_token: value.access_token, token_type: 'Bearer', expires_in: 900 }
  }
  throw new ApiError(0, 'INVALID_RESPONSE')
}

function profile(value: unknown): Profile {
  if (record(value) && typeof value.id === 'string' && typeof value.email === 'string'
    && typeof value.name === 'string'
    && (value.effective_role === 'USER' || value.effective_role === 'ADMIN' || value.effective_role === 'SUPER_ADMIN')) {
    return { id: value.id, email: value.email, name: value.name, effective_role: value.effective_role }
  }
  throw new ApiError(0, 'INVALID_RESPONSE')
}

function accepted(value: unknown): { message: string } {
  if (record(value) && typeof value.message === 'string') return { message: value.message }
  throw new ApiError(0, 'INVALID_RESPONSE')
}

function csrfCookie(cookie: string): string | null {
  const values = cookie.split(';').map((part) => part.trim())
    .filter((part) => part.startsWith('mdm_csrf=')).map((part) => part.slice('mdm_csrf='.length))
  return values.length === 1 ? values[0]! : null
}

const publicFields = new Set(['email', 'name', 'password', 'current_password', 'new_password', 'token'])

/** Low-level transport never retries; the session coordinator owns refresh conflict handling. */
export function createAuthApi(send: typeof fetch, readCookie: () => string) {
  async function request(
    path: string, method: 'GET' | 'POST' | 'PUT' | 'DELETE',
    options: { body?: unknown; bearer?: string; csrf?: boolean } = {},
  ): Promise<unknown> {
    const headers = new Headers({ Accept: 'application/json, application/problem+json' })
    if (options.body !== undefined) headers.set('Content-Type', 'application/json')
    if (options.bearer) headers.set('Authorization', `Bearer ${options.bearer}`)
    if (options.csrf) {
      const csrf = csrfCookie(readCookie())
      if (csrf) headers.set('X-CSRF-Token', csrf)
    }
    let response: Response
    try {
      response = await send(`/api/v1/auth${path}`, {
        method, headers, credentials: 'same-origin', redirect: 'error', cache: 'no-store',
        ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
      })
    } catch {
      throw new ApiError(0, 'NETWORK_ERROR')
    }
    if (response.status === 204) return undefined
    let body: unknown
    try { body = await response.json() } catch { body = null }
    if (!response.ok) {
      const code = record(body) && typeof body.code === 'string' && Object.hasOwn(messages, body.code)
        ? body.code : 'UNKNOWN_ERROR'
      const delay = response.headers.get('Retry-After')
      const seconds = delay !== null && /^\d+$/.test(delay) ? Number(delay) : null
      const retryAfter = seconds !== null && Number.isSafeInteger(seconds) ? seconds : null
      const fields = record(body) && Array.isArray(body.violations)
        ? body.violations.flatMap((violation: unknown) => {
          if (!record(violation) || typeof violation.field !== 'string') return []
          const field = violation.field.replace(/^body\./, '')
          return publicFields.has(field) ? [field] : []
        }) : []
      throw new ApiError(response.status, code, retryAfter, [...new Set(fields)])
    }
    return body
  }

  return {
    async login(input: LoginInput) { return accessGrant(await request('/login', 'POST', { body: input })) },
    async me(bearer: string) { return profile(await request('/me', 'GET', { bearer })) },
    async refresh() { return accessGrant(await request('/session/refresh', 'POST', { csrf: true })) },
    async logout(): Promise<void> { await request('/session', 'DELETE', { csrf: true }) },
    async requestRegistration(email: string) {
      return accepted(await request('/registrations', 'POST', { body: { email } }))
    },
    async completeRegistration(input: RegistrationInput) {
      return accessGrant(await request('/registrations/complete', 'POST', { body: input }))
    },
    async requestPasswordReset(email: string) {
      return accepted(await request('/password-resets', 'POST', { body: { email } }))
    },
    async completePasswordReset(input: PasswordResetInput): Promise<void> {
      await request('/password-resets/complete', 'POST', { body: input })
    },
    async changePassword(bearer: string, input: PasswordChangeInput): Promise<void> {
      await request('/me/password', 'PUT', { body: input, bearer, csrf: true })
    },
  }
}

export type AuthApi = ReturnType<typeof createAuthApi>
