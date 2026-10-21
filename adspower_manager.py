#!/usr/bin/env python3
"""Local-only AdsPower V2 profile manager; Python 3.9+, standard library only."""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAMES = tuple(f"Vinted-{number:02d}" for number in range(1, 5))
SITES = {
    "global": "https://www.vinted.com/",
    "it": "https://www.vinted.it/",
    "fr": "https://www.vinted.fr/",
    "de": "https://www.vinted.de/",
    "es": "https://www.vinted.es/",
    "uk": "https://www.vinted.co.uk/",
    "nl": "https://www.vinted.nl/",
    "be": "https://www.vinted.be/",
    "pl": "https://www.vinted.pl/",
    "pt": "https://www.vinted.pt/",
}
ASSETS = Path(__file__).resolve().parent / "web"


class LocalError(Exception):
    """A message suitable for display, without raw API responses or secrets."""


def api_error(code, message):
    text = str(message).lower()
    if any(word in text for word in ("quota", "limit", "数量", "上限", "额度", "套餐")):
        return LocalError("AdsPower 的套餐额度或请求限制阻止了操作。请在客户端检查；已创建的环境会保留。")
    if any(word in text for word in ("api key", "apikey", "token", "unauthorized", "密钥", "权限")):
        return LocalError("API 验证失败。请检查本机填写的 API Key，以及 AdsPower 的接口权限。")
    safe_code = code if type(code) is int else "未知"
    return LocalError(f"AdsPower 返回错误（代码 {safe_code}）。请查看客户端提示，确认接口、版本和环境状态。")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AdsPower:
    def __init__(self, port, key, interval=1.1):
        if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
            raise LocalError("端口必须是 1–65535 的数字。")
        if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise LocalError("API Key 格式有误，请重新从 AdsPower 复制。")
        self.base = f"http://127.0.0.1:{int(port)}"
        self.key = key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.interval = interval
        self.last_request = 0.0

    def request(self, action, payload):
        delay = self.interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        request = urllib.request.Request(
            f"{self.base}/api/v2/browser-profile/{action}",
            data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
        )
        self.last_request = time.monotonic()
        try:
            with self.opener.open(request, timeout=90 if action == "start" else 25) as response:
                result = json.loads(response.read(4_000_001))
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403):
                raise LocalError("API 验证失败。请检查 API Key，并确认付费套餐的 Local API 已启用。") from None
            if status == 404:
                raise LocalError("客户端未提供所需的 V2 API。请更新 AdsPower，再检查接口状态。") from None
            raise LocalError(f"本地 API 返回 HTTP {status}，请检查 AdsPower 客户端。") from None
        except (TimeoutError, socket.timeout):
            raise LocalError("本地请求超时。创建操作可能已生效，请先重新检查环境列表，避免立即重复操作。") from None
        except urllib.error.URLError:
            raise LocalError("无法连接本机 AdsPower。请保持客户端开启，并检查 API 状态、端口及本机网络权限。") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise LocalError("本地 API 返回了无法识别的数据。请确认填写的是 AdsPower API 端口。") from None
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise LocalError("API 响应格式不符，操作已停止。")
        if result["code"] != 0:
            raise api_error(result["code"], result.get("msg", ""))
        return result.get("data")

    def profiles(self):
        profiles, seen = [], set()
        # Read until an empty page, even if a client returns fewer rows than requested.
        for page in range(1, 1001):
            data = self.request("list", {"page": page, "limit": 100})
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise LocalError("环境列表格式不符，无法判断是否已有同名环境；未创建任何新环境。")
            if not data["list"]:
                return profiles
            for row in data["list"]:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise LocalError("环境列表缺少名称，操作已停止。")
                profile_id = row.get("profile_id") or row.get("user_id")
                if not isinstance(profile_id, str) or not profile_id or profile_id in seen:
                    raise LocalError("环境列表缺少编号或分页重复，操作已停止。")
                seen.add(profile_id)
                profiles.append({"name": row["name"], "profile_id": profile_id})
        raise LocalError("环境分页超过读取上限，未继续创建。")


