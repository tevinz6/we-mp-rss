"""Launch an isolated offline demo and exercise the actual Vue UI in Chromium."""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="collector-ui-") as temporary:
        env = {**os.environ, "COLLECTOR_DATA_DIR": temporary, "COLLECTOR_MOCK": "true", "COLLECTOR_REQUEST_INTERVAL_SECONDS": "0"}
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        subprocess.run([sys.executable, "-m", "core.collection.cli", "demo"], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(Path(temporary) / "server.log", "w") as log:
            server = subprocess.Popen([sys.executable, "-m", "core.collection.cli", "serve", "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
            worker = subprocess.Popen([sys.executable, "-m", "core.collection.cli", "worker"], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                url = f"http://127.0.0.1:{port}"
                for _ in range(50):
                    try:
                        urllib.request.urlopen(url + "/api/v1/collection/health/ready", timeout=1)
                        break
                    except OSError:
                        time.sleep(0.1)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    expect(page.get_by_role("heading", name="公众号采集工作台")).to_be_visible()
                    page.get_by_label("管理员密码").fill(secrets.token_urlsafe(24))
                    page.get_by_role("button", name="创建工作台").click()
                    expect(page.get_by_role("heading", name="值得留存的内容")).to_be_visible()
                    expect(page.locator(".article-row")).to_have_count(6)
                    page.locator(".article-row").first.click()
                    expect(page.locator(".article-body")).to_contain_text("本地示例")
                    page.get_by_role("button", name="返回文章列表").click()
                    page.locator("nav button").nth(1).click()
                    page.get_by_placeholder("输入公众号名称").fill("浏览器验收")
                    page.get_by_role("button", name="搜索公众号", exact=True).click()
                    page.locator(".match").get_by_role("button", name="添加", exact=True).click()
                    expect(page.locator(".feed-card")).to_have_count(3)
                    card = page.locator(".feed-card").filter(has=page.get_by_role("heading", name="浏览器验收", exact=True))
                    card.get_by_role("button", name="检查新文章", exact=True).click()
                    for _ in range(50):
                        response = page.request.get(url + "/api/v1/collection/overview").json()
                        if response["saved"] == 9:
                            break
                        page.wait_for_timeout(200)
                    else:
                        raise AssertionError("Worker failed to archive articles from the UI-created feed")
                    with page.expect_download() as download:
                        card.get_by_role("button", name="JSON", exact=True).click()
                    assert download.value.suggested_filename.endswith(".json")
                    page.locator("nav button").nth(2).click()
                    expect(page.get_by_role("heading", name="最近任务")).to_be_visible()
                    page.locator("nav button").nth(3).click()
                    page.get_by_role("button", name="暂停采集", exact=True).click()
                    expect(page.get_by_role("button", name="暂停采集", exact=True)).to_be_disabled()
                    page.get_by_role("button", name="恢复采集", exact=True).click()
                    expect(page.get_by_role("button", name="恢复采集", exact=True)).to_be_disabled()
                    page.locator("nav button").first.click()
                    page.get_by_role("button", name="刷新状态").click()
                    expect(page.locator(".article-row")).to_have_count(9)
                    if args.screenshot:
                        page.screenshot(path=args.screenshot, full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile layout overflows"
                    expect(page.get_by_role("heading", name="值得留存的内容")).to_be_visible()
                    assert not errors, errors
                    browser.close()
                print("UI smoke passed: setup, reading, search/add, collection, export, pause/resume, mobile layout; no browser errors.")
            finally:
                for process in (worker, server):
                    process.terminate()
                for process in (worker, server):
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
