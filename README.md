# MDM API

FastAPI service for master data whose denormalized codes are derived from referenced dimensions.

## Local development

Requirements: Python 3.13, `uv`, Docker, Docker Compose, and Node.js 24.x with npm.

```bash
make setup
make jwt-key-local
make db-up
make migrate
make dev
```

최초 `SUPER_ADMIN`은 migration이나 HTTP endpoint가 아니라 명시적인 대화형 CLI로 생성한다.

```bash
uv run mdm auth bootstrap-super-admin
```

CLI가 email과 name을 차례로 묻고 password와 password 확인값은 각각 terminal no-echo prompt로
입력받는다. 두 password가 NFC 정규화 후 일치하지 않으면 hash 생성이나 DB 접근 없이
`BOOTSTRAP_PASSWORD_CONFIRMATION_MISMATCH`를 출력하고 종료 코드 `2`로 끝난다. 운영에서는 항상
같은 email을 사용해야 한다. 같은 email의 `ACTIVE + SUPER_ADMIN`이 같은 password로 이미 존재하면
아무 행도 변경하지 않고 성공하며, 상태·역할·password가 다르면 `BOOTSTRAP_STATE_CONFLICT`로
종료한다. 이 명령은 강등·비활성화된 관리자의 복구 수단이 아니다. 종료 코드는 성공 `0`, bootstrap
상태·실행 실패 `1`, 명령 사용·입력값·대화형 입력 환경 오류 `2`, `Ctrl+C` 취소 `130`을 사용한다.

`make jwt-key-local`은 최초 한 번만 실행하며 `secrets/local/`에 `0600` RS256 private PEM과
public-only JWKS를 생성한다. 기존 파일은 덮어쓰지 않는다. 이 디렉터리는 Git과 Docker build
context에서 제외된다. JWT adapter는 `.env`의 `MDM_AUTH_JWT_PRIVATE_KEY_PATH`와
`MDM_AUTH_JWT_JWKS_PATH`를 통해 파일을 읽으며 PEM 원문을 환경변수에 저장하지 않는다.

배포에서는 private PEM을 container image에 포함하지 않는다. Cloud Run은 Google Cloud Secret
Manager의 특정 secret version을 읽기 전용 파일로 마운트하고, 동일한 path 설정을 사용한다.
`MDM_AUTH_JWT_ACTIVE_KID`와 public JWKS에는 비밀값이 없으며 새 public key 배포, active signer
전환, 930초 후 retired public key 제거 순서로 회전한다.

Interactive API documentation is rendered by Scalar at `http://127.0.0.1:8000/docs`. The OpenAPI
JSON document remains available at `http://127.0.0.1:8000/openapi.json`; FastAPI's default Swagger
UI and ReDoc pages are disabled.

`make setup` installs the repository's pre-commit hook. Every commit runs `make ci-check`, the same
clean-database acceptance gate used by GitHub Actions. Run `make check` when you want the faster
version that reuses the development database. See `AGENTS.md` for architecture and contribution
rules.

## 인증 프런트엔드

`frontend/`는 로그인·가입·비밀번호 수명주기를 제공하는 React·TypeScript·Vite SPA다.
승인한 동작은 [인증 프런트엔드 계약](docs/auth/frontend-contract.md)에 기록한다.
관리자 사용자 관리와 MDM 업무 화면은 포함하지 않는다.

```bash
make frontend-install
cd frontend
npm run dev
```

기본 화면은 `http://localhost:5173`이며 `/api/v1/*`를 로컬 API
`http://127.0.0.1:8000`으로 전달한다. 로그인 개발에는 기존 JWT 키와 IP-HMAC secret을 설정하고
`.env`에서 `MDM_AUTH_SESSIONS_ENABLED=true`,
`MDM_AUTH_ALLOWED_ORIGINS=["http://localhost:5173"]`,
`MDM_AUTH_ALLOW_INSECURE_LOCAL_COOKIES=true`를 명시한다. secret은 커밋하지 않는다.
계정은 위 bootstrap CLI로 준비할 수 있다. 공개 가입과 이메일 재설정은 각각 기존 기능
활성화 설정·허용 이메일 도메인·SMTP 설정이 필요하며, 이메일 링크의 Origin은 HTTPS여야 한다.

로컬 HTTPS가 필요하면 Vite 실행 프로세스에 `MDM_FRONTEND_TLS_KEY`와
`MDM_FRONTEND_TLS_CERT` 파일 경로를 함께 전달하고, API에는 그 HTTPS Origin을 허용한다.
다른 로컬 API 포트는 `MDM_FRONTEND_API_ORIGIN`으로 지정한다. 이 값은 로컬 Vite proxy에서만
사용하며 loopback HTTP 주소만 허용한다. 브라우저의 API 경로는 항상 상대 `/api/v1`이다.

```bash
make frontend-check                    # npm ci, ESLint, TypeScript, Vitest, production build
cd frontend && npx playwright install chromium
cd ..
make db-up
make frontend-e2e                       # 앞서 빌드한 dist와 실제 로컬 mdm_test API 검증
make ci-check                          # 새 PostgreSQL에서 Python·frontend·브라우저 전체 검증
```

`frontend-e2e`는 로컬 `mdm_test`만 허용하고 migration을 적용한다. 임시 HTTPS/JWT 키와
임의의 loopback 포트로 production build 및 실제 API를 실행하고 종료 시 서버·키를 정리한다.
메일은 기존 `InMemoryEmailSender`를 테스트 조립 단계에서 주입해 실제 템플릿을 검증한다.
테스트 메일함·만료 조작 경로는 별도 테스트 서버에만 존재하고 매 실행의 임의 키를 요구한다.
SMTP 공급자·Cloudflare Pages·Cloud Run 검증은 후속 #34의 책임이다.
Playwright는 Chromium을 사용하며 trace·video는 저장하지 않는다. `.artifacts/`의 화면 캡처는
가상 테스트 계정만 포함하고 Git에서 제외된다. 409·조회 장애 테스트는 명시적인 오류 주입이며,
나머지 인증 흐름과 탭 간 잠금은 실제 브라우저·API·DB로 검증한다.

Cloudflare Pages 빌드 계약은 Root directory `frontend`, command `npm ci && npm run build`,
output directory `dist`다. 저장소 기준 산출물은 `frontend/dist`이며, same-origin
`/api/v1/*` reverse proxy와 실제 배포는 #34에서 구성한다.
