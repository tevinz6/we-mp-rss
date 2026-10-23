"""Narrow adapters based on upstream searchbiz/appmsgpublish and QR login flows.

No upstream global schedulers, credential printers, or endpoint failover are imported.
"""
import base64
import hashlib
import html as html_module
import ipaddress
import json
import re
import socket
import time
import uuid
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import bleach
from bs4 import BeautifulSoup
import requests
from .security import Vault

ARTICLE_HOSTS = {"mp.weixin.qq.com"}
IMAGE_HOSTS = {"mmbiz.qpic.cn", "mmbiz.qlogo.cn", "res.wx.qq.com"}
SAFE_TAGS = {"p", "br", "h1", "h2", "h3", "h4", "strong", "em", "b", "i", "u", "s", "blockquote", "pre", "code", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "a", "img", "section", "div", "span", "hr"}


class SourceError(Exception):
    def __init__(self, code, message=None, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(message or code)


def canonical_url(url):
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    for key in ("from", "scene", "subscene", "clicktime", "enterid", "sessionid"):
        params.pop(key, None)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(sorted(params.items()), doseq=True), ""))


def validate_url(url, image=False, resolve=True):
    parsed = urlsplit(url)
    allowed = IMAGE_HOSTS if image else ARTICLE_HOSTS
    if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise SourceError("UNSAFE_URL", "仅允许受支持的微信 HTTPS 地址")
    if resolve:
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise SourceError("NETWORK_ERROR", "域名解析失败") from None
        if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise SourceError("UNSAFE_URL", "拒绝访问非公网地址")
    return url


def classify(payload):
    response = payload.get("base_resp")
    if not isinstance(response, dict) or "ret" not in response:
        raise SourceError("SOURCE_CHANGED", "来源返回格式已变化")
    code = response["ret"]
    if code in (200003, 40001, 40014, 42001):
        raise SourceError("AUTH_EXPIRED", "登录状态已失效，请重新扫码")
    if code in (200013, 45009):
        raise SourceError("RATE_LIMITED", "来源限制了访问频率，采集已暂停")
    if code != 0:
        raise SourceError("SOURCE_CHANGED", f"来源返回未支持的状态码 {code}")


def parse_body(html):
    if any(s in html for s in ("当前环境异常", "完成验证后即可继续访问")):
        raise SourceError("CHALLENGE_REQUIRED", "文章页面要求验证，采集已暂停")
    if "该内容已被发布者删除" in html:
        raise SourceError("DELETED", "原文已被发布者删除")
    if any(s in html for s in ("该内容暂时无法查看", "内容审核中", "违规无法查看")):
        raise SourceError("CONTENT_UNAVAILABLE", "原文暂时不可用")
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    content_format = "html"
    if body is None:
        # Same text-only fallback supported by the pinned upstream WeRead parser.
        # Decode only a literal string; never evaluate page JavaScript.
        match = re.search(r"content_noencode\s*:\s*(['\"])", html)
        plain = []
        if match:
            position, quote = match.end(), match.group(1)
            while position < len(html):
                char = html[position]
                if char == quote:
                    break
                if char == "\\" and position + 1 < len(html):
                    escape = html[position + 1]
                    size = 2 if escape == "x" else 4 if escape == "u" else 0
                    if size:
                        try:
                            plain.append(chr(int(html[position + 2:position + 2 + size], 16)))
                            position += 2 + size
                            continue
                        except ValueError:
                            pass
                    plain.append({"n": "\n", "r": "\r", "t": "\t"}.get(escape, escape))
                    position += 2
                    continue
                plain.append(char)
                position += 1
        text = "".join(plain).strip()
        if not text:
            raise SourceError("PARSE_ERROR", "未找到正文，可能是页面结构变化")
        text = text.encode("utf-8", errors="replace").decode("utf-8")
        content_format = "text_only"
        body = BeautifulSoup('<div>' + ''.join('<p>' + html_module.escape(line) + '</p>' for line in text.splitlines() if line.strip()) + '</div>', "html.parser").div
    raw = str(body)
    for node in body.select("script, style, iframe, object, form, input, button"):
        if not node.decomposed:
            node.decompose()
    images = []
    for img in body.find_all("img"):
        # Removing a malformed parent image also decomposes descendants already in this list.
        if img.decomposed:
            continue
        src = img.get("data-src") or img.get("src", "")
        try:
            validate_url(src, image=True, resolve=False)
        except (SourceError, ValueError):
            img.decompose()
            continue
        img["src"] = src
        images.append(src)
    clean = bleach.clean(str(body), tags=SAFE_TAGS, attributes={"a": ["href", "title"], "img": ["src", "alt"], "td": ["colspan", "rowspan"]}, protocols={"https"}, strip=True)
    if not body.get_text(strip=True) and not images:
        raise SourceError("PARSE_ERROR", "正文为空")
    return {"raw": raw, "html": clean, "text": body.get_text("\n", strip=True), "images": list(dict.fromkeys(images)), "format": content_format}