class Manager:
    def __init__(self, client, site):
        if site not in SITES:
            raise LocalError("请选择列表内的 Vinted 站点。")
        self.client = client
        self.site = SITES[site]

    def plan(self):
        by_name = {name: [] for name in NAMES}
        for row in self.client.profiles():
            if row["name"] in by_name:
                by_name[row["name"]].append(row)
        if any(len(rows) > 1 for rows in by_name.values()):
            raise LocalError("发现多个同名的 Vinted 环境。请在 AdsPower 中确认并重命名，工具不会随意选择或删除。")
        return [
            {"name": name, "profile_id": by_name[name][0]["profile_id"] if by_name[name] else None,
             "state": "existing" if by_name[name] else "missing"}
            for name in NAMES
        ]

    def create(self):
        rows = self.plan()
        for row in rows:
            if row["profile_id"]:
                continue
            try:
                data = self.client.request("create", {
                    "name": row["name"], "group_id": "0",
                    "user_proxy_config": {"proxy_soft": "no_proxy"},
                    "tabs": [self.site],
                    "fingerprint_config": {"browser_kernel_config": {"type": "chrome", "version": "latest"}},
                })
                profile_id = (data.get("profile_id") or data.get("user_id")) if isinstance(data, dict) else None
                if not isinstance(profile_id, str) or not profile_id:
                    raise LocalError("创建响应缺少环境编号，可能已经创建成功。请先重新检查列表。")
                row.update(profile_id=profile_id, state="created")
            except LocalError as error:
                return {"rows": rows, "error": str(error)}
        return {"rows": rows, "error": None}

    def start(self, name):
        if name not in NAMES:
            raise LocalError("只能打开列表中的四个环境。")
        row = next(row for row in self.plan() if row["name"] == name)
        if not row["profile_id"]:
            raise LocalError("该环境尚未创建，请先创建缺少的环境。")
        self.client.request("start", {
            "profile_id": row["profile_id"], "headless": "0", "launch_args": [self.site],
        })
        return {"message": f"已发送 {name} 的启动请求。请在打开的浏览器中确认 Vinted 页面并手动登录。已有窗口可能需要手动进入该站点。"}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.manager = None
        self.operation_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def valid_host(self):
        return self.headers.get("Host") == self.server.origin.removeprefix("http://")

    def do_GET(self):
        if not self.valid_host():
            self.send_json(403, {"error": "本机地址校验失败。"})
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path not in assets:
            self.send_json(404, {"error": "页面不存在。"})
            return
        name, content_type = assets[self.path]
        self.send_bytes(200, (ASSETS / name).read_bytes(), content_type)

    def do_POST(self):
        token = self.headers.get("X-Local-Token", "")
        if (not self.valid_host() or self.headers.get("Origin") != self.server.origin
                or not hmac.compare_digest(token.encode("utf-8"), self.server.token.encode("utf-8"))):
            self.send_json(403, {"error": "本机会话校验失败。请从启动工具打开的页面操作。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16000 or self.headers.get("Content-Type") != "application/json":
                raise LocalError("请求格式有误。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise LocalError("请求格式有误。")
            with self.server.operation_lock:
                result = self.dispatch(body)
            self.send_json(200, result)
            if self.path == "/api/exit":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
        except (ValueError, UnicodeError):
            self.send_json(400, {"error": "输入格式有误，请重新填写。"})
        except LocalError as error:
            self.send_json(400, {"error": str(error)})
        except Exception:
            self.send_json(500, {"error": "操作未完成。请重新检查环境列表；创建操作可能已经生效。"})

    def dispatch(self, body):
        if self.path == "/api/connect":
            self.server.manager = None
            port = body.get("port", "")
            if str(port) == str(self.server.server_port):
                raise LocalError("请填写 AdsPower 的 API 端口，而不是管理页面的端口。")
            client = AdsPower(port, body.get("key", ""))
            manager = Manager(client, body.get("site", "global"))
            rows = manager.plan()
            self.server.manager = manager
            return {"rows": rows}
        if self.path == "/api/exit":
            self.server.manager = None
            return {"message": "工具已退出，API Key 已从工具会话中释放。AdsPower 浏览器仍由你自行管理。"}
        if self.path not in ("/api/plan", "/api/create", "/api/start"):
            raise LocalError("此操作不存在。")
        if self.server.manager is None:
            raise LocalError("请先连接 AdsPower。")
        if self.path == "/api/plan":
            return {"rows": self.server.manager.plan()}
        if self.path == "/api/create":
            return self.server.manager.create()
        return self.server.manager.start(body.get("name"))


def main():
    parser = argparse.ArgumentParser(description="Mac 本地 AdsPower 四环境管理工具")
    parser.add_argument("--no-browser", action="store_true", help="仅启动本机服务")
    parser.add_argument("--port", type=int, default=0, help="管理页面端口；默认随机，与 AdsPower 端口无关")
    args = parser.parse_args()
    with LocalServer(args.port) as server:
        url = f"{server.origin}/#{server.token}"
        print("本机管理工具已启动。请保持此终端窗口打开。退出请在页面点击「退出工具」，或按 Control+C。")
        print("如页面未打开，请将以下地址复制到这台 Mac 的浏览器（不要分享此地址）：")
        print(url, flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.manager = None


if __name__ == "__main__":
    main()
