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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Iterable, NamedTuple

# 지도 화면과 API가 같은 호스트다. new.land.naver.com 은 여기로 넘겨주는
# 옛 주소일 뿐이라, Origin/Referer 를 그쪽으로 보내면 교차 출처 요청이 되어
# 거부당한다(429). 광고 요청에 실려 있던 페이지 주소가 근거:
#   url=https://fin.land.naver.com/map?center=...&zoom=...&tradeTypes=B1-B2
PAGE_BASE = "https://fin.land.naver.com"
MAP_PAGE = "https://fin.land.naver.com/map"
DEFAULT_BASE = "https://fin.land.naver.com"

# 브라우저 XHR 로그에서 그대로 확인한 엔드포인트.
#
# 지도 단위 조회는 쿼리스트링이 없다 = 영역 좌표를 본문에 담는 POST다.
# 본문 형식은 아직 확인 전이라 여기 이름만 적어 둔다.
MAP_ENDPOINTS = {
    # 법정동 코드 배열을 그대로 받는다. 지도 좌표를 계산할 필요가 없어서
    # 동 단위 수집에는 이쪽이 boundingBox 방식보다 정확하다.
    "legal_complexes": "/front-api/v1/complex/legalDivisionComplexClusters",
    "legal_articles": "/front-api/v1/article/legalDivisionArticleClusters",
    # 좌표 사각형 기준. 참고용으로만 남긴다.
    "complex_clusters": "/front-api/v1/complex/complexClusters",
    "article_clusters": "/front-api/v1/article/map/articleClusters",
    "bounded_count": "/front-api/v1/article/boundedArticlesCount",
}

# 브라우저가 보내는 filter 객체를 그대로 옮겼다. 빈 배열들도 서버가 존재를
# 기대할 수 있으므로 값이 없다고 빼지 않는다.
def build_filter(trade_types: list[str], warranty_max: int, rent_max: int) -> dict:
    return {
        "tradeTypes": trade_types,
        "realEstateTypes": [REAL_ESTATE_APT],
        "roomCount": [],
        "bathRoomCount": [],
        "optionTypes": [],
        "oneRoomShapeTypes": [],
        "moveInTypes": [],
        "warrantyPrice": {"min": 0, "max": warranty_max},
        "rentPrice": {"min": 0, "max": rent_max},
        "filtersExclusiveSpace": False,
        "floorTypes": [],
        "directionTypes": [],
        "hasArticlePhoto": False,
        "isAuthorizedByOwner": False,
        "parkingTypes": [],
        "entranceTypes": [],
        "hasArticle": False,
    }

# 단지 단위 조회는 전부 GET + 쿼리스트링이라 그대로 부를 수 있다.
COMPLEX_ENDPOINTS = {
    "detail": "/front-api/v1/complex",
    "summary": "/front-api/v1/complex/mapComplexSummaryInfo",
    "pyeong_list": "/front-api/v1/complex/pyeongList",
    "pyeong_groups": "/front-api/v1/complex/pyeongGroups",
    "article_count": "/front-api/v1/complex/article/count",
    "asking_price": "/front-api/v1/complex/asking-price",
    "market_recent": "/front-api/v1/complex/marketPrice/recent",
    "real_price": "/front-api/v1/complex/pyeong/realPrice/list",
}

# 관측된 코드값
REAL_ESTATE_APT = "A01"          # 아파트
MARKET_CPS = ["kab", "kbstar", "neonet"]   # 시세 제공처
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

    def __repr__(self) -> str:
        # 쿠키에는 로그인 세션이 들어 있다. 실수로 화면이나 로그에 남지 않도록
        # 있으면 있다는 사실만 보인다.
        return (
            f"Creds(token={'있음' if self.token else '없음'}, "
            f"cookie={'있음' if self.cookie else '없음'}, base={self.base!r})"
        )


CLIPBOARD_CMDS = (["pbpaste"], ["wl-paste"], ["xclip", "-o", "-selection", "clipboard"])


