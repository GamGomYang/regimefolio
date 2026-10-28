# 토스증권 API 계약 확인

확인일: 2026-10-26 (Asia/Seoul). 확인된 공식 OpenAPI 버전: **1.2.24**.

- 가이드: https://developers.tossinvest.com/llms.txt
- 공식 개요: https://openapi.tossinvest.com/openapi-docs/overview.md
- 공식 명세: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
- 조회한 명세 SHA-256: `7e753c21dc4b938d8f9610ce3efd6542783018977cb030e39db5a006f5b59d40`

## 확인한 계약

| 항목 | 확인 결과 | 구현 정책 |
|---|---|---|
| 인증 | POST /oauth2/token, client_credentials, access_token/token_type/expires_in | 메모리 보관, 만료 60초 전 갱신, 동시 갱신 잠금 |
| API 주소 | https://openapi.tossinvest.com | 고정 호스트, HTTPS 검증, 리디렉션 거부 |
| 계좌 범위 | Authorization + X-Tossinvest-Account | 환경변수로 지정한 계좌만 조회, 계좌 목록 대조 |
| 시세 | GET /api/v1/prices, symbols | 종목·통화·가격·시각 검증 |
| 주문 가능 | GET /api/v1/buying-power, /sellable-quantity | USD 매수 가능 금액, 종목별 판매 가능 수량 조회 |
| 주문 생성 키 | clientOrderId, 최대 36자, 영숫자/-/_ | 내부 UUID 영구 저장 |
| 서버 멱등성 | 키 유효기간 **10분**. 이후 같은 키도 새로운 주문 | 응답 유실 주문을 자동 재전송하지 않음 |
| 주문 조회 | GET /api/v1/orders 및 /{orderId} | OPEN/CLOSED 별도 조회, CLOSED 커서 전부 수집 |
| OPEN 페이지 | 전량 반환, hasNext=false | 응답 검증 |
| CLOSED 페이지 | hasNext/nextCursor/limit, limit 최대 100 | 중복·커서 순환·1000페이지 초과 차단 |
| 조회 Order 스키마 | clientOrderId가 정의되어 있지 않음 | 주문번호까지 유실하면 가격·시간으로 추정 매칭하지 않음 |
| REST 체결 | execution에 누적 수량·평균가·금액·수수료·세금·최종체결시각 | 누적 관측 스냅샷 보존; 개별 체결 ID를 만들어내지 않음 |
| USD 지정가 수량 | 정수 | 소수점 지정가 차단 |
| USD 호가 | 1달러 미만 0.0001, 이상 0.01 | 시뮬레이션 엔진은 잘못된 호가를 절삭하지 않고 거부 |
| 오류 | 인증, 권한, 429, 5xx 등 | GET 최대 3회, 주문 자동 재전송 없음 |

## 2026-10-26 1차 지원 범위 (이력)

실제 토스 연결은 **조회 전용**이다. `TossClient.submit/cancel`은 네트워크 호출 전에
`LIVE_ORDER_TRANSPORT_DISABLED`를 반환한다. 환경변수로 이 차단을 해제할 수 없다.
주문 상태 엔진은 `simulation=True`인 테스트 브로커에 대해서만 사용한다.
실제 데이터와 시뮬레이션 체결은 자동으로 합치지 않는다.

실제 주문 활성화 전 해결할 사항:

1. 주문번호 없는 결과 불명 주문의 검증 가능한 복구 절차. 명세 외 필드를 가정하지 않는다.
2. 누적 REST 체결/실시간 이벤트를 실제 체결 원장으로 반영하는 계약과 정정 처리.
3. 실제 계좌 입출금·환전·수수료·배당·기업행동을 포함하는 현금·보유량 대사.
4. 테스트 계정/모의투자 제공 여부, 운영 권한, 실제 계좌 조회 검증.
5. 실시간 연결 끊김·구독 복구와 누락 보충의 통합 검증.

고객 토큰이나 계좌를 사용한 요청은 이번 구현/테스트에서 실행하지 않았다.

## 2026-10-27 후속 구현

REST 1.2.24 / AsyncAPI 1.2.2를 기준으로 생성·취소·미국 지정가 가격 정정,
주문 WebSocket, 누적 체결 원장, 증빙 기반 현금 대사를 구현했다.
쓰기는 기본 비활성화이며 TradingService의 준비 상태 검사를 통과해야 한다.
정정/취소는 원주문과 다른 orderId를 반환한다. 미국 수량 정정은 허용하지 않는다.
REJECTED/REPLACED에도 부분 체결이 있을 수 있다.
WS는 personal:order만 사용하며 filledAt 부재를 허용하고, 단절 구간은 REST로 복구한다.
현재 한계와 실제 계좌 검증 대기는 [후속 체크리스트](toss-execution-status.md)를 참고한다.

공식 WS 명세: https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json
