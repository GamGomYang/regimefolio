# 토스증권 실행 경로 구현·검증 결과

기준일: 2026-10-27 (Asia/Seoul).

**체결 원장 → 주문 WebSocket/REST 복구 → 생성 → 취소 → 정정 순서로 구현했다.**
기능 구현과 로컬 장애 검증은 완료했지만, 실제 계좌 운영 검증은 완료하지 않았다.
이번 작업에서 실제 계좌 조회·실주문·실제 WebSocket 접속은 실행하지 않았다.

## 완료 체크리스트

### 1. 체결 원장·현금·환전·입출금 대사

- [x] REST/WS 누적 체결 관측을 보존하고 개별 체결 ID·체결시각을 추정 생성하지 않음.
- [x] 누적 수량·금액·수수료·세금을 분리하여 재수신에도 한 번만 반영.
- [x] WS 역순 관측은 과거값으로 되돌리지 않고 REST 재동기화.
- [x] REST 누계 감소·비용 감소는 검토 차단, 근거 있는 정정은 별도 감사 기록으로 보존.
- [x] 초기 보유량·현금·기존 미결제·진행 중 주문의 기준점을 보존하여 과거 체결 중복 방지.
- [x] USD/KRW, 결제 현금/미결제 금액을 분리. 매수 가능 금액을 현금으로 사용하지 않음.
- [x] 출처가 있는 입금·출금·환전·수수료·배당·기업행동·차액 정정 입력 지원.
- [x] 환전 양쪽 통화 변동과 원장 버전을 한 트랜잭션으로 저장.
- [x] 결제 예정일만으로 현금 전환하지 않음. 결제 확인 자료가 있어야 실제 결제 반영.
- [x] 부분 결제 후 추가 체결은 새 미결제로 남김. 첫 결제 확인으로 이후 체결까지 결제하지 않음.
- [x] 관측/원장 중간 저장 실패 롤백, 기록에서 주문 원장 재생성 가능.
- [x] 정정/취소 관련 레코드가 동일 체결을 두 번 집계하지 않도록 관계 저장.
- [x] 잔고 차이·불완전한 비용·알 수 없는 상태가 있으면 신규 주문 차단.
- [ ] 토스의 입출금·환전·현금 자료 자동 수집. 공개 명세에서 완전한 거래내역 경로를 확인하지 못해 정상화된 외부 증빙 입력 방식으로 구현.
- [ ] 실제 거래내역의 반올림·결제 확정·기업행동 사례를 이용한 운영 대사.

주요 파일: `broker/ledger.py`, `tests/test_execution_ledger.py`.

### 2. WebSocket 연결·누락 복구

- [x] `personal:order:{accountSeq}` 실제 프로토콜 연결 어댑터 구현.
- [x] 고정 WSS 주소·토큰 인증·비밀정보를 출력하지 않는 전용 로거.
- [x] 구독 전체 교체 선언, 요청 ID와 필수 계좌 구독 ACK 검증.
- [x] CONNECTING → SUBSCRIBING → SYNCING → READY → DISCONNECTED 관리.
- [x] 소켓 수신 스레드와 REST/DB 처리 분리, 큐 크기 제한 및 초과 시 안전 차단.
- [x] 기본 20초 PING, 15초 PONG 제한. 수신 중에도 heartbeat 유지.
- [x] 연결 단절·토큰 재발급·제한된 재연결 및 재구독.
- [x] 연결 중단 구간은 REST로 복구. 버퍼 이벤트를 처리한 뒤 REST 재확인.
- [x] 구독 거절·다른 계좌·알 수 없는 이벤트는 READY로 오인하지 않음.
- [x] WS에 `filledAt`이 없어도 처리하고 수신 시각을 체결 시각으로 대체하지 않음.
- [x] DB lease로 같은 계좌의 중복 수신 프로세스 방지.
- [x] 단절 후 계좌 중지 유지. 재동기화 성공만으로 실주문을 자동 재개하지 않음.
- [ ] 시세/호가 WebSocket 채널. 현재 주문 이벤트만 구독하고 주문 시세는 REST 최신성 검증 사용.
- [ ] 실제 서버 부하·지연·토큰 만료·장중 재연결 운영 검증.

주요 파일: `broker/stream.py`, `tests/test_order_stream.py`.

### 3. 생성·취소·정정·결과 불명 복구