class WechatAdapter:
    def __init__(self, settings):
        self.settings = settings
        self.vault = Vault(settings)
        self.auth = self.vault.load("wechat")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://mp.weixin.qq.com/"})
        for name, value in self.auth.get("cookies", {}).items():
            self.session.cookies.set(name, value, domain="mp.weixin.qq.com", path="/")

    def request(self, method, url, *, image=False, **kwargs):
        for _ in range(4):
            validate_url(url, image=image)
            try:
                response = self.session.request(method, url, timeout=(10, 25), allow_redirects=False, stream=True, **kwargs)
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    response.close()
                    if method != "GET":
                        raise SourceError("SOURCE_CHANGED", "登录请求出现非预期跳转")
                    kwargs.pop("params", None)
                    continue
                if response.status_code == 429:
                    delay = response.headers.get("Retry-After", "0")
                    response.close()
                    raise SourceError("RATE_LIMITED", retry_after=int(delay) if delay.isdigit() else 0)
                if response.status_code in (401, 403):
                    response.close()
                    raise SourceError("AUTH_EXPIRED" if response.status_code == 401 else "CHALLENGE_REQUIRED")
                if response.status_code >= 500:
                    response.close()
                    raise SourceError("NETWORK_ERROR", "来源暂时不可用")
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 10 * 1024 * 1024:
                        raise SourceError("CONTENT_TOO_LARGE", "内容超过 10 MB 限制")
                response._content = bytes(content)
                response._content_consumed = True
                response.close()
                return response
            except requests.RequestException:
                raise SourceError("NETWORK_ERROR", "请求失败或超时") from None
        raise SourceError("UNSAFE_URL", "跳转次数超过限制")

    def json_request(self, path, params):
        if not self.auth.get("token"):
            raise SourceError("AUTH_EXPIRED", "请先扫码登录微信公众平台")
        params = {**params, "token": self.auth["token"], "lang": "zh_CN", "f": "json", "ajax": "1"}
        response = self.request("GET", "https://mp.weixin.qq.com" + path, params=params)
        try:
            result = response.json()
        except ValueError:
            raise SourceError("SOURCE_CHANGED", "来源未返回有效 JSON") from None
        if not isinstance(result, dict):
            raise SourceError("SOURCE_CHANGED")
        classify(result)
        return result

    def search(self, keyword):
        result = self.json_request("/cgi-bin/searchbiz", {"action": "search_biz", "begin": 0, "count": 5, "query": keyword})
        if not isinstance(result.get("list"), list):
            raise SourceError("SOURCE_CHANGED")
        return {"accounts": [{"external_id": str(x["fakeid"]), "name": x["nickname"], "alias": x.get("alias", "")} for x in result["list"]]}

    def discover(self, feed, offset=0):
        result = self.json_request("/cgi-bin/appmsgpublish", {"sub": "list", "sub_action": "list_ex", "begin": offset, "count": 5, "fakeid": feed["external_id"]})
        try:
            page = result["publish_page"]
            page = json.loads(page) if isinstance(page, str) else page
            groups = page["publish_list"]
            if not isinstance(groups, list):
                raise ValueError()
            articles = []
            for group in groups:
                info = group["publish_info"]
                info = json.loads(info) if isinstance(info, str) else info
                for item in info["appmsgex"]:
                    validate_url(item["link"], resolve=False)
                    articles.append({"source_article_id": str(item["aid"]), "title": item["title"], "url": item["link"], "published_at": item.get("create_time") or item.get("update_time"), "author": item.get("author")})
        except (KeyError, TypeError, ValueError):
            raise SourceError("SOURCE_CHANGED", "文章列表结构不符合预期") from None
        return {"items": articles, "next_cursor": offset + 5, "exhausted": len(groups) < 5, "coverage": "paged_list", "response_valid": True}

    def fetch(self, article):
        html = self.request("GET", article["url"]).content.decode("utf-8", errors="replace")
        try:
            return parse_body(html)
        except SourceError as exc:
            if exc.code != "PARSE_ERROR" or not self.settings.browser:
                raise
        # Browser fallback only for missing rendered content, never for authentication/rate limits.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            context = browser.new_context(service_workers="block")
            def guard(route):
                host = urlsplit(route.request.url).hostname
                try:
                    validate_url(route.request.url, image=host in IMAGE_HOSTS)
                    route.continue_()
                except (SourceError, ValueError):
                    route.abort()
            context.route("**/*", guard)
            page = context.new_page()
            page.goto(article["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            result = parse_body(page.content())
            browser.close()
            return result

    def image(self, url):
        response = self.request("GET", url, image=True)
        mime = response.headers.get("content-type", "").split(";")[0]
        if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            raise SourceError("UNSUPPORTED_MEDIA", "仅归档常见位图")
        return {"bytes": base64.b64encode(response.content).decode(), "mime": mime}

    def qr_start(self):
        fingerprint = uuid.uuid4().hex
        response = self.request("POST", "https://mp.weixin.qq.com/cgi-bin/bizlogin", params={"action": "startlogin"}, data={"fingerprint": fingerprint, "token": "", "lang": "zh_CN", "f": "json", "ajax": "1", "redirect_url": "/cgi-bin/home?t=home/index&lang=zh_CN", "login_type": "3"})
        try:
            start_result = response.json()
        except ValueError:
            raise SourceError("SOURCE_CHANGED", "登录接口返回格式异常") from None
        if "base_resp" in start_result:
            classify(start_result)
        identifier = self.session.cookies.get("uuid")
        if not identifier:
            raise SourceError("SOURCE_CHANGED", "登录接口未返回二维码会话")
        qr = self.request("GET", "https://mp.weixin.qq.com/cgi-bin/scanloginqrcode", params={"action": "getqrcode", "uuid": identifier, "random": int(time.time() * 1000)})
        if not qr.headers.get("content-type", "").startswith("image/"):
            raise SourceError("SOURCE_CHANGED", "二维码返回格式异常")
        self.vault.save("qr", {"cookies": self.session.cookies.get_dict(), "fingerprint": fingerprint, "expires": time.time() + 300})
        return {"image": "data:image/png;base64," + base64.b64encode(qr.content).decode(), "expires_in": 300}

    def qr_check(self):
        state = self.vault.load("qr")
        if state.get("expires", 0) < time.time():
            return {"status": "expired"}
        self.session.cookies.clear()
        for name, value in state["cookies"].items():
            self.session.cookies.set(name, value, domain="mp.weixin.qq.com", path="/")
        response = self.request("GET", "https://mp.weixin.qq.com/cgi-bin/scanloginqrcode", params={"action": "ask", "fingerprint": state["fingerprint"], "lang": "zh_CN", "f": "json", "ajax": "1"})
        try:
            payload = response.json()
        except ValueError:
            raise SourceError("SOURCE_CHANGED") from None
        if not isinstance(payload, dict):
            raise SourceError("SOURCE_CHANGED")
        if "base_resp" in payload:
            classify(payload)
        if payload.get("status") in (5, 6):
            return {"status": "expired"}
        if payload.get("status") not in (1, 3):
            return {"status": "scanned" if payload.get("status") in (2, 4) else "waiting"}
        response = self.request("POST", "https://mp.weixin.qq.com/cgi-bin/bizlogin", params={"action": "login"}, data={"fingerprint": state["fingerprint"], "userlang": "zh_CN", "redirect_url": "", "cookie_forbidden": "0", "cookie_cleaned": "0", "plugin_used": "0", "login_type": "3", "token": "", "lang": "zh_CN", "f": "json", "ajax": "1"})
        try:
            login_result = response.json()
        except ValueError:
            raise SourceError("SOURCE_CHANGED", "登录结果格式异常") from None
        if "base_resp" in login_result:
            classify(login_result)
        match = re.search(r"token=([0-9]+)", response.text)
        if not match:
            raise SourceError("AUTH_EXPIRED", "未取得登录凭据，请确认公众号身份后重新扫码")
        self.vault.save("wechat", {"token": match.group(1), "cookies": self.session.cookies.get_dict()})
        self.vault.save("qr", {})
        return {"status": "success"}


class MockAdapter:
    def __init__(self, source_id="primary"):
        self.source_id = source_id

    def search(self, keyword):
        external_id = "MP_WXS_100000" if self.source_id == "weread" else "demo"
        return {"accounts": [{"external_id": external_id, "name": keyword or "演示公众号", "source_id": self.source_id}]}

    def discover(self, feed, offset=0):
        scenario = feed["external_id"]
        if scenario in ("AUTH_EXPIRED", "RATE_LIMITED", "CHALLENGE_REQUIRED", "NETWORK_ERROR", "SOURCE_CHANGED"):
            raise SourceError(scenario)
        if scenario == "slow":
            time.sleep(120)
        total = 7 if scenario == "pages" else 3
        items = [{"source_article_id": f"{scenario}-{i}", "title": f"{feed['name']} · 示例文章 {i + 1}", "url": f"https://mp.weixin.qq.com/s/demo-{scenario}-{i}", "published_at": int(time.time() // 86400) * 86400 - i * 3600, "author": "模拟来源"} for i in range(offset, min(offset + 5, total))]
        return {"items": items, "next_cursor": offset + 5, "exhausted": offset + 5 >= total, "coverage": "paged_list", "response_valid": True}

    def fetch(self, article):
        return parse_body('<html><div id="js_content"><h2>模拟文章</h2><p>这是一篇用于验证采集、去重、归档与恢复的本地示例。没有请求微信。</p></div></html>')


def adapter(settings, source_id="primary"):
    if settings.mock:
        return MockAdapter(source_id)
    if source_id == "weread":
        from .weread import WereadAdapter
        return WereadAdapter(settings)
    return WechatAdapter(settings)
