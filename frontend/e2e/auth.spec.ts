import { randomUUID } from 'node:crypto'
import { test, expect, type Page, type APIRequestContext } from '@playwright/test'

// Exercise the approved minimum through real registration, login, change and reset flows.
const password = 'Eight123'
const nextPassword = 'Next1234'
const controlOrigin = process.env.MDM_E2E_API_ORIGIN!
const controlHeaders = { 'X-E2E-Key': process.env.MDM_E2E_CONTROL_KEY! }
const recipient = () => `browser-${randomUUID()}@example.net`

async function mail(request: APIRequestContext, email: string): Promise<string> {
  const response = await request.get(`${controlOrigin}/__e2e__/mail`, { params: { email }, headers: controlHeaders })
  expect(response.status()).toBe(200)
  const value = await response.json() as { link: string }
  return value.link
}

async function openLink(page: Page, link: string) {
  // Do not include credential URLs in failure diagnostics or retained traces.
  try { await page.goto(link) } catch { throw new Error('Email link navigation failed') }
  await expect.poll(() => page.evaluate(() => location.hash.length)).toBe(0)
}

async function requestRegistration(page: Page, request: APIRequestContext, email: string) {
  await page.goto('/auth/register')
  await page.getByLabel('이메일', { exact: true }).fill(email)
  await page.getByRole('button', { name: '가입 안내 받기' }).click()
  await expect(page.getByRole('status')).toContainText('메일함을 확인')
  return mail(request, email)
}

async function finishRegistration(page: Page) {
  await page.getByLabel('이름', { exact: true }).fill('브라우저 사용자')
  await page.getByLabel('비밀번호', { exact: true }).fill(password)
  await page.getByRole('button', { name: '가입 완료', exact: true }).click()
}

async function register(page: Page, request: APIRequestContext) {
  const email = recipient()
  const link = await requestRegistration(page, request, email)
  await openLink(page, link)
  await finishRegistration(page)
  await expect(page.getByRole('heading', { name: '내 계정', exact: true })).toBeVisible()
  return { email, link }
}

async function login(page: Page, email: string, value = password) {
  await page.getByLabel('이메일', { exact: true }).fill(email)
  await page.getByLabel('비밀번호', { exact: true }).fill(value)
  await page.getByRole('button', { name: '로그인', exact: true }).click()
}

test('real API: registration, HTTPS cookies, reload, multiple tabs, password change and logout', async ({ page, context, request }) => {
  const { email } = await register(page, request)
  await page.screenshot({ path: '../.artifacts/frontend-account.png', fullPage: true })
  const cookies = await context.cookies()
  for (const name of ['mdm_refresh', 'mdm_csrf']) {
    const cookie = cookies.find((item) => item.name === name)!
    expect(Boolean(cookie)).toBe(true)
    expect(cookie.secure).toBe(true)
    expect(cookie.sameSite).toBe('Strict')
    expect(cookie.path).toBe('/')
    expect(cookie.domain).toBe('localhost')
    expect(cookie.httpOnly).toBe(name === 'mdm_refresh')
  }
  expect(await page.evaluate(() => document.cookie.includes('mdm_refresh='))).toBe(false)
  let reloadRefresh = 0
  page.on('request', (event) => { if (event.url().endsWith('/auth/session/refresh')) reloadRefresh++ })
  await page.reload()
  await expect(page.getByText(email, { exact: true })).toBeVisible()
  expect(reloadRefresh).toBe(1)
  const second = await context.newPage()
  await second.goto('/account')
  await expect(second.getByText(email, { exact: true })).toBeVisible()
  await Promise.all([page.reload(), second.reload()])
  await expect(page.getByText(email, { exact: true })).toBeVisible()
  await expect(second.getByText(email, { exact: true })).toBeVisible()
  await page.getByLabel('현재 비밀번호').fill(password)
  await page.getByLabel('새 비밀번호').fill(nextPassword)
  const otherRefresh = second.waitForResponse((response) => response.url().endsWith('/auth/session/refresh') && response.status() === 200)
  await page.getByRole('button', { name: '비밀번호 변경', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('비밀번호를 변경')
  await otherRefresh
  await expect(second.getByText(email, { exact: true })).toBeVisible()
  await second.getByRole('button', { name: '로그아웃', exact: true }).click()
  await expect(second.getByRole('heading', { name: '로그인', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '로그인', exact: true })).toBeVisible()
  await expect(page.getByText('비밀번호를 변경했습니다.', { exact: true })).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: '../.artifacts/frontend-login-mobile.png', fullPage: true })
  await login(page, email, nextPassword)
  await expect(page.getByText(email, { exact: true })).toBeVisible()
})

