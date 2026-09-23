import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSessionState, SESSION_STATE_KEY, StorageUnavailableError } from './session-state'

beforeEach(() => localStorage.clear())

describe('cross-tab state marker', () => {
  it('persists only the change id and kind and lets another tab read the current state', () => {
    const first = createSessionState(() => localStorage)
    const second = createSessionState(() => localStorage)
    expect(second.read()).toBeNull()
    const change = first.publish('signed-in')
    expect(second.read()).toEqual(change)
    expect(Object.keys(JSON.parse(localStorage.getItem(SESSION_STATE_KEY)!)).sort()).toEqual(['id', 'kind'])
    expect(localStorage.length).toBe(1)
    expect(first.publish('signed-out').id).not.toBe(change.id)
  })

  it('rejects unavailable storage without retaining the underlying diagnostic', () => {
    const state = createSessionState(() => { throw new Error('sensitive browser diagnostic') })
    expect(() => state.read()).toThrow(StorageUnavailableError)
    expect(() => state.assertAvailable()).toThrow(StorageUnavailableError)
    expect(() => state.publish('signed-in')).toThrow(StorageUnavailableError)
    try { state.read() } catch (error) { expect(String(error)).not.toContain('diagnostic') }
  })

  it('detects write failures before starting authentication', () => {
    const storage = { getItem: vi.fn(() => null), setItem: vi.fn(() => { throw new Error('quota') }), removeItem: vi.fn() }
    const state = createSessionState(() => storage)
    expect(() => state.assertAvailable()).toThrow(StorageUnavailableError)
    expect(() => state.publish('signed-out')).toThrow(StorageUnavailableError)
  })

  it('does not accept malformed or credential-bearing state markers', () => {
    const state = createSessionState(() => localStorage)
    for (const value of ['broken', 'null', JSON.stringify({ id: 'id', kind: 'signed-in' }),
      JSON.stringify({ id: crypto.randomUUID(), kind: 'signed-in', token: 'secret' })]) {
      localStorage.setItem(SESSION_STATE_KEY, value)
      expect(() => state.read()).toThrow(StorageUnavailableError)
    }
  })

  it('checks storage writability without modifying the session marker', () => {
    const state = createSessionState(() => localStorage)
    const change = state.publish('signed-in')
    state.assertAvailable()
    expect(state.read()).toEqual(change)
    expect(localStorage.length).toBe(1)
  })
})
