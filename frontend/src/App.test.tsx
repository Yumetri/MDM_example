import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { ApiError, type AuthApi } from './api/auth-api'
import { SessionController } from './api/session-controller'
import { createSessionState } from './api/session-state'
import { createNavigation } from './navigation'

const grant = { access_token: 'memory-secret', token_type: 'Bearer' as const, expires_in: 900 as const }
const profile = { id: 'id', email: 'person@example.net', name: '사용자', effective_role: 'USER' as const }

async function setup(path = '/auth/login', token: string | null = null) {
  history.replaceState(null, '', path)
  const api = {
    login: vi.fn<AuthApi['login']>().mockResolvedValue(grant),
    refresh: vi.fn<AuthApi['refresh']>().mockResolvedValue(grant),
    me: vi.fn<AuthApi['me']>().mockResolvedValue(profile),
    logout: vi.fn<AuthApi['logout']>().mockResolvedValue(undefined),
    requestRegistration: vi.fn<AuthApi['requestRegistration']>().mockResolvedValue({ message: '가입 가능한 이메일이면 인증 안내를 보냈습니다. 메일함을 확인해 주세요.' }),
    completeRegistration: vi.fn<AuthApi['completeRegistration']>().mockResolvedValue(grant),
    requestPasswordReset: vi.fn<AuthApi['requestPasswordReset']>().mockResolvedValue({ message: '비밀번호 재설정이 가능한 계정이면 안내 메일을 보냈습니다. 메일함을 확인해 주세요.' }),
    completePasswordReset: vi.fn<AuthApi['completePasswordReset']>().mockResolvedValue(undefined),
    changePassword: vi.fn<AuthApi['changePassword']>().mockResolvedValue(undefined),
  }
  const controller = new SessionController({
    api, state: createSessionState(() => localStorage), hasCsrf: () => false,
    lock: { assertAvailable() {}, run: (operation) => operation() },
  })
  await controller.initialize()
  const navigation = createNavigation(window, token ? {
    purpose: path === '/auth/registration' ? 'registration' : 'password-reset', token,
  } : null)
  render(<App controller={controller} navigation={navigation} />)
  return { api, controller, navigation }
}

beforeEach(() => localStorage.clear())