test('real API: fragment is erased, token stays out of URLs/storage/logs, 422 does not consume it', async ({ page, request }) => {
  const email = recipient()
  const link = await requestRegistration(page, request, email)
  const token = new URL(link).hash.slice('#token='.length)
  const outbound: { url: string; body: string; headers: string }[] = []
  const consoleMessages: string[] = []
  page.on('request', (event) => outbound.push({ url: event.url(), body: event.postData() ?? '', headers: JSON.stringify(event.headers()) }))
  page.on('console', (event) => consoleMessages.push(event.text()))
  await openLink(page, link)
  await page.getByLabel('이름', { exact: true }).fill('   ')
  await page.getByLabel('비밀번호', { exact: true }).fill(password)
  await page.getByRole('button', { name: '가입 완료', exact: true }).click()
  await expect(page.getByLabel('이름', { exact: true })).toHaveAttribute('aria-invalid', 'true')
  await finishRegistration(page)
  await expect(page.getByRole('heading', { name: '내 계정', exact: true })).toBeVisible()
  expect(outbound.every((event) => !event.url.includes(token) && !event.headers.includes(token))).toBe(true)
  expect(outbound.filter((event) => event.body.includes(token)).every((event) => event.url.endsWith('/auth/registrations/complete'))).toBe(true)
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, state: history.state }))
  expect(stored.includes(token)).toBe(false)
  expect(stored.includes(email)).toBe(false)
  expect(consoleMessages.some((message) => message.includes(token))).toBe(false)
  await page.goBack()
  await expect(page.getByText(/이메일의 링크를 다시 열어/)).toBeVisible()
  expect(await page.evaluate(() => location.hash)).toBe('')
})

test('real API: expired and already-used registration links are rejected', async ({ page, request }) => {
  const email = recipient()
  const expired = await requestRegistration(page, request, email)
  const response = await request.post(`${controlOrigin}/__e2e__/expire-registration`, { params: { email }, headers: controlHeaders })
  expect(response.ok()).toBe(true)
  await openLink(page, expired)
  await finishRegistration(page)
  await expect(page.getByRole('alert')).toContainText('유효하지 않거나 만료된 가입 인증 링크')
  const { link } = await register(page, request)
  await openLink(page, link)
  await finishRegistration(page)
  await expect(page.getByRole('alert')).toContainText('유효하지 않거나 만료된 가입 인증 링크')
})

