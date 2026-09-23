import { describe, expect, it, vi } from 'vitest'
import { ApiError, createAuthApi } from './auth-api'

const grant = { access_token: 'access-jwt', token_type: 'Bearer', expires_in: 900 }
const json = (body: unknown, status = 200, headers = {}) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json', ...headers },
})

describe('authentication API boundary', () => {
  it('sends link credentials only in the matching completion body with redirects disabled', async () => {
    const send = vi.fn<typeof fetch>().mockResolvedValueOnce(json(grant, 201))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    const api = createAuthApi(send, () => '')
    await api.completeRegistration({ token: 'registration-secret', name: '사용자', password: 'password-secret' })
    await api.completePasswordReset({ token: 'reset-secret', new_password: 'new-secret' })

    expect(send.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/auth/registrations/complete', '/api/v1/auth/password-resets/complete',
    ])
    for (const [, options] of send.mock.calls) {
      expect(options).toMatchObject({ credentials: 'same-origin', redirect: 'error', cache: 'no-store' })
      expect(new Headers(options?.headers).has('Authorization')).toBe(false)
    }
    expect(JSON.parse(send.mock.calls[0]![1]!.body as string)).toEqual({
      token: 'registration-secret', name: '사용자', password: 'password-secret',
    })
    expect(JSON.parse(send.mock.calls[1]![1]!.body as string)).toEqual({
      token: 'reset-secret', new_password: 'new-secret',
    })
  })

  it('reads the current CSRF cookie for each protected session request', async () => {
    let cookie = 'other=x; mdm_csrf=first'
    const send = vi.fn<typeof fetch>().mockResolvedValueOnce(json(grant))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    const api = createAuthApi(send, () => cookie)
    await api.refresh()
    cookie = 'mdm_csrf=second; other=x'
    await api.logout()
    expect(new Headers(send.mock.calls[0]![1]?.headers).get('X-CSRF-Token')).toBe('first')
    expect(new Headers(send.mock.calls[1]![1]?.headers).get('X-CSRF-Token')).toBe('second')
  })

  it('exposes the conflict code and retry delay without retrying inside the transport', async () => {
    const send = vi.fn<typeof fetch>().mockResolvedValue(json({
      code: 'refresh_conflict', detail: 'must not expose arbitrary server text',
    }, 409, { 'Retry-After': '1' }))
    const api = createAuthApi(send, () => 'mdm_csrf=csrf')
    await expect(api.refresh()).rejects.toMatchObject({
      status: 409, code: 'refresh_conflict', retryAfterSeconds: 1,
    })
    expect(send).toHaveBeenCalledOnce()
  })

  it('does not expose arbitrary response bodies or network error text to the user', async () => {
    const send = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response('private upstream diagnostic', { status: 502 }))
      .mockRejectedValueOnce(new Error('request contains password-secret'))
    const api = createAuthApi(send, () => '')
    for (let attempt = 0; attempt < 2; attempt++) {
      const error = await api.login({ email: 'person@example.net', password: 'password-secret' }).catch((e: unknown) => e)
      expect(error).toBeInstanceOf(ApiError)
      expect(String(error)).not.toMatch(/private|upstream|password-secret/)
    }
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('preserves actionable field names but not untrusted field messages', async () => {
    const send = vi.fn<typeof fetch>().mockResolvedValue(json({
      code: 'VALIDATION_ERROR', violations: [{ field: 'body.email', message: 'secret echoed here' }],
    }, 422))
    const api = createAuthApi(send, () => '')
    await expect(api.requestRegistration('invalid')).rejects.toMatchObject({
      code: 'VALIDATION_ERROR', fields: ['email'],
    })
  })

  it('keeps the bearer credential on authenticated endpoints only', async () => {
    const send = vi.fn<typeof fetch>().mockResolvedValueOnce(json({
      id: 'user-id', email: 'person@example.net', name: '사용자', effective_role: 'USER',
    })).mockResolvedValueOnce(new Response(null, { status: 204 }))
    const api = createAuthApi(send, () => 'mdm_csrf=csrf')
    await api.me('access-jwt')
    await api.changePassword('access-jwt', { current_password: 'current', new_password: 'new' })
    expect(send.mock.calls.map(([, options]) => new Headers(options?.headers).get('Authorization')))
      .toEqual(['Bearer access-jwt', 'Bearer access-jwt'])
    expect(new Headers(send.mock.calls[0]![1]?.headers).has('X-CSRF-Token')).toBe(false)
    expect(new Headers(send.mock.calls[1]![1]?.headers).get('X-CSRF-Token')).toBe('csrf')
  })
})
