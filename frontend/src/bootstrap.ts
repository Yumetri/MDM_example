import { captureLinkToken } from './fragment'

// Remove the credential before importing React or any application UI.
try {
  const link = captureLinkToken(window.location, window.history)
  void import('./main').then(({ mountApp }) => mountApp(link)).catch(() => {
    document.getElementById('root')!.textContent = '화면을 불러오지 못했습니다. 페이지를 새로고침해 주세요.'
  })
} catch {
  document.getElementById('root')!.textContent = '인증 링크를 안전하게 처리할 수 없습니다. 최신 브라우저에서 링크를 다시 열어 주세요.'
}