- [x] 토스 주문 생성 POST, 취소 POST, 미국 지정가 가격 정정 POST 구현.
- [x] 기본 쓰기 비활성화. `TossClient(..., allow_orders=True)`와 계좌/원장/스트림/위험 검사를 거쳐 사용.
- [x] 생성·취소·정정을 독립된 영구 작업으로 기록하고 요청 해시 저장.
- [x] 원주문·정정/취소 응답 주문번호·주문 의도 연결을 보존.
- [x] 정정 체인의 체결 합계가 원래 의도 수량을 초과하면 사실을 보존하고 추가 주문 차단.
- [x] 요청 전 영구 저장, API 호출 중 DB 쓰기 트랜잭션을 유지하지 않음.
- [x] POST 자동 재시도 금지. 10분 멱등성 기간이 지나도 UNKNOWN을 자동 재전송하지 않음.
- [x] DB 선점으로 동시 생성 중복 차단, 재시작 후 같은 작업 반환.
- [x] 응답 주문번호가 있으면 조회로 자동 복구.
- [x] 주문번호 유실 시 수동 검증된 브로커 영수증의 참조·요청 해시·주문번호를 기록하여 복구.
- [x] 가격/수량/시간의 유사성만으로 주문번호를 추정 매칭하지 않음.
- [x] 부분 체결 후 REJECTED/CANCELED/REPLACED에서도 체결 사실 유지.
- [x] 취소/정정 거절 후 원주문 상태 보존. 정정 거절 시 주문 가격을 바꾸지 않음.
- [x] 미국 수량 정정 차단, 정수 수량·호가·시세/계좌 시각·한도·예약금 검증.
- [x] 별도 시장 DB의 최신성과 확정 신호 출처가 실행 DB와 일치하는지 검증.
- [x] 정정 체결 누계가 독립 누계인지 이월 누계인지 명시적으로 설정하고 근거가 없으면 정정 차단.
- [ ] 실제 정정 주문의 누계 관계 계약 확인. 두 방식 구현은 실제 계약 검증을 대체하지 않음.
- [ ] 주문번호 없는 결과 불명의 무인 자동 복구. 공식 조회에서 clientOrderId가 없어 수동 증빙 방식만 지원.
- [ ] 실제 계좌의 생성·취소·정정·체결 검증. 별도 실거래 범위 확인 후 수행할 단계.

주요 파일: `broker/toss.py`, `broker/execution.py`, `tests/test_execution_service.py`,
`tests/test_execution_crash_concurrency.py`.

## 실행·운영 경계

`execution_cli.py`는 수동 포트폴리오 DB와 다른 `data/toss_execution.db`를 사용한다.
`daily_job.py`나 Streamlit에서 자동 주문을 시작하지 않는다.
파일 권한은 0600이며, 토큰은 환경변수/메모리에만 둔다.

```bash
# 사용 가능한 명령 확인: 계좌/주문 호출 없음
.venv/bin/python execution_cli.py --help

# 실제 자료를 준비한 뒤 사용자가 별도로 실행하는 운영 명령 예시
.venv/bin/python execution_cli.py init --account ACCOUNT_SEQ --file baseline.json
.venv/bin/python execution_cli.py import-movements --account ACCOUNT_SEQ --file movements.json
.venv/bin/python execution_cli.py settle --account ACCOUNT_SEQ --file settlements.json
.venv/bin/python execution_cli.py sync-signals --account ACCOUNT_SEQ
.venv/bin/python execution_cli.py sync --account ACCOUNT_SEQ --cash-file cash-evidence.json
.venv/bin/python execution_cli.py stream --account ACCOUNT_SEQ --cash-file cash-evidence.json
.venv/bin/python execution_cli.py status --account ACCOUNT_SEQ
.venv/bin/python execution_cli.py halt --account ACCOUNT_SEQ
```

생성/취소/정정 명령에는 `--enable-orders`가 별도로 필요하며, 플래그만으로 계좌 중지나
불일치 차단을 해제하지 않는다. `enable`은 최근 대사·READY·미확정 작업 부재를 확인한
후 계좌 중지를 해제할 뿐 주문을 보내지 않는다. 정정에는
`--replacement-semantics`와 `--replacement-evidence`도 필요하다.

