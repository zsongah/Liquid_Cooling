"""本机边缘工作台的 HTTP 边界。

浏览器只访问这里的白名单 API，不能直接打开设备连接、执行 shell、读取
任意文件或绕过 ControlService。控制循环由 Workbench 后台任务拥有，页面
刷新、切换标签或关闭均不会创建、重启或停止控制器。

首版只绑定回环地址。Host 校验防止 DNS rebinding，严格同源检查和会话
Cookie/CSRF 配对防止外部网页触发写操作。跨机器访问应通过经过认证的
安全通道代理到回环地址；本模块没有冒充已经实现多用户鉴权或 TLS。
"""
import hmac
import json
import mimetypes
import re
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .workbench import Workbench, WorkbenchError
from .site_view import site_snapshot


STATIC_ROOT = Path(__file__).resolve().parent / "web"
MAX_BODY = 2 * 1024 * 1024
VERSION = "0.7.0"


def strict_json(data):
    """控制配置拒绝重复键和非有限数字，避免校验器与执行器看到不同值。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key:" + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite_json_number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


class ConsoleServer(ThreadingHTTPServer):
    """一个工作台对应一个运行管理器；每个请求使用独立的只读查询连接。"""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, workbench):
        if address[0] not in ("127.0.0.1", "localhost"):
            raise ValueError("console_requires_loopback_binding")
        self.workbench = workbench
        self.csrf = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        super().__init__(address, ConsoleHandler)
        port = self.server_address[1]
        self.hosts = {"127.0.0.1:%s" % port, "localhost:%s" % port}
        if port == 80:
            self.hosts.update(("localhost", "127.0.0.1"))
        self.cookie_name = "lc_console_%s" % port


class ConsoleHandler(BaseHTTPRequestHandler):
    """固定路由的本机 API；GET 永不修改配置或启动任务。"""
    server_version = "LCControl/0.6"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # 不记录 URL 查询、正文、Cookie、凭据或完整设备异常。
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8", cookie=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict" %
                             (self.server.cookie_name, self.server.session))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 关闭页面不会终止工作台任务。

    def _error(self, status, code, message):
        self._send(status, {"error": {"code": code, "message": message}})

    def _origin_ok(self):
        """只接受本进程服务地址，不信任外部代理转来的 Host/X-Forwarded-*。"""
        host = self.headers.get("Host", "").lower()
        if host not in self.server.hosts:
            self._error(403, "invalid_host", "工作台仅接受本机地址。")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + host:
            self._error(403, "cross_origin_rejected", "请求来源与工作台不一致。")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._error(403, "cross_site_rejected", "不允许外部网页操作工作台。")
            return False
        return True

    def _authenticated(self, mutation=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            cookie = SimpleCookie()
        item = cookie.get(self.server.cookie_name)
        if item is None or not hmac.compare_digest(item.value, self.server.session):
            self._error(401, "session_required", "请重新打开工作台建立会话。")
            return False
        if mutation and not hmac.compare_digest(self.headers.get("X-LC-CSRF", ""), self.server.csrf):
            self._error(403, "csrf_rejected", "操作令牌失效，请刷新页面。")
            return False
        return True

    def _payload(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("transfer_encoding_not_supported")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("application_json_required")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise ValueError("content_length_required")
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            raise ValueError("request_body_size_outside_limit")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete_request_body")
        result = strict_json(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("json_object_required")
        return result

    def _dispatch(self, mutation=False):
        if not self._origin_ok():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/session" and not mutation:
            self._send(200, {"csrf_token": self.server.csrf, "version": VERSION,
                             "allow_hardware_control": self.server.workbench.allow_hardware_control}, cookie=True)
            return
        if path == "/api/health" and not mutation:
            self._send(200, {"status": "ready", "version": VERSION})
            return
        static = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
        if not mutation and path in static:
            file = STATIC_ROOT / static[path]
            if not file.is_file():
                self._error(503, "console_assets_missing", "工作台页面尚未安装完整。")
                return
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self._send(200, file.read_bytes(), kind + "; charset=utf-8")
            return
        if not path.startswith("/api/"):
            self._error(404, "not_found", "未找到页面。")
            return
        if not self._authenticated(mutation):
            return
        workbench = self.server.workbench
        data = self._payload() if mutation else None
        query = parse_qs(parsed.query, max_num_fields=12)
        def scalar(key, default):
            values = query.get(key, [default])
            if len(values) != 1:
                raise ValueError("duplicate_query_field")
            return values[0]
        if not mutation and path == "/api/catalog":
            self._send(200, workbench.catalog())
        elif not mutation and path == "/api/jobs":
            self._send(200, {"jobs": workbench.list_jobs()})
        elif mutation and path == "/api/configs/validate":
            self._send(200, workbench.validate(data))
        elif mutation and path == "/api/configs/draft":
            self._send(200, workbench.save_draft(data))
        elif mutation and path == "/api/jobs":
            self._send(202, workbench.start_job(data))
        else:
            forecast_route = re.fullmatch(r"/api/configs/([A-Za-z0-9_.-]{1,160})/forecast(?:/(descriptor|validate|clear))?", path)
            analysis_route = re.fullmatch(r"/api/configs/([A-Za-z0-9_.-]{1,160})/analysis", path)
            site_route = re.fullmatch(r"/api/configs/([A-Za-z0-9_.-]{1,160})/site", path)
            if analysis_route:
                if not mutation:
                    self._error(405, "method_not_allowed", "分析需使用带会话令牌的 POST，请求将保存独立分析报告。")
                    return
                self._send(200, workbench.analyze_config(analysis_route.group(1), data))
                return
            if site_route and not mutation:
                self._send(200, site_snapshot(workbench, site_route.group(1)))
                return
            if forecast_route:
                identifier, action = forecast_route.groups()
                if not mutation and action == "descriptor":
                    result = workbench.forecast_descriptor(identifier)
                elif not mutation and action is None:
                    result = workbench.get_forecast(identifier, job_id=scalar("job_id", None))
                elif mutation and action is None:
                    result = workbench.submit_forecast(identifier, data)
                elif mutation and action == "validate":
                    result = workbench.validate_forecast(identifier, data)
                elif mutation and action == "clear":
                    result = workbench.clear_forecast(identifier)
                else:
                    self._error(405, "method_not_allowed", "该预测接口不支持此操作。")
                    return
                self._send(200, result)
                return
            match = re.fullmatch(r"/api/(configs|runs|jobs)/([A-Za-z0-9_.-]{1,160})(?:/(publish|events|stop))?", path)
            if not match:
                self._error(404, "not_found", "未找到接口。")
                return
            collection, identifier, action = match.groups()
            if collection == "configs" and action is None and not mutation:
                result = workbench.get_config(identifier)
            elif collection == "configs" and action == "publish" and mutation:
                result = workbench.publish(identifier, data.get("expected_revision"))
            elif collection == "runs" and action is None and not mutation:
                result = workbench.get_run(identifier)
            elif collection == "runs" and action == "events" and not mutation:
                result = workbench.events(identifier, kind=scalar("kind", "telemetry"),
                                          after=int(scalar("after", "0")), limit=int(scalar("limit", "200")),
                                          tail=scalar("tail", "0") == "1")
            elif collection == "jobs" and action == "stop" and mutation:
                result = workbench.stop_job(identifier)
            else:
                self._error(405, "method_not_allowed", "该接口不支持此操作。")
                return
            self._send(200, result)

    def _handle(self, mutation):
        try:
            self._dispatch(mutation)
        except WorkbenchError as error:
            self._error(getattr(error, "status", 400), getattr(error, "code", "workbench_error"), str(error))
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            # 参数错误不回显用户原始字段（其中可能有凭据或设备地址）。
            self._error(400, "invalid_request", "请求参数不完整或格式不正确，请检查配置与输入。")
        except TimeoutError:
            self.close_connection = True
            self._error(408, "request_timeout", "请求超时。")
        except Exception:
            self._error(500, "internal_error", "服务处理失败；运行任务状态请在任务列表中核对。")

    def do_GET(self):
        self._handle(False)

    def do_POST(self):
        self._handle(True)


def serve(project_root, host="127.0.0.1", port=8765, state_dir=None, allow_hardware_control=False):
    """启动本机工作台；退出时请求任务停止并保留设备交接是否确认的结果。"""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid_console_port")
    manager = Workbench(Path(project_root), state_dir=state_dir,
                        allow_hardware_control=allow_hardware_control)
    server = None
    try:
        server = ConsoleServer((host, port), manager)
        print("LC Control 工作台：http://127.0.0.1:%s" % server.server_address[1], flush=True)
        print("关闭浏览器不停止任务；退出自动控制需核对本地接管结果。", flush=True)
        server.serve_forever(poll_interval=0.25)
    finally:
        if server is not None:
            server.server_close()
        manager.close()
