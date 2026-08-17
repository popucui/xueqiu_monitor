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

import database

CN_TZ = timezone(timedelta(hours=8), name="UTC+08:00")
HKEX_BASE = "https://www1.hkexnews.hk"
CNINFO_BASE = "https://www.cninfo.com.cn"
CNINFO_STATIC_BASE = "https://static.cninfo.com.cn"
MAX_PAGINATION_REQUESTS = 1000


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
    sentiment: str = ""


# 公告标题利好/利空关键词（A股中文 + 港股英文公告常见措辞）。
# 同时命中时按"负面优先"归类，宁可多提醒不漏风险。
POSITIVE_KEYWORDS = (
    "回购", "增持", "业绩预增", "扭亏", "中标", "重大合同", "签订合同",
    "分红", "派息", "利润分配", "股权激励", "员工持股", "业绩快报预增",
    "repurchase", "buyback", "dividend", "positive profit alert",
    "grant of shares",
)
NEGATIVE_KEYWORDS = (
    "减持", "立案", "调查", "处罚", "警示函", "监管函", "业绩预减", "预亏",
    "亏损", "退市", "质押", "冻结", "违规", "诉讼", "仲裁", "商誉减值",
    "终止上市", "停牌", "盈利警告",
    "profit warning", "negative profit alert", "investigation", "penalty",
    "litigation", "delisting",
)


# 正面豁免词：优先于负面判定。HKEX 公告标题尾部固定带分类标签
# （如 "[Profit Warning / Inside Information]"），正文却是利好披露
# （IMPROVEMENT / PROFIT ALERT）；"解除质押/冻结"也容易被"质押/冻结"误伤。
POSITIVE_EXEMPTIONS = (
    "estimated improvement", "profit alert", "positive profit alert",
    "业绩预增", "扭亏", "解除质押", "解除冻结",
)


def classify_sentiment(title: str) -> str:
    """按标题关键词判定利好/利空：'positive' / 'negative' / ''（中性）。"""
    lower_title = (title or "").lower()
    if any(keyword.lower() in lower_title for keyword in POSITIVE_EXEMPTIONS):
        return "positive"
    if any(keyword.lower() in lower_title for keyword in NEGATIVE_KEYWORDS):
        return "negative"
    if any(keyword.lower() in lower_title for keyword in POSITIVE_KEYWORDS):
        return "positive"
    return ""


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


def fetch_for_watchlist(
    watchlist: list[dict], days_back: int, page_size: int
) -> tuple[list[Announcement], list[str]]:
    """抓取 watchlist 全部公告，返回 ``(announcements, errors)``。

    单只股票失败只记入 errors 并继续，避免一只股票的网络异常
    导致整个 watchlist 的公告都不落库。
    """
    to_day = datetime.now(CN_TZ).date()
    from_day = to_day - timedelta(days=days_back)
    announcements = []
    errors = []
    for stock in watchlist:
        source = (stock.get("source") or "").lower()
        try:
            if source == "cninfo":
                announcements.extend(fetch_cninfo(stock, from_day, to_day, page_size))
            elif source == "hkex":
                announcements.extend(fetch_hkex(stock, from_day, to_day, page_size))
            else:
                raise RuntimeError(f"不支持的公告源: {source}")
        except Exception as exc:
            print(f"⚠️ 公告抓取失败 {stock.get('code')}: {exc}")
            errors.append(f"{stock.get('code')}: {exc}")
    return announcements, errors


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
            org_id = str(item["orgId"])
            # 回写数据库，之后的抓取不再重复请求解析接口
            database.update_announcement_stock_ids(stock["code"], org_id=org_id)
            return org_id
    raise RuntimeError(f"无法解析巨潮 orgId: {stock['code']}")


