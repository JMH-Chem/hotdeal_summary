# 핫딜요약 (hotdeal_summary)

물건 살 때 필요한 정보를 한곳에 모으는 서비스입니다.

| 페이지 | 하는 일 |
|---|---|
| **핫딜** `/` | 뽐뿌(국내·해외·알리뽐뿌·쿠폰·이벤트), 루리웹, 클리앙, 딜바다(국내·해외), 어미새 핫딜을 5분마다 모아서 `[쇼핑몰]·가격·배송비`를 파싱하고, 중복 딜은 묶어서 보여줍니다. 알리만/쿠폰만 필터 지원 |
| **최저가 비교** `/compare` | 네이버쇼핑 API + 다나와 + **알리익스프레스 Affiliate API**를 동시에 조회해 **실구매가**(가격+배송비, 알리는 관부가세 추정치 포함)로 정렬 |
| **가격 추적** `/track` | 폴센트처럼 상품(검색어 최저가 또는 알리 상품 URL)을 주기적으로 기록 → 목표가 도달 / N% 하락 / 역대 최저가 갱신 시 알림. 핫딜 **키워드 알림**(예: "햇반 72", 최대 가격) |
| **행사** `/events` | 알리 11.11 광군제, 11번가 십일절, 블랙프라이데이 등 일정(D-day) + 관련 핫딜·코드 자동 연결 |
| **쿠폰** `/coupons` | 커뮤니티·유튜버 전용 코드가 올라온 글에서 코드·할인액·최소주문·유효기간·출처를 자동 추출해 정리(클릭 복사) |
| **알림** `/notifications` | 앱 내 알림함 + 텔레그램/디스코드 웹훅 전송 |

### 알리익스프레스를 빠뜨리지 않기
국내 가격비교/가격추적 서비스는 알리를 잘 다루지 않습니다. 여기서는
- 가격비교에 알리를 항상 포함합니다: Affiliate API 키가 있으면 API(`ship_to_country=KR`, `target_currency=KRW`), 없으면 알리 한국 검색 페이지에 내장된 원화 가격 데이터를 사용,
- 면세한도(USD 150, 미국 200) 초과 시 관세+부가세 추정치를 더해 국내가와 같은 기준(실구매가)으로 비교하며,
- 알리 상품 URL을 그대로 추적 등록해 자체 가격 히스토리를 쌓고,
- 알리뽐뿌/클리앙 등에 올라오는 광군제 코드(예: `KBTY04`~`KBTY45`)를 행사와 연결합니다.

## 실행

```bash
uv sync
```

```bash
uv run python -m app.cli collect
```

```bash
uv run uvicorn app.main:app --reload
```

브라우저에서 http://127.0.0.1:8000 을 엽니다. 서버가 떠 있으면 수집(5분)·가격 확인(1시간)이 백그라운드로 돕니다.
API 키는 `.env.example`을 `.env`로 복사해 채운 뒤 `uv run --env-file .env uvicorn app.main:app` 으로 실행합니다.

기타 명령:

```bash
uv run python -m app.cli compare "에어팟 프로 2"
```

```bash
uv run python -m app.cli check-prices
```

```bash
uv run pytest
```

## 구조

```
app/
  main.py                 FastAPI 라우트 + 백그라운드 스케줄러
  cli.py                  collect / compare / check-prices
  config.py db.py http.py parsing.py
  sources/community.py    커뮤니티 수집기(RSS/HTML) + 소스 레지스트리
  sources/shopping.py     네이버/다나와/알리 가격 소스, 알리 API 서명
  services/collector.py   수집 → 저장(중복 제거) → 쿠폰 추출 → 키워드 알림
  services/coupons.py     코드 추출(모델명 오탐 방지 규칙)
  services/compare.py     병렬 비교 · 관련도 필터 · 실구매가 정렬
  services/customs.py     관부가세 추정
  services/tracker.py     가격 기록 · 알림 규칙
  services/events.py      행사 캘린더(data/events.json)
  services/notify.py      앱 내 / 텔레그램 / 디스코드
data/events.json          행사 일정(직접 편집)
tests/                    실제 페이지로 만든 픽스처 기반 테스트
```

## 수집 소스 상태 (해외 IP 기준 확인)

| 소스 | 방식 | 상태 |
|---|---|---|
| 뽐뿌 국내/해외/알리뽐뿌/쿠폰/이벤트 | RSS | ✅ (HTML 목록은 403이라 RSS 사용) |
| 루리웹 유저 핫딜, 어미새 | RSS | ✅ |
| 클리앙 알뜰구매, 딜바다 국내/해외 | HTML | ✅ |
| 퀘이사존, 아카라이브, 에펨코리아, 쿨엔조이 | - | ⛔ 해외 IP 차단(403/430/timeout) → 기본 비활성. 국내 서버에서 파서 추가 후 `ENABLED_SOURCES`로 활성화 |
| 다나와 검색 | Next.js 내장 JSON | ✅ 키 불필요 |
| 알리익스프레스 검색 | 한국 검색 페이지 내장 JSON | ✅ 키 불필요 (API 키 설정 시 Affiliate API 우선) |
| 네이버쇼핑 / 알리 Affiliate | 공식 API | 🔑 키 필요 |

## 운영 원칙
- 공식 API 우선, 커뮤니티는 RSS/목록 페이지만 낮은 빈도로 조회하고 제목·가격·링크 등 메타데이터만 저장합니다(원문 링크로 이동).
- 가격·관부가세·쿠폰 정보는 참고용이며 실제 결제 금액과 다를 수 있습니다.

## 다음 단계 (TODO)
- 쿠팡 파트너스 / 11번가 / G마켓 가격 소스, 홈쇼핑(GS·CJ·현대·롯데 등) 편성표·방송 특가 수집
- 국내 리전 서버 + Playwright로 퀘이사존·아카라이브·펨코 수집
- LLM 기반 쿠폰 구조화(조건/카드사 할인까지), 웹 푸시, 사용자 계정별 알림
- Docker 이미지 및 배포 스크립트
