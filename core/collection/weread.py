"""Isolated WeRead MP adapter. No account rotation or silent source fallback."""
import base64
import io
import re
import time
from urllib.parse import quote

import requests

from .adapters import SourceError, WechatAdapter, parse_body
from .security import Vault

BASE = "https://weread.qq.com"
BOOK_ID = re.compile(r"MP_WXS_\d{1,30}\Z")


def checked_json(response, path):
    try:
        data = response.json()
    except ValueError:
        raise SourceError("SOURCE_CHANGED", f"微信读书 {path} 未返回有效 JSON") from None
    if not isinstance(data, dict):
        raise SourceError("SOURCE_CHANGED", f"微信读书 {path} 返回格式变化")
    code = data.get("errCode", data.get("errcode", 0))
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        raise SourceError("SOURCE_CHANGED", "微信读书返回无法识别的错误码") from None
    if code:
        # Do not retain raw responses, URLs with parameters, or upstream error strings.
        detail = f"微信读书 {path} 返回错误码 {code}"
        if code in (-2012, -2010):
            raise SourceError("AUTH_EXPIRED", detail + "；登录状态或访问权限需检查，请先在官方页面确认")
        if code == -2041:
            raise SourceError("SOURCE_CHANGED", detail + "；当前接口不可访问，需人工检查")
        raise SourceError("SOURCE_CHANGED", detail)
    return data


def article_url(original_id, review_id, book_id):
    token = original_id or (review_id[len(book_id) + 1:] if review_id.startswith(book_id + "_") else "")
    if not token or not re.fullmatch(r"[A-Za-z0-9_~\-]+", token):
        raise SourceError("SOURCE_CHANGED", "微信读书文章缺少有效原文标识")
    return "https://mp.weixin.qq.com/s/" + quote(token.replace("~", "_"), safe="")