def cookie_from_clipboard() -> str:
    """클립보드에서 쿠키를 직접 읽는다.

    쿠키를 명령줄 인자로 넘기면 화면과 셸 기록에 그대로 남는다. 네이버 쿠키에는
    로그인 세션(NID_SES)이 들어 있어서 그건 계정 열쇠를 흘리는 것과 같다.
    클립보드에서 바로 읽으면 어디에도 남지 않는다.
    """
    for cmd in CLIPBOARD_CMDS:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
        text = done.stdout.strip() if done.returncode == 0 else ""
        if not text:
            continue
        if "=" not in text:
            raise TokenError(
                "클립보드 내용이 쿠키처럼 보이지 않습니다.\n"
                "브라우저 콘솔에서  copy(document.cookie)  를 먼저 실행해 주세요."
            )
        return text

    raise TokenError(
        "클립보드를 읽지 못했습니다. --cookie-file <파일> 을 쓰거나,\n"
        "쿠키를 파일로 저장한 뒤 그 경로를 넘겨 주세요."
    )


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


def load_cookie(args: argparse.Namespace) -> str:
    if args.cookie_clipboard:
        return cookie_from_clipboard()
    if args.cookie_file:
        with open(args.cookie_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return args.cookie or os.environ.get("NAVER_LAND_COOKIE", "").strip()


def load_auth(args: argparse.Namespace) -> Creds:
    # 토큰 문자열 직접 지정. HAR/cURL 파일을 못 만드는 상황을 위한 경로라
    # 다른 어떤 방식보다 먼저 본다.
    cookie = load_cookie(args)

    if args.token:
        token = args.token.strip().strip("'\"")
        if not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        base = args.base or os.environ.get("NAVER_LAND_BASE", "").strip() or DEFAULT_BASE
        return Creds(token, cookie, base)

    if args.from_har:
        creds = token_from_har(args.from_har)
        if args.base:
            creds = creds._replace(base=args.base)
        return creds._replace(cookie=cookie) if cookie else creds

    if args.from_curl:
        creds = token_from_curl(args.from_curl)
        # 명시적 --base 는 덤프에서 읽은 값보다 우선한다.
        return creds._replace(base=args.base) if args.base else creds

    base = args.base or os.environ.get("NAVER_LAND_BASE", "").strip() or DEFAULT_BASE
    token = os.environ.get("NAVER_LAND_TOKEN", "").strip()
    if token:
        if not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        return Creds(token, cookie, base)

    if cookie:
        return Creds("", cookie, base)

    # 토큰 없이 그냥 해본다. 브라우저에서 확인해 보니 이 API는 Authorization
    # 헤더 없이도 응답한다. 인증을 요구할 때만 401이 나고, 그때 안내하면 된다.
    print("쿠키 없이 시도합니다. 429가 나오면 --cookie-clipboard 를 붙여 주세요.", file=sys.stderr)
    return Creds("", os.environ.get("NAVER_LAND_COOKIE", "").strip(), base)


# ---------------------------------------------------------------- HTTP


class Client:
    def __init__(self, creds: Creds, delay: float = 2.0, verbose: bool = False):
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
        return self.request("GET", path, params=params, referer=referer)

    def post(self, path: str, body: dict[str, Any], referer: str = "") -> Any:
        return self.request("POST", path, body=body, referer=referer)

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        referer: str = "",
    ) -> Any:
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None

        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": referer or MAP_PAGE,
            "Origin": PAGE_BASE,
            # 같은 출처의 XHR 임을 알린다. 브라우저가 자동으로 붙이는 값들이라
            # 빠지면 정상 트래픽으로 보이지 않는다.
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = self.token
        if self.cookie:
            headers["Cookie"] = self.cookie

        last_err: Exception | None = None
        for attempt in range(4):
            self._throttle()
            if self.verbose:
                print(f"  {method} {url}", file=sys.stderr)
                if payload is not None:
                    print(f"       {payload.decode('utf-8')}", file=sys.stderr)
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise TokenError(
                        f"HTTP {exc.code} — 인증이 거부됐습니다.\n"
                        "이 API는 보통 토큰 없이도 응답하는데, 막힌다면 로그인 쿠키가 필요합니다.\n"
                        "  --cookie 'NNB=...; NAC=...'  형태로 넘겨 보세요."
                    ) from exc
                if exc.code == 429:
                    if not self.cookie:
                        # 첫 요청부터 429면 혼잡이 아니라 봇으로 걸러진 것이다.
                        # 세션 쿠키 없이는 재시도해도 계속 막힌다.
                        raise TokenError(
                            "HTTP 429 — 요청이 거부됐습니다. 세션 쿠키가 없어서\n"
                            "자동 수집으로 걸러진 것으로 보입니다.\n\n"
                            "1) 브라우저 지도 화면의 콘솔(⌥⌘I → Console)에서\n"
                            "     copy(document.cookie)\n"
                            "2) 터미널에서 같은 명령에 --cookie-clipboard 만 덧붙이세요.\n"
                            "   클립보드에서 바로 읽으므로 붙여넣을 필요가 없습니다.\n\n"
                            "쿠키에는 네이버 로그인 세션이 들어 있습니다. 화면에 붙여넣거나\n"
                            "남에게 보내지 마세요."
                        ) from exc
                    back = 10 * (2**attempt)
                    print(f"  429 — {back}초 대기 후 재시도", file=sys.stderr)
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

        raise RuntimeError(f"{method} {url} 요청 실패: {last_err}")


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
        "링크": f"{PAGE_BASE}/articles/{first(article, 'articleNo')}"
        if first(article, "articleNo")
        else "",
    }


