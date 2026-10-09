#!/usr/bin/env python3
"""mihomo 控制台 TUI —— Windows Terminal 专用版 (纯标准库, 可独立分发).

与 tui.py 功能一致 (由 _gen_tuiwin.py 生成), 仅保留 Windows 终端层, 无需 curses:
  输出: ANSI/VT 转义序列 (备用缓冲区 + 256 色 + 行级差量刷新 + 同步输出)
  输入: Win32 ReadConsoleInputW (键盘 / 鼠标单击双击 / 滚轮 / 窗口缩放)
仅「修改写回配置文件」一项功能需要可选的 PyYAML, 未安装时仅该功能不可用.

页面与按键同 tui.py:
  1 代理   代理组 / 节点选择、测速、筛选与排序、连通测试
  2 连接   实时连接列表、速度、断开连接、查看详情
  3 规则   规则列表与规则集 (可更新远程规则集)
  4 日志   内核实时日志, 可切换等级、暂停、筛选
  5 设置   常规 / 入站端口 / TUN / 订阅与内核 / 测速与界面, 分组管理

鼠标: 点击导航、代理组、节点 (单击即切换)、按钮、开关、分段选项; 滚轮滚动列表.
常用键: ↑↓/jk 移动  ←→/hl 切换焦点  ⏎ 确认  / 筛选  ? 帮助  q 退出
全局键: m 切换模式  T TUN  a 局域网共享  u 更新订阅  R 重启内核  r 刷新

用法: tui-win.py [--api URL | --host HOST --port PORT | pipe:名称] [--secret KEY] [代理组名]
  所有参数均可省略, 默认自动探测控制器地址 / 密钥 / 代理组:
    上次成功的连接 → Clash Verge 等前端与 mihomo 配置里的 external-controller / secret
    → 常见端口 9097 9090; 需要密钥而配置里没有时, 启动时提示输入并记住
  --api     控制器完整地址 http://127.0.0.1:9097, 或 pipe:管道名 / pipe:auto
            (Windows 命名管道, 适配 Clash Verge 内核); 同环境变量 MIHOMO_API
  --host    控制器主机, 默认 127.0.0.1 (环境变量 MIHOMO_HOST)
  --port    控制器端口, 默认 9097 (环境变量 MIHOMO_PORT)
  --secret  API 密钥 (环境变量 MIHOMO_SECRET)
例: python tui-win.py        python tui-win.py -a pipe:auto -s 123
"""
import argparse, calendar, ctypes, getpass, http.client, json, locale, os, queue, re, shutil, subprocess
import sys, threading, time, unicodedata, urllib.parse, urllib.request
from ctypes import wintypes

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache

if sys.platform == "win32":
    import msvcrt

HERE = os.path.dirname(os.path.realpath(__file__))
IS_WIN = sys.platform == "win32"
API = "http://127.0.0.1:9097"       # 启动时由 setup_controller 探测并覆盖
SECRET = ""
API_PIPE = None     # --api pipe:... 时保存命名管道全名 (仅 Windows)
CONTROLLER_DESC = ""   # 控制器来源说明, 显示在设置页
CONFIG_FILE = os.path.join(HERE, "config.yaml")
UPDATE = os.path.join(HERE, "update.sh")
SUB_FILE = os.path.join(HERE, "subscription.url")
PREFS_FILE = os.path.join(HERE, ".tui.json")
START_GROUP = None   # 命令行位置参数指定的代理组; None 时自动选择 (见 default_group)

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
GROUP_TAG = {"Selector": "手选", "URLTest": "自动", "Fallback": "故障转移",
             "LoadBalance": "负载均衡", "Relay": "链式"}
MODES = [("规则", "rule"), ("全局", "global"), ("直连", "direct")]
MODE_LABEL = dict((v, k) for k, v in MODES)
LOG_LEVELS = ["debug", "info", "warning", "error"]
LOG_TAG = {"debug": ("DBG", "dim"), "info": ("INF", "accent"),
           "warning": ("WRN", "warn"), "error": ("ERR", "bad")}
DEFAULT_PREFS = {"delay_url": "https://www.gstatic.com/generate_204",
                 "delay_timeout": 3000, "concurrency": 8, "persist": True,
                 "log_level": "info", "close_on_switch": False}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ACTIVE_REFRESH_MS = 150
IDLE_REFRESH_MS = 1000


# ───────────────────────────── 数据层 ─────────────────────────────

def api(method, path, data=None, timeout=8):
    if API_PIPE:
        return pipe_api(method, path, data)
    r = urllib.request.Request(API + path, method=method,
                               headers={"Authorization": "Bearer " + SECRET})
    if data is not None:
        r.data = json.dumps(data).encode()
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=timeout) as f:
        body = f.read()
    return json.loads(body) if body.strip() else {}


def q(name):
    return urllib.parse.quote(name, safe="")


def load_prefs():
    p = dict(DEFAULT_PREFS)
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            p.update(json.load(f))
    except Exception:
        pass
    return p


def save_prefs(p):
    tmp = PREFS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PREFS_FILE)


_persist_lock = threading.Lock()


def update_cmd():
    """选择订阅更新脚本的执行命令; bash 以相对路径调用, 避免 Windows 路径传入 MSYS 出错."""
    bash = shutil.which("bash")
    if os.path.isfile(UPDATE) and (bash or not IS_WIN):
        return [bash or "bash", os.path.basename(UPDATE)]
    for ext in (".bat", ".cmd"):
        f = os.path.join(HERE, "update" + ext)
        if os.path.isfile(f):
            return [os.environ.get("COMSPEC", "cmd.exe"), "/c", f]
    ps1 = os.path.join(HERE, "update.ps1")
    if os.path.isfile(ps1):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
    return None


def persist_config(patch):
    """把运行时修改同步写回 config.yaml (tun 等字典项做合并)."""
    try:
        import yaml
    except ImportError:
        raise RuntimeError("缺少 PyYAML, 无法写回 config.yaml (pip install pyyaml)")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)
    with _persist_lock:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            doc = yaml.load(f, Loader=loader) or {}
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(doc.get(k), dict):
                doc[k].update(v)
            else:
                doc[k] = v
        tmp = CONFIG_FILE + ".tui.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(doc, f, Dumper=dumper, allow_unicode=True, sort_keys=False)
        os.replace(tmp, CONFIG_FILE)


def fmt_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def fmt_speed(n):
    return fmt_bytes(n) + "/s"


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