test('real API: password reset ends login, accepts the new password and rejects token reuse', async ({ page, context, request }) => {
  const { email } = await register(page, request)
  const other = await context.newPage()
  await other.goto('/account')
  await expect(other.getByText(email, { exact: true })).toBeVisible()
  await page.goto('/auth/forgot-password')
  await page.getByLabel('이메일', { exact: true }).fill(email)
  await page.getByRole('button', { name: '재설정 안내 받기' }).click()
  await expect(page.getByRole('status')).toContainText('메일함을 확인')
  const link = await mail(request, email)
  await openLink(page, link)
  await page.getByLabel('새 비밀번호').fill(nextPassword)
  await page.getByRole('button', { name: '비밀번호 재설정', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('새 비밀번호로 로그인')
  await expect(other.getByRole('heading', { name: '로그인', exact: true })).toBeVisible()
  await login(page, email, password)
  await expect(page.getByRole('alert')).toContainText('이메일 또는 비밀번호')
  await login(page, email, nextPassword)
  await expect(page.getByText(email, { exact: true })).toBeVisible()
  await openLink(page, link)
  await page.getByLabel('새 비밀번호').fill(password)
  await page.getByRole('button', { name: '비밀번호 재설정', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('유효하지 않거나 만료된 비밀번호 재설정 링크')
})

test('real API: CSRF and Origin failures return 403 without ending the session', async ({ page, request }) => {
  const { email } = await register(page, request)
  const missingCsrf = await page.evaluate(async () => (await fetch('/api/v1/auth/session', { method: 'DELETE' })).status)
  expect(missingCsrf).toBe(403)
  const invalidOrigin = await request.post(`${controlOrigin}/api/v1/auth/login`, {
    headers: { Origin: 'https://untrusted.example.net' }, data: { email, password },
  })
  expect(invalidOrigin.status()).toBe(403)
  await page.reload()
  await expect(page.getByText(email, { exact: true })).toBeVisible()
})

test('injected conflict: browser retries once after Retry-After and then surfaces a repeated conflict', async ({ page, request }) => {
  await register(page, request)
  let attempts = 0
  let firstAt = 0
  let secondAt = 0
  await page.route('**/api/v1/auth/session/refresh', async (route) => {
    attempts++
    if (attempts === 1) firstAt = Date.now()
    else secondAt = Date.now()
    await route.fulfill({ status: 409, headers: { 'Retry-After': '1' }, json: { code: 'refresh_conflict' } })
  })
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('다른 탭에서 로그인 정보를 갱신')
  expect(attempts).toBe(2)
  expect(secondAt - firstAt).toBeGreaterThanOrEqual(950)
  await page.unroute('**/api/v1/auth/session/refresh')
  await page.reload()
  await expect(page.getByRole('heading', { name: '내 계정', exact: true })).toBeVisible()
})

test('real API: invalid refresh clears shared cookies and signs out an already-open tab', async ({ page, context, request }) => {
  const { email } = await register(page, request)
  const second = await context.newPage()
  await second.goto('/account')
  await expect(second.getByText(email, { exact: true })).toBeVisible()
  await context.addCookies([
    { name: 'mdm_refresh', value: 'A'.repeat(43), domain: 'localhost', path: '/', secure: true, httpOnly: true, sameSite: 'Strict' },
    { name: 'mdm_csrf', value: 'A'.repeat(43), domain: 'localhost', path: '/', secure: true, httpOnly: false, sameSite: 'Strict' },
  ])
  const failed = page.waitForResponse((response) => response.url().endsWith('/auth/session/refresh') && response.status() === 401)
  await page.goto('/account')
  await failed
  await expect(page.getByRole('heading', { name: '로그인', exact: true })).toBeVisible()
  await expect(second.getByRole('heading', { name: '로그인', exact: true })).toBeVisible()
  await expect(second.getByText(email, { exact: true })).toHaveCount(0)
  expect((await context.cookies()).some((cookie) => cookie.name.startsWith('mdm_'))).toBe(false)
})

test('injected profile outage: completed registration has recovery without duplicate submission', async ({ page, request }) => {
  const email = recipient()
  const link = await requestRegistration(page, request, email)
  await openLink(page, link)
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 503, json: { code: 'SERVICE_UNAVAILABLE' } }))
  await finishRegistration(page)
  await expect(page.getByRole('heading', { name: '로그인 상태 확인 필요' })).toBeVisible()
  await expect(page.getByRole('status')).toContainText('가입은 완료')
  await expect(page.getByRole('button', { name: '가입 완료', exact: true })).toHaveCount(0)
  await page.unroute('**/api/v1/auth/me')
  await page.getByRole('button', { name: '세션 확인 다시 시도' }).click()
  await expect(page.getByText(email, { exact: true })).toBeVisible()
})

test('unsupported Web Locks and blocked storage stop authentication before requests', async ({ browser }) => {
  for (const blocked of ['locks', 'storage']) {
    const context = await browser.newContext({ ignoreHTTPSErrors: true, baseURL: process.env.MDM_E2E_ORIGIN })
    await context.addInitScript((kind) => {
      if (kind === 'locks') Object.defineProperty(navigator, 'locks', { value: undefined })
      else Storage.prototype.setItem = () => { throw new DOMException('blocked', 'SecurityError') }
    }, blocked)
    const page = await context.newPage()
    const apiRequests: string[] = []
    page.on('request', (event) => { if (event.url().includes('/api/v1/')) apiRequests.push(event.url()) })
    await page.goto('/auth/login')
    await expect(page.getByRole('heading', { name: '브라우저 설정 확인' })).toBeVisible()
    expect(apiRequests.length).toBe(0)
    await context.close()
  }
})

test('real Web Locks: a queued logout cannot end the account created by another tab', async ({ page, context, request }) => {
  await register(page, request)
  const nextEmail = recipient()
  const second = await context.newPage()
  const link = await requestRegistration(second, request, nextEmail)
  await openLink(second, link)
  await second.getByLabel('이름', { exact: true }).fill('새 계정')
  await second.getByLabel('비밀번호', { exact: true }).fill(password)

  await page.evaluate(async () => {
    let acquired!: () => void
    const ready = new Promise<void>((resolve) => { acquired = resolve })
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    Object.assign(window, { releaseTestLock: release })
    void navigator.locks.request('mdm-session-mutation', async () => { acquired(); await gate })
    await ready
  })
  await second.getByRole('button', { name: '가입 완료', exact: true }).click()
  await expect(second.getByRole('button', { name: '처리 중…' })).toBeDisabled()
  let logoutRequests = 0
  context.on('request', (event) => {
    if (event.method() === 'DELETE' && event.url().endsWith('/auth/session')) logoutRequests++
  })
  await page.getByRole('button', { name: '로그아웃', exact: true }).click()
  await expect(page.getByRole('button', { name: '처리 중…' })).toBeDisabled()
  await page.evaluate(() => (window as unknown as { releaseTestLock(): void }).releaseTestLock())
  await expect(second.getByText(nextEmail, { exact: true })).toBeVisible()
  await expect(page.getByText(nextEmail, { exact: true })).toBeVisible()
  await expect(page.getByText('다른 탭에서 로그인 상태가 변경되어 요청을 취소했습니다. 현재 계정을 확인한 뒤 다시 제출해 주세요.', { exact: true })).toBeVisible()
  expect(logoutRequests).toBe(0)
})
