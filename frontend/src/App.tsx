import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { ApiError } from './api/auth-api'
import { CompletedActionError, SessionChangedError, type SessionController } from './api/session-controller'
import { StorageUnavailableError } from './api/session-state'
import { UnsupportedBrowserError } from './api/session-lock'
import type { Navigation } from './navigation'

interface Field { name: string; label: string; password?: boolean; autoComplete?: string; hint?: string }
const email: Field = { name: 'email', label: '이메일', autoComplete: 'email' }
const password: Field = { name: 'password', label: '비밀번호', password: true, autoComplete: 'current-password' }
const policy = '8~128자. 공백과 대소문자를 구분합니다.'
const newPassword: Field = { name: 'new_password', label: '새 비밀번호', password: true, autoComplete: 'new-password', hint: policy }

function errorMessage(error: unknown): string {
  return error instanceof ApiError || error instanceof SessionChangedError || error instanceof StorageUnavailableError
    || error instanceof UnsupportedBrowserError || error instanceof CompletedActionError
    ? error.message : '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.'
}

function SubmitForm({ fields, label, submit, globalNotice }: {
  fields: Field[]; label: string; submit(values: Record<string, string>): Promise<void>; globalNotice: string | null
}) {
  const active = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const violations = error instanceof ApiError ? error.fields : []
  return <form onSubmit={async (event) => {
    event.preventDefault()
    if (active.current) return
    const form = event.currentTarget
    const values = Object.fromEntries(fields.map((field) => [field.name, String(new FormData(form).get(field.name) ?? '')]))
    active.current = true
    setBusy(true)
    setError(null)
    try { await submit(values); form.reset() } catch (failure) { setError(failure) }
    finally { active.current = false; setBusy(false) }
  }}>
    <fieldset disabled={busy}>
      {fields.map((field) => <div className="field" key={field.name}>
        <label htmlFor={field.name}>{field.label}</label>
        <input id={field.name} name={field.name} type={field.password ? 'password' : 'text'}
          inputMode={field.name === 'email' ? 'email' : undefined} autoComplete={field.autoComplete}
          required aria-invalid={violations.includes(field.name)}
          aria-describedby={field.hint ? `${field.name}-hint` : undefined} />
        {field.hint && <small id={`${field.name}-hint`}>{field.hint}</small>}
      </div>)}
      {error !== null && errorMessage(error) !== globalNotice && <p role="alert" className="message error">{errorMessage(error)}</p>}
      <button type="submit" className="primary" disabled={busy}>{busy ? '처리 중…' : label}</button>
    </fieldset>
  </form>
}