def parse_time(s):
    """解析 mihomo 的 RFC3339(纳秒) 时间为 epoch 秒."""
    try:
        base = calendar.timegm(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").timetuple())
        tz = s[-6:]
        if s.endswith("Z"):
            return base
        if tz[0] in "+-" and tz[3] == ":":
            off = int(tz[1:3]) * 3600 + int(tz[4:6]) * 60
            return base - off if tz[0] == "+" else base + off
        return base
    except Exception:
        return None


def proxy_test(port, url):
    """经本机代理端口真实访问一次测速地址 (https 走 CONNECT 隧道)."""
    u = urllib.parse.urlsplit(url)
    t0 = time.time()
    if u.scheme == "https":
        c = http.client.HTTPSConnection("127.0.0.1", port, timeout=8)
        c.set_tunnel(u.hostname, u.port or 443)
        c.request("GET", (u.path or "/") + ("?" + u.query if u.query else ""))
    else:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        c.request("GET", url)
    code = c.getresponse().status
    c.close()
    return code, time.time() - t0


# ─── Windows 命名管道传输 (--api pipe:..., 适配 Clash Verge 的 pipe 控制器) ───

if IS_WIN:
    _w32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _w32.CreateFileW.restype = wintypes.HANDLE
    _w32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _w32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _w32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _w32.CloseHandle.argtypes = [wintypes.HANDLE]
    _w32.FindFirstFileW.restype = wintypes.HANDLE
    _w32.FindFirstFileW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    _w32.FindNextFileW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _w32.FindClose.argtypes = [wintypes.HANDLE]
W32_INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF


def _find_verge_pipe():
    """枚举 \\\\.\\pipe\\ 找 verge-mihomo-* / *mihomo* 命名管道, 返回管道名."""
    names = _mihomo_pipes()
    return names[0] if names else None


def _mihomo_pipes():
    """枚举 \\\\.\\pipe\\ 中名字含 mihomo 的命名管道, verge-mihomo-* 排在前面."""
    if not IS_WIN:
        return []

    class FT(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    class FD(ctypes.Structure):
        _fields_ = [("attr", wintypes.DWORD), ("t", FT * 3),
                    ("szh", wintypes.DWORD), ("szl", wintypes.DWORD),
                    ("r0", wintypes.DWORD), ("r1", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 260), ("alt", wintypes.WCHAR * 14)]

    fd = FD()
    h = _w32.FindFirstFileW("\\\\.\\pipe\\*", ctypes.byref(fd))
    verge, other = [], []
    if h not in (None, W32_INVALID_HANDLE):
        while True:
            n = fd.name
            if n.startswith("verge-mihomo-"):
                verge.append(n)
            elif "mihomo" in n.lower():
                other.append(n)
            if not _w32.FindNextFileW(h, ctypes.byref(fd)):
                break
        _w32.FindClose(h)
    return verge + other


def _pipe_path(spec):
    """pipe:xxx -> \\\\.\\pipe\\xxx; pipe / pipe:auto 自动探测 mihomo 管道."""
    if not IS_WIN:
        raise RuntimeError("命名管道仅支持 Windows")
    n = spec[5:] if spec.startswith("pipe:") else spec
    if not n or n == "auto":
        n = _find_verge_pipe()
        if not n:
            raise RuntimeError("自动探测不到 mihomo 命名管道, 请使用 --api pipe:完整管道名")
    return n if n.startswith("\\\\") else "\\\\.\\pipe\\" + n


def _pipe_write(h, data):
    n = wintypes.DWORD()
    off = 0
    while off < len(data):
        if not _w32.WriteFile(h, data[off:], len(data) - off, ctypes.byref(n), None):
            raise OSError("命名管道写入失败 (err=%d)" % ctypes.get_last_error())
        off += n.value


def _pipe_call(name, req, stream=False):
    """打开管道 -> 写入 HTTP 请求; stream=False 读到关闭后返回全部字节."""
    h = _w32.CreateFileW(name, 0xC0000000, 0, None, 3, 0, None)
    if h in (None, W32_INVALID_HANDLE):
        raise OSError("无法打开命名管道 %s (err=%d)" % (name, ctypes.get_last_error()))
    try:
        _pipe_write(h, req)
        if stream:
            return h
        buf, out = ctypes.create_string_buffer(65536), bytearray()
        n = wintypes.DWORD()
        while _w32.ReadFile(h, buf, 65536, ctypes.byref(n), None) and n.value:
            out += buf.raw[:n.value]
        return bytes(out)
    except Exception:
        _w32.CloseHandle(h)
        raise


def _http_head(method, path, body, secret=None):
    h = (method + " " + path + " HTTP/1.1\r\nHost: localhost\r\n"
         "Authorization: Bearer " + (SECRET if secret is None else secret)
         + "\r\nAccept: application/json\r\n")
    if body is not None:
        h += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(body)
    return h + "Connection: close\r\n\r\n"


def _dechunk(buf):
    out = bytearray()
    while True:
        i = buf.find(b"\r\n")
        if i < 0:
            break
        try:
            n = int(buf[:i].split(b";")[0], 16)
        except ValueError:
            break
        if n == 0:
            break
        out += buf[i + 2:i + 2 + n]
        buf = buf[i + 2 + n + 2:]
    return bytes(out)


def _http_body(raw):
    """解析 HTTP 响应 (含 chunked), 返回 body bytes; 状态码 >=400 抛错."""
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise OSError("管道响应不是 HTTP")
    lines, p = head.split(b"\r\n"), head.split(b" ", 2)
    code = int(p[1]) if len(p) > 1 and p[1].isdigit() else 0
    if code >= 400:
        raise OSError("HTTP %d %s" % (code, body[:200].decode("utf-8", "replace")))
    hdr = {}
    for l in lines[1:]:
        k, _, v = l.partition(b":")
        hdr[k.strip().lower()] = v.strip()
    if b"chunked" in hdr.get(b"transfer-encoding", b""):
        body = _dechunk(body)
    elif b"content-length" in hdr:
        body = body[:int(hdr[b"content-length"])]
    return body


def pipe_api(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    raw = _pipe_call(API_PIPE, _http_head(method, path, body).encode() + (body or b""))
    body = _http_body(raw)
    return json.loads(body) if body.strip() else {}


class PipeReader:
    """命名管道上的 HTTP 流 (/logs /traffic): 自动解 chunked, 按行产出 bytes."""

    def __init__(self, h):
        self.h = h
        self.buf, self.lines = bytearray(), bytearray()
        self.chunked, self.cleft, self.remain, self.eof = False, -1, -1, False
        self._headers()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _fill(self):
        b = ctypes.create_string_buffer(65536)
        n = wintypes.DWORD()
        if not _w32.ReadFile(self.h, b, 65536, ctypes.byref(n), None) or not n.value:
            self.eof = True
            return False
        self.buf += b.raw[:n.value]
        return True

    def _headers(self):
        while b"\r\n\r\n" not in self.buf:
            if not self._fill():
                raise OSError("管道流无响应头")
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        hdr = {}
        for l in head.split(b"\r\n")[1:]:
            k, _, v = l.partition(b":")
            hdr[k.strip().lower()] = v.strip()
        self.chunked = b"chunked" in hdr.get(b"transfer-encoding", b"")
        if not self.chunked and b"content-length" in hdr:
            self.remain = int(hdr[b"content-length"])

    def _payload(self):
        """取下一段已解 chunk 的载荷, 流结束返回 b\"\"."""
        while not self.eof:
            if self.chunked:
                if self.cleft < 0:                      # 读 chunk 长度行
                    i = self.buf.find(b"\r\n")
                    if i < 0:
                        self._fill()
                        continue
                    try:
                        self.cleft = int(self.buf[:i].split(b";")[0], 16)
                    except ValueError:
                        self.eof = True
                        return b""
                    del self.buf[:i + 2]
                    if self.cleft == 0:
                        self.eof = True
                        return b""
                take = min(65536, self.cleft)
                if len(self.buf) < take + 2:            # 等数据 + 结尾 CRLF
                    self._fill()
                    continue
                out = bytes(self.buf[:take])
                del self.buf[:take + 2]
                self.cleft -= take
                if self.cleft == 0:
                    self.cleft = -1
                return out
            if self.remain == 0:
                self.eof = True
                return b""
            if not self.buf:
                self._fill()
                continue
            take = len(self.buf) if self.remain < 0 else min(len(self.buf), self.remain)
            out = bytes(self.buf[:take])
            del self.buf[:take]
            if self.remain > 0:
                self.remain -= take
            return out
        return b""

    def readline(self):
        while True:
            i = self.lines.find(b"\n")
            if i >= 0:
                out = bytes(self.lines[:i + 1])
                del self.lines[:i + 1]
                return out
            d = self._payload()
            if not d:
                out = bytes(self.lines)
                del self.lines[:]
                return out
            self.lines += d

    def __iter__(self):
        return self

    def __next__(self):
        l = self.readline()
        if l:
            return l
        raise StopIteration

    def close(self):
        try:
            _w32.CloseHandle(self.h)
        except Exception:
            pass
        self.eof = True


def open_api_stream(path, timeout=3600):
    """/logs /traffic 流接口: 返回可按行迭代 bytes、可作上下文管理器的流."""
    if API_PIPE:
        h = _pipe_call(API_PIPE, _http_head("GET", path, None).encode(), stream=True)
        return PipeReader(h)
    r = urllib.request.Request(API + path,
                               headers={"Authorization": "Bearer " + SECRET})
    return urllib.request.urlopen(r, timeout=timeout)


# ─── 控制器自动探测 (上次连接 / 前端与内核配置 / 常见端口) ───

COMMON_PORTS = (9097, 9090)
_FRONTEND_DIR = re.compile(r"verge|mihomo|clash", re.I)
_CFG_LINE = re.compile(r"^(external-controller|external-controller-pipe|secret)[ \t]*:[ \t]*(.*?)[ \t]*$")


def _yaml_scalar(v):
    """取 YAML 顶层标量的值: 处理引号与行尾注释."""
    if v[:1] == '"':
        m = re.match(r'"((?:[^"\\]|\\.)*)"', v)
        if m:
            try:
                return json.loads('"' + m.group(1) + '"')
            except ValueError:
                return m.group(1)
    elif v[:1] == "'":
        m = re.match(r"'((?:[^']|'')*)'", v)
        if m:
            return m.group(1).replace("''", "'")
    return re.sub(r"\s+#.*$", "", v).strip()


def read_controller_cfg(path):
    """读 mihomo 配置里的控制器字段 (只认顶层 key: value, 不依赖 PyYAML)."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _CFG_LINE.match(line)
                if m and m.group(1) not in out:
                    out[m.group(1)] = _yaml_scalar(m.group(2))
    except OSError:
        return {}
    return out


def config_files():
    """可能含控制器设置的配置文件: 本工具目录, 再是 Clash Verge / Mihomo Party 等前端的数据目录
    (按修改时间新→旧), 最后是常见内核目录. Windows/Linux/macOS 的数据根目录都会扫描."""
    home = os.path.expanduser("~")
    bases = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
             os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"),
             os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
             os.path.join(home, "Library", "Application Support")]
    dirs = []
    for b in bases:
        try:
            names = sorted(os.listdir(b)) if b else []
        except OSError:
            continue
        for n in names:
            if _FRONTEND_DIR.search(n):
                dirs.append(os.path.join(b, n))
    dirs += ["/etc/mihomo", "/etc/clash"]
    found = []
    for d in dirs:
        # clash-verge.yaml 是 Verge 合并后的运行时配置, 优先于 config.yaml
        for sub in ("", "work"):
            for name in ("clash-verge.yaml", "config.yaml", "config.yml"):
                p = os.path.join(d, sub, name)
                if os.path.isfile(p):
                    found.append(p)
    try:
        found.sort(key=lambda p: -os.path.getmtime(p))
    except OSError:
        pass
    return [CONFIG_FILE] + found if os.path.isfile(CONFIG_FILE) else found


def _controller_url(addr):
    """external-controller 的 host:port -> http 地址; 监听全部网卡的写法改连本机."""
    host, _, port = (addr or "").strip().rpartition(":")
    if not port.isdigit():
        return None
    host = host.strip("[]")
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    return "http://%s:%s" % ("[%s]" % host if ":" in host else host, port)


def _is_local(api):
    if api.startswith("pipe"):
        return True
    return (urllib.parse.urlsplit(api).hostname or "") in ("127.0.0.1", "localhost", "::1")


def _classify(status, body):
    """用 /version 的响应判断端点: ok 可用 / auth 是 mihomo 但需要(正确的)密钥 / down 其它."""
    if status == 200 and b'"version"' in body:
        return "ok"
    if status == 401 and b"Unauthorized" in body:
        return "auth"
    return "down"


def _probe(api, secret, timeout=1.5):
    try:
        if api.startswith("pipe"):
            if not IS_WIN:
                return "down"
            raw = _pipe_call(_pipe_path(api), _http_head("GET", "/version", None, secret).encode())
            head, _, body = raw.partition(b"\r\n\r\n")
            parts = head.split(b" ", 2)
            return _classify(int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0, body)
        u = urllib.parse.urlsplit(api)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
        try:
            c.request("GET", "/version", headers={"Authorization": "Bearer " + secret} if secret else {})
            r = c.getresponse()
            return _classify(r.status, r.read(4096))
        finally:
            c.close()
    except Exception:
        return "down"


def _probe_all(apis, secret=""):
    """并行探测, 避免 Windows 上连接未监听端口时逐个等待超时."""
    res = {}

    def run(a):
        res[a] = _probe(a, secret)

    ts = [threading.Thread(target=run, args=(a,), daemon=True) for a in apis]
    for t in ts:
        t.start()
    end = time.time() + 3
    while time.time() < end:
        # 按优先级排在前面的都已出结果且命中 ok, 后面的不必再等
        pre = [res.get(a) for a in apis]
        if None not in pre or "ok" in pre and None not in pre[:pre.index("ok")]:
            break
        time.sleep(0.01)
    return dict((a, res.get(a, "down")) for a in apis)


def _uniq(items):
    out = []
    for i in items:
        if i is not None and i not in out:
            out.append(i)
    return out


def resolve_controller(api=None, secret=None):
    """探测可用的控制器, 返回 (api, secret, 来源说明, 是否连通, 是否应缓存密钥).

    指定了 api 就只验证它; 否则依次尝试: 上次成功的连接 → 各前端/内核配置里的
    external-controller 与 external-controller-pipe → 常见端口.
    先不带密钥探测 (401 即可确认是 mihomo), 之后才尝试已知密钥, 避免把密钥发给无关的本地服务.
    配置里的密钥只会用于本机控制器. 全部失败时给出最佳猜测, 交由界面的离线重试页处理."""
    prefs = load_prefs()
    cache = prefs.get("controller") if isinstance(prefs.get("controller"), dict) else {}
    cfgs = [(p, read_controller_cfg(p)) for p in config_files()]
    cfgs = [(p, c) for p, c in cfgs if c]
    cfg_secrets = _uniq([c.get("secret") for _, c in cfgs if c.get("secret")])

    cands = []                                   # (地址, 首选密钥, 来源)
    if api:
        cands.append((api, None, "命令行/环境变量"))
    else:
        if cache.get("api"):
            cands.append((cache["api"], cache.get("secret"), "上次连接"))
        for p, c in cfgs:
            u = _controller_url(c.get("external-controller"))
            if u:
                cands.append((u, c.get("secret"), p))
        if IS_WIN:
            for p, c in cfgs:
                if c.get("external-controller-pipe"):
                    cands.append(("pipe:" + c["external-controller-pipe"], c.get("secret"), p))
            # 配置里的管道名可能过期 (Verge 服务模式实际用 verge-mihomo-production-*), 再看现存的管道
            for n in _mihomo_pipes():
                cands.append(("pipe:" + n, cfg_secrets[0] if cfg_secrets else None, "现存命名管道"))
        for port in COMMON_PORTS:
            cands.append(("http://127.0.0.1:%d" % port, None, "常见端口 %d" % port))

    status = _probe_all(_uniq([c[0] for c in cands]))
    need_auth = None
    done = set()
    for url, pref, src in cands:
        if url in done:
            continue
        done.add(url)
        st = status[url]
        if st == "ok":
            return url, secret or "", src, True, False
        if st != "auth":
            continue
        need_auth = need_auth or (url, src)
        pool = [secret, pref]
        if url == cache.get("api"):
            pool.append(cache.get("secret"))
        if _is_local(url):
            pool += cfg_secrets
        for s in _uniq(pool):
            if s and _probe(url, s) == "ok":
                return url, s, src, True, s == cache.get("secret") and s not in cfg_secrets

    if need_auth and sys.stdin.isatty():
        url, src = need_auth
        for _ in range(3):
            try:
                s = getpass.getpass("%s 需要 API 密钥 (config 里没有可用的), 输入后回车, 留空跳过: " % url)
            except (EOFError, KeyboardInterrupt):
                break
            if not s:
                break
            if _probe(url, s) == "ok":
                return url, s, src, True, True
            print("密钥不正确")
    cfg_urls = [c[0] for c in cands if c[2] in [p for p, _ in cfgs]]
    fallback = (need_auth[0] if need_auth else api) or (cfg_urls[0] if cfg_urls else "http://127.0.0.1:9097")
    guess = secret or (cfg_secrets[0] if cfg_secrets and _is_local(fallback) else "")
    return fallback, guess, "未连通", False, False


def setup_controller(args):
    """按命令行/环境变量 + 自动探测确定 API / API_PIPE / SECRET / START_GROUP."""
    global API, API_PIPE, SECRET, START_GROUP, CONTROLLER_DESC
    api = args.api
    if not api and (args.host or args.port):
        api = "http://%s:%d" % (args.host or "127.0.0.1", args.port or 9097)
    url, SECRET, src, ok, keep = resolve_controller(api.rstrip("/") if api else None, args.secret)
    API = url.rstrip("/")
    if API == "pipe" or API.startswith("pipe:"):
        try:
            API_PIPE = _pipe_path(API)
        except RuntimeError as e:
            sys.exit(str(e))
    CONTROLLER_DESC = "%s  [%s]" % (API_PIPE or API, src)
    START_GROUP = args.group
    if ok and not api:                           # 只记住自动探测到的结果, 手动指定的不覆盖
        rec = {"api": API}
        if keep:
            rec["secret"] = SECRET
        prefs = load_prefs()
        if prefs.get("controller") != rec:
            prefs["controller"] = rec
            try:
                save_prefs(prefs)
            except OSError:
                pass


# ───────────────────────────── 文本宽度 ─────────────────────────────

try:
    _wcwidth = ctypes.CDLL(None).wcwidth
    _wcwidth.argtypes = [ctypes.c_wchar]
    _wcwidth.restype = ctypes.c_int
except Exception:
    _wcwidth = None


@lru_cache(maxsize=8192)
def cw(ch):
    if _wcwidth is not None:
        r = _wcwidth(ch)
        if r >= 0:
            return r
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0 or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def tw(s):
    return sum(cw(c) for c in s)


def clip(s, w):
    s = str(s)
    if w <= 0:
        return ""
    if tw(s) <= w:
        return s
    out, used = [], 0
    for c in s:
        k = cw(c)
        if used + k > w - 1:
            break
        out.append(c)
        used += k
    return "".join(out) + "…"


def pad(s, w, align="<"):
    s = clip(s, w)
    gap = " " * max(0, w - tw(s))
    return s + gap if align == "<" else gap + s


def wrap(s, w):
    lines = []
    for para in str(s).split("\n"):
        cur, used = [], 0
        for c in para:
            k = cw(c)
            if used + k > w:
                lines.append("".join(cur))
                cur, used = [], 0
            cur.append(c)
            used += k
        lines.append("".join(cur))
    return lines


# ───────────────────────────── 绘制基础 ─────────────────────────────

# 名称 -> 256 色索引
PALETTE = {
    "default": -1, "white": 255, "black": 234,
    "dim": 245, "muted": 240,
    "border": 239, "accent": 75,
    "ok": 114, "warn": 221,
    "bad": 203, "purple": 141,
    "bg_header": 236, "bg_sel": 238,
    "bg_sel2": 235, "bg_btn": 238,
    "bg_accent": 75, "bg_ok": 71,
    "bg_warn": 179,
    "bg_bad": 167, "bg_modal": 235,
}


# ───── 终端层: Windows 用 Win32 控制台, 其它系统用 termios + ANSI/VT ─────

if IS_WIN:
    _k = ctypes.windll.kernel32               # 句柄可能为大整数, 必须显式声明原型
    _k.CreateFileW.restype = wintypes.HANDLE
    _k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                               wintypes.HANDLE]
    _k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k.GetNumberOfConsoleInputEvents.argtypes = [wintypes.HANDLE,
                                                 ctypes.POINTER(wintypes.DWORD)]
    _k.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _k.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k.CloseHandle.argtypes = [wintypes.HANDLE]
    _k.GetConsoleOutputCP.restype = wintypes.UINT
    _k.SetConsoleOutputCP.argtypes = [wintypes.UINT]

GENERIC_RW = 0x80000000 | 0x40000000          # GENERIC_READ | GENERIC_WRITE
FILE_SHARE_RW, OPEN_EXISTING = 0x3, 3
IN_MODE_KEEP = 0x0080 | 0x0010 | 0x0008       # EXTENDED | MOUSE_INPUT | WINDOW_INPUT
IN_MODE_DROP = 0x0040 | 0x0002 | 0x0004 | 0x0001  # QUICK_EDIT | LINE | ECHO | PROCESSED
OUT_VT = 0x0004                               # ENABLE_VIRTUAL_TERMINAL_PROCESSING
WAIT_OBJECT_0 = 0

EVT_KEY, EVT_MOUSE, EVT_RESIZE = 0x0001, 0x0002, 0x0004
MOUSE_MOVED, MOUSE_DBL, MOUSE_WHEELED = 0x0001, 0x0002, 0x0004
KS_ALT, KS_CTRL, KS_SHIFT = 0x0003, 0x000C, 0x0010


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwMousePosition", COORD), ("dwButtonState", wintypes.DWORD),
                ("dwControlKeyState", wintypes.DWORD), ("dwEventFlags", wintypes.DWORD)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


class WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
    _fields_ = [("dwSize", COORD)]


class EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD),
                ("WindowBufferSizeEvent", WINDOW_BUFFER_SIZE_RECORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT_UNION)]


# 虚拟键码 -> 键名
VK_NAME = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x24: "home",
           0x23: "end", 0x21: "pgup", 0x22: "pgdn", 0x2E: "delete", 0x0D: "enter",
           0x08: "backspace", 0x1B: "esc"}
FKEY_NAME = {0x70: "?", 0x74: "r"}            # F1 帮助 / F5 刷新

BLANK_CELL = (" ", ("default", "default", False))


class _TermBase:
    """替代 curses 的 screen 对象, 接口对齐: getmaxyx / erase / refresh / read.

    erase() 清空帧缓冲 -> 页面经 Canvas 写入 -> refresh() 与上帧逐格对比后
    仅重写变化的行段. read(ms) 等待输入并返回事件列表: 键名字符串,
    ("press",x,y) / ("dbl",x,y) / ("wheel",x,y,up) 鼠标元组, 或 "resize".
    平台差异 (初始化 / 窗口尺寸 / 输入) 由平台子类实现.
    """

    def __init__(self):
        self.out = None
        self.h = self.w = 0
        self.cells, self.prev = [], None
        self._sgr = {}

    # ---- 帧缓冲 ----
    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        h, w = self._winsize()
        if (h, w) != (self.h, self.w):
            self.h, self.w = h, w
            self.prev = None                        # 尺寸变化 -> 全量重绘
        self.cells = [[BLANK_CELL] * w for _ in range(h)]

    def draw_text(self, y, x, s, attr, room):
        """把已裁剪字符串写入第 y 行 x 列起的格子; 返回占用列数."""
        row, W = self.cells[y], self.w
        cx, used, last = x, 0, -1
        for ch in s:
            k = cw(ch)
            if k == 0:                              # 组合字符并入前一格
                if last >= 0:
                    row[last] = (row[last][0] + ch, attr)
                continue
            if used + k > room or cx + k > W:
                break
            row[cx] = (ch, attr)
            last = cx
            if k == 2 and cx + 1 < W:
                row[cx + 1] = (None, attr)          # 宽字符右半格占位
            cx += k
            used += k
        return used

    def refresh(self):
        prev = self.prev if self.prev and len(self.prev) == self.h else [None] * self.h
        out = ["\033[?2026h"]                      # 同步输出, 不支持的终端会忽略
        for y, cur in enumerate(self.cells):
            prv = prev[y]
            lo = hi = -1
            if prv is None:
                lo, hi = 0, self.w - 1
            else:
                for i in range(self.w):
                    if cur[i] != prv[i]:
                        if lo < 0:
                            lo = i
                        hi = i
            if lo < 0:
                continue
            out.append("\033[%d;%dH" % (y + 1, lo + 1))
            attr = None
            for i in range(lo, hi + 1):
                ch, a = cur[i]
                if ch is None:
                    continue
                if a != attr:
                    attr = a
                    out.append(self.sgr(a))
                out.append(ch)
        out.append("\033[0m\033[?2026l")
        self.out.write("".join(out))
        self.out.flush()
        self.prev = self.cells

    def sgr(self, attr):
        code = self._sgr.get(attr)
        if code is None:
            fg, bg, bold = attr
            f, b = PALETTE[fg], PALETTE[bg]
            s = "0;1" if bold else "0"
            s += ";39" if f < 0 else ";38;5;%d" % f
            s += ";49" if b < 0 else ";48;5;%d" % b
            code = self._sgr[attr] = "\033[%sm" % s
        return code


class _WinTerm(_TermBase):
    """Windows 实现: ReadConsoleInputW 读键盘/鼠标/缩放, CONOUT$ 写 VT 序列."""

    # ---- 生命周期 ----
    def setup(self):
        if not IS_WIN:                          # 仅在裁剪后的 tui-win.py 中可达
            raise OSError("tui-win.py 仅适用于 Windows, 其它平台请使用 tui.py")
        k32 = self.k32 = ctypes.windll.kernel32
        self.in_h = None
        self.old_in = self.old_out = self.old_cp = None
        self.out = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                        buffering=1, newline="")
        out_h = msvcrt.get_osfhandle(self.out.fileno())
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(out_h, ctypes.byref(mode)):
            raise OSError("CONOUT$ 不是控制台")
        self.old_out = mode.value
        k32.SetConsoleMode(out_h, mode.value | OUT_VT)
        self.in_h = k32.CreateFileW("CONIN$", GENERIC_RW, FILE_SHARE_RW,
                                    None, OPEN_EXISTING, 0, None)
        if self.in_h in (None, 0, -1, W32_INVALID_HANDLE):
            raise OSError("无法打开控制台输入 CONIN$")
        if not k32.GetConsoleMode(self.in_h, ctypes.byref(mode)):
            raise OSError("无法获取控制台输入模式")
        self.old_in = mode.value
        k32.SetConsoleMode(self.in_h, (mode.value | IN_MODE_KEEP) & ~IN_MODE_DROP)
        self.old_cp = k32.GetConsoleOutputCP()
        k32.SetConsoleOutputCP(65001)
        # 备用缓冲区 + 隐藏光标 + 窗口标题
        self.out.write("\033]0;mihomo TUI\007\033[?1049h\033[?25l\033[2J\033[H")
        self.out.flush()
        self.erase()

    def leave(self):
        try:
            if self.out:
                self.out.write("\033[0m\033[?25h\033[?1049l")
                self.out.flush()
        except Exception:
            pass
        try:
            if self.in_h and self.old_in is not None:
                self.k32.SetConsoleMode(self.in_h, self.old_in)
            if self.out:
                if self.old_out is not None:
                    self.k32.SetConsoleMode(msvcrt.get_osfhandle(self.out.fileno()),
                                            self.old_out)
                if self.old_cp:
                    self.k32.SetConsoleOutputCP(self.old_cp)
        except Exception:
            pass
        try:
            if self.in_h:
                self.k32.CloseHandle(self.in_h)
            if self.out:
                self.out.close()
        except Exception:
            pass

    def _winsize(self):
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if self.k32.GetConsoleScreenBufferInfo(
                msvcrt.get_osfhandle(self.out.fileno()), ctypes.byref(info)):
            w = info.srWindow
            return w.Bottom - w.Top + 1, w.Right - w.Left + 1
        try:                                        # 兜底: 标准输出尺寸
            s = os.get_terminal_size()
            return s.lines, s.columns
        except OSError:
            return 24, 80

    # ---- 输入 ----
    def read(self, ms):
        evs = []
        if self.k32.WaitForSingleObject(self.in_h, ms) != WAIT_OBJECT_0:
            return evs
        n = wintypes.DWORD()
        self.k32.GetNumberOfConsoleInputEvents(self.in_h, ctypes.byref(n))
        if not n.value:
            return evs
        buf = (INPUT_RECORD * n.value)()
        got = wintypes.DWORD()
        self.k32.ReadConsoleInputW(self.in_h, buf, n.value, ctypes.byref(got))
        for i in range(got.value):
            r = buf[i]
            if r.EventType == EVT_KEY:
                k = self._key(r.Event.KeyEvent)
                if k is not None:
                    evs.append(k)
            elif r.EventType == EVT_MOUSE:
                self._mouse(r.Event.MouseEvent, evs)
            elif r.EventType == EVT_RESIZE:
                evs.append("resize")
        return evs

    def _key(self, ke):
        if not ke.bKeyDown:
            return None
        ch, vk, st = ke.UnicodeChar, ke.wVirtualKeyCode, ke.dwControlKeyState
        if vk == 0x09:                              # Tab / Shift+Tab
            return "btab" if st & KS_SHIFT else "tab"
        if ch and ch != "\x00":                     # 特殊键的 UnicodeChar 为 NUL
            if ch == " ":
                return "space"
            if ch in "\r\n":
                return "enter"
            if ch == "\x1b":
                return "esc"
            if ch in "\x08\x7f":
                return "backspace"
            o = ord(ch)
            if o < 32:
                return "ctrl-" + chr(o + 96) if o else None
            # Ctrl+可打印字符不产普通键; AltGr 的 Ctrl+Alt 组合除外
            if st & KS_CTRL and not st & KS_ALT:
                return None
            return ch
        if vk in FKEY_NAME:
            return FKEY_NAME[vk]
        if vk in VK_NAME:
            return VK_NAME[vk]
        if 0x70 <= vk <= 0x87:
            return "f" + str(vk - 0x6F)
        return None

    def _mouse(self, me, evs):
        x, y = me.dwMousePosition.X, me.dwMousePosition.Y
        fl, bs = me.dwEventFlags, me.dwButtonState
        if fl & MOUSE_WHEELED:
            evs.append(("wheel", x, y, ctypes.c_short((bs >> 16) & 0xFFFF).value > 0))
        elif fl & MOUSE_DBL:
            if bs & 1:
                evs.append(("dbl", x, y))
        elif fl == 0 and bs & 1:
            evs.append(("press", x, y))


Term = _WinTerm


class Theme:
    """保持与原 curses 版相同的接口; 属性直接以 (fg, bg, bold) 元组存入格子."""

    def __init__(self):
        self.ok = True
        self.rich = True

    def attr(self, fg="default", bg="default", bold=False):
        return (fg, bg, bold)


class Hits:
    """每帧重建的鼠标命中区: 后添加的优先."""

    def __init__(self):
        self.clicks, self.wheels = [], []

    def clear(self):
        self.clicks, self.wheels = [], []

    def add(self, y, x, w, fn, h=1):
        if w > 0:
            self.clicks.append((y, x, y + h, x + w, fn))

    def wheel(self, y, x, h, w, fn):
        self.wheels.append((y, x, y + h, x + w, fn))

    @staticmethod
    def _find(items, y, x):
        for y0, x0, y1, x1, fn in reversed(items):
            if y0 <= y < y1 and x0 <= x < x1:
                return fn
        return None


class Canvas:
    def __init__(self, scr, theme):
        self.scr, self.t = scr, theme

    def size(self):
        return self.scr.getmaxyx()

    def put(self, y, x, s, fg="default", bg="default", bold=False, w=None):
        H, W = self.scr.getmaxyx()
        if y < 0 or y >= H or x < 0 or x >= W:
            return 0
        room = W - x if w is None else min(w, W - x)
        s = clip(s, room)
        if not s:
            return 0
        return self.scr.draw_text(y, x, s, self.t.attr(fg, bg, bold), room)

    def fill(self, y, x, w, bg="default"):
        if w <= 0:
            return
        H, W = self.scr.getmaxyx()
        if y < 0 or y >= H or x >= W:
            return
        cell = (" ", self.t.attr("default", bg))
        row = self.scr.cells[y]
        for i in range(max(0, x), min(W, x + w)):
            row[i] = cell

    def rect(self, y, x, h, w, bg):
        for i in range(h):
            self.fill(y + i, x, w, bg)

    def box(self, y, x, h, w, title="", focus=False, bg="default", right=""):
        if h < 2 or w < 4:
            return
        fg = "accent" if focus else "border"
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", fg, bg)
        for i in range(1, h - 1):
            self.put(y + i, x, "│", fg, bg)
            self.put(y + i, x + w - 1, "│", fg, bg)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", fg, bg)
        if title:
            self.put(y, x + 2, " " + clip(title, w - 8) + " ",
                     "accent" if focus else "white", bg, bold=True)
        if right:
            r = " " + clip(right, w // 2) + " "
            self.put(y, x + w - 2 - tw(r), r, "dim", bg)

    def hline(self, y, x, w, fg="border", bg="default"):
        self.put(y, x, "─" * max(0, w), fg, bg)


class ListView:
    """可滚动列表: 键盘选择 + 滚轮 + 点击 + 滚动条."""

    def __init__(self, ih=1):
        self.sel, self.top, self.ih, self.rows = 0, 0, ih, 1
        self._wheel = False

    def move(self, d, n):
        if n:
            self.sel = max(0, min(n - 1, self.sel + d))

    def page(self, d, n):
        self.move(d * max(1, self.rows - 1), n)

    def home(self):
        self.sel = 0

    def end(self, n):
        self.sel = max(0, n - 1)

    def scroll(self, d):
        self.top += d
        self._wheel = True

    def layout(self, n, rows):
        self.rows = rows
        maxtop = max(0, n - rows)
        if self._wheel:
            self._wheel = False
            self.top = max(0, min(self.top, maxtop))
            if n:
                self.sel = max(self.top, min(self.sel, self.top + rows - 1, n - 1))
        self.sel = max(0, min(self.sel, n - 1)) if n else 0
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        self.top = max(0, min(self.top, maxtop))

    def draw(self, app, y, x, h, w, n, item, click=None, sb_x=None, empty=""):
        rows = max(1, h // self.ih)
        self.layout(n, rows)
        app.hits.wheel(y, x, h, (sb_x - x + 1) if sb_x is not None else w,
                       lambda d: self.scroll(d))
        if not n and empty:
            app.c.put(y + h // 2 - 1, x + max(0, (w - tw(empty)) // 2), empty, "muted")
        for i in range(self.top, min(n, self.top + rows)):
            yy = y + (i - self.top) * self.ih
            if click:
                app.hits.add(yy, x, w, (lambda i=i: click(i)), self.ih)
            item(i, yy, i == self.sel)
        if sb_x is not None and n > rows and h > 2:
            th = max(1, h * rows // n)
            ty = y + (h - th) * self.top // max(1, n - rows)
            for i in range(th):
                app.c.put(ty + i, sb_x, "┃", "accent")


def layout_cols(cols, width):
    """cols: [(key, title, fixed_or_None, flex, align, prio)] -> [(col, x, w)]; 窄屏时按 prio 丢列."""
    active = list(cols)
    while True:
        fixed = sum(c[2] or 6 for c in active) + len(active) - 1
        if fixed <= width or len(active) <= 1:
            break
        active.remove(max(active, key=lambda c: c[5]))
    spare = max(0, width - (sum(c[2] or 0 for c in active) + len(active) - 1))
    flex = sum(c[3] for c in active if not c[2]) or 1
    out, x = [], 0
    flexcols = [c for c in active if not c[2]]
    for c in active:
        if c[2]:
            w = c[2]
        else:
            w = spare * c[3] // flex
            if c is flexcols[-1]:
                w = spare - sum(spare * f[3] // flex for f in flexcols[:-1])
        out.append((c, x, w))
        x += w + 1
    return out


# ───────────────────────────── 弹窗 ─────────────────────────────

class Modal:
    width = 56

    def frame(self, app, h, title):
        H, W = app.c.size()
        w = min(W - 4, self.width)
        h = min(H - 2, h)
        y, x = (H - h) // 2, (W - w) // 2
        app.c.rect(y, x, h, w, "bg_modal")
        app.c.box(y, x, h, w, title, True, "bg_modal")
        app.hits.add(y, x, w, lambda: None, h)  # 吞掉弹窗内空白处的点击
        return y, x, h, w

    def buttons(self, app, y, x, w, specs):
        """specs: [(label, fn, kind)] 右对齐."""
        total = sum(tw(l) + 2 for l, _, _ in specs) + len(specs) - 1
        bx = x + w - 2 - total
        for label, fn, kind in specs:
            bx += app.button(y, bx, label, fn, kind=kind) + 1

    def key(self, app, k):
        if k == "esc":
            app.close_modal()


class ConfirmModal(Modal):
    def __init__(self, title, text, on_yes, yes="确定", danger=False):
        self.title, self.text, self.on_yes, self.yes, self.danger = title, text, on_yes, yes, danger

    def draw(self, app):
        lines = wrap(self.text, min(app.c.size()[1] - 8, self.width - 4))
        y, x, h, w = self.frame(app, len(lines) + 5, self.title)
        for i, l in enumerate(lines):
            app.c.put(y + 1 + i, x + 2, l, "white", "bg_modal")
        self.buttons(app, y + h - 2, x, w, [("取消", app.close_modal, "normal"),
                                             (self.yes, self.ok, "danger" if self.danger else "primary")])

    def ok(self):
        app_close = self.on_yes
        APP.close_modal()
        app_close()

    def key(self, app, k):
        if k in ("enter", "y", "Y"):
            self.ok()
        elif k in ("esc", "n", "N", "q"):
            app.close_modal()


class InputModal(Modal):
    def __init__(self, title, value, on_submit, hint="", parse=None):
        self.title, self.buf, self.on_submit, self.hint = title, list(str(value)), on_submit, hint
        self.cur, self.parse, self.err = len(self.buf), parse, ""

    def draw(self, app):
        y, x, h, w = self.frame(app, 8, self.title)
        c = app.c
        fw = w - 4
        c.put(y + 1, x + 2, clip(self.hint, fw), "dim", "bg_modal")
        c.fill(y + 3, x + 2, fw, "bg_sel")
        text = "".join(self.buf)
        before = "".join(self.buf[:self.cur])
        off = 0
        while tw(before[off:]) > fw - 2:
            off += 1
        vis = text[off:]
        cx = x + 3 + tw(before[off:])
        c.put(y + 3, x + 3, clip(vis, fw - 2) if tw(vis) > fw - 2 else vis, "white", "bg_sel")
        ch = self.buf[self.cur] if self.cur < len(self.buf) else " "
        c.put(y + 3, cx, ch, "black", "bg_accent")
        app.hits.add(y + 3, x + 2, fw, lambda: None)
        if self.err:
            c.put(y + 4, x + 2, "✗ " + self.err, "bad", "bg_modal", w=fw)
        self.buttons(app, y + h - 2, x, w, [("取消", app.close_modal, "normal"),
                                             ("确定", self.submit, "primary")])

    def submit(self):
        text = "".join(self.buf).strip()
        try:
            val = self.parse(text) if self.parse else text
        except ValueError as e:
            self.err = str(e) or "输入无效"
            return
        APP.close_modal()
        self.on_submit(val)

    def key(self, app, k):
        b = self.buf
        if k == "esc":
            app.close_modal()
            return
        if k == "enter":
            self.submit()
            return
        if k == "backspace":
            if self.cur:
                del b[self.cur - 1]
                self.cur -= 1
        elif k == "delete":
            if self.cur < len(b):
                del b[self.cur]
        elif k == "left":
            self.cur = max(0, self.cur - 1)
        elif k == "right":
            self.cur = min(len(b), self.cur + 1)
        elif k in ("home", "ctrl-a"):
            self.cur = 0
        elif k in ("end", "ctrl-e"):
            self.cur = len(b)
        elif k == "ctrl-u":
            del b[:self.cur]
            self.cur = 0
        elif k == "space":
            b.insert(self.cur, " ")
            self.cur += 1
        elif isinstance(k, str) and len(k) == 1:
            b.insert(self.cur, k)
            self.cur += 1
        else:
            return
        self.err = ""


class SelectModal(Modal):
    width = 40

    def __init__(self, title, options, current, on_pick):
        self.title, self.options, self.on_pick = title, options, on_pick
        self.lv = ListView()
        vals = [v for _, v in options]
        self.lv.sel = vals.index(current) if current in vals else 0
        self.current = current

    def draw(self, app):
        n = len(self.options)
        y, x, h, w = self.frame(app, min(n, 14) + 4, self.title)

        def item(i, yy, sel):
            label, val = self.options[i]
            bg = "bg_sel" if sel else "bg_modal"
            app.c.fill(yy, x + 1, w - 3, bg)
            app.c.put(yy, x + 2, "●" if val == self.current else "○",
                      "ok" if val == self.current else "muted", bg)
            app.c.put(yy, x + 4, label, "white", bg, bold=sel, w=w - 7)
        self.lv.draw(app, y + 2, x + 1, h - 4, w - 3, n, item, click=self.pick, sb_x=x + w - 1)

    def pick(self, i):
        APP.close_modal()
        self.on_pick(self.options[i][1])

    def key(self, app, k):
        n = len(self.options)
        if k in ("up", "k"):
            self.lv.move(-1, n)
        elif k in ("down", "j"):
            self.lv.move(1, n)
        elif k in ("enter", "space"):
            self.pick(self.lv.sel)
        elif k in ("esc", "q"):
            app.close_modal()


class InfoModal(Modal):
    width = 88

    def __init__(self, title, rows, extra=None, keymap=None):
        """rows: [(key, value)] 或 [str]; extra: 附加按钮 [(label, fn, kind)]; keymap: {键: fn}."""
        self.title, self.rows, self.top = title, rows, 0
        self.extra, self.keymap = extra or [], keymap or {}

    def lines(self, w):
        out = []
        kw = max([tw(r[0]) for r in self.rows if isinstance(r, tuple)] or [0]) + 2
        for r in self.rows:
            if isinstance(r, tuple):
                for i, l in enumerate(wrap(r[1], max(10, w - kw))):
                    out.append((r[0] if i == 0 else "", l, kw))
            else:
                for l in wrap(r, w):
                    out.append(("", l, 0))
        return out

    def draw(self, app):
        H, W = app.c.size()
        w = min(W - 4, self.width)
        lines = self.lines(w - 5)
        y, x, h, w = self.frame(app, len(lines) + 4, self.title)
        vis = h - 4
        self.top = max(0, min(self.top, len(lines) - vis))
        app.hits.wheel(y, x, h, w, self.scroll)
        for i, (k, v, kw) in enumerate(lines[self.top:self.top + vis]):
            if kw:
                app.c.put(y + 1 + i, x + 2, k, "dim", "bg_modal")
            app.c.put(y + 1 + i, x + 2 + kw, v, "white", "bg_modal", w=w - 4 - kw)
        if len(lines) > vis:
            app.c.put(y + h - 2, x + 2, f"{self.top + 1}-{self.top + vis}/{len(lines)}  滚轮/↑↓ 翻看",
                      "muted", "bg_modal")
        extra = [(l, (lambda f=f: (app.close_modal(), f())), k) for l, f, k in self.extra]
        self.buttons(app, y + h - 2, x, w, extra + [("关闭", app.close_modal, "primary")])

    def scroll(self, d):
        self.top = max(0, self.top + d)

    def key(self, app, k):
        if k in self.keymap:
            app.close_modal()
            self.keymap[k]()
        elif k in ("up", "k"):
            self.scroll(-1)
        elif k in ("down", "j"):
            self.scroll(1)
        elif k == "pgup":
            self.scroll(-10)
        elif k == "pgdn":
            self.scroll(10)
        elif k in ("esc", "enter", "q", "space"):
            app.close_modal()


HELP = [
    ("全局", "1-5 切换页面   ? 帮助   q 退出   r 刷新   鼠标点击 / 滚轮均可用"),
    ("", "←/Esc 逐层返回, 直到进入最左侧的页面导航 (↑↓ 选择页面, ⏎ 进入)"),
    ("", "m 切换代理模式   T 开关 TUN   a 开关局域网共享   u 更新订阅   R 重启内核"),
    ("代理", "←→/hl 在代理组与节点间切换焦点   ↑↓/jk 移动   ⏎ 切换节点 (单击节点同效)"),
    ("", "Tab / [ ] 上下一个代理组   d 测速当前组   D 测速选中节点 (点延迟列同效)"),
    ("", "t 经代理端口连通测试   s 排序   o 定位当前节点   / 筛选   Esc 清除筛选"),
    ("连接", "单击或 ⏎ 查看详情 (可从中断开)   x 断开选中   X 断开全部   p 暂停刷新   s 排序   / 筛选"),
    ("规则", "Tab 切换 规则 / 规则集   单击或 ⏎ 查看详情   规则集详情中可更新   / 筛选"),
    ("日志", "←→ 调整日志等级   p 暂停   c 清空   G 跟随最新   ⏎ 查看完整内容"),
    ("设置", "←→/hl 在分组与条目间切换   ⏎ 进入选项   ←→ 预览取值 (黄色为未生效)   ⏎ 生效   Esc 取消"),
    ("", "鼠标单击条目或控件即生效/打开; 开启「修改写回配置文件」后, 修改会同步写入 config.yaml"),
]


# ───────────────────────────── 页面 ─────────────────────────────

class Page:
    title = ""
    hints = []

    def __init__(self, app):
        self.app = app

    def on_show(self):
        pass

    def tick(self):
        pass

    def draw(self, y, x, h, w):
        pass

    def key(self, k):
        return False

    def ask_filter(self):
        self.app.modal(InputModal("筛选", self.filter, self.set_filter,
                                  "输入关键字 (不区分大小写), 留空表示清除"))

    def set_filter(self, v):
        self.filter = v


def match(text, flt):
    return not flt or flt.lower() in text.lower()


class ProxiesPage(Page):
    title = "代理"
    hints = [("⏎", "切换"), ("d", "测速"), ("t", "连通"), ("Tab", "下一组"), ("/", "筛选"), ("s", "排序")]
    SORTS = ["默认", "延迟", "名称"]

    def __init__(self, app):
        super().__init__(app)
        self.focus = 1
        self.glist, self.nlist = ListView(2), ListView()
        self.gname, self.nname = None, None
        self.filter, self.sort = "", 0

    def groups(self):
        return self.app.groups()

    def info(self):
        return self.app.proxies.get(self.gname, {}) if self.app.proxies else {}

    def nodes(self):
        a = self.app
        ns = [n for n in self.info().get("all") or [] if match(n, self.filter)]
        if self.sort == 1:
            def k(n):
                d = a.delays.get(n)
                return (0, d) if isinstance(d, int) else (1 if d == "timeout" else 2, 0)
            ns.sort(key=k)
        elif self.sort == 2:
            ns.sort(key=str.lower)
        return ns

    def sync(self):
        gs = self.groups()
        if not gs:
            return gs, []
        if self.gname not in gs:
            self.select_group(self.default_group(gs))
        self.glist.sel = gs.index(self.gname)
        ns = self.nodes()
        if self.nname in ns:
            self.nlist.sel = ns.index(self.nname)
        elif ns:
            self.nlist.sel = min(self.nlist.sel, len(ns) - 1)
            self.nname = ns[self.nlist.sel]
        return gs, ns

    def default_group(self, gs):
        """命令行指定的组 > 排序最靠前的手选组 (规则模式下即主代理组) > 第一个组."""
        if START_GROUP in gs:
            return START_GROUP
        px = self.app.proxies
        for g in gs:
            if px.get(g, {}).get("type") == "Selector":
                return g
        return gs[0]

    def select_group(self, g):
        self.gname = g
        self.nname = self.app.proxies.get(g, {}).get("now")
        self.nlist.top = 0

    def step_group(self, d):
        gs = self.groups()
        if gs:
            self.select_group(gs[(gs.index(self.gname) + d) % len(gs)] if self.gname in gs else gs[0])

    def draw(self, y, x, h, w):
        a, c = self.app, self.app.c
        gs, ns = self.sync()
        if not gs:
            c.put(y + 2, x + 2, "没有可用的代理组", "muted")
            return
        gw = max(22, min(34, w // 3))
        content_focus = not a.nav_focus
        c.box(y, x, h, gw, "代理组", content_focus and self.focus == 0, right=str(len(gs)))

        def gitem(i, yy, sel):
            g = gs[i]
            p = a.proxies.get(g, {})
            iw = gw - 3
            bg = ("bg_sel" if content_focus and self.focus == 0 else "bg_sel2") if sel else "default"
            c.fill(yy, x + 1, iw, bg)
            c.fill(yy + 1, x + 1, iw, bg)
            if sel:
                c.put(yy, x + 1, "▌", "accent", bg)
                c.put(yy + 1, x + 1, "▌", "accent", bg)
            tag = GROUP_TAG.get(p.get("type"), p.get("type", ""))
            c.put(yy, x + 3, pad(g, iw - 4 - tw(tag)), "white" if sel else "default", bg, bold=sel)
            c.put(yy, x + iw - tw(tag), tag, "purple" if p.get("type") != "Selector" else "dim", bg)
            c.put(yy + 1, x + 3, "› " + p.get("now", ""), "accent" if sel else "muted", bg, w=iw - 3)
        self.glist.draw(a, y + 1, x + 1, h - 2, gw - 3, len(gs), gitem,
                        click=self.click_group, sb_x=x + gw - 1)

        nx, nw = x + gw + 1, w - gw - 1
        info = self.info()
        now, typ = info.get("now", ""), info.get("type", "")
        allc = len(info.get("all") or [])
        c.box(y, nx, h, nw, self.gname, content_focus and self.focus == 1,
              right=f"{GROUP_TAG.get(typ, typ)} · {len(ns)}/{allc} 个节点")
        ix, iw = nx + 2, nw - 4
        k = c.put(y + 1, ix, "当前  ", "dim")
        k += c.put(y + 1, ix + k, "● ", "ok")
        k += c.put(y + 1, ix + k, now, "ok", bold=True, w=iw - k - 12)
        dt, dfg = a.delay_view(now)
        c.put(y + 1, ix + k + 2, dt, dfg)

        bx, by, maxx = ix, y + 2, ix + iw
        for label, fn, key in (("测速", self.test_all, "d"), ("连通测试", a.conn_test, "t"),
                               ("定位当前", self.locate, "o"),
                               ("排序·" + self.SORTS[self.sort], self.cycle_sort, "s"),
                               ("筛选" + ("·" + self.filter if self.filter else ""), self.ask_filter, "/")):
            bw = a.button(by, bx, label, fn, key=key, maxx=maxx,
                          kind="on" if (key == "/" and self.filter) or (key == "s" and self.sort) else "normal")
            bx += bw + 1 if bw else 0
        c.hline(y + 3, nx + 1, nw - 2)

        showtype = iw >= 46
        namew = iw - 2 - 9 - (11 if showtype else 0)
        c.put(y + 4, ix + 2, pad("节点", namew), "muted")
        if showtype:
            c.put(y + 4, ix + 2 + namew + 1, "类型", "muted")
        c.put(y + 4, ix + iw - 8, pad("延迟", 8, ">"), "muted")

        def nitem(i, yy, sel):
            n = ns[i]
            p = a.proxies.get(n, {})
            bg = ("bg_sel" if content_focus and self.focus == 1 else "bg_sel2") if sel else "default"
            c.fill(yy, nx + 1, nw - 3, bg)
            if sel:
                c.put(yy, nx + 1, "▌", "accent", bg)
            cur = n == now
            c.put(yy, ix, "●" if cur else " ", "ok", bg)
            c.put(yy, ix + 2, pad(n, namew), "ok" if cur else ("white" if sel else "default"), bg,
                  bold=cur or sel)
            if showtype:
                t = p.get("type", "")
                c.put(yy, ix + 2 + namew + 1, pad(t, 10),
                      "purple" if t in GROUP_TYPES else "dim", bg)
            dtx, dfg = a.delay_view(n)
            c.put(yy, ix + iw - 8, pad(dtx, 8, ">"), dfg, bg)
            a.hits.add(yy, ix + iw - 8, 8, lambda n=n: self.test_nodes([n]))
        self.nlist.draw(a, y + 5, nx + 1, h - 6, nw - 3, len(ns), nitem,
                        click=self.click_node, sb_x=nx + nw - 1,
                        empty="没有匹配的节点" if self.filter else "该组没有节点")

    def click_group(self, i):
        self.focus = 0
        self.select_group(self.groups()[i])

    def click_node(self, i):
        self.focus = 1
        ns = self.nodes()
        self.nlist.sel, self.nname = i, ns[i]
        self.switch(ns[i])

    def switch(self, node):
        a, g = self.app, self.gname
        info = self.info()
        if info.get("type") != "Selector":
            a.toast(f"「{g}」是{GROUP_TAG.get(info.get('type'), '自动')}组, 由内核自动选择", "warn")
            return
        if node == info.get("now"):
            a.toast(f"{g} 已在使用 {node}", "info")
            return

        def run():
            api("PUT", "/proxies/" + q(g), {"name": node})
            closed = 0
            if a.prefs.get("close_on_switch"):
                for cn in api("GET", "/connections").get("connections") or []:
                    if g in (cn.get("chains") or []):
                        api("DELETE", "/connections/" + cn["id"])
                        closed += 1
            return closed

        def done(ok, res):
            if ok:
                a.proxies.setdefault(g, {})["now"] = node
                a.toast(f"{g} → {node}" + (f" · 已断开 {res} 个旧连接" if res else ""), "ok")
            else:
                a.toast(f"切换失败: {res}", "bad")
        a.task("switch", "切换节点", run, done)

    def test_all(self):
        self.test_nodes(self.nodes(), f"测速 {self.gname}")

    def test_nodes(self, ns, label="测速"):
        a = self.app
        ns = [n for n in ns if a.proxies.get(n, {}).get("type") not in ("Direct", "Reject", "RejectDrop", "Pass")]
        if not ns:
            return
        a.testing.update(ns)
        total, done = len(ns), [0]
        url, tmo = a.prefs["delay_url"], int(a.prefs["delay_timeout"])

        def one(n):
            try:
                d = api("GET", f"/proxies/{q(n)}/delay?url={q(url)}&timeout={tmo}",
                        timeout=tmo / 1000 + 3).get("delay")
                r = d if isinstance(d, int) and d > 0 else "timeout"
            except Exception:
                r = "timeout"

            def apply():
                a.delays[n] = r
                a.testing.discard(n)
                done[0] += 1
                a.progress["delay"] = f"{done[0]}/{total}"
            a.ui(apply)
            return r

        def run():
            with ThreadPoolExecutor(max_workers=max(1, int(a.prefs["concurrency"]))) as ex:
                res = list(ex.map(one, ns))
            return sum(1 for r in res if isinstance(r, int)), total

        def fin(ok, res):
            a.testing.difference_update(ns)
            if ok and total > 1:
                a.toast(f"测速完成: {res[0]}/{res[1]} 可用", "ok" if res[0] else "warn")
            elif ok:
                d = a.delays.get(ns[0])
                a.toast(f"{ns[0]}: " + (f"{d} ms" if isinstance(d, int) else "超时"),
                        "ok" if isinstance(d, int) else "bad")
        a.task("delay", label, run, fin)

    def locate(self):
        now = self.info().get("now")
        self.filter = "" if now and now not in self.nodes() else self.filter
        self.nname, self.focus = now, 1

    def cycle_sort(self):
        self.sort = (self.sort + 1) % len(self.SORTS)
        self.app.toast("排序: " + self.SORTS[self.sort], "info")

    def key(self, k):
        gs, ns = self.sync()
        lst, n = (self.glist, len(gs)) if self.focus == 0 else (self.nlist, len(ns))
        if k in ("up", "k", "down", "j", "pgup", "pgdn", "home", "end", "g", "G"):
            if k in ("up", "k"):
                lst.move(-1, n)
            elif k in ("down", "j"):
                lst.move(1, n)
            elif k in ("pgup", "pgdn"):
                lst.page(-1 if k == "pgup" else 1, n)
            elif k in ("home", "g"):
                lst.home()
            else:
                lst.end(n)
            if self.focus == 0 and gs:
                self.select_group(gs[lst.sel])
            elif ns:
                self.nname = ns[lst.sel]
        elif k in ("left", "h"):
            if self.focus == 0:
                return False  # 交给上层: 进入页面导航
            self.focus = 0
        elif k in ("right", "l"):
            self.focus = 1
        elif k in ("enter", "space"):
            if self.focus == 0:
                self.focus = 1
            elif ns:
                self.switch(ns[self.nlist.sel])
        elif k in ("tab", "]"):
            self.step_group(1)
        elif k in ("btab", "["):
            self.step_group(-1)
        elif k == "d":
            self.test_all()
        elif k == "D" and ns:
            self.test_nodes([ns[self.nlist.sel]])
        elif k == "t":
            self.app.conn_test()
        elif k == "s":
            self.cycle_sort()
        elif k == "o":
            self.locate()
        elif k == "/":
            self.ask_filter()
        elif k == "esc" and self.filter:
            self.filter = ""
        else:
            return False
        return True


class ConnectionsPage(Page):
    title = "连接"
    hints = [("⏎", "详情"), ("x", "断开"), ("X", "全部断开"), ("p", "暂停"), ("/", "筛选"), ("s", "排序")]
    SORTS = ["最新", "下载速度", "总流量"]
    COLS = [("host", "目标", None, 5, "<", 0), ("net", "网络", 4, 0, "<", 4),
            ("rule", "规则", None, 3, "<", 3), ("chain", "链路", None, 4, "<", 2),
            ("dl", "↓ 速度", 10, 0, ">", 1), ("ul", "↑ 速度", 10, 0, ">", 5),
            ("total", "↓ 总量", 9, 0, ">", 6), ("dur", "时长", 7, 0, ">", 7)]

    def __init__(self, app):
        super().__init__(app)
        self.lv = ListView()
        self.conns, self.prev, self.speed = [], {}, {}
        self.paused, self.filter, self.sort = False, "", 0
        self.inflight, self.last, self.selid = False, 0, None
        self.totals = (0, 0)

    def on_show(self):
        self.last = 0

    def tick(self):
        if self.app.page is not self or self.paused or self.inflight or time.time() - self.last < 1:
            return
        self.inflight, self.last = True, time.time()

        def run():
            try:
                d = api("GET", "/connections", timeout=5)
                self.app.ui(lambda: self.apply(d))
            except Exception:
                pass
            finally:
                self.inflight = False
        threading.Thread(target=run, daemon=True).start()

    def apply(self, d):
        now = time.time()
        conns = d.get("connections") or []
        prev, self.prev = self.prev, {}
        for cn in conns:
            p = prev.get(cn["id"])
            if p:
                dt = max(0.2, now - p[2])
                self.speed[cn["id"]] = (max(0, (cn.get("upload", 0) - p[0]) / dt),
                                        max(0, (cn.get("download", 0) - p[1]) / dt))
            self.prev[cn["id"]] = (cn.get("upload", 0), cn.get("download", 0), now)
            cn["_start"] = parse_time(cn.get("start", "")) or now
        self.speed = {k: v for k, v in self.speed.items() if k in self.prev}
        self.conns = conns
        self.totals = (d.get("uploadTotal", 0), d.get("downloadTotal", 0))

    def row(self, cn):
        m = cn.get("metadata", {})
        host = m.get("host") or m.get("destinationIP", "")
        port = m.get("destinationPort", "")
        rule = cn.get("rule", "") + (f"({cn['rulePayload']})" if cn.get("rulePayload") else "")
        ul, dl = self.speed.get(cn["id"], (0, 0))
        return {"host": f"{host}:{port}" if port else host, "net": m.get("network", ""),
                "rule": rule, "chain": " › ".join(reversed(cn.get("chains") or [])),
                "dl": fmt_speed(dl) if dl else "-", "ul": fmt_speed(ul) if ul else "-",
                "total": fmt_bytes(cn.get("download", 0)),
                "dur": fmt_dur(time.time() - cn.get("_start", time.time())), "_dl": dl}

    def view(self):
        rows = []
        for cn in self.conns:
            r = self.row(cn)
            m = cn.get("metadata", {})
            if match(" ".join([r["host"], r["rule"], r["chain"], m.get("process", ""),
                               m.get("sourceIP", "")]), self.filter):
                rows.append((cn, r))
        if self.sort == 0:
            rows.sort(key=lambda t: -t[0].get("_start", 0))
        elif self.sort == 1:
            rows.sort(key=lambda t: -t[1]["_dl"])
        else:
            rows.sort(key=lambda t: -t[0].get("download", 0))
        return rows

    def draw(self, y, x, h, w):
        a, c = self.app, self.app.c
        rows = self.view()
        ids = [cn["id"] for cn, _ in rows]
        if self.selid in ids:
            self.lv.sel = ids.index(self.selid)
        c.box(y, x, h, w, "活动连接", True,
              right=f"{len(self.conns)} 个 · 累计 ↓ {fmt_bytes(self.totals[1])}  ↑ {fmt_bytes(self.totals[0])}")
        ix, iw = x + 2, w - 4
        bx, maxx = ix, ix + iw
        for label, fn, key, on in (("断开选中", self.close_sel, "x", False),
                                   ("全部断开", self.close_all, "X", False),
                                   ("继续" if self.paused else "暂停", self.toggle_pause, "p", self.paused),
                                   ("排序·" + self.SORTS[self.sort], self.cycle_sort, "s", self.sort),
                                   ("筛选" + ("·" + self.filter if self.filter else ""),
                                    self.ask_filter, "/", bool(self.filter))):
            bw = a.button(y + 1, bx, label, fn, key=key, maxx=maxx, kind="on" if on else "normal")
            bx += bw + 1 if bw else 0
        c.hline(y + 2, x + 1, w - 2)
        cols = layout_cols(self.COLS, iw - 1)
        for col, cx, cwid in cols:
            c.put(y + 3, ix + cx, pad(col[1], cwid, col[4]), "muted")

        def item(i, yy, sel):
            cn, r = rows[i]
            bg = "bg_sel" if sel else "default"
            c.fill(yy, x + 1, w - 3, bg)
            if sel:
                c.put(yy, x + 1, "▌", "accent", bg)
            for col, cx, cwid in cols:
                k = col[0]
                fg = {"host": "white", "net": "dim", "rule": "purple", "chain": "accent",
                      "dl": "ok" if r["_dl"] > 0 else "muted", "ul": "dim", "total": "dim",
                      "dur": "dim"}[k]
                c.put(yy, ix + cx, pad(r[k], cwid, col[4]), fg, bg, bold=sel and k == "host")
        self.lv.draw(a, y + 4, x + 1, h - 5, w - 3, len(rows), item, click=self.click,
                     sb_x=x + w - 1, empty="暂无活动连接" if not self.filter else "没有匹配的连接")
        if rows:
            self.selid = rows[self.lv.sel][0]["id"]

    def click(self, i):
        rows = self.view()
        self.lv.sel, self.selid = i, rows[i][0]["id"]
        self.detail()

    def current(self):
        for cn in self.conns:
            if cn["id"] == self.selid:
                return cn
        return None

    def detail(self):
        cn = self.current()
        if not cn:
            return
        m = cn.get("metadata", {})
        ul, dl = self.speed.get(cn["id"], (0, 0))
        rows = [("目标", f"{m.get('host') or '-'}  ({m.get('destinationIP', '')}:{m.get('destinationPort', '')})"),
                ("来源", f"{m.get('sourceIP', '')}:{m.get('sourcePort', '')}  {m.get('type', '')}/{m.get('network', '')}"),
                ("进程", m.get("process") or m.get("processPath") or "-"),
                ("规则", cn.get("rule", "") + (f" ({cn['rulePayload']})" if cn.get("rulePayload") else "")),
                ("链路", " › ".join(reversed(cn.get("chains") or []))),
                ("流量", f"↓ {fmt_bytes(cn.get('download'))}  ↑ {fmt_bytes(cn.get('upload'))}"),
                ("速度", f"↓ {fmt_speed(dl)}  ↑ {fmt_speed(ul)}"),
                ("开始", cn.get("start", "")[:19].replace("T", " ")),
                ("嗅探", m.get("sniffHost") or "-"), ("ID", cn["id"])]
        self.app.modal(InfoModal("连接详情", rows,
                                 extra=[("断开此连接", lambda cid=cn["id"], h=self.row(cn)["host"]:
                                         self.close_conn(cid, h), "danger")],
                                 keymap={"x": lambda cid=cn["id"], h=self.row(cn)["host"]:
                                         self.close_conn(cid, h)}))

    def close_conn(self, cid, host):
        self.app.task("close", "断开连接", lambda: api("DELETE", "/connections/" + cid),
                      lambda ok, r: self.app.toast(f"已断开 {host}" if ok else f"断开失败: {r}",
                                                   "ok" if ok else "bad"))

    def close_sel(self):
        cn = self.current()
        if cn:
            self.close_conn(cn["id"], self.row(cn)["host"])

    def close_all(self):
        n = len(self.conns)
        self.app.modal(ConfirmModal("断开全部连接", f"将断开当前全部 {n} 个连接, 正在进行的下载会中断。",
                                    lambda: self.app.task("close", "断开全部连接",
                                                          lambda: api("DELETE", "/connections"),
                                                          lambda ok, r: self.app.toast(
                                                              "已断开全部连接" if ok else f"失败: {r}",
                                                              "ok" if ok else "bad")),
                                    yes="全部断开", danger=True))

    def toggle_pause(self):
        self.paused = not self.paused

    def cycle_sort(self):
        self.sort = (self.sort + 1) % len(self.SORTS)

    def key(self, k):
        n = len(self.view())
        if k in ("up", "k"):
            self.lv.move(-1, n)
        elif k in ("down", "j"):
            self.lv.move(1, n)
        elif k in ("pgup", "pgdn"):
            self.lv.page(-1 if k == "pgup" else 1, n)
        elif k in ("home", "g"):
            self.lv.home()
        elif k in ("end", "G"):
            self.lv.end(n)
        elif k == "enter":
            self.detail()
            return True
        elif k == "x":
            self.close_sel()
        elif k == "X":
            self.close_all()
        elif k == "p":
            self.toggle_pause()
        elif k == "s":
            self.cycle_sort()
        elif k == "/":
            self.ask_filter()
        elif k == "esc" and self.filter:
            self.filter = ""
        else:
            return False
        rows = self.view()
        if rows and k in ("up", "k", "down", "j", "pgup", "pgdn", "home", "g", "end", "G"):
            self.selid = rows[self.lv.sel][0]["id"]
        return True


class RulesPage(Page):
    title = "规则"
    hints = [("Tab", "规则/规则集"), ("⏎", "详情/更新"), ("/", "筛选"), ("r", "刷新")]
    RCOLS = [("idx", "#", 4, 0, ">", 3), ("type", "类型", 14, 0, "<", 2),
             ("payload", "内容", None, 5, "<", 0), ("proxy", "策略", None, 3, "<", 1),
             ("hit", "命中", 7, 0, ">", 4)]
    PCOLS = [("name", "名称", None, 4, "<", 0), ("behavior", "行为", 10, 0, "<", 2),
             ("vehicle", "来源", 8, 0, "<", 3), ("count", "条数", 8, 0, ">", 1),
             ("updated", "更新于", 16, 0, "<", 4)]

    def __init__(self, app):
        super().__init__(app)
        self.tab, self.filter = 0, ""
        self.rules, self.providers = None, None
        self.lv = [ListView(), ListView()]

    def on_show(self):
        if self.rules is None:
            self.load()

    def load(self):
        def run():
            r = api("GET", "/rules").get("rules") or []
            p = api("GET", "/providers/rules").get("providers") or {}
            return r, sorted(p.values(), key=lambda v: v.get("name", ""))

        def done(ok, res):
            if ok:
                self.rules, self.providers = res
            else:
                self.app.toast(f"加载规则失败: {res}", "bad")
        self.app.task("rules", "加载规则", run, done)

    def rows(self):
        if self.tab == 0:
            out = []
            for r in self.rules or []:
                ex = r.get("extra") or {}
                d = {"idx": str(r.get("index", "")), "type": r.get("type", ""),
                     "payload": r.get("payload", ""), "proxy": r.get("proxy", ""),
                     "hit": str(ex.get("hitCount", "")) if ex else "",
                     "_off": ex.get("disabled", False), "_raw": r}
                if match(" ".join([d["type"], d["payload"], d["proxy"]]), self.filter):
                    out.append(d)
            return out
        out = []
        for p in self.providers or []:
            d = {"name": p.get("name", ""), "behavior": p.get("behavior", ""),
                 "vehicle": p.get("vehicleType", ""), "count": str(p.get("ruleCount", "")),
                 "updated": (p.get("updatedAt") or "")[:16].replace("T", " "), "_raw": p}
            if match(d["name"], self.filter):
                out.append(d)
        return out

    def draw(self, y, x, h, w):
        a, c = self.app, self.app.c
        rows = self.rows()
        c.box(y, x, h, w, "规则", True,
              right=f"{len(self.rules or [])} 条规则 · {len(self.providers or [])} 个规则集")
        ix, iw = x + 2, w - 4
        bx = ix + a.segmented(y + 1, ix, [(f"规则 {len(self.rules or [])}", 0),
                                          (f"规则集 {len(self.providers or [])}", 1)],
                              self.tab, self.set_tab) + 2
        bx += a.button(y + 1, bx, "刷新", self.load, key="r", maxx=ix + iw) + 1
        if self.tab == 1:
            bx += a.button(y + 1, bx, "更新全部规则集", self.update_all, maxx=ix + iw) + 1
        a.button(y + 1, bx, "筛选" + ("·" + self.filter if self.filter else ""), self.ask_filter,
                 key="/", maxx=ix + iw, kind="on" if self.filter else "normal")
        c.hline(y + 2, x + 1, w - 2)
        cols = layout_cols(self.RCOLS if self.tab == 0 else self.PCOLS, iw - 1)
        for col, cx, cwid in cols:
            c.put(y + 3, ix + cx, pad(col[1], cwid, col[4]), "muted")
        colors = {"idx": "muted", "type": "purple", "payload": "white", "proxy": "accent",
                  "hit": "dim", "name": "white", "behavior": "purple", "vehicle": "dim",
                  "count": "accent", "updated": "dim"}

        def item(i, yy, sel):
            r = rows[i]
            bg = "bg_sel" if sel else "default"
            c.fill(yy, x + 1, w - 3, bg)
            if sel:
                c.put(yy, x + 1, "▌", "accent", bg)
            for col, cx, cwid in cols:
                fg = "muted" if r.get("_off") else colors[col[0]]
                c.put(yy, ix + cx, pad(r[col[0]], cwid, col[4]), fg, bg)
        lv = self.lv[self.tab]
        loading = "加载中…" if self.rules is None else ("没有匹配项" if self.filter else "空")
        lv.draw(a, y + 4, x + 1, h - 5, w - 3, len(rows), item, click=self.click,
                sb_x=x + w - 1, empty=loading)

    def set_tab(self, t):
        self.tab = t

    def click(self, i):
        self.lv[self.tab].sel = i
        self.activate()

    def activate(self):
        rows = self.rows()
        if not rows:
            return
        r = rows[self.lv[self.tab].sel]["_raw"]
        if self.tab == 0:
            ex = r.get("extra") or {}
            self.app.modal(InfoModal("规则详情", [
                ("序号", str(r.get("index", ""))), ("类型", r.get("type", "")),
                ("内容", r.get("payload", "") or "-"), ("策略", r.get("proxy", "")),
                ("命中", f"{ex.get('hitCount', '-')} 次, 最近 {(ex.get('hitAt') or '-')[:19].replace('T', ' ')}"),
                ("未命中", str(ex.get("missCount", "-")))]))
        else:
            upd = r.get("vehicleType") in ("HTTP", "File")
            self.app.modal(InfoModal("规则集详情", [
                ("名称", r.get("name", "")), ("行为", r.get("behavior", "")),
                ("来源", r.get("vehicleType", "")), ("条数", str(r.get("ruleCount", "-"))),
                ("更新于", (r.get("updatedAt") or "-")[:19].replace("T", " ")),
                ("路径", r.get("path", "-"))],
                extra=[("更新", lambda p=r: self.update_provider(p), "primary")] if upd else [],
                keymap={"u": (lambda p=r: self.update_provider(p))} if upd else {}))

    def update_provider(self, p):
        if p.get("vehicleType") not in ("HTTP", "File"):
            self.app.toast(f"{p.get('name')} 是 {p.get('vehicleType')} 类型, 无需更新", "warn")
            return
        name = p["name"]
        self.app.task("rp-" + name, f"更新规则集 {name}",
                      lambda: api("PUT", "/providers/rules/" + q(name), timeout=60),
                      lambda ok, r: (self.app.toast(f"规则集 {name} 已更新" if ok else f"更新失败: {r}",
                                                    "ok" if ok else "bad"), ok and self.load()))

    def update_all(self):
        ps = [p for p in self.providers or [] if p.get("vehicleType") in ("HTTP", "File")]
        if not ps:
            self.app.toast("没有可远程更新的规则集", "warn")
            return

        def run():
            for p in ps:
                api("PUT", "/providers/rules/" + q(p["name"]), timeout=60)
            return len(ps)
        self.app.task("rp-all", "更新全部规则集", run,
                      lambda ok, r: (self.app.toast(f"已更新 {r} 个规则集" if ok else f"更新失败: {r}",
                                                    "ok" if ok else "bad"), ok and self.load()))

    def key(self, k):
        n = len(self.rows())
        lv = self.lv[self.tab]
        if k in ("up", "k"):
            lv.move(-1, n)
        elif k in ("down", "j"):
            lv.move(1, n)
        elif k in ("pgup", "pgdn"):
            lv.page(-1 if k == "pgup" else 1, n)
        elif k in ("home", "g"):
            lv.home()
        elif k in ("end", "G"):
            lv.end(n)
        elif k in ("tab", "btab", "left", "right", "h", "l"):
            self.tab = 1 - self.tab
        elif k in ("enter", "space"):
            self.activate()
        elif k == "r":
            self.load()
        elif k == "/":
            self.ask_filter()
        elif k == "esc" and self.filter:
            self.filter = ""
        else:
            return False
        return True


class LogsPage(Page):
    title = "日志"
    hints = [("←→", "等级"), ("p", "暂停"), ("c", "清空"), ("G", "跟随"), ("/", "筛选")]

    def __init__(self, app):
        super().__init__(app)
        self.lv, self.filter, self.paused, self.follow = ListView(), "", False, True

    def rows(self):
        return [e for e in self.app.logs if match(e[2], self.filter)]

    def draw(self, y, x, h, w):
        a, c = self.app, self.app.c
        rows = self.rows()
        n = len(rows)
        c.box(y, x, h, w, "内核日志", True, right=f"{n} 条" + (" · 已暂停" if self.paused else ""))
        ix, iw = x + 2, w - 4
        lvl = a.prefs.get("log_level", "info")
        bx = ix + a.segmented(y + 1, ix, [(l, l) for l in LOG_LEVELS], lvl, a.set_log_level) + 2
        for label, fn, key, on in (("继续" if self.paused else "暂停", self.toggle_pause, "p", self.paused),
                                   ("清空", self.clear, "c", False),
                                   ("跟随最新", self.to_end, "G", self.follow),
                                   ("筛选" + ("·" + self.filter if self.filter else ""),
                                    self.ask_filter, "/", bool(self.filter))):
            bw = a.button(y + 1, bx, label, fn, key=key, maxx=ix + iw, kind="on" if on else "normal")
            bx += bw + 1 if bw else 0
        c.hline(y + 2, x + 1, w - 2)
        if self.follow:
            self.lv.sel = max(0, n - 1)

        def item(i, yy, sel):
            ts, lv, msg = rows[i]
            bg = "bg_sel" if sel and not self.follow else "default"
            c.fill(yy, x + 1, w - 3, bg)
            tag, fg = LOG_TAG.get(lv, (lv[:3].upper(), "dim"))
            k = c.put(yy, ix, ts + " ", "muted", bg)
            k += c.put(yy, ix + k, tag + " ", fg, bg, bold=True)
            c.put(yy, ix + k, msg, "bad" if lv == "error" else "default", bg, w=iw - k - 1)
        self.lv.draw(a, y + 3, x + 1, h - 4, w - 3, n, item, click=self.click, sb_x=x + w - 1,
                     empty="等待日志… (等级越低日志越多, debug 最详细)")
        self.follow = n == 0 or self.lv.sel >= n - 1

    def click(self, i):
        self.lv.sel = i
        self.follow = False
        if self.app.dbl:
            self.detail()

    def detail(self):
        rows = self.rows()
        if rows:
            ts, lv, msg = rows[self.lv.sel]
            self.app.modal(InfoModal("日志", [("时间", ts), ("等级", lv), ("内容", msg)]))

    def toggle_pause(self):
        self.paused = not self.paused
        self.app.logs_paused = self.paused

    def clear(self):
        self.app.logs.clear()
        self.lv.sel = self.lv.top = 0

    def to_end(self):
        self.follow = True

    def key(self, k):
        n = len(self.rows())
        if k in ("up", "k"):
            self.lv.move(-1, n)
            self.follow = False
        elif k in ("down", "j"):
            self.lv.move(1, n)
        elif k in ("pgup", "pgdn"):
            self.lv.page(-1 if k == "pgup" else 1, n)
            self.follow = self.follow and k == "pgdn"
        elif k in ("home", "g"):
            self.lv.home()
            self.follow = False
        elif k in ("end", "G"):
            self.to_end()
        elif k in ("left", "right", "h", "l"):
            lvl = self.app.prefs.get("log_level", "info")
            i = LOG_LEVELS.index(lvl) if lvl in LOG_LEVELS else 1
            i = max(0, min(len(LOG_LEVELS) - 1, i + (-1 if k in ("left", "h") else 1)))
            self.app.set_log_level(LOG_LEVELS[i])
        elif k == "enter":
            self.detail()
        elif k == "p":
            self.toggle_pause()
        elif k == "c":
            self.clear()
        elif k == "/":
            self.ask_filter()
        elif k == "esc" and self.filter:
            self.filter = ""
        else:
            return False
        return True


class Opt:
    """设置条目. kind: toggle / seg / select / input / action / info."""

    def __init__(self, kind, label, desc="", get=None, apply=None, options=None, parse=None,
                 btn="", danger=False, fmt=None, hint=""):
        self.kind, self.label, self.desc = kind, label, desc
        self.get, self.apply, self.options = get, apply, options or []
        self.parse, self.btn, self.danger, self.fmt, self.hint = parse, btn, danger, fmt, hint


def int_in(lo, hi):
    def parse(s):
        try:
            v = int(s)
        except ValueError:
            raise ValueError("请输入整数")
        if not lo <= v <= hi:
            raise ValueError(f"范围 {lo} - {hi}")
        return v
    return parse


def non_empty(s):
    if not s:
        raise ValueError("不能为空")
    return s


def http_url(s):
    if not s.startswith(("http://", "https://")):
        raise ValueError("需以 http:// 或 https:// 开头")
    return s


class SettingsPage(Page):
    title = "设置"
    hints = [("↑↓", "分组"), ("⏎", "进入分组")]

    def __init__(self, app):
        super().__init__(app)
        self.focus, self.editing, self.pend = 0, False, None
        self.slist, self.ilist = ListView(2), ListView(3)
        self.sections = self.build()

    def build(self):
        a = self.app
        cfg = lambda k, d=None: (a.cfg or {}).get(k, d)
        tun = lambda k, d=None: ((a.cfg or {}).get("tun") or {}).get(k, d)

        def patch(key, label):
            return lambda v: a.patch_cfg({key: v}, label)

        def tpatch(key, label):
            return lambda v: a.patch_cfg({"tun": {"enable": bool(tun("enable")), key: v}}, label)
        port = int_in(0, 65535)
        return [
            ("常规", "模式与核心行为", [
                Opt("seg", "代理模式", "规则: 按规则分流 · 全局: 全部走 GLOBAL 组 · 直连: 不经代理",
                    lambda: cfg("mode", "rule"), patch("mode", "代理模式"), MODES),
                Opt("toggle", "局域网共享", "允许局域网内其它设备连接本机代理端口 (allow-lan)",
                    lambda: bool(cfg("allow-lan")), patch("allow-lan", "局域网共享")),
                Opt("toggle", "IPv6", "允许解析与连接 IPv6 地址",
                    lambda: bool(cfg("ipv6")), patch("ipv6", "IPv6")),
                Opt("toggle", "TCP 并发", "同时连接域名解析出的多个 IP, 取最快者 (tcp-concurrent)",
                    lambda: bool(cfg("tcp-concurrent")), patch("tcp-concurrent", "TCP 并发")),
                Opt("toggle", "域名嗅探", "从 TLS / HTTP 流量中还原真实域名用于规则匹配 (sniffing)",
                    lambda: bool(cfg("sniffing")), patch("sniffing", "域名嗅探")),
                Opt("select", "日志等级", "内核写入日志文件的详细程度 (日志页可单独选择查看等级)",
                    lambda: cfg("log-level", "info"), patch("log-level", "日志等级"),
                    [(v, v) for v in ("silent", "error", "warning", "info", "debug")]),
                Opt("select", "进程匹配", "是否查找连接所属进程, 供 PROCESS-NAME 规则使用",
                    lambda: cfg("find-process-mode", "strict"), patch("find-process-mode", "进程匹配"),
                    [("strict · 按需", "strict"), ("always · 总是", "always"), ("off · 关闭", "off")]),
            ]),
            ("入站端口", "监听地址与端口", [
                Opt("input", "混合端口", "HTTP 与 SOCKS5 共用端口, 0 表示关闭 (mixed-port)",
                    lambda: cfg("mixed-port", 0), patch("mixed-port", "混合端口"), parse=port),
                Opt("input", "HTTP 端口", "单独的 HTTP 代理端口, 0 表示关闭 (port)",
                    lambda: cfg("port", 0), patch("port", "HTTP 端口"), parse=port),
                Opt("input", "SOCKS5 端口", "单独的 SOCKS5 代理端口, 0 表示关闭 (socks-port)",
                    lambda: cfg("socks-port", 0), patch("socks-port", "SOCKS5 端口"), parse=port),
                Opt("input", "绑定地址", "开启局域网共享时监听的地址, * 表示全部网卡 (bind-address)",
                    lambda: cfg("bind-address", "*"), patch("bind-address", "绑定地址"), parse=non_empty),
                Opt("info", "允许的局域网网段", "lan-allowed-ips, 需在配置文件中修改",
                    lambda: ", ".join(cfg("lan-allowed-ips") or []) or "全部"),
            ]),
            ("TUN 模式", "虚拟网卡接管流量", [
                Opt("toggle", "启用 TUN", "需要 root 或 CAP_NET_ADMIN 权限; 失败时请查看日志页",
                    lambda: bool(tun("enable")),
                    lambda v: a.patch_cfg({"tun": {"enable": v}}, "TUN 模式")),
                Opt("select", "协议栈", "gVisor 兼容性最好 · System 性能更高 · Mixed 两者结合",
                    lambda: str(tun("stack", "gvisor")).lower(), tpatch("stack", "TUN 协议栈"),
                    [("gVisor", "gvisor"), ("System", "system"), ("Mixed", "mixed")]),
                Opt("input", "网卡名称", "创建的虚拟网卡名称 (device)",
                    lambda: tun("device", "Mihomo"), tpatch("device", "TUN 网卡名称"), parse=non_empty),
                Opt("toggle", "自动路由", "自动添加系统路由, 将流量导入 TUN (auto-route)",
                    lambda: bool(tun("auto-route")), tpatch("auto-route", "TUN 自动路由")),
                Opt("toggle", "自动检测出口", "自动识别默认出口网卡, 避免回环 (auto-detect-interface)",
                    lambda: bool(tun("auto-detect-interface")),
                    tpatch("auto-detect-interface", "TUN 自动检测出口")),
                Opt("input", "MTU", "虚拟网卡最大传输单元", lambda: tun("mtu", 1500),
                    tpatch("mtu", "TUN MTU"), parse=int_in(576, 65535)),
                Opt("info", "DNS 劫持", "dns-hijack, 需在配置文件中修改",
                    lambda: ", ".join(tun("dns-hijack") or []) or "-"),
            ]),
            ("订阅与内核", "更新、重载与维护", [
                Opt("info", "订阅地址", "来自 subscription.url", a.sub_url),
                Opt("action", "更新订阅", "下载订阅并合并本机定制 (update.sh), 随后热重载并恢复节点选择",
                    apply=a.update_sub, btn="立即更新"),
                Opt("action", "重载配置文件", "从 config.yaml 重新加载, 保留各代理组的节点选择",
                    apply=a.reload_config, btn="重载"),
                Opt("action", "更新 GeoData", "下载最新 GeoIP / GeoSite 数据库并重载",
                    apply=a.update_geo, btn="更新"),
                Opt("action", "清空 FakeIP 缓存", "解决 fake-ip 映射错乱导致的个别网站无法访问",
                    apply=a.flush_fakeip, btn="清空"),
                Opt("action", "重启内核", "重启 mihomo 进程, 所有连接会中断",
                    apply=a.ask_restart, btn="重启", danger=True),
                Opt("info", "内核版本", "", lambda: a.version or "-"),
                Opt("info", "控制器", "", lambda: CONTROLLER_DESC or API),
                Opt("info", "配置文件", "", lambda: CONFIG_FILE),
            ]),
            ("测速与界面", "TUI 偏好 (.tui.json)", [
                Opt("toggle", "修改写回配置文件", "开启后本页修改同步写入 config.yaml, 重启或更新订阅后依然生效",
                    lambda: bool(a.prefs.get("persist")), lambda v: a.set_pref("persist", v, "写回配置文件")),
                Opt("toggle", "切换节点时断开旧连接", "切换后立即断开经过该组的连接, 让新节点马上生效",
                    lambda: bool(a.prefs.get("close_on_switch")),
                    lambda v: a.set_pref("close_on_switch", v, "切换时断开旧连接")),
                Opt("input", "测速地址", "延迟测试与连通测试访问的 URL",
                    lambda: a.prefs["delay_url"], lambda v: a.set_pref("delay_url", v, "测速地址"),
                    parse=http_url),
                Opt("input", "测速超时 (ms)", "超过该时间视为超时",
                    lambda: a.prefs["delay_timeout"], lambda v: a.set_pref("delay_timeout", v, "测速超时"),
                    parse=int_in(500, 30000)),
                Opt("input", "测速并发数", "同时测速的节点数量",
                    lambda: a.prefs["concurrency"], lambda v: a.set_pref("concurrency", v, "测速并发数"),
                    parse=int_in(1, 64)),
            ]),
        ]

    def items(self):
        return self.sections[self.slist.sel][2]

    def control_width(self, o, maxw):
        v = o.get() if o.get else None
        if o.kind == "toggle":
            return 7
        if o.kind == "seg":
            return sum(tw(l) + 2 for l, _ in o.options)
        if o.kind == "select":
            label = dict((val, l) for l, val in o.options).get(v, str(v))
            return min(maxw, tw(label) + 4)
        if o.kind == "input":
            return min(maxw, tw(str(v)) + 4)
        if o.kind == "action":
            return tw(o.btn) + 2
        return min(maxw, tw(str(v)))

    def draw_control(self, o, i, y, x, maxw, bg):
        a, c = self.app, self.app.c
        editing = self.editing and i == self.ilist.sel
        real = o.get() if o.get else None
        v = self.pend if editing else real
        diff = editing and v != real
        ih = self.ilist.ih
        if o.kind == "toggle":
            a.toggle(y, x, bool(v), lambda: self.control_click(i, o), warn=diff, h=ih)
        elif o.kind == "seg":
            a.segmented(y, x, o.options, v, lambda val: self.seg_click(i, o, val),
                        warn=diff, h=ih)
        elif o.kind == "select":
            label = dict((val, l) for l, val in o.options).get(v, str(v))
            a.button(y, x, clip(label, maxw - 4) + " ▾", lambda: self.control_click(i, o),
                     kind="primary" if diff else "normal", h=ih)
        elif o.kind == "input":
            a.button(y, x, clip(str(v), maxw - 4) + " ✎", lambda: self.control_click(i, o), h=ih)
        elif o.kind == "action":
            a.button(y, x, o.btn, lambda: self.control_click(i, o),
                     kind="danger" if o.danger else "primary", h=ih)
        else:
            c.put(y, x, str(v), "accent", bg, w=maxw)

    def draw(self, y, x, h, w):
        a, c = self.app, self.app.c
        sw = max(20, min(26, w // 4))
        content_focus = not a.nav_focus
        c.box(y, x, h, sw, "设置", content_focus and self.focus == 0)

        def sitem(i, yy, sel):
            name, sub, _ = self.sections[i]
            bg = ("bg_sel" if content_focus and self.focus == 0 else "bg_sel2") if sel else "default"
            c.fill(yy, x + 1, sw - 2, bg)
            c.fill(yy + 1, x + 1, sw - 2, bg)
            if sel:
                c.put(yy, x + 1, "▌", "accent", bg)
                c.put(yy + 1, x + 1, "▌", "accent", bg)
            c.put(yy, x + 3, name, "white" if sel else "default", bg, bold=sel, w=sw - 5)
            c.put(yy + 1, x + 3, sub, "accent" if sel else "muted", bg, w=sw - 5)
        self.slist.draw(a, y + 1, x + 1, h - 2, sw - 2, len(self.sections), sitem, click=self.click_section)

        px, pw = x + sw + 1, w - sw - 1
        name, sub, items = self.sections[self.slist.sel]
        c.box(y, px, h, pw, name, content_focus and self.focus == 1,
              right="←→ 预览 · ⏎ 生效 · Esc 取消" if self.editing else sub)
        self.hints = ([("←→", "预览"), ("⏎", "生效"), ("Esc", "取消")] if self.editing else
                      ([("↑↓", "选择"), ("⏎", "进入选项"), ("Esc", "返回分组")] if self.focus == 1 else
                       [("↑↓", "分组"), ("⏎", "进入分组")]))
        ix, iw = px + 2, pw - 5

        def item(i, yy, sel):
            o = items[i]
            bg = ("bg_sel" if content_focus and self.focus == 1 else "bg_sel2") if sel else "default"
            c.fill(yy, px + 1, pw - 3, bg)
            c.fill(yy + 1, px + 1, pw - 3, bg)
            if sel:
                c.put(yy, px + 1, "▌", "accent", bg)
                c.put(yy + 1, px + 1, "▌", "accent", bg)
            ctlw = self.control_width(o, max(10, iw // 2))
            lw = iw - ctlw - 2
            k = c.put(yy, ix, o.label, "white" if sel else "default", bg, bold=True, w=lw)
            if o.label in a.pending:
                c.put(yy, ix + k + 1, SPIN[a.frame % len(SPIN)], "accent", bg)
            if o.desc:
                c.put(yy + 1, ix, o.desc, "dim", bg, w=iw)
            cx = ix + iw - ctlw
            if sel and self.editing:
                c.put(yy, cx - 1, "◂", "accent", bg, bold=True)
                c.put(yy, ix + iw, "▸", "accent", bg, bold=True)
            self.draw_control(o, i, yy, cx, max(10, iw // 2), bg)
        self.ilist.draw(a, y + 1, px + 1, h - 2, pw - 3, len(items), item,
                        click=self.click_item, sb_x=px + pw - 1)

    def click_section(self, i):
        self.focus, self.editing, self.pend = 0, False, None
        if i != self.slist.sel:
            self.slist.sel, self.ilist.sel, self.ilist.top = i, 0, 0

    def click_item(self, i):
        """鼠标单击 = 选中并直接生效/打开, 预览编辑态只给键盘."""
        self.focus, self.editing, self.pend = 1, False, None
        self.ilist.sel = i
        self.activate(self.items()[i])

    def control_click(self, i, o):
        """点击控件: 选中该行并直接生效 (鼠标不进入预览态)."""
        self.focus, self.editing, self.pend = 1, False, None
        self.ilist.sel = i
        self.activate(o)

    def seg_click(self, i, o, val):
        self.focus, self.editing, self.pend = 1, False, None
        self.ilist.sel = i
        if val != o.get():
            o.apply(val)

    def enter_edit(self, o):
        self.editing, self.pend = True, o.get()

    def pend_step(self, o, d):
        """编辑态下 ←→ 只移动预览值, 不生效."""
        vals = [v for _, v in o.options]
        i = vals.index(self.pend) if self.pend in vals else -1
        self.pend = vals[(i + d) % len(vals)]

    def commit_edit(self, o):
        if self.editing and self.pend is not None and o.get and self.pend != o.get():
            o.apply(self.pend)
        self.editing, self.pend = False, None

    def cancel_edit(self):
        self.editing, self.pend = False, None

    def activate(self, o):
        a = self.app
        v = o.get() if o.get else None
        if o.kind == "toggle":
            o.apply(not v)
        elif o.kind in ("seg", "select"):
            # 鼠标点在控件之外 (标签/说明文字) 时弹选择框, 而不是猜一个值
            a.modal(SelectModal(o.label, o.options, v,
                                lambda val: o.apply(val) if val != o.get() else None))
        elif o.kind == "input":
            a.modal(InputModal(o.label, v, lambda val: o.apply(val) if val != v else None,
                               o.desc, o.parse))
        elif o.kind == "action":
            o.apply()
        elif o.kind == "info":
            a.modal(InfoModal(o.label, [(o.label, str(v))] + ([("说明", o.desc)] if o.desc else [])))

    def key(self, k):
        items = self.items()
        if self.focus == 0:
            n = len(self.sections)
            if k in ("up", "k", "down", "j"):
                self.slist.move(-1 if k in ("up", "k") else 1, n)
                self.ilist.sel = self.ilist.top = 0
            elif k in ("right", "l", "enter", "space", "tab"):
                self.focus = 1
            else:
                return False
            return True
        o = items[self.ilist.sel] if items else None
        if self.editing and o:
            if k in ("left", "h"):
                self.pend_step(o, -1)
            elif k in ("right", "l"):
                self.pend_step(o, 1)
            elif k in ("up", "k"):
                self.commit_edit(o)
                self.ilist.move(-1, len(items))
            elif k in ("down", "j"):
                self.commit_edit(o)
                self.ilist.move(1, len(items))
            elif k in ("enter", "space", "tab", "btab"):
                self.commit_edit(o)
            elif k == "esc":
                self.cancel_edit()
            else:
                return False
            return True
        if k in ("up", "k"):
            self.ilist.move(-1, len(items))
        elif k in ("down", "j"):
            self.ilist.move(1, len(items))
        elif k in ("left", "h", "esc", "btab"):
            self.focus = 0
        elif k in ("enter", "space") and o:
            if o.kind in ("seg", "select"):
                self.enter_edit(o)
            else:
                self.activate(o)
        else:
            return False
        return True


# ───────────────────────────── 应用 ─────────────────────────────

class App:
    NAV_W = 12

    def __init__(self, scr):
        self.scr = scr
        self.theme = Theme()
        self.c = Canvas(scr, self.theme)
        self.hits = Hits()
        self.prefs = load_prefs()
        self.events = queue.Queue()
        self.proxies, self.cfg, self.version = None, None, ""
        self.online, self.error, self.last_try = False, "", 0
        self.delays, self.testing = {}, set()
        self.tasks, self.progress, self.pending = {}, {}, set()
        self.traffic = (0, 0)
        self.logs, self.logs_paused, self.log_gen = deque(maxlen=2000), False, 0
        self.msg = None
        self.frame = 0
        self.dbl, self.last_click = False, (0, 0, 0)
        self.modals = []
        self.running = True
        self.pages = [ProxiesPage(self), ConnectionsPage(self), RulesPage(self),
                      LogsPage(self), SettingsPage(self)]
        self.page = self.pages[0]
        self.nav_focus, self.nav_sel = False, 0

    # ---- 线程与任务 ----
    def ui(self, fn):
        self.events.put(fn)

    def task(self, key, label, fn, done=None):
        if key in self.tasks:
            self.toast(f"{self.tasks[key]} 正在进行中", "warn")
            return
        self.tasks[key] = label

        def run():
            try:
                res, ok = fn(), True
            except Exception as e:
                res, ok = e, False

            def fin():
                self.tasks.pop(key, None)
                self.progress.pop(key, None)
                if done:
                    done(ok, res)
                elif not ok:
                    self.toast(f"{label}失败: {res}", "bad")
            self.ui(fin)
        threading.Thread(target=run, daemon=True).start()

    def toast(self, text, level="info"):
        self.msg = (str(text), level, time.time())

    def modal(self, m):
        self.modals.append(m)

    def close_modal(self):
        if self.modals:
            self.modals.pop()

    # ---- 状态 ----
    def refresh(self, quiet=True):
        def run():
            return (api("GET", "/proxies")["proxies"], api("GET", "/configs"),
                    api("GET", "/version", timeout=5).get("version", "?"))

        def done(ok, res):
            self.last_try = time.time()
            if ok:
                self.proxies, self.cfg, self.version = res
                self.online, self.error = True, ""
                for n, p in self.proxies.items():
                    h = p.get("history") or []
                    if h and n not in self.testing:
                        d = h[-1].get("delay", 0)
                        self.delays[n] = d if d > 0 else "timeout"
                if not quiet:
                    self.toast("已刷新", "ok")
            else:
                self.online, self.error = False, str(res)
                if not quiet or self.proxies is not None:
                    self.toast(f"连接控制器失败: {res}", "bad")
        if "refresh" not in self.tasks:
            self.task("refresh", "刷新", run, done)

    def groups(self):
        if not self.proxies:
            return []
        isg = lambda n: self.proxies.get(n, {}).get("type") in GROUP_TYPES and self.proxies[n].get("all")
        order = [n for n in self.proxies.get("GLOBAL", {}).get("all") or [] if isg(n)]
        order += sorted(n for n in self.proxies if isg(n) and n not in order and n != "GLOBAL")
        if isg("GLOBAL"):
            order = (["GLOBAL"] + order) if (self.cfg or {}).get("mode") == "global" else order + ["GLOBAL"]
        return order

    def delay_view(self, n):
        if n in self.testing:
            return SPIN[self.frame % len(SPIN)] + " 测速", "accent"
        if self.proxies and self.proxies.get(n, {}).get("type") in ("Direct", "Reject", "RejectDrop", "Pass"):
            return "", "muted"
        d, seen = self.delays.get(n), {n}
        while d is None and self.proxies and self.proxies.get(n, {}).get("type") in GROUP_TYPES:
            n = self.proxies[n].get("now")
            if not n or n in seen:
                break
            seen.add(n)
            d = self.delays.get(n)
        if isinstance(d, int):
            return f"{d} ms", "ok" if d < 300 else ("warn" if d < 800 else "bad")
        return ("超时", "bad") if d == "timeout" else ("--", "muted")

    def proxy_port(self):
        c = self.cfg or {}
        return int(c.get("mixed-port") or c.get("port") or 0)

    def sub_url(self):
        try:
            return open(SUB_FILE, encoding="utf-8").read().strip() or "未配置"
        except Exception:
            return "未配置"

    # ---- 操作 ----
    def patch_cfg(self, data, label):
        self.pending.add(label)

        def run():
            api("PATCH", "/configs", data, timeout=20)
            cfg = api("GET", "/configs")
            note = ""
            want_tun = (data.get("tun") or {}).get("enable")
            if "tun" in data and want_tun and not (cfg.get("tun") or {}).get("enable"):
                return cfg, "warn", "TUN 未能启动 (通常需要 root 权限), 详见日志页"
            if self.prefs.get("persist"):
                try:
                    persist_config(data)
                    note = " · 已写入 config.yaml"
                except Exception as e:
                    note = f" · 写入配置文件失败: {e}"
            return cfg, "ok", note

        def done(ok, res):
            self.pending.discard(label)
            if not ok:
                self.toast(f"{label} 修改失败: {res}", "bad")
                return
            self.cfg = res[0]
            self.toast(res[2] if res[1] == "warn" else f"{label} 已更新{res[2]}", res[1])
        self.task("patch-" + label, label, run, done)

    def set_pref(self, key, val, label):
        self.prefs[key] = val
        try:
            save_prefs(self.prefs)
            self.toast(f"{label} 已保存", "ok")
        except Exception as e:
            self.toast(f"保存偏好失败: {e}", "bad")

    def cycle_mode(self):
        mode = (self.cfg or {}).get("mode", "rule")
        vals = [v for _, v in MODES]
        nxt = vals[(vals.index(mode) + 1) % 3] if mode in vals else "rule"
        self.patch_cfg({"mode": nxt}, "代理模式")

    def toggle_tun(self):
        self.patch_cfg({"tun": {"enable": not ((self.cfg or {}).get("tun") or {}).get("enable")}}, "TUN 模式")

    def toggle_lan(self):
        self.patch_cfg({"allow-lan": not (self.cfg or {}).get("allow-lan")}, "局域网共享")

    def conn_test(self):
        port, url = self.proxy_port(), self.prefs["delay_url"]
        if not port:
            self.toast("未开启混合/HTTP 端口, 无法测试", "warn")
            return
        now = (self.proxies or {}).get("Proxies", {}).get("now", "")

        def done(ok, res):
            if ok:
                self.toast(f"代理连通: HTTP {res[0]} · {res[1]:.2f}s" + (f" · {now}" if now else ""),
                           "ok" if res[0] < 400 else "warn")
            else:
                self.toast(f"代理不通: {res}", "bad")
        self.task("conntest", "连通测试", lambda: proxy_test(port, url), done)

    def snapshot_sel(self):
        return {n: p.get("now") for n, p in (self.proxies or {}).items() if p.get("type") == "Selector"}

    def restore_sel(self, sel):
        cur = api("GET", "/proxies")["proxies"]
        for g, n in sel.items():
            if n and g in cur and n in (cur[g].get("all") or []) and cur[g].get("now") != n:
                try:
                    api("PUT", "/proxies/" + q(g), {"name": n})
                except Exception:
                    pass

    def reload_config(self):
        sel = self.snapshot_sel()

        def run():
            api("PUT", "/configs?force=true", {"path": CONFIG_FILE}, timeout=30)
            self.restore_sel(sel)

        def done(ok, res):
            self.toast("配置已重载, 节点选择已恢复" if ok else f"重载失败: {res}", "ok" if ok else "bad")
            self.refresh()
        self.task("reload", "重载配置", run, done)

    def update_sub(self):
        def run():
            cmd = update_cmd()
            if cmd is None:
                raise RuntimeError("找不到可执行的更新脚本 (update.sh + bash / update.bat / update.ps1)")
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", cwd=HERE)
            for line in p.stdout:
                line = line.strip()
                if line:
                    self.ui(lambda l=line: (self.progress.__setitem__("update", l),
                                            self.add_log("info", "[update] " + l)))
            return p.wait()

        def done(ok, res):
            if ok and res == 0:
                self.toast("订阅更新完成, 节点列表已刷新", "ok")
                self.refresh()
            else:
                self.toast(f"订阅更新失败 ({'exit=' + str(res) if ok else res}), 详见日志页", "bad")
        self.task("update", "更新订阅", run, done)

    def update_geo(self):
        self.task("geo", "更新 GeoData", lambda: api("POST", "/configs/geo", {}, timeout=180),
                  lambda ok, r: self.toast("GeoData 已更新" if ok else f"更新 GeoData 失败: {r}",
                                           "ok" if ok else "bad"))

    def flush_fakeip(self):
        self.task("fakeip", "清空 FakeIP", lambda: api("POST", "/cache/fakeip/flush"),
                  lambda ok, r: self.toast("FakeIP 缓存已清空" if ok else f"失败: {r}",
                                           "ok" if ok else "bad"))

    def ask_restart(self):
        self.modal(ConfirmModal("重启内核", "将重启 mihomo 进程, 当前全部连接会中断, 通常几秒内恢复。",
                                self.restart, yes="重启", danger=True))

    def restart(self):
        def run():
            try:
                api("POST", "/restart", {})
            except Exception:
                pass
            time.sleep(1)
            for _ in range(30):
                try:
                    api("GET", "/version", timeout=2)
                    return True
                except Exception:
                    time.sleep(0.5)
            return False

        def done(ok, res):
            self.toast("内核已重启" if res else "内核未在 15 秒内恢复, 请检查进程", "ok" if res else "bad")
            self.refresh()
        self.task("restart", "重启内核", run, done)

    # ---- 后台流 ----
    def add_log(self, level, msg):
        if not self.logs_paused:
            self.logs.append((time.strftime("%H:%M:%S"), level, msg))

    def set_log_level(self, lvl):
        if lvl != self.prefs.get("log_level"):
            self.prefs["log_level"] = lvl
            try:
                save_prefs(self.prefs)
            except Exception:
                pass
            self.start_logs()

    def start_logs(self):
        self.log_gen += 1
        gen, lvl = self.log_gen, self.prefs.get("log_level", "info")

        def run():
            while self.running and gen == self.log_gen:
                try:
                    with open_api_stream("/logs?level=" + lvl) as f:
                        for line in f:
                            if gen != self.log_gen:
                                return
                            try:
                                d = json.loads(line)
                                self.ui(lambda d=d: self.add_log(d.get("type", "info"), d.get("payload", "")))
                            except ValueError:
                                pass
                except Exception:
                    time.sleep(3)
        threading.Thread(target=run, daemon=True).start()

    def start_traffic(self):
        def run():
            while self.running:
                try:
                    with open_api_stream("/traffic", timeout=10) as f:
                        for line in f:
                            d = json.loads(line)
                            self.ui(lambda d=d: setattr(self, "traffic", (d.get("up", 0), d.get("down", 0))))
                except Exception:
                    self.ui(lambda: setattr(self, "traffic", (0, 0)))
                    time.sleep(3)
        threading.Thread(target=run, daemon=True).start()

    # ---- 控件 ----
    def button(self, y, x, label, fn, key=None, kind="normal", maxx=None, h=1):
        bg = {"normal": "bg_btn", "primary": "bg_accent", "danger": "bg_bad", "on": "bg_accent"}[kind]
        fg = "white" if kind in ("normal", "danger") else "black"
        text = f" {label} "
        total = tw(text) + (tw(key) + 1 if key else 0)
        if maxx is not None and x + total > maxx:
            return 0
        cx = x
        if key:
            cx += self.c.put(y, cx, " " + key, "accent" if kind == "normal" else fg, bg, bold=True)
        self.c.put(y, cx, text, fg, bg, bold=kind != "normal")
        self.hits.add(y, x, total, fn, h)
        return total

    def toggle(self, y, x, on, fn, warn=False, h=1):
        if on:
            self.c.put(y, x, " 开  ● ", "black", "bg_warn" if warn else "bg_ok", bold=True)
        else:
            self.c.put(y, x, " ○  关 ", "warn" if warn else "dim", "bg_btn")
        self.hits.add(y, x - 1, 9, fn, h)
        return 7

    def segmented(self, y, x, options, current, fn, warn=False, h=1):
        cx = x
        n = len(options)
        for i, (label, val) in enumerate(options):
            on = val == current
            t = f" {label} "
            sw = tw(t)
            cx += self.c.put(y, cx, t, "black" if on else "dim",
                             ("bg_warn" if warn else "bg_accent") if on else "bg_btn", bold=on)
            hx, hw = cx - sw - (1 if i == 0 else 0), sw + (1 if i in (0, n - 1) else 0)
            self.hits.add(y, hx, hw, lambda v=val: fn(v), h)
        return cx - x

    # ---- 布局 ----
    def draw(self):
        self.scr.erase()
        self.hits.clear()
        H, W = self.c.size()
        if H < 16 or W < 64:
            self.c.put(H // 2, max(0, (W - 30) // 2), f"终端太小 ({W}x{H}), 至少需要 64x16", "warn")
            self.scr.refresh()
            return
        self.draw_header(W)
        self.draw_nav(1, H - 2)
        if self.proxies is None:
            self.draw_connecting(1, self.NAV_W + 1, H - 2, W - self.NAV_W - 2)
        else:
            self.page.draw(1, self.NAV_W + 1, H - 2, W - self.NAV_W - 2)
        self.draw_footer(H - 1, W)
        for m in self.modals:
            if m is self.modals[-1]:
                self.hits.clear()
                self.hits.add(0, 0, W, self.close_modal, H)
            m.draw(self)
        self.scr.refresh()

    def draw_header(self, W):
        c, bg = self.c, "bg_header"
        c.fill(0, 0, W, bg)
        x = c.put(0, 1, " ◆ mihomo ", "black", "bg_accent", bold=True) + 2
        x += c.put(0, x, self.version or "", "dim", bg) + 2
        on = self.online
        x += c.put(0, x, "● 在线" if on else "● 离线", "ok" if on else "bad", bg, bold=True) + 1
        cfg = self.cfg or {}
        mode = cfg.get("mode", "?")
        tun = (cfg.get("tun") or {}).get("enable")
        lan = cfg.get("allow-lan")
        chips = [("模式 " + MODE_LABEL.get(mode, mode), True, self.cycle_mode),
                 ("TUN " + ("开" if tun else "关"), tun, lambda: self.goto_setting(2)),
                 ("局域网 " + ("开" if lan else "关"), lan, lambda: self.goto_setting(0)),
                 ("端口 " + str(self.proxy_port() or "-"), False, lambda: self.goto_setting(1))]
        for text, hl, fn in chips:
            x += c.put(0, x, "│ ", "border", bg)
            w = c.put(0, x, text, "accent" if hl else "white", bg, bold=bool(hl))
            self.hits.add(0, x, w, fn)
            x += w + 1
        up, down = self.traffic
        right = f"↑ {fmt_speed(up):>11}   ↓ {fmt_speed(down):>11} "
        rx = W - tw(right) - 1
        if rx > x + 2:
            c.put(0, rx, "↑ ", "warn", bg)
            c.put(0, rx + 2, f"{fmt_speed(up):>11}   ", "white", bg)
            c.put(0, rx + 16, "↓ ", "ok", bg)
            c.put(0, rx + 18, f"{fmt_speed(down):>11} ", "white", bg)

    def goto_setting(self, sec):
        sp = self.pages[4]
        self.page, sp.focus = sp, 1
        sp.slist.sel, sp.ilist.sel, sp.ilist.top = sec, 0, 0

    def draw_nav(self, y, h):
        c, w = self.c, self.NAV_W
        # 分隔线只表达区域边界，不参与焦点高亮；焦点统一由菜单项自身表达。
        for i in range(h):
            c.put(y + i, w - 1, "│", "border")
        for i, p in enumerate(self.pages):
            yy = y + 1 + i * 2
            cur = p is self.page
            if self.nav_focus and i == self.nav_sel:
                c.fill(yy, 0, w - 1, "bg_sel")
                c.put(yy, 0, "▌", "accent", "bg_sel")
                c.put(yy, 2, p.title, "white", "bg_sel", bold=True)
                c.put(yy, w - 3, str(i + 1), "accent", "bg_sel", bold=True)
            elif self.nav_focus and cur:
                c.put(yy, 2, p.title, "accent", bold=True)
                c.put(yy, w - 3, str(i + 1), "accent")
            else:
                # 焦点位于内容区时，当前页面只保留与右侧非焦点选中行一致的弱高亮。
                bg = "bg_sel2" if (not self.nav_focus and cur) else "default"
                c.fill(yy, 0, w - 1, bg)
                if bg != "default":
                    c.put(yy, 0, "▌", "accent", bg)
                c.put(yy, 2, p.title, "white" if cur else "dim", bg, bold=cur)
                c.put(yy, w - 3, str(i + 1), "accent" if cur else "muted", bg)
            self.hits.add(yy, 0, w - 1, lambda p=p: self.show(p))
        for j, (k, label, fn) in enumerate((("?", "帮助", self.help), ("q", "退出", self.quit))):
            yy = y + h - 3 + j
            c.put(yy, 2, k, "accent", bold=True)
            c.put(yy, 4, label, "dim")
            self.hits.add(yy, 0, w - 1, fn)

    def draw_connecting(self, y, x, h, w):
        c = self.c
        c.box(y, x, h, w, "连接控制器", True)
        sp = SPIN[self.frame % len(SPIN)]
        c.put(y + h // 2 - 2, x + 4, f"{sp} 正在连接 mihomo 控制器 {API}", "accent", bold=True)
        if self.error:
            c.put(y + h // 2, x + 4, "上次错误: " + self.error, "bad", w=w - 8)
            c.put(y + h // 2 + 1, x + 4, "请确认内核已启动 (--api/--port/--secret 指定控制器), 每 3 秒自动重试", "dim", w=w - 8)
        self.button(y + h // 2 + 3, x + 4, "立即重试", lambda: self.refresh(False), key="r")

    def draw_footer(self, y, W):
        c = self.c
        x = 1
        if self.tasks:
            labels = " · ".join(self.tasks.values())
            prog = " ".join(v for v in self.progress.values())
            x += c.put(y, x, SPIN[self.frame % len(SPIN)] + " ", "accent", bold=True)
            x += c.put(y, x, labels + ("  " + prog if prog else ""), "accent", w=W // 2)
        elif self.msg and time.time() - self.msg[2] < 6:
            icon, fg = {"ok": ("✓", "ok"), "bad": ("✗", "bad"), "warn": ("!", "warn")}.get(
                self.msg[1], ("›", "accent"))
            x += c.put(y, x, icon + " ", fg, bold=True)
            x += c.put(y, x, self.msg[0], fg if self.msg[1] != "info" else "white", w=W * 2 // 3)
        hints = ([("↑↓", "页面"), ("⏎", "进入"), ("Esc", "返回")] if self.nav_focus
                 else list(self.page.hints) + [("?", "帮助")])
        parts, total = [], 0
        for k, label in hints:
            seg = tw(k) + 1 + tw(label) + 2
            if x + total + seg > W - 1:
                break
            parts.append((k, label))
            total += seg
        hx = W - 1 - total
        for k, label in parts:
            start = hx
            hx += c.put(y, hx, k, "accent", bold=True) + 1
            hx += c.put(y, hx, label, "dim") + 2
            self.hits.add(y, start, hx - start - 1, lambda k=k: self.handle_key(KEY_ALIAS.get(k, k)))

    # ---- 交互 ----
    def show(self, p):
        self.nav_focus = False
        self.select_page(p)

    def select_page(self, p):
        """切换当前页面；调用方决定是否保留左侧导航焦点。"""
        self.nav_sel = self.pages.index(p)
        if p is not self.page:
            self.page = p
            p.on_show()

    def help(self):
        self.modal(InfoModal("快捷键与操作", HELP))

    def quit(self):
        self.running = False

    def mouse_press(self, mx, my, dbl=False):
        self.nav_focus = False
        now = time.time()
        ly, lx, lt = self.last_click
        self.dbl = dbl or (ly == my and lx == mx and now - lt < 0.4)
        self.last_click = (my, mx, 0 if self.dbl else now)
        fn = Hits._find(self.hits.clicks, my, mx)
        if fn:
            fn()
        self.dbl = False

    def mouse_wheel(self, mx, my, up):
        fn = Hits._find(self.hits.wheels, my, mx)
        if fn:
            fn(-3 if up else 3)

    def handle_key(self, k):
        if isinstance(k, tuple):
            kind = k[0]
            if kind == "press":
                self.mouse_press(k[1], k[2])
            elif kind == "dbl":
                self.mouse_press(k[1], k[2], True)
            elif kind == "wheel":
                self.mouse_wheel(k[1], k[2], k[3])
            return
        if k in ("resize", "mouse"):
            return
        if k == "ctrl-c":
            self.quit()
            return
        if self.modals:
            self.modals[-1].key(self, k)
            return
        if self.nav_focus:
            n = len(self.pages)
            if k in ("up", "k"):
                self.nav_sel = (self.nav_sel - 1) % n
                self.select_page(self.pages[self.nav_sel])
            elif k in ("down", "j"):
                self.nav_sel = (self.nav_sel + 1) % n
                self.select_page(self.pages[self.nav_sel])
            elif k in ("right", "l", "enter", "space", "tab"):
                self.show(self.pages[self.nav_sel])
            elif k in ("left", "h", "esc", "btab"):
                self.nav_focus = False
            else:
                self.global_key(k)
            return
        if self.proxies is not None and self.page.key(k):
            return
        if k in ("left", "h", "esc"):
            self.nav_focus = True
            self.nav_sel = self.pages.index(self.page)
            return
        self.global_key(k)

    def global_key(self, k):
        if k == "q":
            self.quit()
        elif k in ("1", "2", "3", "4", "5"):
            self.show(self.pages[int(k) - 1])
        elif k == "?":
            self.help()
        elif k == "r":
            self.refresh(False)
        elif self.proxies is None:
            return
        elif k == "c":
            self.show(self.pages[1])
        elif k == "m":
            self.cycle_mode()
        elif k == "T":
            self.toggle_tun()
        elif k == "a":
            self.toggle_lan()
        elif k == "u":
            self.update_sub()
        elif k == "R":
            self.ask_restart()
        elif k == "t":
            self.conn_test()

    def run(self):
        self.refresh()
        self.start_traffic()
        self.start_logs()
        while self.running:
            while True:
                try:
                    self.events.get_nowait()()
                except queue.Empty:
                    break
            if not self.online and "refresh" not in self.tasks and time.time() - self.last_try > 3:
                self.refresh()
            for p in self.pages:
                p.tick()
            self.frame += 1
            self.draw()
            # 空闲界面无需每 150ms 全量重绘；输入会立即打断等待，
            # 因此降低空闲刷新频率不会增加键盘操作延迟。
            animated = bool(self.tasks or self.testing or self.pending or not self.online)
            for k in self.scr.read(ACTIVE_REFRESH_MS if animated else IDLE_REFRESH_MS):
                if not self.running:
                    break
                self.handle_key(k)


KEY_ALIAS = {"⏎": "enter", "Tab": "tab", "←→": "right", "↑↓": "down", "Esc": "esc"}
APP = None


def _env_port():
    try:
        return int(os.environ["MIHOMO_PORT"])
    except (KeyError, ValueError):
        return None


def parse_args():
    env = os.environ.get
    p = argparse.ArgumentParser(
        prog="tui-win.py",
        description="mihomo 控制台 TUI —— Windows Terminal 版 (纯标准库, 可独立分发). "
                    "所有参数均可省略, 默认自动探测控制器")
    p.add_argument("group", nargs="?", default=None,
                   help="启动时选中的代理组名 (默认自动选择主代理组)")
    p.add_argument("-a", "--api", metavar="ADDR", default=env("MIHOMO_API"),
                   help="控制器地址 http://127.0.0.1:9097, 或 pipe:管道名 / pipe:auto "
                        "(Windows 命名管道, 适配 Clash Verge 内核) (环境变量 MIHOMO_API)")
    p.add_argument("--host", metavar="HOST", default=env("MIHOMO_HOST"),
                   help="控制器主机, 默认 127.0.0.1 (环境变量 MIHOMO_HOST)")
    p.add_argument("-p", "--port", metavar="PORT", type=int, default=_env_port(),
                   help="控制器端口, 默认 9097 (环境变量 MIHOMO_PORT); --api/MIHOMO_API 存在时无效")
    p.add_argument("-s", "--secret", metavar="KEY", default=env("MIHOMO_SECRET"),
                   help="API 密钥 (环境变量 MIHOMO_SECRET); 默认从配置文件读取")
    return p.parse_args()


def main():
    global APP
    term = Term()
    try:
        term.setup()
    except OSError as e:
        sys.exit("初始化终端失败 (需要交互式终端): " + str(e))
    try:
        APP = App(term)
        APP.run()
    finally:
        term.leave()


if __name__ == "__main__":
    setup_controller(parse_args())
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        main()
    except KeyboardInterrupt:
        pass
