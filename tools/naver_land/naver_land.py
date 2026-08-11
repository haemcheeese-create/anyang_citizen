#!/usr/bin/env python3
"""
네이버페이 부동산 매물 수집기 — 평촌 평안동/범계동 전월세용.

공식 API가 아니라 웹사이트가 내부적으로 쓰는 엔드포인트를 그대로 호출한다.
따라서 (1) 브라우저에서 발급된 Bearer 토큰이 필요하고, (2) 네이버가 스펙을
바꾸면 깨질 수 있다. 스펙 변경에 대비해 정규화한 CSV와 별개로 원본 JSON을
항상 같이 저장한다.

사용법은 같은 폴더의 README.md 참고.

의존성 없음(표준 라이브러리만). Python 3.9+
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Iterable, NamedTuple

# 확인된 동작 주소. 서비스 이름이 네이버페이 부동산으로 바뀌면서 페이지에
# NAVER FINANCIAL 로고가 붙지만 도메인은 그대로다.
#   https://new.land.naver.com/complexes?ms=<lat>,<lon>,<zoom>&a=APT:PRE:ABYG:JGC&e=RETAIL
# 그래도 이 값은 폴백일 뿐이고, --from-har / --from-curl 을 쓰면 실제 요청
# URL에서 오리진을 읽어 쓴다. 주소를 코드가 알고 있다고 가정하지 않는다.
DEFAULT_BASE = "https://new.land.naver.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 법정동 코드. 부동산 사이트는 전부 법정동 기준이라 행정동 이름으로는 검색이 안 된다.
#   평안동(행정동) -> 평촌동(법정동)
#   범계동(행정동) -> 호계동(법정동)
CORTAR = {
    "평촌동": "4117310300",
    "호계동": "4117310400",
    "관양동": "4117310200",
    "비산동": "4117310100",
}

# 행정동 -> (법정동, 단지명 필터 키워드)
PRESETS = {
    "평안동": ("평촌동", ["초원", "향촌"]),
    "범계동": ("호계동", ["목련"]),
}

TRADE_TYPES = {"A1": "매매", "B1": "전세", "B2": "월세", "B3": "단기임대"}

# discover 폴백용. 평촌신도시 전체를 덮는 대략적인 bbox.
FALLBACK_BBOX = {
    "top": 37.4060,
    "bottom": 37.3740,
    "left": 126.9330,
    "right": 126.9770,
}


class TokenError(RuntimeError):
    pass


# ---------------------------------------------------------------- 인증


class Creds(NamedTuple):
    token: str
    cookie: str
    base: str


def token_from_curl(path: str) -> Creds:
    """DevTools 'Copy as cURL' 덤프에서 오리진 / Authorization / Cookie 를 뽑아낸다.

    토큰 형식이나 도메인을 추측하는 것보다 브라우저가 실제로 보낸 요청을 그대로
    쓰는 게 가장 확실하다. 네이버가 도메인이나 파라미터를 바꿔도 이 방식은 살아남는다.
    """
    with open(path, "r", encoding="utf-8") as fh:
        blob = fh.read()

    auth = re.search(
        r"""["']?authorization["']?\s*:\s*["']?(Bearer\s+[A-Za-z0-9._~+/=-]+)""",
        blob,
        re.IGNORECASE,
    )
    if not auth:
        raise TokenError(
            f"{path} 에서 authorization 헤더를 못 찾았습니다. "
            "XHR 요청을 'Copy as cURL'로 복사했는지 확인해 주세요."
        )

    # cURL은 헤더 전체를 한 번에 따옴표로 감싸므로(-H 'cookie: a=b; c=d')
    # 값 앞에 따옴표가 없다. 값 뒤의 닫는 따옴표/줄바꿈까지만 잘라낸다.
    cookie = re.search(r"""["']?cookie["']?\s*:\s*([^"'\n]+)""", blob, re.IGNORECASE)

    # 도메인은 하드코딩하지 않고 덤프 안의 실제 요청 URL에서 읽는다.
    origin = DEFAULT_BASE
    url = re.search(r"""curl\s+['"]?(https?://[^/'"\s]+)""", blob, re.IGNORECASE)
    if url:
        origin = url.group(1)

    return Creds(
        auth.group(1).strip(),
        cookie.group(1).strip() if cookie else "",
        origin,
    )