# ---------------------------------------------------------------- 단지 탐색


def discover_complexes(
    client: Client,
    cortar_nos: list[str],
    trade_types: list[str],
    warranty_max: int = 2_000_000_000,
    rent_max: int = 20_000_000,
) -> list[dict]:
    """법정동 코드로 단지 목록을 가져온다.

    브라우저는 지도 사각형(boundingBox)과 법정동 코드 두 가지 방식을 모두 쓰는데,
    동 단위로 훑을 때는 좌표를 추정할 필요가 없는 법정동 쪽이 정확하다.
    """
    body = {
        "filter": {
            **build_filter(trade_types, warranty_max, rent_max),
            "legalDivisionNumbers": cortar_nos,
            "legalDivisionType": "EUP",
        }
    }
    data = client.post(MAP_ENDPOINTS["legal_complexes"], body)

    # 응답 구조를 아직 확인하지 못했으므로 단지 번호를 통째로 훑어 모은다.
    numbers = sorted(str(v) for v in find_values(data, "complexNumber"))
    if not numbers:
        raise RuntimeError(
            "단지 목록 응답에서 complexNumber 를 못 찾았습니다.\n"
            "받은 구조는 아래와 같습니다. 키 이름을 알려주시면 맞추겠습니다.\n  "
            + "\n  ".join(describe(data)[:40])
        )
    return [{"complexNo": no} for no in numbers]


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
    referer = f"{MAP_PAGE}?complexNumber={complex_no}"

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


def complex_name(client: Client, complex_no: str) -> str:
    """단지 이름을 찾는다. 못 찾으면 번호를 그대로 쓴다."""
    for key in ("detail", "summary"):
        try:
            data = complex_get(client, key, complex_no)
        except Exception:  # noqa: BLE001 - 이름은 있으면 좋은 정보지 필수가 아니다
            continue
        for field in ("complexName", "complexNm", "name"):
            names = find_values(data, field)
            if names:
                return str(sorted(names)[0])
    return f"단지{complex_no}"


