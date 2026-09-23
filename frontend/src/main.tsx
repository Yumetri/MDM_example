import { createRoot } from 'react-dom/client'
import { App } from './App'
import { createBrowserSession } from './api/browser-session'
import { connectSessionEvents } from './api/session-events'
import type { LinkToken } from './fragment'
import { createNavigation } from './navigation'
import './style.css'

export function mountApp(link: LinkToken | null) {
  const controller = createBrowserSession(window)
  const navigation = createNavigation(window, link)
  connectSessionEvents(controller, window)
  createRoot(document.getElementById('root')!).render(<App controller={controller} navigation={navigation} />)
  void controller.initialize().catch(() => { /* The controller exposes a sanitized notice. */ })
}
