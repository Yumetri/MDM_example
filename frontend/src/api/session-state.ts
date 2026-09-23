export const SESSION_STATE_KEY = 'mdm.auth-state.v1'
export type SessionChangeKind = 'signed-in' | 'signed-out'
export interface SessionChange { id: string; kind: SessionChangeKind }
export interface SessionState {
  read(): SessionChange | null
  publish(kind: SessionChangeKind): SessionChange
  assertAvailable(): void
}

export class StorageUnavailableError extends Error {
  constructor() {
    super('브라우저 저장소를 사용할 수 없어 탭 사이의 로그인 상태를 확인할 수 없습니다. 저장소 사용을 허용한 뒤 새로고침해 주세요.')
    this.name = 'StorageUnavailableError'
  }
}

type StateStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export function createSessionState(storage: () => StateStorage): SessionState {
  function read(): SessionChange | null {
    try {
      const raw = storage().getItem(SESSION_STATE_KEY)
      if (raw === null) return null
      const value: unknown = JSON.parse(raw)
      if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error()
      const item = value as Record<string, unknown>
      if (Object.keys(item).length !== 2 || typeof item.id !== 'string'
        || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(item.id)
        || (item.kind !== 'signed-in' && item.kind !== 'signed-out')) throw new Error()
      return { id: item.id, kind: item.kind }
    } catch { throw new StorageUnavailableError() }
  }

  return {
    read,
    publish(kind) {
      try {
        const change = { id: crypto.randomUUID(), kind }
        storage().setItem(SESSION_STATE_KEY, JSON.stringify(change))
        return change
      } catch { throw new StorageUnavailableError() }
    },
    assertAvailable() {
      try {
        read()
        const target = storage()
        const key = `mdm.storage-probe.${crypto.randomUUID()}`
        target.setItem(key, '')
        target.removeItem(key)
      } catch { throw new StorageUnavailableError() }
    },
  }
}
