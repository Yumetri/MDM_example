import { describe, expect, it, vi } from 'vitest'
import { captureLinkToken } from './fragment'

const token = 'A'.repeat(43)

describe('email link entry', () => {
  it.each([
    ['/auth/registration', 'registration'],
    ['/auth/password-reset', 'password-reset'],
  ] as const)('removes the fragment before returning the token for %s', (path, purpose) => {
    history.replaceState({ retained: 'state' }, '', `${path}#token=${token}`)
    const replace = vi.spyOn(history, 'replaceState')
    const result = captureLinkToken(window.location, window.history)

    expect(replace).toHaveBeenCalledWith({ retained: 'state' }, '', path)
    expect(location.hash).toBe('')
    expect(result).toEqual({ purpose, token })
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it.each([
    ['/auth/login', `#token=${token}`],
    ['/auth/registration', `#token=${token}&token=${token}`],
    ['/auth/registration', '#token=invalid'],
    ['/auth/password-reset', ''],
  ])('does not retain a token from an invalid link: %s %s', (path, hash) => {
    history.replaceState(null, '', path + hash)
    expect(captureLinkToken(window.location, window.history)).toBeNull()
    expect(location.hash).toBe('')
  })

  it('does not expose the token if the address cannot be sanitized', () => {
    history.replaceState(null, '', `/auth/registration#token=${token}`)
    vi.spyOn(history, 'replaceState').mockImplementation(() => { throw new Error('denied') })
    expect(() => captureLinkToken(window.location, window.history)).toThrow('denied')
  })
})