class WereadAdapter:
    def __init__(self, settings):
        self.settings = settings
        self.vault = Vault(settings)
        self.auth = self.vault.load("weread")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0", "Origin": BASE, "Referer": BASE + "/",
            "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.set_cookies(self.auth.get("cookies", {}))

    def set_cookies(self, cookies):
        self.session.cookies.clear()
        for name, value in cookies.items():
            if name.startswith("wr_"):
                self.session.cookies.set(name, value, domain="weread.qq.com", path="/")

    def cookies(self):
        return {c.name: c.value for c in self.session.cookies
                if c.name.startswith("wr_") and c.domain.lstrip(".") == "weread.qq.com"}

    def request(self, path, params=None, auth=True):
        if auth and not self.cookies().get("wr_skey"):
            raise SourceError("AUTH_EXPIRED", "请先扫码授权微信读书")
        if path not in {"/api/auth/getLoginUid", "/api/auth/getLoginInfo", "/web/shelf/sync", "/web/mp/articles", "/web/mp/content", "/api/mp/cover"}:
            raise SourceError("UNSAFE_URL", "不支持的微信读书接口")
        try:
            with self.session.get(BASE + path, params=params, timeout=(10, 25), allow_redirects=False, stream=True) as response:
                if response.status_code == 429:
                    delay = response.headers.get("Retry-After", "0")
                    raise SourceError("RATE_LIMITED", f"微信读书 {path} 返回 HTTP 429，已暂停", int(delay) if delay.isdigit() else 0)
                if response.status_code == 401:
                    raise SourceError("AUTH_EXPIRED", f"微信读书 {path} 返回 HTTP 401，请重新授权")
                if response.status_code == 403 or 300 <= response.status_code < 400:
                    raise SourceError("CHALLENGE_REQUIRED", f"微信读书 {path} 要求验证或跳转，请检查官方页面")
                if response.status_code >= 500:
                    raise SourceError("NETWORK_ERROR", f"微信读书 {path} 暂时不可用")
                if response.status_code != 200:
                    raise SourceError("SOURCE_CHANGED", f"微信读书 {path} 返回 HTTP {response.status_code}")
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 10 * 1024 * 1024:
                        raise SourceError("CONTENT_TOO_LARGE", "微信读书响应超过 10 MB")
                response._content = bytes(content)
                response._content_consumed = True
                return response
        except requests.Timeout:
            if path == "/api/auth/getLoginInfo":
                return None  # Long poll timeout is a pending QR, not failed authentication.
            raise SourceError("NETWORK_ERROR", "微信读书请求超时") from None
        except requests.RequestException:
            raise SourceError("NETWORK_ERROR", "微信读书连接失败") from None

    def shelf(self):
        path = "/web/shelf/sync"
        data = checked_json(self.request(path, {"userVid": "", "synckey": 0, "lectureSynckey": 0}), path)
        if not isinstance(data.get("books"), list):
            raise SourceError("SOURCE_CHANGED", "微信读书书架格式变化，无法确认授权有效")
        return data["books"]

    def search(self, keyword):
        books = self.shelf()
        return {"accounts": [{"external_id": str(b["bookId"]), "name": b.get("title") or str(b["bookId"]), "alias": "微信读书书架", "source_id": "weread"}
                             for b in books if isinstance(b, dict) and BOOK_ID.fullmatch(str(b.get("bookId", "")))
                             and keyword.casefold() in str(b.get("title", "")).casefold()],
                "scope": "shelf"}

    def discover(self, feed, offset=0):
        book_id = feed["external_id"]
        if not BOOK_ID.fullmatch(book_id):
            raise SourceError("SOURCE_CHANGED", "微信读书公众号标识应为 MP_WXS_数字")
        if feed.get("collection_mode") == "latest":
            return self.latest(feed)
        path = "/web/mp/articles"
        data = checked_json(self.request(path, {"bookId": book_id, "offset": offset}), path)
        groups = data.get("reviews")
        if not isinstance(groups, list):
            raise SourceError("SOURCE_CHANGED", "微信读书文章列表结构变化")
        items = []
        try:
            for group in groups:
                for sub in group["subReviews"]:
                    review = sub["review"]
                    info = review["mpInfo"]
                    review_id = str(review.get("reviewId") or sub["reviewId"])
                    if not review_id.startswith(book_id + "_") or not info.get("title"):
                        raise ValueError()
                    items.append({"source_article_id": review_id, "title": info["title"],
                                  "url": article_url(info.get("originalId"), review_id, book_id),
                                  "published_at": int(review.get("createTime") or group.get("createTime") or info.get("time") or 0),
                                  "author": feed.get("name")})
        except (KeyError, TypeError, ValueError):
            raise SourceError("SOURCE_CHANGED", "微信读书文章字段变化，未推进采集位置") from None
        # Offsets count groups, not the number of articles within each group.
        return {"items": items, "next_cursor": offset + len(groups), "exhausted": not groups or data.get("hasMore") in (False, 0),
                "coverage": "weread_list", "response_valid": True}

    def latest(self, feed):
        path = "/api/mp/cover"
        data = checked_json(self.request(path, {"bookId": feed["external_id"]}), path)
        review_id = data.get("reviewId")
        if not isinstance(review_id, str) or not review_id.startswith(feed["external_id"] + "_") or not data.get("title"):
            raise SourceError("SOURCE_CHANGED", "微信读书最新文章信息不完整，无法确认该公众号可读取")
        item = {"source_article_id": review_id, "title": data["title"],
                "url": article_url(data.get("originalId"), review_id, feed["external_id"]),
                # Cover does not reliably include a publication date. Never invent one.
                "published_at": None, "author": data.get("name") or feed.get("name")}
        return {"items": [item], "next_cursor": 0, "exhausted": True,
                "coverage": "latest_only", "response_valid": True}

    def fetch(self, article):
        review_id = article.get("source_article_id", "")
        if not re.fullmatch(r"MP_WXS_\d+_[A-Za-z0-9_~\-]+", review_id):
            raise SourceError("UNSUPPORTED_JOB", "微信读书正文需来自该渠道的文章列表；单篇微信链接请导入公众平台订阅")
        path = "/web/mp/content"
        response = self.request(path, {"reviewId": review_id})
        if "json" in response.headers.get("content-type", "") or response.content.lstrip().startswith(b"{"):
            checked_json(response, path)
            raise SourceError("SOURCE_CHANGED", "微信读书正文未返回 HTML")
        return parse_body(response.content.decode("utf-8", errors="replace"))

    def image(self, url):
        # Separate client: neither WeRead nor public-platform credentials go to the CDN.
        client = WechatAdapter(self.settings)
        client.session.cookies.clear()
        return client.image(url)

    def qr_start(self):
        import qrcode
        from qrcode.image.svg import SvgPathImage
        self.set_cookies({})
        path = "/api/auth/getLoginUid"
        data = checked_json(self.request(path, auth=False), path)
        uid = data.get("uid") or (data.get("data") or {}).get("uid")
        if not isinstance(uid, (str, int)) or not str(uid):
            raise SourceError("SOURCE_CHANGED", "微信读书未返回登录二维码标识")
        self.vault.save("weread_pending", {"uid": str(uid), "expires": time.time() + 240, "cookies": self.cookies()})
        output = io.BytesIO()
        qrcode.make(BASE + "/web/confirm?uid=" + quote(str(uid), safe=""), image_factory=SvgPathImage).save(output)
        return {"image": "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode(), "expires_in": 240}

    def qr_check(self):
        pending = self.vault.load("weread_pending")
        if not pending or pending.get("expires", 0) <= time.time():
            return {"status": "expired"}
        self.set_cookies(pending.get("cookies", {}))
        path = "/api/auth/getLoginInfo"
        response = self.request(path, {"uid": pending["uid"], "otp": ""}, auth=False)
        if response is None:
            return {"status": "waiting"}
        data = checked_json(response, path)
        inner = data.get("data") or {}
        if not isinstance(inner, dict):
            raise SourceError("SOURCE_CHANGED", "微信读书登录响应结构变化")
        pending["cookies"] = self.cookies()
        self.vault.save("weread_pending", pending)
        if data.get("succeed") or inner.get("succeed"):
            cookies = self.cookies()
            for name, fields in {"wr_vid": ("webLoginVid", "vid", "userVid"), "wr_skey": ("accessToken",)}.items():
                for field in fields:
                    value = data.get(field) or inner.get(field)
                    if not cookies.get(name) and value:
                        cookies[name] = str(value)
            self.set_cookies(cookies)
            self.shelf()  # Verify the actual cookie jar; do not guess or rotate tokens.
            self.vault.save("weread", {"cookies": self.cookies()})
            self.vault.save("weread_pending", {})
            return {"status": "success"}
        logic = data.get("logicCode", inner.get("logicCode"))
        if logic == "NEED_OTP":
            raise SourceError("CHALLENGE_REQUIRED", "微信读书要求额外验证，请先在官方页面完成")
        return {"status": "scanned" if logic == 1 else "waiting"}
