# -*- coding: utf-8 -*-
"""fnOS 统一网关：监听 Unix Socket，把请求转发给本机 Flask 端口。

飞牛在把请求交给应用之前会先校验 NAS 登录态，因此走网关的访问天然带登录，
也只有这样 FN Connect / 桌面图标才能在远程访问里打开。

约束与做法：
- 网关转发过来的 path 仍带 `gatewayPrefix`（如 /app/quark-auto-save/data），
  这里把前缀剥掉再转发给 Flask，Flask 侧仍然只需处理 /data。
- 每连接短连接（Connection: close），避免网关侧复用语义差异；管理界面无长连接需求。
- 只做 HTTP 转发，不支持 WebSocket（本应用不需要）。
"""

import http.client
import logging
import os
import socket
import threading

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

MAX_HEADER = 1 << 20      # 1 MB 请求头上限
MAX_BODY = 64 << 20       # 64 MB 请求体上限


def _strip_prefix(path, prefix):
    if not prefix:
        return path
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix):]
    if path.startswith(prefix + "?"):
        return "/" + path[len(prefix):]
    return path


def _read_until(conn, buf, marker):
    while marker not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > MAX_HEADER:
            return None
    return buf


def _read_exact(conn, buf, n):
    while len(buf) < n:
        chunk = conn.recv(min(65536, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf


def _read_chunked(conn, buf):
    body = bytearray()
    while True:
        buf = _read_until(conn, buf, b"\r\n")
        if buf is None:
            return bytes(body)
        line, _, buf = buf.partition(b"\r\n")
        try:
            size = int(line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return bytes(body)
        if size == 0:
            return bytes(body)
        buf = _read_exact(conn, buf, size + 2)
        body += buf[:size]
        buf = buf[size + 2:]
        if len(body) > MAX_BODY:
            return bytes(body)


class Gateway:
    def __init__(self, sock_path, host, port, prefix=""):
        self.sock_path = os.path.abspath(sock_path)
        self.host = host
        self.port = int(port)
        self.prefix = prefix.rstrip("/")
        self._srv = None

    def start(self):
        os.makedirs(os.path.dirname(self.sock_path) or ".", exist_ok=True)
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logging.warning(f"[gateway] 清理旧 socket 失败：{e}")

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        os.chmod(self.sock_path, 0o660)
        srv.listen(128)
        self._srv = srv
        logging.info(f"[gateway] 监听 {self.sock_path}（前缀 {self.prefix or '/'}）→ 127.0.0.1:{self.port}")
        threading.Thread(target=self._accept_loop, name="gateway", daemon=True).start()

    def _accept_loop(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(300)
            buf = b""
            buf = _read_until(conn, buf, b"\r\n\r\n")
            if buf is None:
                return
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            if not lines or not lines[0]:
                return
            try:
                method, raw_path, _ver = lines[0].decode("latin-1").split(" ", 2)
            except ValueError:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return

            headers = []
            lower = {}
            for raw in lines[1:]:
                key, _, val = raw.decode("latin-1").partition(":")
                key = key.strip()
                val = val.strip()
                if not key or key.lower() in HOP_BY_HOP:
                    continue
                lower[key.lower()] = val
                headers.append((key, val))

            body = rest
            if "chunked" in lower.get("transfer-encoding", "").lower():
                body = _read_chunked(conn, body)
            elif lower.get("content-length"):
                try:
                    need = int(lower["content-length"])
                except ValueError:
                    need = 0
                body = _read_exact(conn, body, need)[:need] if need else b""

            path = _strip_prefix(raw_path, self.prefix)

            hdrs = dict(headers)
            hdrs["Connection"] = "close"
            hdrs.pop("Host", None)
            if self.prefix:
                # 告诉上游“这次是带着网关前缀进来的”，页面据此拼接口地址
                hdrs["X-Forwarded-Prefix"] = self.prefix

            up = http.client.HTTPConnection(self.host, self.port, timeout=300)
            try:
                up.request(method, path, body=body if body else None, headers=hdrs)
                resp = up.getresponse()
                payload = resp.read()
                out = [f"HTTP/1.1 {resp.status} {resp.reason}".encode("latin-1")]
                for k, v in resp.getheaders():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    out.append(f"{k}: {v}".encode("latin-1"))
                out.append(f"Content-Length: {len(payload)}".encode("latin-1"))
                out.append(b"Connection: close")
                out.append(b"")
                out.append(payload)
                conn.sendall(b"\r\n".join(out))
            finally:
                up.close()
        except Exception as e:  # noqa: BLE001
            logging.warning(f"[gateway] 转发失败：{e}")
            try:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def start(sock_path, host, port, prefix=""):
    """启动网关监听；失败不阻断主服务（端口访问仍可用）。"""
    try:
        gw = Gateway(sock_path, host, port, prefix)
        gw.start()
        return gw
    except Exception as e:  # noqa: BLE001
        logging.error(f"[gateway] 启动失败（不影响端口访问）：{e}")
        return None