**저수준 TossClient의 쓰기 메서드를 전략/화면에서 직접 호출하지 않는다.**
운영 진입점은 영구 작업/위험 검사를 담당하는 TradingService 또는 실행 CLI이다.
가짜 테스트용 market_guard/connector/send 주입을 운영 구성에서 사용하지 않는다.

## 증빙 입력 계약

입력 JSON은 토스가 제공하는 원본 파일 형식이 아니라 앱의 정상화된 입력 계약이다.
출처·계좌·기준 시각을 확인한 자료를 변환해 넣어야 한다. 파일의 source/reference 문자열은
자동 진위 검증이 아니며, 운영자가 실제 증빙과 대응시켜야 한다.

- baseline: `account`, `cutover`(UTC offset 포함), `source`, `holdings`(`USD:XLK` 식),
  `settled`(USD/KRW 모두 명시), `unsettled`(결제일별 cash 목록), `orders`(기준점의 주문 스냅샷).
- movements: `account`, `items`; 각 항목은 `id`, `kind`, `effective_at`, `source`, `cash`, `holdings`.
  금액은 부호 있는 문자열. FX는 두 통화의 반대 부호 금액을 함께 기록.
  CORRECTION은 기존 `replaces` ID를 참조하는 **차액**이며 원래 금액 전체를 다시 넣지 않는다.
- settlements: `account`, `items`; 각 항목은 `reference`(주문번호 또는 baseline:N),
  `confirmed_at`, `source`. 확인 시점의 체결 금액만 결제로 이동.
- cash-evidence: `account`, `basis: SETTLED_AND_UNSETTLED`, `observed_at`, `source`,
  `settled`, `unsettled`(두 항목 모두 USD/KRW 명시). 오래된 파일의 시각만 갱신해서는 안 된다.
- receipt: `kind: BROKER_RECEIPT`, `request_hash`, `order_id`, `reference`.
  요청 해시는 해당 execution_operations 기록과 일치해야 한다.

현금 자료가 30초 최신성 조건을 충족하지 못하면 신규 주문을 허용하지 않는다.
실시간 현금 근거 공급 경로를 확보하지 못한 상태는 무인 실거래 준비 완료가 아니다.

## 테스트 및 통과 기준

- [x] 최종 전체 테스트 **182 passed**, 실패/오류/건너뛰기 **0건**.
- [x] 핵심 변이 테스트 **10/10 검출**.
- [x] Python compileall 및 git diff --check 통과.

- 기존 회귀 테스트에 원장/WS/생성·취소·정정/CLI/강제 종료 테스트 추가.
- 독립 4개 프로세스 × 100개 신규 주문 시나리오: 실제 localhost HTTP 접수 각 1건.
- 1,000개 고정 시드 이벤트 순서 시나리오: 중복·역순에도 최종 수량/금액 동일.
- 전송 전, 서버 접수 직후, ACK 저장 후 강제 종료: 재전송 없이 조회/증빙 복구 또는 안전 차단.
- localhost WebSocket 서버로 구독, PING, 응답 단절, 다른 계좌, 큐 초과, REST 동기화 중 이벤트 검증.
- 원장 저장 오류·비용 정정·재생성·환전 중복·부분 결제·복원 후 잠금 검증.
- 핵심 방어 코드 10개를 제거하는 변이 검증. 운영 파일이 아닌 임시 복사본에서만 실행.

정확한 최종 집계는 [toss-execution-test-results.json](toss-execution-test-results.json)에 기록한다.
단위/분기 커버리지 100%, 무제한 부하 안정성, 실제 계좌 운영 안전성을 주장하지 않는다.

## 운영 전 남은 체크

- [ ] 실계좌 읽기/WS 권한과 모의투자 제공 여부 확인.
- [ ] 최신 현금·입출금·환전·결제 자료의 신뢰 가능한 공급 경로 연결.
- [ ] 정정 주문의 누적 체결 관계 및 취소/정정 거절 레코드의 실제 예시 확인.
- [ ] 주문번호 유실 수동 복구 증빙 확인 절차와 담당자 확정.
- [ ] 실제 예상 수신 부하, 장시간 운영, 서버 429/점검/토큰 만료 동작 확인.
- [ ] 계좌·종목·금액·수량·취소 절차를 제한한 실제 주문 검증.

이 항목들이 남아 있으므로 **무인 실거래 전환 판정은 아직 NO-GO**이다.
