# 네이버 부동산 매물 수집기 (평촌 평안동 / 범계동)

네이버페이 부동산의 내부 API를 호출해서 아파트 전월세 **호가 매물**을 CSV로 뽑는다.
국토부 실거래가와 달리 "지금 시장에 나와 있는 매물"이 대상이다.

> 서비스가 네이버 부동산 → **네이버페이 부동산**(NAVER FINANCIAL)으로 넘어가면서
> 옛 주소 `new.land.naver.com` 은 404를 낸다. 도메인을 코드에 박아두지 않고
> `--from-curl` 덤프에 들어 있는 실제 요청 URL에서 읽으므로, 주소가 또 바뀌어도
> 새로 복사만 하면 된다.

> 로컬(터미널/데스크톱)에서 실행할 것. Claude Code 웹 세션은 egress allowlist에
> 막혀 있어서 네이버 도메인에 접속하지 못한다.

## 준비

의존성 없음. Python 3.9+ 면 된다.

### 1. 토큰 받기

공식 API가 아니라서 브라우저가 발급받은 Bearer 토큰을 빌려 쓴다.

1. 브라우저에서 네이버페이 부동산 접속 (네이버에 "네이버 부동산" 검색 → 첫 결과)
2. DevTools 열기 — **맥은 F12가 아니라 ⌥⌘I**, 또는 우클릭 → **검사**
   (윈도우/리눅스는 F12)
3. **Network** 탭 → 필터 줄에서 **Fetch/XHR**
4. **DevTools를 열어둔 채로** 아파트 단지를 하나 클릭 — 이때부터 요청이 잡힌다.
   열기 전에 오간 요청은 목록에 안 남는다.
5. 매물 목록을 불러오는 요청(이름에 `article` / `complex` 가 들어간 것) 우클릭 →
   **Copy → Copy as cURL** (한글판은 **복사 → cURL로 복사**)
6. 붙여넣어서 파일로 저장 (예: `curl.txt`)

제대로 복사했는지 먼저 확인:

```bash
python3 -c "import naver_land; print(naver_land.token_from_curl('curl.txt'))"
```

`Creds(token='Bearer eyJ...', cookie='NNB=...', base='https://...')` 처럼
세 개가 다 나오면 된다.

```bash
python naver_land.py --from-curl curl.txt run
```

토큰만 따로 쓰고 싶으면 환경변수로 넘겨도 되는데, 이때는 도메인을 자동으로
알아낼 수 없으니 기본값과 다르면 `--base` 를 같이 줘야 한다:

```bash
export NAVER_LAND_TOKEN='Bearer eyJ...'
python naver_land.py --base https://fin.land.naver.com run
```

**토큰은 대략 하루 안팎으로 만료된다.** `HTTP 401` 이 뜨면 위 5~6단계를 다시 하면 된다.

## 사용

```bash
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