# HAR 안에는 광고/트래킹 요청이 잔뜩 섞여 있다. 부동산 API 요청만 골라내는 기준.
API_HINTS = ("land.naver.com", "/api/", "/front-api/", "article", "complex", "cortar")
NOISE_HINTS = ("gfp-display", "gfp-core", "doubleclick", "google", "adcr", "nlog", "wcslog")


EMPTY_HAR_HELP = (
    "HAR에 기록된 요청이 0건입니다. 브라우저가 아무것도 안 받은 상태로 저장된 겁니다.\n"
    "흔한 원인:\n"
    "  · 404 같은 오류 페이지에서 저장했다 (그 페이지는 요청을 안 보냅니다)\n"
    "  · DevTools를 연 뒤 페이지에서 아무 동작도 안 했다\n"
    "    → Network 탭을 연 채로 새로고침(⌘R)하거나 단지를 클릭한 뒤 저장하세요\n"
    "  · 왼쪽 위 빨간 ⏺ 버튼이 꺼져 있어 기록이 멈춰 있었다"
)


def har_total(path: str) -> int:
    """HAR에 기록된 전체 요청 수. 0이면 애초에 아무것도 안 잡힌 것이다."""
    with open(path, "r", encoding="utf-8") as fh:
        return len(json.load(fh).get("log", {}).get("entries", []))


def har_entries(path: str) -> list[dict]:
    """HAR 파일에서 부동산 API로 보이는 요청만 추려낸다.

    'Copy as cURL' 은 사용자가 목록에서 맞는 요청을 직접 찾아야 하는데,
    광고/트래킹 요청에 파묻혀 있어 실패하기 쉽다. HAR 은 Network 탭의
    내려받기 버튼 한 번이면 전부 나오므로, 고르는 일을 코드가 대신한다.
    """
    with open(path, "r", encoding="utf-8") as fh:
        har = json.load(fh)

    out = []
    for entry in har.get("log", {}).get("entries", []):
        req = entry.get("request", {})
        url = req.get("url", "")
        low = url.lower()
        if any(n in low for n in NOISE_HINTS):
            continue
        if not any(h in low for h in API_HINTS):
            continue

        headers = {h.get("name", "").lower(): h.get("value", "") for h in req.get("headers", [])}
        out.append(
            {
                "url": url,
                "method": req.get("method", "GET"),
                "status": entry.get("response", {}).get("status"),
                "auth": headers.get("authorization", ""),
                "cookie": headers.get("cookie", ""),
            }
        )
    return out


def token_from_har(path: str) -> Creds:
    entries = har_entries(path)
    if not entries:
        total = har_total(path)
        if total == 0:
            raise TokenError(f"{path}\n{EMPTY_HAR_HELP}")
        raise TokenError(
            f"{path} 에 요청은 {total}건 기록됐는데 그 중 부동산 API는 없습니다.\n"
            "홈 화면이 아니라 아파트 단지를 클릭해서 매물 목록이 뜬 뒤에\n"
            "HAR 을 내려받아 주세요. (홈 화면 요청은 광고/추천 위젯뿐입니다)"
        )

    authed = [e for e in entries if e["auth"].lower().startswith("bearer")]
    if not authed:
        raise TokenError(
            f"{path} 에 부동산 API 요청은 {len(entries)}건 있는데 "
            "Authorization 헤더가 붙은 건 없습니다.\n"
            f"  python3 {os.path.basename(__file__)} inspect --from-har {path}\n"
            "로 어떤 요청들이 잡혔는지 확인해 주세요."
        )

    # 매물 목록 요청을 우선 고른다. 같은 오리진이면 토큰은 어차피 같지만,
    # 이 선택이 곧 아래 inspect 출력의 기준이 된다.
    authed.sort(key=lambda e: ("article" not in e["url"].lower(), len(e["url"])))
    best = authed[0]
    parts = urllib.parse.urlsplit(best["url"])
    return Creds(best["auth"].strip(), best["cookie"].strip(), f"{parts.scheme}://{parts.netloc}")