def cmd_discover(args: argparse.Namespace, client: Client) -> int:
    dongs = args.dong or ["평촌동", "호계동"]
    cortars = [CORTAR.get(d, d) for d in dongs]
    complexes = discover_complexes(client, cortars, args.trade)

    print(f"\n{'/'.join(dongs)} ({', '.join(cortars)}) — {len(complexes)}개 단지\n")
    print(f"{'complexNo':<12}단지명")
    print("-" * 46)
    for c in complexes:
        print(f"{c['complexNo']:<12}{complex_name(client, c['complexNo'])}")
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
    """프리셋 동네의 단지를 찾아 평형별 호가를 모은다."""
    dongs = args.dong or list(PRESETS)
    for dong in dongs:
        if dong not in PRESETS:
            print(f"프리셋에 없는 동: {dong} (가능: {', '.join(PRESETS)})", file=sys.stderr)
            return 2

    cortars = [CORTAR[PRESETS[d][0]] for d in dongs]
    keywords = [kw for d in dongs for kw in PRESETS[d][1]]
    print(f"\n{'/'.join(dongs)} 단지 탐색 …", file=sys.stderr)
    complexes = discover_complexes(client, cortars, args.trade)
    print(f"  {len(complexes)}개 단지 확인", file=sys.stderr)

    named = [(c["complexNo"], complex_name(client, c["complexNo"])) for c in complexes]
    targets = [(no, nm) for no, nm in named if any(kw in nm for kw in keywords)]
    print(f"  '{'/'.join(keywords)}' 매칭 {len(targets)}개", file=sys.stderr)
    if not targets:
        print("\n이름이 안 맞습니다. 전체 단지는 아래와 같습니다:", file=sys.stderr)
        for no, nm in named:
            print(f"  {no}  {nm}", file=sys.stderr)
        return 1

    rows: list[dict] = []
    raw: dict[str, Any] = {}
    for idx, (no, name) in enumerate(targets, 1):
        print(f"[{idx}/{len(targets)}] {name} ({no})", file=sys.stderr)
        try:
            pyeong_data = complex_get(client, "pyeong_list", no)
        except Exception as exc:  # noqa: BLE001 - 한 단지 실패로 전체를 멈추지 않는다
            print(f"    평형 조회 실패: {exc}", file=sys.stderr)
            continue
        raw[f"{no}.pyeong_list"] = pyeong_data

        for pyeong in sorted(find_values(pyeong_data, "pyeongTypeNumber")):
            for trade in args.trade:
                try:
                    ask = complex_get(
                        client, "asking_price", no,
                        pyeongTypeNumber=pyeong,
                        realEstateType=REAL_ESTATE_APT,
                        tradeType=trade,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"    평형{pyeong} {trade} 실패: {exc}", file=sys.stderr)
                    continue
                raw[f"{no}.asking.{pyeong}.{trade}"] = ask
                rows.append(collect_asking_row(no, name, pyeong, trade, pyeong_data, ask))

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    label = "-".join(dongs)
    raw_path = os.path.join(args.out, f"{label}-{stamp}.raw.json")
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=2)

    if not rows:
        print(f"\n호가를 못 뽑았습니다. 원본을 확인해 주세요: {raw_path}", file=sys.stderr)
        return 1

    csv_path = os.path.join(args.out, f"{label}-{stamp}.csv")
    fields = list(rows[0])
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print_asking_table(rows)
    print(f"\nCSV: {csv_path}\n원본: {raw_path}")
    return 0


def collect_asking_row(
    complex_no: str, name: str, pyeong: Any, trade: str,
    pyeong_data: Any, ask: Any,
) -> dict:
    """호가 응답에서 값을 뽑는다. 필드명을 확정하지 못해 후보를 넓게 본다."""
    def pick(data: Any, *names: str) -> Any:
        for n in names:
            vals = {v for v in find_values(data, n) if isinstance(v, (int, float))}
            if vals:
                return min(vals) if "min" in n.lower() else max(vals)
        return None

    return {
        "단지명": name,
        "단지번호": complex_no,
        "평형번호": pyeong,
        "거래유형": TRADE_TYPES.get(trade, trade),
        "최저가": pick(ask, "minPrice", "minDealPrice", "lowPrice"),
        "최고가": pick(ask, "maxPrice", "maxDealPrice", "highPrice"),
        "매물수": pick(ask, "count", "articleCount", "totalCount"),
        "링크": f"{MAP_PAGE}?complexNumber={complex_no}",
    }


