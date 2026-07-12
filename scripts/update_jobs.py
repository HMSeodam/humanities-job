"""하이브레인넷 공개 공고에서 인문학 채용정보를 선별해 data/jobs.json을 갱신한다.

사이트 구조나 이용 정책이 바뀌면 선택자와 수집 범위를 조정해야 한다. 수집 실패 시에는
기존 데이터를 보존한다. 원문 전체가 아닌 채용 사실정보와 링크만 저장한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "jobs.json"
KST = timezone(timedelta(hours=9))

LIST_URL = "https://www.hibrain.net/recruitment/recruits"
# 역사학·철학은 하이브레인넷의 세부전공 코드를 사용한다. 미술사학과 문헌정보학은
# 독립 전공 코드가 없어 공개 검색어 결과를 합친 뒤 중복을 제거한다.
SEARCHES = [
    ("역사학", {"mjrRelCds": "H02"}),
    ("철학", {"mjrRelCds": "H03"}),
    ("미술사학", {"keyword": "미술사"}),
    ("미술사학", {"keyword": "미술이론"}),
    ("미술사학", {"keyword": "예술사"}),
    ("미술사학", {"keyword": "박물관"}),
    ("미술사학", {"keyword": "학예"}),
    ("문헌정보학", {"keyword": "문헌정보"}),
    ("문헌정보학", {"keyword": "기록관리"}),
    ("문헌정보학", {"keyword": "기록학"}),
    ("문헌정보학", {"keyword": "도서관"}),
    ("문헌정보학", {"keyword": "아카이브"}),
]
FIELDS = {
    "역사학": ["역사학", "사학과", "한국사", "동양사", "서양사", "고고학", "역사교육", "문화사"],
    "철학": ["철학", "윤리학", "윤리교육", "미학", "논리학", "과학철학"],
    "미술사학": ["미술사", "미술이론", "예술사", "박물관학", "큐레이터"],
    "문헌정보학": ["문헌정보", "도서관학", "기록관리", "기록학", "정보조직", "디지털 아카이브"],
}
ROLE_WORDS = {
    "박사후연구원": ["박사후", "post-doc", "postdoc"],
    "강사": ["강사", "시간강사"],
    "연구원": ["연구원", "연구교수", "연구직"],
    "교수": ["교수", "전임교원", "비전임교원", "초빙교원", "겸임교원"],
}
HEADERS = {
    # 하이브레인넷 목록 CDN은 일반 봇 식별 문자열을 403으로 차단한다. 공개 목록을
    # 브라우저와 동일한 HTML 표현으로 하루 한 번만 요청한다.
    "User-Agent": os.getenv("SCRAPER_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": "https://www.hibrain.net/recruitment",
}
MAX_LIST_RETRIES = int(os.getenv("MAX_LIST_RETRIES", "3"))


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def classify(text: str) -> str | None:
    lowered = text.lower()
    scores = {field: sum(1 for word in words if word.lower() in lowered) for field, words in FIELDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def role_for(text: str) -> str:
    lowered = text.lower()
    for role, words in ROLE_WORDS.items():
        if any(word.lower() in lowered for word in words):
            return role
    return "연구원"


def parse_deadline(text: str) -> str | None:
    now = datetime.now(KST)
    if "오늘마감" in text:
        return now.replace(hour=23, minute=59, second=0, microsecond=0).isoformat()
    if "내일마감" in text:
        return (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0).isoformat()
    patterns = [
        r"(?:20)?(\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})일?\s*(\d{1,2})?(?::(\d{2}))?",
        r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\s*(\d{1,2})?(?::(\d{2}))?",
    ]
    matches = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            groups = match.groups()
            year = int(groups[0]); year = year + 2000 if year < 100 else year
            month, day = int(groups[1]), int(groups[2])
            hour, minute = int(groups[3] or 23), int(groups[4] or 59)
            try:
                matches.append(datetime(year, month, day, hour, minute, tzinfo=KST))
            except ValueError:
                pass
    future = [d for d in matches if d >= now - timedelta(days=1)]
    return max(future or matches).isoformat() if matches else None


def table_value(soup: BeautifulSoup, labels: list[str]) -> str:
    for cell in soup.find_all(["th", "dt", "td"]):
        if any(label in clean(cell.get_text()) for label in labels):
            sibling = cell.find_next_sibling(["td", "dd"])
            if sibling:
                return clean(sibling.get_text(" "))
    return ""


def extract_external_url(soup: BeautifulSoup, page_url: str) -> str:
    own_host = "hibrain.net"
    preferred = []
    for a in soup.select("a[href]"):
        href = urljoin(page_url, a.get("href"))
        label = clean(a.get_text(" "))
        if href.startswith("http") and own_host not in href:
            preferred.append((2 if any(k in label for k in ["지원", "접수", "바로가기", "홈페이지"]) else 1, href))
    return sorted(preferred, reverse=True)[0][1] if preferred else page_url


def parse_detail(session: requests.Session, url: str) -> dict | None:
    response = session.get(url, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    text = clean(soup.get_text(" "))
    title_node = soup.select_one("h1, h2, .title, .view-title")
    title = clean(title_node.get_text(" ") if title_node else soup.title.get_text(" ") if soup.title else "")
    field = classify(title + " " + text[:12000])
    if not field:
        return None
    deadline = parse_deadline(table_value(soup, ["접수기간", "지원기간", "마감일"]) or text)
    if not deadline:
        return None
    school = table_value(soup, ["기관명", "기관", "대학명"])
    if not school:
        school_match = re.search(r"([가-힣A-Za-z· ]+(?:대학교|대학|연구원|연구소|도서관|박물관))", title)
        school = clean(school_match.group(1)) if school_match else "기관명 확인 필요"
    department = table_value(soup, ["학과", "소속", "부서", "채용부서"]) or "학과·부서 확인 필요"
    openings_match = re.search(r"(?:채용|모집)?\s*인원\s*[:：]?\s*(\d+|O|○)\s*명?", text, re.I)
    openings = (openings_match.group(1) + "명") if openings_match else "미기재"
    method = table_value(soup, ["지원방법", "접수방법", "제출방법"]) or "공식 공고에서 확인"
    location = table_value(soup, ["근무예정지", "근무지", "지역"]) or "미기재"
    job_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:14]
    return {
        "id": job_id,
        "school": school,
        "college": "",
        "department": department,
        "title": title[:180],
        "field": field,
        "role": role_for(title + " " + text[:3000]),
        "openings": openings,
        "deadline": deadline,
        "posted": datetime.now(KST).date().isoformat(),
        "location": location,
        "method": method[:180],
        "applyUrl": extract_external_url(soup, url),
        "sourceUrl": url,
        "verified": False,
        "qualifications": [],
        "documents": [],
    }


def school_from_title(title: str) -> str:
    match = re.search(r"^(.+?(?:대학교|대학|연구원|연구소|도서관|박물관|문화원|재단|청|부|원))(?=\s)", title)
    return clean(match.group(1)) if match else clean(title.split(" ")[0])


def fetch_listing(session: requests.Session, params: dict) -> requests.Response:
    """목록 요청을 짧게 재시도한다.

    하이브레인넷 CDN이 GitHub Actions 주소를 간헐적으로 403 처리하는 경우가 있어
    첫 요청 전에 사이트 쿠키를 준비하고, 403/429/5xx만 제한적으로 재시도한다.
    """
    last_response: requests.Response | None = None
    last_error: requests.RequestException | None = None
    try:
        session.get("https://www.hibrain.net/", timeout=15)
    except requests.RequestException:
        pass
    for attempt in range(1, MAX_LIST_RETRIES + 1):
        try:
            response = session.get(LIST_URL, params=params, timeout=30)
            last_response = response
            if response.status_code not in {403, 429} and response.status_code < 500:
                response.raise_for_status()
                return response
        except requests.RequestException as exc:
            last_error = exc
        if attempt < MAX_LIST_RETRIES:
            time.sleep(attempt * 3)
    if last_response is not None:
        last_response.raise_for_status()
    if last_error is not None:
        raise last_error
    raise RuntimeError("목록 요청에 응답이 없습니다.")


def listing_items(session: requests.Session) -> list[dict]:
    found: dict[str, dict] = {}
    errors = []
    for field, filters in SEARCHES:
        params = {"listType": "ING", "limit": "100", **filters}
        try:
            response = fetch_listing(session, params)
        except requests.RequestException as exc:
            errors.append(f"{field}/{filters}: {exc}")
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for row in soup.select("li.row.sortRoot"):
            anchor = row.select_one('a[href*="/recruitment/recruits/"]')
            if not anchor:
                continue
            url = urljoin(LIST_URL, anchor.get("href", "").split("#")[0])
            match = re.search(r"/recruitment/recruits/(\d+)", url)
            if not match:
                continue
            job_id = match.group(1)
            title = clean(anchor.get("title") or anchor.get_text(" "))
            receipt_node = row.select_one(".td_receipt")
            receipt = clean(receipt_node.get_text(" ") if receipt_node else "")
            deadline = parse_deadline(receipt)
            if not deadline:
                # '관련 URL 참조'처럼 목록에 마감일이 없는 공고는 사이트에서 종료 여부를
                # 판단할 수 없으므로 30일 뒤를 임시 확인일로 두고 명확히 표시한다.
                deadline = (datetime.now(KST) + timedelta(days=30)).replace(hour=23, minute=59).isoformat()
                deadline_note = "마감일은 원문 확인 필요"
            else:
                deadline_note = ""
            posted_text = clean(row.select_one(".td_rdtm").get_text(" ") if row.select_one(".td_rdtm") else "")
            posted_parsed = parse_deadline(posted_text)
            posted = posted_parsed[:10] if posted_parsed else datetime.now(KST).date().isoformat()
            item = {
                "id": job_id,
                "school": school_from_title(title),
                "college": "",
                "department": "상세 공고 확인",
                "title": title,
                "field": field,
                "role": role_for(title),
                "openings": "미기재",
                "deadline": deadline,
                "deadlineNote": deadline_note,
                "posted": posted,
                "location": "미기재",
                "method": "하이브레인넷 원문에서 확인",
                "applyUrl": url,
                "sourceUrl": url,
                "verified": False,
                "collectionLevel": "목록 수집",
                "qualifications": [],
                "documents": [],
            }
            if job_id not in found:
                found[job_id] = item
    if not found:
        raise RuntimeError("검색 목록을 가져오지 못했습니다. " + " | ".join(errors))
    return list(found.values())


def main() -> int:
    session = requests.Session(); session.headers.update(HEADERS)
    try:
        jobs = listing_items(session)
    except RuntimeError as exc:
        # 원본 사이트의 일시 차단 때문에 기존 정상 데이터와 Pages 배포까지 잃지 않는다.
        # 다음 예약 실행에서 다시 신규 목록 수집을 시도하고, 이번 실행은 기존 목록을
        # 외부 대학 공식 공고 보강 단계로 넘긴다.
        try:
            payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"[실패] 기존 데이터도 없어 보존할 수 없습니다. {exc}", file=sys.stderr)
            return 1
        if not payload.get("jobs"):
            print(f"[실패] 보존할 기존 공고가 없습니다. {exc}", file=sys.stderr)
            return 1
        payload["sourceStatus"] = "stale-preserved"
        payload["lastCollectionAttemptAt"] = datetime.now(KST).isoformat()
        payload["notice"] = (
            "하이브레인넷이 자동 요청을 일시 차단하여 직전 정상 목록을 유지했습니다. "
            "대학·기관 공식 원문 보강과 사이트 배포는 계속되며 다음 예약 실행에서 다시 시도합니다."
        )
        OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"::warning::하이브레인넷 목록 수집 차단으로 기존 {len(payload['jobs'])}건을 보존합니다. {exc}")
        return 0
    # 상세 페이지는 CloudFront 정책에 따라 자동 요청이 차단될 수 있다. 명시적으로
    # 활성화한 배포 환경에서만 시도하고, 실패하면 목록 정보는 그대로 유지한다.
    if os.getenv("ENABLE_DETAIL_FETCH", "false").lower() == "true":
        enriched = []
        for base in jobs:
            try:
                detail = parse_detail(session, base["sourceUrl"])
                enriched.append(detail or base)
            except requests.RequestException:
                enriched.append(base)
        jobs = enriched
    jobs.sort(key=lambda job: job["deadline"])
    payload = {
        "updatedAt": datetime.now(KST).isoformat(),
        "sourceStatus": "live-list",
        "source": LIST_URL,
        "notice": "하이브레인넷 검색 목록 자동 수집. 인원·학과·지원방법은 원문 확인 필요.",
        "jobs": jobs,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(jobs)}개 공고를 저장했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