def load_auth(args: argparse.Namespace) -> Creds:
    # 토큰 문자열 직접 지정. HAR/cURL 파일을 못 만드는 상황을 위한 경로라
    # 다른 어떤 방식보다 먼저 본다.
    if args.token:
        token = args.token.strip().strip("'\"")
        if not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        base = args.base or os.environ.get("NAVER_LAND_BASE", "").strip() or DEFAULT_BASE
        return Creds(token, args.cookie or "", base)

    if args.from_har:
        creds = token_from_har(args.from_har)
        return creds._replace(base=args.base) if args.base else creds

    if args.from_curl:
        creds = token_from_curl(args.from_curl)
        # 명시적 --base 는 덤프에서 읽은 값보다 우선한다.
        return creds._replace(base=args.base) if args.base else creds

    base = args.base or os.environ.get("NAVER_LAND_BASE", "").strip() or DEFAULT_BASE
    token = os.environ.get("NAVER_LAND_TOKEN", "").strip()
    if token:
        if not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        return Creds(token, os.environ.get("NAVER_LAND_COOKIE", "").strip(), base)

    raise TokenError(
        "토큰이 없습니다.\n"
        "  1) 브라우저에서 네이버페이 부동산 접속\n"
        "  2) 아파트 단지를 하나 클릭해서 매물 목록이 뜨게 함 (홈 화면만으론 안 됨)\n"
        "  3) DevTools > Network 탭 툴바의 아래쪽 화살표(⬇)를 눌러 .har 저장\n"
        "  4) --from-har <파일> 로 넘기면 토큰/도메인/쿠키를 알아서 찾습니다.\n"
        "\n"
        "     맞는 요청을 직접 고르고 싶으면 요청 우클릭 > Copy as cURL 후\n"
        "     --from-curl <파일> 도 됩니다.\n"
    )


# ---------------------------------------------------------------- HTTP