def fetch_cninfo(stock: dict, from_day, to_day, page_size: int) -> list[Announcement]:
    code, market = stock["code"].split(".", 1)
    column = {"SH": "sse", "SZ": "szse", "BJ": "bj"}.get(market.upper())
    if not column:
        raise RuntimeError(f"不支持的巨潮市场后缀: {stock['code']}")
    org_id = resolve_cninfo_org_id(stock)
    page_size = max(1, int(page_size))
    rows = []
    seen_ids = set()
    previous_signature = None

    for page_num in range(1, MAX_PAGINATION_REQUESTS + 1):
        response = http_json(
            f"{CNINFO_BASE}/new/hisAnnouncement/query",
            method="POST",
            data={
                "pageNum": str(page_num),
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
        items = response.get("announcements") or []
        signature = tuple(
            str(item.get("announcementId") or item.get("adjunctUrl") or "")
            for item in items
        )
        if page_num > 1 and items and signature == previous_signature:
            raise RuntimeError(f"巨潮分页未前进: {stock['code']} page={page_num}")
        previous_signature = signature
        for item in items:
            title = clean_text(item.get("announcementTitle") or item.get("shortTitle") or "")
            timestamp_ms = item.get("announcementTime")
            published = ""
            if timestamp_ms:
                published = datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=CN_TZ).strftime("%Y-%m-%d %H:%M")
            url = item.get("adjunctUrl") or ""
            if url and not url.startswith(("http://", "https://")):
                url = f"{CNINFO_STATIC_BASE}/{url.lstrip('/')}"
            ann_id = str(item.get("announcementId") or url or title)
            if ann_id in seen_ids:
                continue
            seen_ids.add(ann_id)
            rows.append(
                Announcement(
                    source="cninfo",
                    stock_code=stock["code"],
                    stock_name=stock["name"],
                    ann_id=ann_id,
                    title=title,
                    published_at=published,
                    url=url,
                    matched_keywords=", ".join(match_keywords(title, stock.get("keywords", []))),
                    sentiment=classify_sentiment(title),
                )
            )

        total = _as_int(response.get("totalAnnouncement"))
        total_pages = _as_int(response.get("totalpages") or response.get("totalPages"))
        if not items:
            return rows
        if total is not None:
            has_more = len(seen_ids) < total
        elif total_pages is not None:
            has_more = page_num < total_pages
        elif "hasMore" in response:
            has_more = _as_bool(response.get("hasMore"))
        else:
            has_more = len(items) >= page_size
        if not has_more:
            return rows

    raise RuntimeError(f"巨潮分页超过安全上限: {stock['code']}")


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
            stock_id = str(item["i"])
            # 回写数据库，避免每次抓取都下载全市场股票列表来解析
            database.update_announcement_stock_ids(stock["code"], stock_id=stock_id)
            return stock_id
    raise RuntimeError(f"无法解析 HKEX stockId: {stock['code']}")


def fetch_hkex(stock: dict, from_day, to_day, page_size: int) -> list[Announcement]:
    code = stock["code"].split(".", 1)[0].zfill(5)
    stock_id = resolve_hkex_stock_id(stock)
    page_size = max(1, int(page_size))
    row_range = page_size
    rows = []
    seen_ids = set()
    previous_loaded = -1

    for _ in range(MAX_PAGINATION_REQUESTS):
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
                "rowRange": str(row_range),
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
        raw_result = response.get("result") or "[]"
        items = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        for item in items:
            title = clean_text(item.get("TITLE") or item.get("LONG_TEXT") or "")
            detail = clean_text(item.get("LONG_TEXT") or "")
            full_title = title if not detail or detail == title else f"{title} - {detail}"
            file_link = item.get("FILE_LINK") or ""
            url = file_link if file_link.startswith(("http://", "https://")) else f"{HKEX_BASE}{file_link}"
            ann_id = str(item.get("NEWS_ID") or file_link or full_title)
            if ann_id in seen_ids:
                continue
            seen_ids.add(ann_id)
            rows.append(
                Announcement(
                    source="hkex",
                    stock_code=f"{code}.HK",
                    stock_name=stock["name"],
                    ann_id=ann_id,
                    title=full_title,
                    published_at=parse_hkex_date(item.get("DATE_TIME") or ""),
                    url=url,
                    matched_keywords=", ".join(match_keywords(full_title, stock.get("keywords", []))),
                    sentiment=classify_sentiment(full_title),
                )
            )

        loaded = _as_int(response.get("loadedRecord"), len(items))
        total = _as_int(response.get("recordCnt"))
        has_more = _as_bool(response.get("hasNextRow")) or (
            total is not None and loaded < total
        )
        if not has_more or (total is not None and loaded >= total):
            return rows
        if loaded <= previous_loaded:
            raise RuntimeError(f"HKEX 分页未前进: {stock['code']} loaded={loaded}")
        previous_loaded = loaded
        next_range = loaded + page_size
        if total is not None:
            next_range = min(next_range, total)
        if next_range <= row_range:
            raise RuntimeError(f"HKEX 分页范围未前进: {stock['code']} rowRange={row_range}")
        row_range = next_range

    raise RuntimeError(f"HKEX 分页超过安全上限: {stock['code']}")


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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
