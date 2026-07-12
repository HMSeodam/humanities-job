"""공고 제목으로 대학·기관의 공식 원문을 찾아 구조화 정보를 보강한다.

하이브레인넷 상세 화면이 로그인 또는 자동접근 제한으로 열리지 않을 때를 대비해
공식 검색 결과를 이용한다. 검색 결과는 공식 도메인만 허용하고, 제목 유사도가 낮은
페이지는 채택하지 않는다. 결과는 캐시해 같은 사이트를 매일 반복 요청하지 않는다.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
try:
    from pypdf import PdfReader
except ImportError:  # 로컬 미설치 환경에서는 HTML 수집만 계속한다.
    PdfReader = None

ROOT = Path(__file__).resolve().parents[1]
JOBS_PATH = ROOT / "data" / "jobs.json"
CACHE_PATH = ROOT / "data" / "enrichment-cache.json"
STATUS_PATH = ROOT / "data" / "collection-status.json"
INSTITUTIONS_PATH = ROOT / "data" / "institution-domains.json"
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"}
MAX_SEARCHES = int(os.getenv("MAX_EXTERNAL_SEARCHES", "25"))
CACHE_DAYS = int(os.getenv("DETAIL_CACHE_DAYS", "7"))
NAVER_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

BLOCKED_HOSTS = (
    "hibrain.net", "naver.com", "daum.net", "google.com", "bing.com",
    "namu.wiki", "wikipedia.org", "jobkorea.co.kr", "saramin.co.kr",
    "instagram.com", "facebook.com", "youtube.com",
)
OFFICIAL_SUFFIXES = (".ac.kr", ".go.kr", ".or.kr", ".re.kr", ".edu")
GENERIC_ROOTS = {"jobs.ac.kr", "job.ac.kr", "work.go.kr", "academyinfo.go.kr"}
STOPWORDS = {
    "대학교", "대학", "학교", "공고", "모집", "채용", "초빙", "공개", "학년도",
    "학기", "전임", "비전임", "교원", "강사", "교수", "연구원", "대한", "및",
}


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def tokens(value: str) -> set[str]:
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", clean(value).lower())
    return {word for word in words if word not in STOPWORDS and not re.fullmatch(r"20\d{2}", word)}


def similarity(left: str, right: str) -> float:
    wanted = tokens(left)
    if not wanted:
        return 0.0
    return len(wanted & tokens(right)) / len(wanted)


def strict_tokens(value: str) -> set[str]:
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", clean(value).lower())
    generic = {"대학교", "대학", "학교", "공고", "학년도", "학기", "대한", "및"}
    return {word for word in words if word not in generic and not re.fullmatch(r"20\d{2}", word)}


def strict_similarity(left: str, right: str) -> float:
    wanted = strict_tokens(left)
    return len(wanted & strict_tokens(right)) / len(wanted) if wanted else 0.0


def is_official_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return False
    if any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_HOSTS):
        return False
    return host.endswith(OFFICIAL_SUFFIXES)


def domain_root(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for suffix in OFFICIAL_SUFFIXES:
        bare = suffix.lstrip(".")
        if host.endswith("." + bare):
            labels = host.split(".")
            suffix_labels = bare.split(".")
            return ".".join(labels[-(len(suffix_labels) + 1):])
    return host


def raw_search_results(session: requests.Session, query: str) -> list[dict]:
    results: list[dict] = []
    if NAVER_ID and NAVER_SECRET:
        response = session.get(
            "https://openapi.naver.com/v1/search/webkr.json",
            params={"query": query, "display": 10, "sort": "sim"},
            headers={**HEADERS, "X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET},
            timeout=25,
        )
        response.raise_for_status()
        for item in response.json().get("items", []):
            results.append({
                "url": unescape(item.get("link", "")),
                "text": clean(BeautifulSoup(item.get("title", ""), "html.parser").get_text(" ")),
                "snippet": clean(BeautifulSoup(item.get("description", ""), "html.parser").get_text(" ")),
            })
    else:
        response = session.get(
            "https://search.naver.com/search.naver",
            params={"where": "web", "query": query},
            timeout=25,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select("a[href]"):
            url = anchor.get("href", "")
            text = clean(anchor.get_text(" "))
            if text and url.startswith("http"):
                results.append({"url": url, "text": text, "snippet": ""})

    return results


def discover_institution_root(session: requests.Session, school: str) -> str:
    normalized = re.sub(r"(국립|대학교|대학|학교법인|재단법인|법인|\s)", "", school)
    results = raw_search_results(session, f'"{school}" 공식 홈페이지')
    scored = []
    for item in results:
        url = item.get("url", "")
        if not is_official_url(url):
            continue
        root = domain_root(url)
        if root in GENERIC_ROOTS:
            continue
        label = clean(item.get("text", "") + " " + item.get("snippet", ""))
        normalized_label = re.sub(r"(국립|대학교|대학|학교법인|재단법인|법인|\s)", "", label)
        score = similarity(school, label)
        if normalized and normalized in normalized_label:
            score += 0.9
        if urlparse(url).path in ("", "/"):
            score += 0.15
        scored.append((score, root))
    return sorted(scored, reverse=True)[0][1] if scored and sorted(scored, reverse=True)[0][0] >= 0.6 else ""


def search_official(session: requests.Session, job: dict, institution_root: str) -> list[dict]:
    if not institution_root:
        return []
    query = f'"{job["title"]}" site:{institution_root}'
    results = raw_search_results(session, query)
    deduped: dict[str, dict] = {}
    for item in results:
        url = item["url"].split("#")[0]
        if not is_official_url(url) or domain_root(url) != institution_root:
            continue
        score = similarity(job["title"], item["text"] + " " + item["snippet"])
        if job["school"].replace(" ", "")[:4] in (item["text"] + item["snippet"]).replace(" ", ""):
            score += 0.25
        if any(word in url.lower() for word in ("recruit", "notice", "board", "bbs", "job", "hire")):
            score += 0.08
        candidate = {**item, "url": url, "score": score}
        if url not in deduped or deduped[url]["score"] < score:
            deduped[url] = candidate
    return sorted(deduped.values(), key=lambda item: item["score"], reverse=True)[:6]


def extract_pdf(data: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:40])
    except Exception:
        return ""


class PageFetcher:
    def __init__(self, session: requests.Session):
        self.session = session
        self.playwright = None
        self.browser = None
        self.page = None

    def start_browser(self) -> None:
        if self.page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True)
            context = self.browser.new_context(locale="ko-KR", user_agent=USER_AGENT)
            self.page = context.new_page()
            self.page.set_default_timeout(18000)
        except Exception:
            self.close()

    def fetch(self, url: str) -> tuple[str, str, str]:
        """(최종 URL, HTML 또는 텍스트, 형식)을 반환한다."""
        try:
            response = self.session.get(url, timeout=25, allow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "pdf" in content_type or response.url.lower().endswith(".pdf"):
                text = extract_pdf(response.content)
                return response.url, text, "pdf"
            if len(response.text) >= 500:
                return response.url, response.text, "html"
        except requests.RequestException:
            pass
        self.start_browser()
        if self.page is None:
            return url, "", "error"
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
            self.page.wait_for_timeout(800)
            return self.page.url, self.page.content(), "html-browser"
        except Exception:
            return url, "", "error"

    def close(self) -> None:
        for item in (self.browser, self.playwright):
            try:
                if item:
                    item.close() if item is self.browser else item.stop()
            except Exception:
                pass
        self.page = self.browser = self.playwright = None


def best_detail_link(html: str, base_url: str, title: str) -> tuple[str, float]:
    soup = BeautifulSoup(html, "html.parser")
    best_url, best_score = "", 0.0
    base_host = urlparse(base_url).hostname
    for anchor in soup.select("a[href]"):
        label = clean(anchor.get_text(" "))
        if len(label) < 4:
            continue
        url = urljoin(base_url, anchor.get("href", "")).split("#")[0]
        if urlparse(url).hostname != base_host:
            continue
        score = strict_similarity(title, label)
        if score > best_score:
            best_url, best_score = url, score
    return best_url, best_score


def page_text(content: str, kind: str) -> str:
    if kind == "pdf":
        return clean(content)
    soup = BeautifulSoup(content, "html.parser")
    for node in soup.select("script,style,noscript,nav,footer,header"):
        node.decompose()
    return clean(soup.get_text("\n"))


def heading_score(content: str, kind: str, title: str) -> float:
    if kind == "pdf":
        return strict_similarity(title, content[:4000])
    soup = BeautifulSoup(content, "html.parser")
    selectors = "title,h1,h2,h3,.title,.viewTit,.view-title,.board-title,.bbs-title"
    candidates = [clean(node.get_text(" ")) for node in soup.select(selectors)]
    return max((strict_similarity(title, candidate) for candidate in candidates if candidate), default=0.0)


def find_official_page(fetcher: PageFetcher, job: dict, candidates: list[dict]) -> tuple[str, str, str]:
    specific_title = job["title"].replace(job["school"], "", 1).strip() or job["title"]
    for candidate in candidates:
        final_url, content, kind = fetcher.fetch(candidate["url"])
        if not content or not is_official_url(final_url):
            continue
        if kind.startswith("html"):
            detail_url, link_score = best_detail_link(content, final_url, job["title"])
            if detail_url and link_score >= 0.45 and detail_url != final_url:
                detail_final, detail_content, detail_kind = fetcher.fetch(detail_url)
                if detail_content and heading_score(detail_content, detail_kind, specific_title) >= 0.65:
                    return detail_final, detail_content, detail_kind
        text = page_text(content, kind)
        direct_score = strict_similarity(specific_title, text[:25000])
        title_score = heading_score(content, kind, specific_title)
        if title_score >= 0.65 and direct_score >= 0.55:
            return final_url, content, kind
    return "", "", "error"


def labeled_value(soup: BeautifulSoup, labels: tuple[str, ...]) -> str:
    for cell in soup.find_all(["th", "dt", "td", "strong", "b"]):
        label = clean(cell.get_text(" "))
        if any(key in label for key in labels):
            sibling = cell.find_next_sibling(["td", "dd", "span", "div", "p"])
            if sibling:
                value = clean(sibling.get_text(" "))
                if 1 < len(value) <= 300:
                    return value
    return ""


def line_after(text: str, labels: tuple[str, ...], limit: int = 220) -> str:
    for label in labels:
        match = re.search(rf"{label}\s*[:：]?\s*([^\n]{{2,{limit}}})", text, re.I)
        if match:
            return clean(match.group(1))[:limit]
    return ""


def section_items(text: str, headings: tuple[str, ...]) -> list[str]:
    lines = [clean(line) for line in re.split(r"[\r\n]+", text) if clean(line)]
    for index, line in enumerate(lines):
        if any(heading in line for heading in headings):
            items = []
            for value in lines[index + 1:index + 12]:
                if re.match(r"^\d+[.)]\s|^[가-하][.)]\s", value) and items:
                    break
                if 3 <= len(value) <= 220:
                    items.append(re.sub(r"^[·•※\-*○①-⑳\s]+", "", value))
                if len(items) == 6:
                    break
            return [item for item in items if item]
    return []


def application_url(soup: BeautifulSoup, base_url: str) -> str:
    preferred = []
    for anchor in soup.select("a[href]"):
        label = clean(anchor.get_text(" "))
        url = urljoin(base_url, anchor.get("href", ""))
        if not url.startswith("http"):
            continue
        score = sum(word in label for word in ("지원", "접수", "원서", "채용시스템", "바로가기"))
        score += sum(word in url.lower() for word in ("apply", "recruit", "hire", "insa"))
        if score:
            preferred.append((score, url))
    return sorted(preferred, reverse=True)[0][1] if preferred else base_url


def extract_details(job: dict, official_url: str, content: str, kind: str) -> dict:
    raw_text = content if kind == "pdf" else BeautifulSoup(content, "html.parser").get_text("\n")
    text = clean(raw_text)
    soup = BeautifulSoup(content, "html.parser") if kind != "pdf" else BeautifulSoup("", "html.parser")

    department = ""
    title_without_school = job["title"].replace(job["school"], "", 1)
    match = re.search(r"([가-힣A-Za-z· ]{2,35}?(?:학과|학부|대학원|연구소|학술원|센터|박물관|도서관|연구원|본부|단|팀|실))", title_without_school)
    if match:
        department = clean(match.group(1))
    if not department:
        department = labeled_value(soup, ("학과", "소속", "채용부서", "모집단위", "근무부서"))
    if not department:
        department = "상세 공고 확인"

    openings = labeled_value(soup, ("모집인원", "채용인원", "선발인원"))
    if openings and not re.search(r"\d|[ＯO○]|명|미정|약간", openings):
        openings = ""
    if not openings:
        match = re.search(r"(?:모집|채용|선발)\s*인원\s*[:：]?\s*(\d+|[ＯO○]|약\s*\d+)\s*명?", text, re.I)
        openings = (clean(match.group(1)) + ("명" if match and match.group(1).isdigit() else "")) if match else "미기재"

    method = labeled_value(soup, ("지원방법", "접수방법", "제출방법", "원서접수"))
    method = method or line_after(raw_text, ("지원방법", "접수방법", "제출방법", "원서접수")) or "공식 공고에서 확인"

    qualifications = section_items(raw_text, ("지원자격", "응시자격", "자격요건", "지원 자격"))
    documents = section_items(raw_text, ("제출서류", "구비서류", "제출 서류"))
    apply_url = application_url(soup, official_url) if kind != "pdf" else official_url
    return {
        "department": department[:120],
        "openings": openings[:80],
        "method": method[:220],
        "applyUrl": apply_url,
        "officialUrl": official_url,
        "qualifications": qualifications,
        "documents": documents,
        "verified": False,
        "collectionLevel": "외부 원문 자동 수집",
        "externalStatus": "found",
        "detailCheckedAt": NOW.isoformat(),
    }


def cache_fresh(record: dict) -> bool:
    try:
        checked = datetime.fromisoformat(record.get("detailCheckedAt", ""))
        return checked >= NOW - timedelta(days=CACHE_DAYS)
    except (ValueError, TypeError):
        return False


def main() -> int:
    payload = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    jobs = payload.get("jobs", [])
    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    institutions = json.loads(INSTITUTIONS_PATH.read_text(encoding="utf-8")) if INSTITUTIONS_PATH.exists() else {}
    session = requests.Session(); session.headers.update(HEADERS)
    fetcher = PageFetcher(session)
    checked = enriched = not_found = errors = 0
    new_cache: dict[str, dict] = dict(cache)

    try:
        for job in jobs:
            record = cache.get(str(job["id"]), {})
            if record and cache_fresh(record):
                job.update({key: value for key, value in record.items() if key not in ("id", "title")})
                if record.get("externalStatus") == "found":
                    enriched += 1
                continue
            if checked >= MAX_SEARCHES:
                continue
            checked += 1
            try:
                institution_root = institutions.get(job["school"], "")
                if not institution_root:
                    institution_root = discover_institution_root(session, job["school"])
                    if institution_root:
                        institutions[job["school"]] = institution_root
                candidates = search_official(session, job, institution_root)
                url, content, kind = find_official_page(fetcher, job, candidates)
                if url:
                    details = extract_details(job, url, content, kind)
                    job.update(details)
                    new_cache[str(job["id"])] = {"id": job["id"], "title": job["title"], **details}
                    enriched += 1
                else:
                    record = {
                        "id": job["id"], "title": job["title"], "externalStatus": "not-found",
                        "detailCheckedAt": NOW.isoformat(), "collectionLevel": "목록 수집 · 공식 원문 탐색 실패",
                    }
                    job.update({key: value for key, value in record.items() if key not in ("id", "title")})
                    new_cache[str(job["id"])] = record
                    not_found += 1
                time.sleep(0.7)
            except Exception as exc:
                errors += 1
                print(f"[외부 원문 오류] {job['title']}: {exc}")
    finally:
        fetcher.close()

    payload["jobs"] = jobs
    payload["detailsUpdatedAt"] = NOW.isoformat()
    payload["detailsStatus"] = {"listed": len(jobs), "checkedThisRun": checked, "enriched": enriched, "notFound": not_found, "errors": errors}
    JOBS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CACHE_PATH.write_text(json.dumps(new_cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    INSTITUTIONS_PATH.write_text(json.dumps(institutions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATUS_PATH.write_text(json.dumps({"updatedAt": NOW.isoformat(), **payload["detailsStatus"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"외부 원문 확인 {checked}건, 상세 보강 누적 {enriched}건, 미발견 {not_found}건, 오류 {errors}건")
    return 0 if errors < max(3, checked // 2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
