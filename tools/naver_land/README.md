# 네이버 부동산 매물 수집기 (평촌 평안동 / 범계동)

네이버페이 부동산의 내부 API를 호출해서 아파트 전월세 **호가 매물**을 CSV로 뽑는다.
국토부 실거래가와 달리 "지금 시장에 나와 있는 매물"이 대상이다.

> 확인된 주소는 `https://new.land.naver.com/complexes?ms=<lat>,<lon>,<zoom>&a=APT:PRE:ABYG:JGC&e=RETAIL`.
> 서비스 이름이 네이버페이 부동산으로 바뀌어 페이지에 NAVER FINANCIAL 로고가 붙지만
> 도메인은 그대로다. 일부 경로는 404를 내므로, 404 화면이 떴다고 도메인이 죽은 걸로
> 판단하면 안 된다. 어차피 도메인은 코드에 박아두지 않고 `--from-har` / `--from-curl`
> 의 실제 요청 URL에서 읽는다.

> 로컬(터미널/데스크톱)에서 실행할 것. Claude Code 웹 세션은 egress allowlist에
> 막혀 있어서 네이버 도메인에 접속하지 못한다.

## 준비

의존성 없음. Python 3.9+ 면 된다.

### 1. 토큰 받기 — HAR 통째로 내보내기 (권장)

공식 API가 아니라서 브라우저가 발급받은 Bearer 토큰을 빌려 쓴다.
요청을 하나 골라내는 것보다 **전부 내보내고 코드가 고르게 하는 쪽**이 쉽고 안 깨진다.

1. 브라우저에서 네이버페이 부동산 접속
2. DevTools 열기 — **맥은 F12가 아니라 ⌥⌘I**, 또는 우클릭 → **검사**
   (윈도우/리눅스는 F12)
3. **Network** 탭 → 필터 줄에서 **Fetch/XHR**
4. **DevTools를 열어둔 채로** 검색창에 단지명(예: `평촌동 초원세경`)을 넣고
   **아파트 단지 상세로 들어가 매물 목록이 뜨게 한다.**
   홈 화면에서는 광고·추천 위젯 요청(`airsList.naver`, `gfp-display-sdk` 등)만
   잡히고 매물 API는 나오지 않는다.
5. Network 툴바의 **아래쪽 화살표(⬇, Export HAR)** 클릭 → `naver.har` 로 저장

무엇이 잡혔는지 먼저 확인 (토큰 없이 동작하고, 토큰·쿠키 값은 찍지 않으므로
그대로 공유해도 안전하다):

```bash
python3 naver_land.py inspect --from-har naver.har
```

`🔑` 가 붙은 줄이 하나라도 보이면 준비 끝:

```bash
python3 naver_land.py run --from-har naver.har
```

### 대안: Copy as cURL

맞는 요청을 직접 고르고 싶다면, 매물 목록 요청(이름에 `article` / `complex`)을
우클릭 → **Copy → Copy as cURL** (한글판 **복사 → cURL로 복사**) 후 파일로 저장:

```bash
python3 -c "import naver_land; print(naver_land.token_from_curl('curl.txt'))"
python3 naver_land.py run --from-curl curl.txt
```

토큰만 따로 쓰고 싶으면 환경변수로 넘겨도 되는데, 이때는 도메인을 자동으로
알아낼 수 없으니 기본값과 다르면 `--base` 를 같이 줘야 한다:

```bash
export NAVER_LAND_TOKEN='Bearer eyJ...'
python naver_land.py --base https://fin.land.naver.com run
```

**토큰은 대략 하루 안팎으로 만료된다.** `HTTP 401` 이 뜨면 HAR을 다시 받으면 된다.

> `.har` 파일에는 **로그인 세션 쿠키와 토큰이 그대로 들어 있다.** 남에게 보내거나
> 저장소에 커밋하지 말 것. (`.gitignore` 에 넣어뒀다.) 공유가 필요하면 위의
> `inspect` 출력을 보내면 된다 — 그쪽은 URL만 찍는다.

## 사용

```bash
# HAR 안에 어떤 API 요청이 잡혔는지 확인 (인증 불필요)
python naver_land.py inspect --from-har naver.har

# 프리셋 전체 — 평안동(초원·향촌마을) + 범계동(목련마을), 전세+월세
python naver_land.py run

# 범계동 전세만
python naver_land.py run --dong 범계동 --trade B1

# 법정동의 단지 목록과 complexNo 확인
python naver_land.py discover 평촌동

# complexNo 직접 지정 (단지 페이지 URL new.land.naver.com/complexes/12345 의 숫자)
python naver_land.py fetch --complex 12345 67890 --trade B1 B2
```

결과는 `out/` 에 두 개가 떨어진다.

| 파일 | 내용 |
|---|---|
| `*.csv` | 정규화된 표 (엑셀에서 바로 열림, `utf-8-sig`) |
| `*.raw.jsonl` | 네이버 원본 응답 한 줄에 하나씩 |

원본을 같이 남기는 이유: 네이버가 필드명을 바꾸면 CSV의 일부 칼럼이 비게 되는데,
그때도 `raw.jsonl` 에는 데이터가 그대로 남아 있어 복구할 수 있다.

## 동네 이름 주의

동안구 **법정동**은 비산·관양·평촌·호계동 4개뿐이다. **평안동과 범계동은 행정동**이라
부동산 사이트에서 그 이름으로는 검색이 안 된다.

| 행정동 | 법정동 | cortarNo | 주요 단지 |
|---|---|---|---|
| 평안동 | 평촌동 | `4117310300` | 초원마을 7개, 향촌마을 3개 |
| 범계동 | 호계동 | `4117310400` | 목련마을 8개 |
| — | 관양동 | `4117310200` | |
| — | 비산동 | `4117310100` | |

## 코드값

- `tradeType` — `A1` 매매 / `B1` 전세 / `B2` 월세 / `B3` 단기임대
- `realEstateType` — `APT` 아파트 / `OPST` 오피스텔 / `VL` 빌라

## 주의

- 네이버는 자동 수집을 약관에서 제한하고 IP 차단도 건다. 기본 요청 간격이 1.5초로
  잡혀 있으니 `--delay` 를 더 낮추지 말 것. 개인 확인 용도 범위로만 쓴다.
- 공식 API가 아니라서 네이버가 스펙을 바꾸면 깨진다. 실제로 네이버페이로
  넘어가면서 도메인이 바뀌었고 엔드포인트 경로도 함께 바뀌었을 수 있다.
  `discover` 가 실패하면 브라우저에서 지도를 움직일 때 나가는 요청을
  Copy as cURL 로 확인해서 `discover_complexes()` 의 후보 목록에 추가하면 된다.
  `-v` 를 주면 스크립트가 실제로 부른 URL이 찍히니 브라우저 쪽과 비교하기 쉽다.
- 급하면 `discover` 없이 단지 페이지 URL에서 번호를 직접 읽어 `fetch --complex` 로 넘겨도 된다.

## 참고: 실거래가가 필요하다면

호가가 아니라 **체결된 실거래가**가 목적이면 공공데이터포털의 국토교통부
아파트 전월세 실거래가 API가 공식·무료·안정적이다. 안양시 동안구는 `LAWD_CD=41173`.