class Client:
    def __init__(self, creds: Creds, delay: float = 1.5, verbose: bool = False):
        self.token = creds.token
        self.cookie = creds.cookie
        self.base = creds.base
        self.delay = delay
        self.verbose = verbose
        self._last_call = 0.0

    def _throttle(self) -> None:
        # 네이버는 자동 수집을 약관에서 제한하고 IP 차단도 건다.
        # 개인 확인 용도 범위를 벗어나지 않도록 요청 간격을 강제한다.
        gap = time.monotonic() - self._last_call
        wait = self.delay + random.uniform(0, 0.4) - gap
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict[str, Any] | None = None, referer: str = "") -> Any:
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        headers = {
            "Authorization": self.token,
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": referer or f"{self.base}/complexes",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        last_err: Exception | None = None
        for attempt in range(4):
            self._throttle()
            if self.verbose:
                print(f"  GET {url}", file=sys.stderr)
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise TokenError(
                        f"HTTP {exc.code} — 토큰이 만료됐거나 거부됐습니다. "
                        "브라우저에서 토큰을 다시 복사해 주세요. (보통 하루 안팎으로 만료)"
                    ) from exc
                if exc.code == 429:
                    back = 5 * (2**attempt)
                    print(f"  429 rate limited, {back}s 대기", file=sys.stderr)
                    time.sleep(back)
                    last_err = exc
                    continue
                if 500 <= exc.code < 600:
                    time.sleep(2**attempt)
                    last_err = exc
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                time.sleep(2**attempt)
                last_err = exc

        raise RuntimeError(f"{url} 요청 실패: {last_err}")


# ---------------------------------------------------------------- 파싱


def parse_price(text: str | None) -> int | None:
    """'3억 5,000' -> 35000 (만원 단위). 파싱 실패 시 None."""
    if not text:
        return None
    text = str(text).replace(",", "").strip()
    total = 0
    matched = False

    eok = re.search(r"(\d+)\s*억", text)
    if eok:
        total += int(eok.group(1)) * 10000
        matched = True
        text = text[eok.end():]

    man = re.search(r"(\d+)", text)
    if man:
        total += int(man.group(1))
        matched = True

    return total if matched else None


def first(d: dict, *keys: str) -> Any:
    """네이버가 필드명을 바꿔도 견디도록 여러 후보 키를 순서대로 시도."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def normalize(article: dict, complex_name: str, trade_code: str, base: str = DEFAULT_BASE) -> dict:
    deal = first(article, "dealOrWarrantPrc")
    rent = first(article, "rentPrc")
    return {
        "단지명": first(article, "articleName", "buildingName") or complex_name,
        "거래유형": first(article, "tradeTypeName") or TRADE_TYPES.get(trade_code, trade_code),
        "보증금_만원": parse_price(deal),
        "월세_만원": parse_price(rent),
        "보증금_표기": deal,
        "월세_표기": rent,
        "공급면적_m2": first(article, "area1"),
        "전용면적_m2": first(article, "area2"),
        "면적명": first(article, "areaName"),
        "층": first(article, "floorInfo"),
        "향": first(article, "direction"),
        "동": first(article, "buildingName"),
        "확인일자": first(article, "articleConfirmYmd"),
        "특징": first(article, "articleFeatureDesc"),
        "중개사": first(article, "realtorName"),
        "동일매물수": first(article, "sameAddrCnt"),
        "태그": ",".join(article.get("tagList") or []),
        "매물번호": first(article, "articleNo"),
        "링크": f"{base}/articles/{first(article, 'articleNo')}"
        if first(article, "articleNo")
        else "",
    }


# ---------------------------------------------------------------- 단지 탐색


def discover_complexes(client: Client, cortar_no: str) -> list[dict]:
    """법정동 코드로 단지 목록(complexNo 포함)을 가져온다.

    엔드포인트가 여러 번 바뀐 이력이 있어 후보를 순서대로 시도하고,
    처음 성공한 것을 쓴다.
    """
    attempts = [
        (
            "/api/regions/complexes",
            {"cortarNo": cortar_no, "realEstateType": "APT", "order": ""},
        ),
        (
            "/api/complexes/single-markers/v2",
            {
                "cortarNo": cortar_no,
                "zoom": 15,
                "realEstateType": "APT",
                "tradeType": "",
                "priceType": "RETAIL",
                "leftLon": FALLBACK_BBOX["left"],
                "rightLon": FALLBACK_BBOX["right"],
                "topLat": FALLBACK_BBOX["top"],
                "bottomLat": FALLBACK_BBOX["bottom"],
            },
        ),
    ]

    for path, params in attempts:
        try:
            data = client.get(path, params)
        except TokenError:
            raise
        except Exception as exc:  # noqa: BLE001 - 다음 후보로 넘어간다
            print(f"  {path} 실패 ({exc}), 다음 방식 시도", file=sys.stderr)
            continue

        rows = data.get("complexList") if isinstance(data, dict) else data
        if not rows:
            print(f"  {path} 응답에 단지가 없음, 다음 방식 시도", file=sys.stderr)
            continue

        out = []
        for row in rows:
            no = first(row, "complexNo", "markerId", "hscpNo")
            name = first(row, "complexName", "complexNm", "hscpNm")
            if no and name:
                out.append(
                    {
                        "complexNo": str(no),
                        "complexName": str(name),
                        "totalHouseholdCount": first(row, "totalHouseholdCount", "totHsehCnt"),
                        "useApproveYmd": first(row, "useApproveYmd", "useAprDay"),
                    }
                )
        if out:
            print(f"  {path} 로 {len(out)}개 단지 확인", file=sys.stderr)
            return out

    raise RuntimeError(
        f"cortarNo={cortar_no} 단지 목록을 못 가져왔습니다. 위 실패 사유를 확인하세요.\n"
        "  · 'Tunnel connection failed' / 'urlopen error' → 네트워크가 막힌 환경입니다.\n"
        "    Claude Code 웹 세션이 아니라 로컬 터미널에서 실행해 주세요.\n"
        "  · 그 외 → 네이버가 엔드포인트를 바꿨을 수 있습니다. 브라우저에서 지도를\n"
        "    움직일 때 나가는 요청을 Copy as cURL 로 확인한 뒤\n"
        "    discover_complexes() 의 후보 목록에 추가하면 됩니다.\n"
        "  · 급하면 단지 페이지 URL(new.land.naver.com/complexes/12345)의 숫자를\n"
        "    fetch --complex 12345 로 직접 넘겨도 됩니다."
    )


# ---------------------------------------------------------------- 매물 수집


def fetch_articles(
    client: Client,
    complex_no: str,
    complex_name: str,
    trade_code: str,
    max_pages: int = 20,
) -> tuple[list[dict], list[dict]]:
    """단지 하나의 매물을 전부 긁는다. (정규화 결과, 원본) 튜플 반환."""
    normalized: list[dict] = []
    raw: list[dict] = []
    referer = f"{client.base}/complexes/{complex_no}"

    for page in range(1, max_pages + 1):
        data = client.get(
            f"/api/articles/complex/{complex_no}",
            {
                "realEstateType": "APT",
                "tradeType": trade_code,
                "page": page,
                "complexNo": complex_no,
                "order": "rank",
                "priceType": "RETAIL",
                "sameAddressGroup": "false",
            },
            referer=referer,
        )

        articles = data.get("articleList") or []
        for art in articles:
            art["_complexNo"] = complex_no
            art["_complexName"] = complex_name
            raw.append(art)
            normalized.append(normalize(art, complex_name, trade_code, client.base))

        if not data.get("isMoreData"):
            break

    return normalized, raw


# ---------------------------------------------------------------- 출력


CSV_FIELDS = [
    "단지명", "거래유형", "보증금_만원", "월세_만원", "보증금_표기", "월세_표기",
    "공급면적_m2", "전용면적_m2", "면적명", "층", "향", "동", "확인일자",
    "특징", "중개사", "동일매물수", "태그", "매물번호", "링크",
]


def write_outputs(rows: list[dict], raw: list[dict], outdir: str, label: str) -> tuple[str, str]:
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    csv_path = os.path.join(outdir, f"{label}-{stamp}.csv")
    raw_path = os.path.join(outdir, f"{label}-{stamp}.raw.jsonl")

    # utf-8-sig: 엑셀에서 한글이 깨지지 않게
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    # 정규화 과정에서 뭘 놓쳤든 원본은 남겨둔다.
    with open(raw_path, "w", encoding="utf-8") as fh:
        for art in raw:
            fh.write(json.dumps(art, ensure_ascii=False) + "\n")

    return csv_path, raw_path


def summarize(rows: list[dict]) -> None:
    if not rows:
        print("\n매물 0건.")
        return

    by_complex: dict[str, list[dict]] = {}
    for row in rows:
        by_complex.setdefault(row["단지명"], []).append(row)

    print(f"\n총 {len(rows)}건 / {len(by_complex)}개 단지\n")
    print(f"{'단지':<24}{'건수':>5}{'보증금 최저':>13}{'보증금 최고':>13}{'중앙값':>11}")
    print("-" * 66)

    def fmt(man: int | None) -> str:
        if man is None:
            return "-"
        if man >= 10000:
            eok, rest = divmod(man, 10000)
            return f"{eok}억 {rest:,}" if rest else f"{eok}억"
        return f"{man:,}"

    for name in sorted(by_complex):
        items = by_complex[name]
        prices = sorted(r["보증금_만원"] for r in items if r["보증금_만원"] is not None)
        if prices:
            mid = prices[len(prices) // 2]
            print(f"{name:<24}{len(items):>5}{fmt(prices[0]):>13}{fmt(prices[-1]):>13}{fmt(mid):>11}")
        else:
            print(f"{name:<24}{len(items):>5}{'-':>13}{'-':>13}{'-':>11}")


# ---------------------------------------------------------------- CLI


def cmd_discover(args: argparse.Namespace, client: Client) -> int:
    cortar = CORTAR.get(args.dong, args.dong)
    complexes = discover_complexes(client, cortar)
    print(f"\n{args.dong} (cortarNo={cortar}) — {len(complexes)}개 단지\n")
    print(f"{'complexNo':<12}{'단지명':<28}{'세대수':>8}  준공")
    print("-" * 62)
    for c in complexes:
        print(
            f"{c['complexNo']:<12}{c['complexName']:<28}"
            f"{str(c['totalHouseholdCount'] or '-'):>8}  {c['useApproveYmd'] or '-'}"
        )
    return 0


def collect(
    client: Client,
    targets: Iterable[tuple[str, str]],
    trade_codes: list[str],
) -> tuple[list[dict], list[dict]]:
    all_rows: list[dict] = []
    all_raw: list[dict] = []
    targets = list(targets)

    for idx, (no, name) in enumerate(targets, 1):
        for trade in trade_codes:
            label = TRADE_TYPES.get(trade, trade)
            print(f"[{idx}/{len(targets)}] {name} ({no}) {label} …", file=sys.stderr)
            try:
                rows, raw = fetch_articles(client, no, name, trade)
            except TokenError:
                raise
            except Exception as exc:  # noqa: BLE001 - 한 단지 실패로 전체를 멈추지 않는다
                print(f"    실패: {exc}", file=sys.stderr)
                continue
            print(f"    {len(rows)}건", file=sys.stderr)
            all_rows.extend(rows)
            all_raw.extend(raw)

    return all_rows, all_raw


def cmd_fetch(args: argparse.Namespace, client: Client) -> int:
    targets = [(no, f"complex-{no}") for no in args.complex]
    rows, raw = collect(client, targets, args.trade)
    csv_path, raw_path = write_outputs(rows, raw, args.out, "fetch")
    summarize(rows)
    print(f"\nCSV: {csv_path}\n원본: {raw_path}")
    return 0


def cmd_run(args: argparse.Namespace, client: Client) -> int:
    dongs = args.dong or list(PRESETS)
    targets: list[tuple[str, str]] = []

    for dong in dongs:
        if dong not in PRESETS:
            print(f"프리셋에 없는 동: {dong} (가능: {', '.join(PRESETS)})", file=sys.stderr)
            return 2
        legal_dong, keywords = PRESETS[dong]
        print(f"\n== {dong} (법정동 {legal_dong}) 단지 탐색", file=sys.stderr)
        complexes = discover_complexes(client, CORTAR[legal_dong])
        matched = [
            c for c in complexes if any(kw in c["complexName"] for kw in keywords)
        ]
        print(f"   '{'/'.join(keywords)}' 매칭 {len(matched)}개", file=sys.stderr)
        targets.extend((c["complexNo"], c["complexName"]) for c in matched)

    if not targets:
        print("대상 단지가 없습니다. discover 로 단지명을 먼저 확인해 보세요.", file=sys.stderr)
        return 1

    rows, raw = collect(client, targets, args.trade)
    label = "-".join(dongs)
    csv_path, raw_path = write_outputs(rows, raw, args.out, label)
    summarize(rows)
    print(f"\nCSV: {csv_path}\n원본: {raw_path}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """HAR 안에서 발견한 부동산 API 요청을 보여준다. 인증이 필요 없다.

    토큰과 쿠키는 일부러 찍지 않는다. 이 출력은 그대로 공유해도 안전해야 한다.
    """
    if not args.from_har:
        print("inspect 는 --from-har <파일> 이 필요합니다.", file=sys.stderr)
        return 2

    total = har_total(args.from_har)
    entries = har_entries(args.from_har)
    if not entries:
        if total == 0:
            print(f"{args.from_har}\n{EMPTY_HAR_HELP}", file=sys.stderr)
        else:
            print(
                f"{args.from_har} 에 요청은 {total}건 기록됐는데 "
                "그 중 부동산 API는 없습니다.\n"
                "홈 화면이 아니라 아파트 단지를 클릭해서 매물 목록이 뜬 뒤에\n"
                "HAR 을 내려받아 주세요.",
                file=sys.stderr,
            )
        return 1
    print(f"\n전체 기록 {total}건 중 부동산 API로 보이는 요청만 추립니다.", file=sys.stderr)

    seen: set[str] = set()
    print(f"\n부동산 API 요청 {len(entries)}건\n")
    for e in entries:
        parts = urllib.parse.urlsplit(e["url"])
        key = f"{e['method']} {parts.netloc}{parts.path}"
        if key in seen:
            continue
        seen.add(key)
        lock = "🔑" if e["auth"] else "  "
        print(f"{lock} [{e['status']}] {key}")
        if parts.query:
            for kv in parts.query.split("&"):
                print(f"      {kv}")
    print("\n🔑 = Authorization 헤더가 붙은 요청. 토큰/쿠키 값은 출력하지 않습니다.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # 공통 플래그를 부모 파서에 두고 하위 명령에도 물려줘서
    # `--from-curl x run` 과 `run --from-curl x` 가 둘 다 되게 한다.
    # default=SUPPRESS 라야 하위 파서의 기본값이 앞에서 파싱한 값을 덮어쓰지 않는다.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--from-curl", metavar="FILE", default=argparse.SUPPRESS,
        help="DevTools 'Copy as cURL' 덤프 파일",
    )
    common.add_argument(
        "--token", metavar="STR", default=argparse.SUPPRESS,
        help="Bearer 토큰 문자열 직접 지정 (파일 없이 실행)",
    )
    common.add_argument(
        "--cookie", metavar="STR", default=argparse.SUPPRESS,
        help="쿠키 문자열. 보통 없어도 된다",
    )
    common.add_argument(
        "--from-har", metavar="FILE", default=argparse.SUPPRESS,
        help="DevTools Network 탭에서 내려받은 .har 파일",
    )
    common.add_argument(
        "--base", metavar="URL", default=argparse.SUPPRESS,
        help="API 오리진 직접 지정 (예: https://new.land.naver.com). "
             "--from-curl 을 쓰면 덤프에서 자동으로 읽으므로 보통 불필요",
    )
    common.add_argument(
        "--delay", type=float, default=argparse.SUPPRESS,
        help="요청 간 최소 간격(초), 기본 1.5",
    )
    common.add_argument(
        "--out", default=argparse.SUPPRESS, help="출력 디렉터리, 기본 out/",
    )
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="요청 URL 출력",
    )

    p = argparse.ArgumentParser(
        parents=[common],
        description="네이버 부동산 전월세 매물 수집 (평촌 평안동/범계동)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""예시:
  export NAVER_LAND_TOKEN='Bearer eyJ...'
  python naver_land.py discover 평촌동
  python naver_land.py run --dong 평안동 범계동 --trade B1 B2
  python naver_land.py fetch --complex 12345 --trade B1
  python naver_land.py run --from-curl curl.txt
""",
    )
    # set_defaults() 는 쓰지 않는다. parents= 는 action 객체를 복사가 아니라
    # 공유하므로 set_defaults() 가 공유 action 의 default 를 덮어써서
    # SUPPRESS 가 풀리고, 하위 파서가 앞에서 파싱한 값을 도로 밀어버린다.
    # 기본값은 파싱이 끝난 뒤 apply_defaults() 에서 채운다.
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", parents=[common], help="법정동의 단지 목록과 complexNo 조회")
    d.add_argument("dong", help="법정동명(평촌동/호계동/…) 또는 cortarNo")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("run", parents=[common], help="프리셋(평안동/범계동) 전체 수집")
    r.add_argument("--dong", nargs="*", choices=list(PRESETS), help="기본: 전체")
    r.add_argument("--trade", nargs="+", default=["B1", "B2"], choices=list(TRADE_TYPES))
    r.set_defaults(func=cmd_run)

    i = sub.add_parser("inspect", parents=[common], help="HAR 안의 API 요청 확인 (인증 불필요)")
    i.set_defaults(func=None)

    f = sub.add_parser("fetch", parents=[common], help="complexNo 직접 지정해서 수집")
    f.add_argument("--complex", nargs="+", required=True, help="단지 번호(들)")
    f.add_argument("--trade", nargs="+", default=["B1"], choices=list(TRADE_TYPES))
    f.set_defaults(func=cmd_fetch)

    return p


COMMON_DEFAULTS = {"token": None, "cookie": None, "from_curl": None, "from_har": None, "base": None, "delay": 1.5, "out": "out", "verbose": False}


def apply_defaults(args: argparse.Namespace) -> argparse.Namespace:
    for key, value in COMMON_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    return args


def main(argv: list[str] | None = None) -> int:
    args = apply_defaults(build_parser().parse_args(argv))

    # inspect 는 토큰 없이도 돌아야 한다. 토큰을 못 구했을 때 쓰는 진단 명령이라
    # 여기서 인증을 요구하면 순서가 거꾸로다.
    if args.cmd == "inspect":
        try:
            return cmd_inspect(args)
        except (TokenError, RuntimeError) as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1

    try:
        creds = load_auth(args)
    except TokenError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    client = Client(creds, delay=args.delay, verbose=args.verbose)
    try:
        return args.func(args, client)
    except TokenError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        # 네트워크 차단이나 스펙 변경은 예상 가능한 실패다. 트레이스백 대신
        # 다음에 뭘 하면 되는지만 보여준다.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
