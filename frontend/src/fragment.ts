export type LinkPurpose = 'registration' | 'password-reset'

export interface LinkToken {
  purpose: LinkPurpose
  token: string
}

/** Run before loading the application; the fragment must never enter router state. */
export function captureLinkToken(location: Location, history: History): LinkToken | null {
  const fragment = location.hash.slice(1)
  history.replaceState(history.state, '', location.pathname + location.search)

  const purpose = location.pathname === '/auth/registration'
    ? 'registration'
    : location.pathname === '/auth/password-reset' ? 'password-reset' : null
  if (!purpose) return null

  const parameters = new URLSearchParams(fragment)
  const token = parameters.get('token')
  // 256-bit canonical unpadded base64url, including its final two zero padding bits.
  if (parameters.size !== 1 || !token || !/^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/.test(token)) {
    return null
  }
  return { purpose, token }
}