describe('authentication screens', () => {
  it('shows the account after login without exposing the access token', async () => {
    await setup()
    fireEvent.change(screen.getByLabelText('이메일'), { target: { value: profile.email } })
    fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'a long password phrase' } })
    fireEvent.click(screen.getByRole('button', { name: '로그인' }))
    await screen.findByRole('heading', { name: '내 계정' })
    expect(screen.getByText(profile.email)).toBeInTheDocument()
    expect(location.pathname).toBe('/account')
    expect(document.body.textContent).not.toContain(grant.access_token)
  })

  it('prevents repeated submission while an email request is pending', async () => {
    const { api } = await setup('/auth/register')
    let finish!: (result: { message: string }) => void
    api.requestRegistration.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    fireEvent.change(screen.getByLabelText('이메일'), { target: { value: profile.email } })
    const form = screen.getByRole('button', { name: '가입 안내 받기' }).closest('form')!
    fireEvent.submit(form)
    fireEvent.submit(form)
    expect(api.requestRegistration).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: '처리 중…' })).toBeDisabled()
    await act(async () => { finish({ message: '메일함을 확인해 주세요.' }) })
    expect(await screen.findByText('메일함을 확인해 주세요.')).toBeInTheDocument()
  })

  it.each([
    { link: '가입 신청', submit: '가입 안내 받기' },
    { link: '비밀번호를 잊으셨나요?', submit: '재설정 안내 받기' },
  ])('keeps a failed login out of $link and its success announcement', async ({ link, submit }) => {
    const { api } = await setup()
    const failure = new ApiError(401, 'INVALID_CREDENTIALS')
    api.login.mockRejectedValueOnce(failure)
    fireEvent.change(screen.getByLabelText('이메일'), { target: { value: profile.email } })
    fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'wrong password' } })
    fireEvent.click(screen.getByRole('button', { name: '로그인' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(failure.message)

    fireEvent.click(screen.getByRole('link', { name: link }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('이메일'), { target: { value: profile.email } })
    fireEvent.click(screen.getByRole('button', { name: submit }))
    expect(await screen.findByRole('status')).toHaveTextContent('메일함을 확인해 주세요.')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  describe.each([
    { path: '/auth/register', method: 'requestRegistration' as const, label: '가입 안내 받기' },
    { path: '/auth/forgot-password', method: 'requestPasswordReset' as const, label: '재설정 안내 받기' },
  ])('$method errors', ({ path, method, label }) => {
    it('removes the failed request error after a successful retry', async () => {
      const { api } = await setup(path)
      const failure = new ApiError(503, 'SERVICE_UNAVAILABLE')
      api[method].mockRejectedValueOnce(failure)
      fireEvent.change(screen.getByLabelText('이메일'), { target: { value: profile.email } })
      fireEvent.click(screen.getByRole('button', { name: label }))
      expect(await screen.findByRole('alert')).toHaveTextContent(failure.message)

      fireEvent.click(screen.getByRole('button', { name: label }))
      expect(await screen.findByRole('status')).toHaveTextContent('메일함을 확인해 주세요.')
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(api[method]).toHaveBeenCalledTimes(2)
    })

    it('does not carry the failed email request error to the login form', async () => {
      const { api } = await setup(path)
      const failure = new ApiError(503, 'SERVICE_UNAVAILABLE')
      api[method].mockRejectedValueOnce(failure)
      fireEvent.change(screen.getByLabelText('이메일'), { target: { value: profile.email } })
      fireEvent.click(screen.getByRole('button', { name: label }))
      expect(await screen.findByRole('alert')).toHaveTextContent(failure.message)

      fireEvent.click(screen.getByRole('link', { name: '로그인으로 돌아가기' }))
      expect(await screen.findByRole('heading', { name: '로그인' })).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })

  it('retains the email token on 422 and submits it only to registration completion', async () => {
    const { api } = await setup('/auth/registration', 'A'.repeat(43))
    api.completeRegistration.mockRejectedValueOnce(new ApiError(422, 'VALIDATION_ERROR', null, ['name']))
    fireEvent.change(screen.getByLabelText('이름'), { target: { value: '사용자' } })
    fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'a long password phrase' } })
    fireEvent.click(screen.getByRole('button', { name: '가입 완료' }))
    await waitFor(() => expect(screen.getByLabelText('이름')).toHaveAttribute('aria-invalid', 'true'))
    fireEvent.click(screen.getByRole('button', { name: '가입 완료' }))
    await screen.findByRole('heading', { name: '내 계정' })
    expect(api.completeRegistration).toHaveBeenCalledTimes(2)
    expect(api.completeRegistration.mock.calls[1]?.[0].token).toBe('A'.repeat(43))
    expect(api.requestRegistration).not.toHaveBeenCalled()
    expect(document.querySelector('input[name="token"]')).toBeNull()
  })

  it('asks to reopen an email link when a reload has lost its in-memory token', async () => {
    await setup('/auth/password-reset')
    expect(screen.getByText(/이메일의 링크를 다시 열어/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '비밀번호 재설정' })).not.toBeInTheDocument()
  })

  it('routes a completed password reset to login without automatically logging in', async () => {
    const { api } = await setup('/auth/password-reset', 'A'.repeat(43))
    fireEvent.change(screen.getByLabelText('새 비밀번호'), { target: { value: 'another long password phrase' } })
    fireEvent.click(screen.getByRole('button', { name: '비밀번호 재설정' }))
    await screen.findByRole('heading', { name: '로그인' })
    expect(screen.getByText(/새 비밀번호로 로그인해/)).toBeInTheDocument()
    expect(api.login).not.toHaveBeenCalled()
    expect(api.refresh).not.toHaveBeenCalled()
  })
})