export function App({ controller, navigation }: { controller: SessionController; navigation: Navigation }) {
  const session = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  const route = useSyncExternalStore(navigation.subscribe, navigation.getSnapshot)
  const link = navigation.getLink()
  useEffect(() => navigation.connect(), [navigation])
  useEffect(() => {
    if (session.status === 'loading' || session.status === 'blocked') return
    if (route.path === '/') navigation.go(session.status === 'authenticated' ? '/account' : '/auth/login', '', true)
    else if (route.path === '/account' && session.status === 'anonymous') navigation.go('/auth/login', '', true)
    else if (route.path === '/auth/login' && session.status === 'authenticated') navigation.go('/account', '', true)
  }, [session.status, route.path, route.announcement, navigation])

  const go = (path: string, label: string) => <a href={path} onClick={(event) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
    event.preventDefault(); navigation.go(path)
  }}>{label}</a>
  const complete = async (operation: () => Promise<void>, destination: string, message = '') => {
    try { await operation(); navigation.clearLink(); navigation.go(destination, message) }
    catch (error) {
      if (!(error instanceof CompletedActionError)) throw error
      navigation.clearLink()
      navigation.go('/auth/recovery', error.message)
    }
  }
  const form = (fields: Field[], label: string, submit: (values: Record<string, string>) => Promise<void>) =>
    <SubmitForm key={`${route.path}:${label}:${session.revision}`} fields={fields} label={label} submit={submit} globalNotice={session.notice} />
  let title = '로그인'
  let description = '계정으로 로그인해 주세요.'
  let content: ReactNode

  if (session.status === 'blocked') {
    title = '브라우저 설정 확인'
    description = '현재 브라우저에서 안전하게 인증을 진행할 수 없습니다.'
    content = <p>최신 브라우저를 사용하고 사이트 저장소를 허용한 뒤 페이지를 새로고침해 주세요.</p>
  } else if (session.status === 'loading') {
    title = '로그인 상태 확인'
    description = '잠시만 기다려 주세요.'
    content = <p role="status">로그인 정보를 확인하고 있습니다…</p>
  } else if (route.path === '/account' && session.profile) {
    title = '내 계정'
    description = '계정 정보를 확인하고 비밀번호를 관리하세요.'
    const roles = { USER: '사용자', ADMIN: '관리자', SUPER_ADMIN: '최고 관리자' }
    content = <>
      <dl className="profile"><dt>이름</dt><dd>{session.profile.name}</dd><dt>이메일</dt><dd>{session.profile.email}</dd>
        <dt>현재 권한</dt><dd>{roles[session.profile.effective_role]}</dd></dl>
      <div className="actions">
        {form([], '내 정보 새로고침', () => controller.loadProfile())}
        {form([], '로그아웃', () => complete(() => controller.logout(), '/auth/login', '로그아웃했습니다.'))}
      </div>
      <section aria-labelledby="password-change-title"><h2 id="password-change-title">비밀번호 변경</h2>
        {form([{ name: 'current_password', label: '현재 비밀번호', password: true, autoComplete: 'current-password' }, newPassword],
          '비밀번호 변경', (values) => complete(() => controller.changePassword({ current_password: values.current_password!, new_password: values.new_password! }), '/account', '비밀번호를 변경했습니다.'))}
      </section>
    </>
  } else if (route.path === '/auth/register') {
    title = '가입 신청'
    description = '이메일 인증 후 이름과 비밀번호를 설정합니다.'
    content = <>{form([email], '가입 안내 받기', async (values) => {
      const result = await controller.requestRegistration(values.email!)
      navigation.go(route.path, result.message, true)
    })}<nav className="form-links">{go('/auth/login', '로그인으로 돌아가기')}</nav></>
  } else if (route.path === '/auth/forgot-password') {
    title = '비밀번호 재설정 신청'
    description = '가입한 이메일로 재설정 안내를 요청하세요.'
    content = <>{form([email], '재설정 안내 받기', async (values) => {
      const result = await controller.requestPasswordReset(values.email!)
      navigation.go(route.path, result.message, true)
    })}<nav className="form-links">{go('/auth/login', '로그인으로 돌아가기')}</nav></>
  } else if (route.path === '/auth/registration' || route.path === '/auth/password-reset') {
    const registration = route.path === '/auth/registration'
    title = registration ? '가입 완료' : '비밀번호 재설정'
    description = registration ? '이름과 비밀번호를 설정해 가입을 완료하세요.' : '새 비밀번호를 설정하세요.'
    if (!link || link.purpose !== (registration ? 'registration' : 'password-reset')) {
      content = <><p>인증 링크를 확인할 수 없습니다. 이메일의 링크를 다시 열어 주세요. 새로고침하면 링크 정보가 사라집니다.</p>
        <nav className="form-links">{go(registration ? '/auth/register' : '/auth/forgot-password', '안내 메일 다시 신청')}</nav></>
    } else if (registration) {
      content = <>{form([{ name: 'name', label: '이름', autoComplete: 'name' }, { ...password, autoComplete: 'new-password', hint: policy }], '가입 완료',
        (values) => complete(() => controller.completeRegistration({ token: link.token, name: values.name!, password: values.password! }), '/account'))}
        <nav className="form-links">{go('/auth/register', '안내 메일 다시 신청')}</nav></>
    } else {
      content = <>{form([newPassword], '비밀번호 재설정', (values) => complete(() => controller.completePasswordReset({ token: link.token, new_password: values.new_password! }),
        '/auth/login', '비밀번호를 재설정했습니다. 새 비밀번호로 로그인해 주세요.'))}
        <nav className="form-links">{go('/auth/forgot-password', '안내 메일 다시 신청')}</nav></>
    }
  } else if (route.path === '/auth/recovery') {
    title = '로그인 상태 확인 필요'
    description = '완료한 작업을 다시 제출할 필요는 없습니다.'
    content = <>{form([], '세션 확인 다시 시도', async () => {
      await controller.initialize()
      navigation.go(controller.getSnapshot().status === 'authenticated' ? '/account' : '/auth/login')
    })}<nav className="form-links">{go('/auth/login', '로그인 화면으로 이동')}</nav></>
  } else if (['/', '/auth/login', '/account'].includes(route.path)) {
    content = <>{form([email, password], '로그인', (values) => complete(() => controller.login({ email: values.email!, password: values.password! }), '/account'))}
      <nav className="form-links">{go('/auth/register', '가입 신청')}{go('/auth/forgot-password', '비밀번호를 잊으셨나요?')}</nav></>
  } else {
    title = '페이지를 찾을 수 없습니다'
    description = '주소를 확인해 주세요.'
    content = <nav className="form-links">{go('/auth/login', '로그인으로 이동')}</nav>
  }

  return <div className="layout"><header className="brand"><span className="brand-symbol" aria-hidden="true">M</span><span>MDM <span className="brand-label">계정</span></span></header>
    <main className="card"><h1>{title}</h1><p className="description">{description}</p>
      {route.announcement && <p className="message" role="status">{route.announcement}</p>}
      {session.notice && session.notice !== route.announcement && <p className="message error" role="alert">{session.notice}</p>}
      {content}
    </main><footer>계정 정보와 인증 링크를 다른 사람에게 공유하지 마세요.</footer></div>
}