def print_asking_table(rows: list[dict]) -> None:
    def won(v: Any) -> str:
        if not isinstance(v, (int, float)):
            return "-"
        man = int(v) // 10000          # 원 -> 만원
        if man >= 10000:
            eok, rest = divmod(man, 10000)
            return f"{eok}억 {rest:,}" if rest else f"{eok}억"
        return f"{man:,}"

    print(f"\n{'단지':<22}{'평형':>5}{'유형':>6}{'최저':>13}{'최고':>13}{'매물':>6}")
    print("-" * 66)
    for r in rows:
        print(
            f"{r['단지명'][:20]:<22}{str(r['평형번호']):>5}{r['거래유형']:>6}"
            f"{won(r['최저가']):>13}{won(r['최고가']):>13}{str(r['매물수'] or '-'):>6}"
        )


# ---------------------------------------------------------------- 단지 조회


def describe(obj: Any, path: str = "", out: list[str] | None = None, depth: int = 0) -> list[str]:
    """응답의 구조를 키 경로로 펼쳐 보여준다.

    응답 형식을 아직 모르는 상태라 파서를 먼저 쓸 수가 없다. 실제로 받은
    모양을 눈으로 확인하고 나서 정확한 필드만 뽑는 게 순서다.
    """
    if out is None:
        out = []
    if depth > 4 or len(out) > 120:
        return out

    if isinstance(obj, dict):
        for k, v in obj.items():
            describe(v, f"{path}.{k}" if path else k, out, depth + 1)
    elif isinstance(obj, list):
        out.append(f"{path}[] ({len(obj)}건)")
        if obj:
            describe(obj[0], f"{path}[0]", out, depth + 1)
    else:
        text = str(obj)
        if len(text) > 60:
            text = text[:57] + "..."
        out.append(f"{path} = {text}")
    return out


def complex_get(client: Client, key: str, complex_no: str, **extra: Any) -> Any:
    params: dict[str, Any] = {"complexNumber": complex_no}
    params.update(extra)
    return client.get(COMPLEX_ENDPOINTS[key], params,
                      referer=f"{MAP_PAGE}?complexNumber={complex_no}")


