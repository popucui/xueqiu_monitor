"""
公告追踪 — A 股巨潮资讯与港股 HKEXnews
"""
import html
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CN_TZ = timezone(timedelta(hours=8), name="UTC+08:00")
HKEX_BASE = "https://www1.hkexnews.hk"
CNINFO_BASE = "https://www.cninfo.com.cn"
CNINFO_STATIC_BASE = "https://static.cninfo.com.cn"


@dataclass(frozen=True)
class Announcement:
    source: str
    stock_code: str
    stock_name: str
    ann_id: str
    title: str
    published_at: str
    url: str
    matched_keywords: str


def http_json(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 3,
) -> Any:
    body = None
    req_headers = {
        "User-Agent": "Mozilla/5.0 xueqiu-monitor/announcement-tracker",
        "Accept": "application/json,text/plain,*/*",
    }
    if headers:
        req_headers.update(headers)
    if data is not None:
        body = urlencode(data).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")

    last_error = None
    for attempt in range(1, retries + 1):
        request = Request(url, data=body, headers=req_headers, method=method)
        try:
            with urlopen(request, timeout=25) as response:
                payload = response.read().decode("utf-8-sig")
                return json.loads(payload)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"公告源请求失败: {url}: {last_error}")


def fetch_for_watchlist(watchlist: list[dict], days_back: int, page_size: int) -> list[Announcement]:
    to_day = datetime.now(CN_TZ).date()
    from_day = to_day - timedelta(days=days_back)
    announcements = []
    for stock in watchlist:
        source = (stock.get("source") or "").lower()
        if source == "cninfo":
            announcements.extend(fetch_cninfo(stock, from_day, to_day, page_size))
        elif source == "hkex":
            announcements.extend(fetch_hkex(stock, from_day, to_day, page_size))
        else:
            raise RuntimeError(f"不支持的公告源: {stock.get('code')} {source}")
    return announcements


def resolve_cninfo_org_id(stock: dict) -> str:
    if stock.get("org_id"):
        return str(stock["org_id"])
    code = stock["code"].split(".", 1)[0]
    response = http_json(
        f"{CNINFO_BASE}/new/information/topSearch/detailOfQuery",
        method="POST",
        data={"keyWord": code, "maxSecNum": "10", "maxListNum": "5"},
        headers={
            "Referer": f"{CNINFO_BASE}/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    for item in response.get("keyBoardList", []):
        if item.get("code") == code:
            return item["orgId"]
    raise RuntimeError(f"无法解析巨潮 orgId: {stock['code']}")


def fetch_cninfo(stock: dict, from_day, to_day, page_size: int) -> list[Announcement]:
    code, market = stock["code"].split(".", 1)
    column = {"SH": "sse", "SZ": "szse", "BJ": "bj"}.get(market.upper())
    if not column:
        raise RuntimeError(f"不支持的巨潮市场后缀: {stock['code']}")
    org_id = resolve_cninfo_org_id(stock)
    response = http_json(
        f"{CNINFO_BASE}/new/hisAnnouncement/query",
        method="POST",
        data={
            "pageNum": "1",
            "pageSize": str(page_size),
            "column": column,
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{code},{org_id}",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{from_day.isoformat()}~{to_day.isoformat()}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        },
        headers={
            "Referer": f"{CNINFO_BASE}/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    rows = []
    for item in response.get("announcements") or []:
        title = clean_text(item.get("announcementTitle") or item.get("shortTitle") or "")
        timestamp_ms = item.get("announcementTime")
        published = ""
        if timestamp_ms:
            published = datetime.fromtimestamp(timestamp_ms / 1000, tz=CN_TZ).strftime("%Y-%m-%d %H:%M")
        url = item.get("adjunctUrl") or ""
        if url and not url.startswith(("http://", "https://")):
            url = f"{CNINFO_STATIC_BASE}/{url.lstrip('/')}"
        rows.append(
            Announcement(
                source="cninfo",
                stock_code=stock["code"],
                stock_name=stock["name"],
                ann_id=str(item.get("announcementId") or url or title),
                title=title,
                published_at=published,
                url=url,
                matched_keywords=", ".join(match_keywords(title, stock.get("keywords", []))),
            )
        )
    return rows


def resolve_hkex_stock_id(stock: dict) -> str:
    if stock.get("stock_id"):
        return str(stock["stock_id"])
    code = stock["code"].split(".", 1)[0].zfill(5)
    response = http_json(
        f"{HKEX_BASE}/ncms/script/eds/activestock_sehk_e.json",
        headers={
            "Referer": f"{HKEX_BASE}/search/titlesearch.xhtml?lang=en",
            "Accept": "application/json",
        },
    )
    for item in response:
        if item.get("c") == code:
            return str(item["i"])
    raise RuntimeError(f"无法解析 HKEX stockId: {stock['code']}")


def fetch_hkex(stock: dict, from_day, to_day, page_size: int) -> list[Announcement]:
    code = stock["code"].split(".", 1)[0].zfill(5)
    stock_id = resolve_hkex_stock_id(stock)
    query = urlencode(
        {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": from_day.strftime("%Y%m%d"),
            "toDate": to_day.strftime("%Y%m%d"),
            "title": "",
            "searchType": "0",
            "t1code": "-2",
            "t2Gcode": "-2",
            "t2code": "-2",
            "rowRange": str(page_size),
            "lang": "E",
        }
    )
    response = http_json(
        f"{HKEX_BASE}/search/titleSearchServlet.do?{query}",
        headers={
            "Referer": f"{HKEX_BASE}/search/titlesearch.xhtml?lang=en",
            "Accept": "application/json",
        },
    )
    rows = []
    for item in json.loads(response.get("result") or "[]"):
        title = clean_text(item.get("TITLE") or item.get("LONG_TEXT") or "")
        detail = clean_text(item.get("LONG_TEXT") or "")
        full_title = title if not detail or detail == title else f"{title} - {detail}"
        file_link = item.get("FILE_LINK") or ""
        url = file_link if file_link.startswith(("http://", "https://")) else f"{HKEX_BASE}{file_link}"
        rows.append(
            Announcement(
                source="hkex",
                stock_code=f"{code}.HK",
                stock_name=stock["name"],
                ann_id=str(item.get("NEWS_ID") or file_link or full_title),
                title=full_title,
                published_at=parse_hkex_date(item.get("DATE_TIME") or ""),
                url=url,
                matched_keywords=", ".join(match_keywords(full_title, stock.get("keywords", []))),
            )
        )
    return rows


def clean_text(value: str) -> str:
    return " ".join(html.unescape(value).replace("<br/>", " ").replace("<br>", " ").split())


def parse_hkex_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%d/%m/%Y %H:%M").replace(tzinfo=CN_TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def match_keywords(title: str, keywords: list[str]) -> tuple[str, ...]:
    lower_title = title.lower()
    return tuple(keyword for keyword in keywords if keyword.lower() in lower_title)
