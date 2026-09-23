export class UnsupportedBrowserError extends Error {
  constructor() {
    super('이 브라우저에서는 안전한 로그인을 지원하지 않습니다. 최신 브라우저를 사용해 주세요.')
    this.name = 'UnsupportedBrowserError'
  }
}

export interface SessionLock {
  assertAvailable(): void
  run<T>(operation: () => Promise<T>): Promise<T>
}

export function createSessionLock(locks: Pick<LockManager, 'request'> | undefined): SessionLock {
  return {
    assertAvailable() {
      if (!locks) throw new UnsupportedBrowserError()
    },
    async run<T>(operation: () => Promise<T>): Promise<T> {
      if (!locks) throw new UnsupportedBrowserError()
      return locks.request('mdm-session-mutation', { mode: 'exclusive' }, operation)
    },
  }
}