def cmd_complex(args: argparse.Namespace, client: Client) -> int:
    """단지 하나를 확인된 GET 엔드포인트로 훑는다.

    응답을 전부 파일로 남기고 구조를 출력한다. 지도 단위 POST 의 본문 형식을
    아직 모르므로, 단지 번호를 아는 경우에 한해 여기부터 실제로 동작한다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    year_ago = f"{datetime.now().year - 1}{datetime.now().strftime('-%m-%d')}"
    dump: dict[str, Any] = {}

    for complex_no in args.complex:
        print(f"\n{'=' * 60}\n단지 {complex_no}\n{'=' * 60}")

        calls: list[tuple[str, dict[str, Any]]] = [
            ("summary", {}),
            ("article_count", {}),
            ("pyeong_list", {}),
        ]
        for key, extra in calls:
            try:
                data = complex_get(client, key, complex_no, **extra)
            except TokenError:
                raise
            except Exception as exc:  # noqa: BLE001 - 한 엔드포인트 실패로 멈추지 않는다
                print(f"\n[{key}] 실패: {exc}")
                continue
            dump[f"{complex_no}.{key}"] = data
            print(f"\n[{key}]")
            for line in describe(data):
                print(f"  {line}")

        # 평형 번호를 알아야 호가/실거래를 부를 수 있다. 응답 어디에 있는지
        # 모르니 pyeongList 전체에서 그럴듯한 값을 긁는다.
        pyeongs = sorted(find_values(dump.get(f"{complex_no}.pyeong_list"), "pyeongTypeNumber"))
        if not pyeongs:
            print("\n평형 번호를 못 찾았습니다. 위 pyeong_list 구조를 보고 키 이름을 알려주세요.")
            pyeongs = [args.pyeong] if args.pyeong else []

        for pyeong in pyeongs[: args.max_pyeong]:
            for trade in args.trade:
                for key, extra in (
                    ("asking_price", {"pyeongTypeNumber": pyeong,
                                      "realEstateType": REAL_ESTATE_APT, "tradeType": trade}),
                    ("real_price", {"pyeongTypeNumber": pyeong,
                                    "realEstateType": REAL_ESTATE_APT, "tradeType": trade,
                                    "startDate": year_ago, "endDate": today}),
                    ("market_recent", {"pyeongTypeNumber": pyeong,
                                       "realEstateType": REAL_ESTATE_APT,
                                       "cpList[]": MARKET_CPS}),
                ):
                    try:
                        data = complex_get(client, key, complex_no, **extra)
                    except TokenError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        print(f"\n[{key} 평형{pyeong} {trade}] 실패: {exc}")
                        continue
                    dump[f"{complex_no}.{key}.{pyeong}.{trade}"] = data
                    print(f"\n[{key}] 평형{pyeong} {TRADE_TYPES.get(trade, trade)}")
                    for line in describe(data):
                        print(f"  {line}")

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"complex-{datetime.now():%Y%m%d-%H%M}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dump, fh, ensure_ascii=False, indent=2)
    print(f"\n원본 응답 저장: {path}")
    return 0


def find_values(obj: Any, key: str) -> set:
    """중첩 구조 어디에 있든 해당 키의 값을 전부 모은다."""
    found: set = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, (int, str)):
                found.add(v)
            else:
                found |= find_values(v, key)
    elif isinstance(obj, list):
        for item in obj:
            found |= find_values(item, key)
    return found


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
        "--cookie-clipboard", action="store_true", default=argparse.SUPPRESS,
        help="클립보드에서 쿠키를 읽는다. 화면·셸 기록에 남지 않아 가장 안전 (권장)",
    )
    common.add_argument(
        "--cookie-file", metavar="FILE", default=argparse.SUPPRESS,
        help="쿠키가 저장된 파일 경로",
    )
    common.add_argument(
        "--cookie", metavar="STR", default=argparse.SUPPRESS,
        help="쿠키 문자열 직접 지정. 셸 기록에 남으므로 권장하지 않는다",
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
        help="요청 간 최소 간격(초), 기본 2.0",
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
    d.add_argument("dong", nargs="*", help="법정동명(평촌동/호계동/…) 또는 코드. 기본: 평촌동 호계동")
    d.add_argument("--trade", nargs="+", default=["B1", "B2"], choices=list(TRADE_TYPES))
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("run", parents=[common], help="프리셋(평안동/범계동) 전체 수집")
    r.add_argument("--dong", nargs="*", choices=list(PRESETS), help="기본: 전체")
    r.add_argument("--trade", nargs="+", default=["B1", "B2"], choices=list(TRADE_TYPES))
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("complex", parents=[common],
                       help="단지 번호로 조회 (확인된 GET API, 지금 동작함)")
    c.add_argument("complex", nargs="+", help="단지 번호. 예: 3022")
    c.add_argument("--trade", nargs="+", default=["B1", "B2"], choices=list(TRADE_TYPES))
    c.add_argument("--pyeong", help="평형 번호를 직접 지정")
    c.add_argument("--max-pyeong", type=int, default=3, help="평형 몇 개까지 볼지, 기본 3")
    c.set_defaults(func=cmd_complex)

    i = sub.add_parser("inspect", parents=[common], help="HAR 안의 API 요청 확인 (인증 불필요)")
    i.set_defaults(func=None)

    f = sub.add_parser("fetch", parents=[common], help="complexNo 직접 지정해서 수집")
    f.add_argument("--complex", nargs="+", required=True, help="단지 번호(들)")
    f.add_argument("--trade", nargs="+", default=["B1"], choices=list(TRADE_TYPES))
    f.set_defaults(func=cmd_fetch)

    return p


COMMON_DEFAULTS = {"token": None, "cookie": None, "cookie_clipboard": False, "cookie_file": None, "from_curl": None, "from_har": None, "base": None, "delay": 2.0, "out": "out", "verbose": False}


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
