import { captureLinkToken, type LinkToken } from './fragment'

export function createNavigation(target: Window, initialLink: LinkToken | null) {
  let link = initialLink
  let snapshot = { path: target.location.pathname, announcement: '' }
  const listeners = new Set<() => void>()
  const notify = () => { listeners.forEach((listener) => listener()) }
  const clearLink = () => { link = null }
  const changed = () => {
    if (target.location.hash) link = captureLinkToken(target.location, target.history)
    else if (target.location.pathname !== snapshot.path) clearLink()
    snapshot = { path: target.location.pathname, announcement: '' }
    notify()
  }
  return {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    getLink: () => link,
    clearLink,
    go(path: string, announcement = '', replace = false) {
      if (path !== snapshot.path) clearLink()
      target.history[replace ? 'replaceState' : 'pushState'](null, '', path)
      snapshot = { path, announcement }
      notify()
    },
    connect() {
      target.addEventListener('popstate', changed)
      target.addEventListener('hashchange', changed)
      return () => {
        target.removeEventListener('popstate', changed)
        target.removeEventListener('hashchange', changed)
      }
    },
  }
}

export type Navigation = ReturnType<typeof createNavigation>
