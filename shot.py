#!/usr/bin/env python3
"""自动化截图 (dev-only, 需 PIL + 中文字体, 不进分发包).

用内置假 mihomo 服务提供确定性数据, 无头驱动 App 到各状态,
把终端网格渲染成 PNG:

  python shot.py              # 重抓 docs/screenshot1~6.png + docs/shots/ 附加状态
  python shot.py --list       # 列出可抓的状态
  python shot.py proxies connections   # 只抓指定状态

注意: 输出为纯 TUI 网格图 (无终端窗口边框), 与旧实拍图风格不同, 但可复现.
数据全部固定 (延迟 md5 定死、时间戳相对当前时间), 适合回归对比.
"""
import copy
import hashlib
import json
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
ARGS = sys.argv[1:]  # 先留存, tui 导入不碰 argv, 但以防万一

import tui

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("需要 Pillow: pip install pillow")

TMP = tempfile.mkdtemp()
tui.PREFS_FILE = os.path.join(TMP, ".tui.json")
tui.CONFIG_FILE = os.path.join(TMP, "config.yaml")
tui.USAGE_FILE = os.path.join(TMP, ".usage.json")
tui.MANAGED = False
tui.SECRET = ""
tui.API_PIPE = None

NODES = ["香港 01", "香港 02", "日本 01", "日本 02", "新加坡 01",
         "台湾 01", "韩国 01", "美国 01", "美国 02", "德国 01"]
TYPES = ["Shadowsocks", "Vmess", "Trojan", "Hysteria2", "Shadowsocks",
         "Vmess", "Trojan", "Hysteria2", "Shadowsocks", "Vmess"]


def delay_of(name):
    if name == "德国 01":
        return 0  # 超时行
    return 20 + int(hashlib.md5(name.encode()).hexdigest(), 16) % 170


def ts_ago(sec):
    """相对当前时间往前推 sec 秒, 输出 mihomo 风格时间串 (截图时长稳定)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - sec))


STATE = {
    "cfg": {"mode": "rule", "tun": {"enable": True, "stack": "gvisor"},
            "allow-lan": False, "mixed-port": 7890, "port": 0,
            "socks-port": 7891, "log-level": "info", "secret": ""},
    "proxies": {
        "GLOBAL": {"type": "Selector", "all": ["Proxies", "Auto", "DIRECT"],
                   "now": "Proxies"},
        "Proxies": {"type": "Selector",
                    "all": NODES + ["DIRECT"], "now": "香港 01"},
        "Auto": {"type": "URLTest", "all": NODES, "now": "日本 01"},
    },
    "conns": [
        {"id": "c1", "metadata": {"host": "www.youtube.com", "destinationIP": "142.0.0.1",
                                  "destinationPort": "443", "network": "TCP",
                                  "sourceIP": "192.168.1.10", "sourcePort": "51234",
                                  "process": "chrome"},
         "rule": "FINAL", "chains": ["Proxies", "香港 01"],
         "upload": 300000, "download": 8000000, "start": ts_ago(9 * 3600 + 9 * 60)},
        {"id": "c2", "metadata": {"host": "github.com", "destinationIP": "20.0.0.1",
                                  "destinationPort": "443", "network": "TCP",
                                  "sourceIP": "192.168.1.10", "sourcePort": "51235",
                                  "process": "code"},
         "rule": "GEOSITE", "chains": ["Proxies", "日本 01"],
         "upload": 50000, "download": 900000, "start": ts_ago(9 * 3600 + 4 * 60)},
        {"id": "c3", "metadata": {"host": "", "destinationIP": "8.8.8.8",
                                  "destinationPort": "53", "network": "UDP",
                                  "sourceIP": "192.168.1.10", "sourcePort": "51236"},
         "rule": "DIRECT", "chains": ["DIRECT"],
         "upload": 1000, "download": 2000, "start": ts_ago(9 * 3600)},
    ],
    "rules": [
        {"index": 0, "type": "DOMAIN-SUFFIX", "payload": "google.com",
         "proxy": "Proxies", "extra": {"hitCount": 128, "hitAt": ts_ago(3600),
                                       "missCount": 3, "disabled": False}},
        {"index": 1, "type": "GEOSITE", "payload": "geolocation-!cn",
         "proxy": "Proxies", "extra": {"hitCount": 64, "hitAt": ts_ago(3500),
                                       "missCount": 0, "disabled": False}},
        {"index": 2, "type": "MATCH", "payload": "", "proxy": "DIRECT", "extra": {}},
    ],
    "providers": {"rep": {"name": "rep", "behavior": "domain", "vehicleType": "HTTP",
                          "ruleCount": 15000, "updatedAt": ts_ago(2 * 3600),
                          "path": "/data/rep.yaml"}},
}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass

    def _route(self):
        p = urllib.parse.urlsplit(self.path).path
        m = self.command
        body = self._body() if m in ("PATCH", "PUT", "POST") else None
        if m == "GET" and p == "/version":
            return self._send({"version": "v1.19.5"})
        if m == "GET" and p == "/proxies":
            return self._send({"proxies": STATE["proxies"]})
        if m == "GET" and p == "/configs":
            return self._send(STATE["cfg"])
        if m == "PATCH" and p == "/configs":
            for k, v in (body or {}).items():
                if isinstance(v, dict) and isinstance(STATE["cfg"].get(k), dict):
                    STATE["cfg"][k].update(v)
                else:
                    STATE["cfg"][k] = v
            return self._send({})
        if m == "PUT" and p == "/configs":
            return self._send({})
        if m == "POST" and p == "/restart":
            return self._send({})
        if p.startswith("/proxies/") and p.endswith("/delay"):
            name = urllib.parse.unquote(p[len("/proxies/"):-len("/delay")])
            return self._send({"delay": delay_of(name)})
        if m == "PUT" and p.startswith("/proxies/"):
            name = urllib.parse.unquote(p[len("/proxies/"):])
            if isinstance(body, dict) and body.get("name"):
                STATE["proxies"].setdefault(name, {}).update({"now": body["name"]})
            return self._send({})
        if m == "GET" and p == "/connections":
            return self._send({"connections": STATE["conns"],
                               "downloadTotal": 9000000, "uploadTotal": 400000})
        if m == "DELETE" and p.startswith("/connections"):
            return self._send({})
        if m == "GET" and p == "/rules":
            return self._send({"rules": STATE["rules"]})
        if m == "GET" and p == "/providers/rules":
            return self._send({"providers": STATE["providers"]})
        return self._send({"message": "not found"}, 404)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _route


for i, n in enumerate(NODES):
    STATE["proxies"][n] = {"type": TYPES[i],
                           "history": [{"delay": delay_of(n)}]}


# ---------- 无头屏幕 ----------
class ShotScr:
    def __init__(self, h=36, w=128):
        self.H, self.W = h, w
        self.cells = [[(" ", ("default", "default", False)) for _ in range(w)]
                      for _ in range(h)]

    def getmaxyx(self):
        return (self.H, self.W)

    def draw_text(self, y, x, s, attr, room):
        cx = x
        for ch in s:
            w = tui.cw(ch)
            if w <= 0 or cx >= self.W:
                continue
            if 0 <= y < self.H and cx >= 0:
                self.cells[y][cx] = (ch, attr)
                if w == 2 and cx + 1 < self.W:
                    self.cells[y][cx + 1] = (None, attr)
            cx += w
        return tui.tw(s)

    def erase(self):
        self.cells = [[(" ", ("default", "default", False)) for _ in range(self.W)]
                      for _ in range(self.H)]

    def refresh(self):
        pass

    def read(self, ms):
        return []


# ---------- 256 色 -> RGB ----------
_STD = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128),
        (128, 0, 128), (0, 128, 128), (192, 192, 192), (128, 128, 128),
        (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255), (255, 0, 255),
        (0, 255, 255), (255, 255, 255)]
_LV = [0, 95, 135, 175, 215, 255]


def xterm(i):
    if i < 0:
        return None
    if i < 16:
        return _STD[i]
    if i < 232:
        i -= 16
        return (_LV[i // 36], _LV[(i // 6) % 6], _LV[i % 6])
    g = 8 + 10 * (i - 232)
    return (g, g, g)


FG_DEFAULT = (204, 204, 204)
BG_DEFAULT = (12, 12, 12)

MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONOB = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"  # 盲文 spinner 专属
CJK = "/usr/share/fonts/truetype/arphic/uming.ttc"
FS = 19

_font_cache = {}


def is_cjk(ch):
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or
            0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF or
            0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF)


def pick_font(mono, monob, sans, cjk, ch, bold):
    """中日韩走 UMing, 盲文走 DejaVu Sans (Mono 缺盲文), 其余走 DejaVu Mono."""
    key = (ch, bold)
    if key not in _font_cache:
        o = ord(ch)
        if is_cjk(ch):
            _font_cache[key] = cjk
        elif 0x2800 <= o <= 0x28FF:
            _font_cache[key] = sans
        else:
            _font_cache[key] = monob if bold else mono
    return _font_cache[key]


def tofu_audit(mono, monob, sans, cjk, chars):
    """像素级比对 .notdef, 揪出两边都画不出的字符 (新增 UI 字形时防呆)."""
    from PIL import Image as _I, ImageDraw as _D

    def ink(font, ch):
        img = _I.new("L", (40, 40), 0)
        _D.Draw(img).text((2, 2), ch, font=font, fill=255)
        return list(img.getdata())

    refs = {id(f): ink(f, "\U0010FFFF") for f in (mono, monob, sans, cjk)}
    bad = sorted({ch for ch in chars if ch.strip() and all(
        ink(f, ch) == refs[id(f)] for f in (mono, monob, sans, cjk))})
    if bad:
        print("警告: 以下字符在本地字体中全是 tofu: %s" % " ".join(bad))
    return bad


# 制表符走程序化绘制, 保证跨格无缝 (字体自带的行块字在小字号下会断成虚线)
_BOX_ARMS = {  # 以格子中心为原点: (x0,y0,x1,y1) 相对坐标, 半格为单位
    "│": [(0, -1, 0, 1)],
    "─": [(-1, 0, 1, 0)],
    "┌": [(0, 0, 1, 0), (0, 0, 0, 1)],
    "┐": [(-1, 0, 0, 0), (0, 0, 0, 1)],
    "└": [(0, 0, 1, 0), (0, -1, 0, 0)],
    "┘": [(-1, 0, 0, 0), (0, -1, 0, 0)],
    "├": [(0, 0, 1, 0), (0, -1, 0, 1)],
    "┤": [(-1, 0, 0, 0), (0, -1, 0, 1)],
    "┬": [(-1, 0, 1, 0), (0, 0, 0, 1)],
    "┴": [(-1, 0, 1, 0), (0, -1, 0, 0)],
    "┼": [(-1, 0, 1, 0), (0, -1, 0, 1)],
    "╭": [(0, 0, 1, 0), (0, 0, 0, 1)],
    "╮": [(-1, 0, 0, 0), (0, 0, 0, 1)],
    "╰": [(0, 0, 1, 0), (0, -1, 0, 0)],
    "╯": [(-1, 0, 0, 0), (0, -1, 0, 0)],
}


def render(scr, path):
    mono = ImageFont.truetype(MONO, FS)
    monob = ImageFont.truetype(MONOB, FS)
    sans = ImageFont.truetype(SANS, FS)
    cjk = ImageFont.truetype(CJK, FS, index=0)
    tofu_audit(mono, monob, sans, cjk,
               {ch for row in scr.cells for ch, _ in row if ch})
    cw = mono.getsize("M")[0]
    asc, desc = mono.getmetrics()
    ch = asc + desc + 8
    img = Image.new("RGB", (scr.W * cw, scr.H * ch), BG_DEFAULT)
    d = ImageDraw.Draw(img)
    # 第一遍: 所有格子 (含宽字符占位格) 填背景
    for y in range(scr.H):
        for x in range(scr.W):
            _, (_, bg, _) = scr.cells[y][x]
            bgrgb = xterm(tui.PALETTE.get(bg, -1)) or BG_DEFAULT
            if bgrgb != BG_DEFAULT:
                d.rectangle([x * cw, y * ch, (x + 1) * cw, (y + 1) * ch], fill=bgrgb)
    # 第二遍: 字形
    for y in range(scr.H):
        for x in range(scr.W):
            ch_, (fg, bg, bold) = scr.cells[y][x]
            if ch_ is None or ch_ == " ":
                continue
            fgrgb = xterm(tui.PALETTE.get(fg, -1)) or FG_DEFAULT
            if ch_ in _BOX_ARMS:
                cx, cy = x * cw + cw // 2, y * ch + ch // 2
                for ax0, ay0, ax1, ay1 in _BOX_ARMS[ch_]:
                    d.line([cx + ax0 * cw // 2, cy + ay0 * ch // 2,
                            cx + ax1 * cw // 2, cy + ay1 * ch // 2],
                           fill=fgrgb, width=2)
                continue
            if ch_ == "▌":
                d.rectangle([x * cw, y * ch, x * cw + cw // 2, (y + 1) * ch],
                            fill=fgrgb)
                continue
            wide = tui.cw(ch_) == 2
            f = pick_font(mono, monob, sans, cjk, ch_, bold)
            tw = f.getsize(ch_)[0]
            span = 2 * cw if wide else cw
            px = x * cw + max(0, (span - tw) // 2)
            d.text((px, y * ch + 3), ch_, font=f, fill=fgrgb)
    img.save(path)
    print("saved", path, img.size)


def drain(app, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            app.events.get_nowait()()
        except queue.Empty:
            if not app.tasks:
                return
            time.sleep(0.05)
    raise RuntimeError("drain 超时, 残留任务: %s" % app.tasks)


def online_app():
    app = tui.App(ShotScr())
    app.refresh()
    drain(app)
    assert app.online, "假服务连不上"
    app.traffic = (256061, 5138022)  # ↑ 250.1 KB/s ↓ 4.9 MB/s
    return app


# ---------- 状态 ----------
def st_proxies(app):
    app.show(app.pages[0])
    pg = app.pages[0]
    pg.test_all()
    drain(app)
    app.draw()  # 先走一次 sync, 初始化 gname/nname
    pg.nname = "韩国 01"  # sync 会按 nname 对齐选中行
    app.handle_key("enter")  # 经正常按键链路切换, footer 留下 toast
    drain(app)
    app.draw()


def st_connections(app):
    app.show(app.pages[1])
    cp = app.pages[1]
    cp.last = 0
    cp.tick()
    drain(app)
    end = time.time() + 10
    while cp.inflight and time.time() < end:
        drain(app, timeout=2)
        time.sleep(0.05)
    for c, (du, up) in zip(STATE["conns"], [(9000000, 400000),
                                                 (500000, 50000),
                                                 (3000, 1500)]):
        c["upload"] += up
        c["download"] += du
    cp.last = 0  # 绕过 1 秒节流, 保证第二次采样生效
    cp.tick()
    end = time.time() + 10
    while cp.inflight and time.time() < end:
        drain(app, timeout=2)
        time.sleep(0.05)
    drain(app)
    app.draw()


def st_rules(app):
    app.show(app.pages[2])
    drain(app)
    app.draw()


def st_logs(app):
    app.show(app.pages[3])
    for i in range(8):
        app.add_log("info", "TCP 192.168.1.%d:5123%d -> example%d.com:443 PROXY" % (i, i, i))
    app.add_log("warning", "DNS 劫持到 198.18.0.5 (fake-ip)")
    app.add_log("error", "代理 德国 01 连接超时, 10s 后重试")
    for i in range(8, 14):
        app.add_log("info", "UDP 192.168.1.%d:53410 -> dns:53 DIRECT" % i)
    # 时间戳钉死, 截图可复现
    app.logs = tui.deque([("12:00:%02d" % (i % 60), lv, m)
                          for i, (_, lv, m) in enumerate(app.logs)],
                         maxlen=2000)
    app.draw()


def st_settings(app):
    app.show(app.pages[4])
    sp = app.pages[4]
    sp.slist.sel = 3  # 订阅与内核
    sp.focus = 1
    app.draw()


def st_usage(app):
    app.show(app.pages[5])
    now = time.time()
    day_hosts = {
        "www.youtube.com": [7_800_000_000, 120_000_000, now - 20],
        "github.com": [820_000_000, 35_000_000, now - 180],
        "cdn.jsdelivr.net": [310_000_000, 8_000_000, now - 900],
        "api.openai.com": [120_000_000, 6_000_000, now - 3600],
        "steamcommunity.com": [60_000_000, 2_000_000, now - 7200],
    }
    total_hosts = dict((k, [int(v[0] * 1.6), int(v[1] * 1.6), v[2]])
                       for k, v in day_hosts.items())
    app.usage = {
        "up": sum(v[1] for v in total_hosts.values()),
        "down": sum(v[0] for v in total_hosts.values()),
        "rows": {"host": total_hosts},
        "day": time.strftime("%Y-%m-%d"),
        "day_up": sum(v[1] for v in day_hosts.values()),
        "day_down": sum(v[0] for v in day_hosts.values()),
        "day_rows": {"host": day_hosts},
        "seen": {},
    }
    app.usage_active = 3
    app.draw()


def st_offline():
    old, tui.API = tui.API, "http://127.0.0.1:9097"  # 文档图用常规端口展示
    old_hint = tui.STARTUP_HINT
    tui.STARTUP_HINT = ("未找到 config.yaml; 请用 --config 指定内核配置, "
                        "或用 -a/-p/-s 直接指定控制器与密钥, 或使用 --managed 托管模式")
    try:
        app = tui.App(ShotScr())
        app.draw()
    finally:
        tui.API, tui.STARTUP_HINT = old, old_hint
    return app


def st_confirm(app):
    app.show(app.pages[0])
    app.handle_key("R")
    app.draw()


def st_help(app):
    app.show(app.pages[0])
    app.handle_key("?")
    app.draw()


STATES = [
    ("proxies", "docs/screenshot1.png", True, st_proxies),
    ("connections", "docs/screenshot2.png", True, st_connections),
    ("rules", "docs/screenshot3.png", True, st_rules),
    ("logs", "docs/screenshot4.png", True, st_logs),
    ("settings", "docs/screenshot5.png", True, st_settings),
    ("usage", "docs/screenshot6.png", True, st_usage),
    ("offline", "docs/shots/offline.png", False, lambda app: None),
    ("confirm", "docs/shots/confirm.png", True, st_confirm),
    ("help", "docs/shots/help.png", True, st_help),
]


def main():
    args = ARGS
    if args == ["--list"]:
        for name, path, online, _ in STATES:
            print("%-12s -> %s" % (name, path))
        return
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tui.API = "http://127.0.0.1:%d" % srv.server_address[1]

    want = args or [n for n, _, _, _ in STATES[:6]]
    app = online_app() if any(n for n, _, on, _ in STATES if n in want and on) else None
    if app is not None:
        # 文档图用体面的展示值 (请求仍走假服务端口)
        tui.CONTROLLER_DESC = "http://127.0.0.1:9097"
        tui.CONFIG_FILE = "~/.config/mihomo/config.yaml"
    for name, path, need_online, fn in STATES:
        if name not in want:
            continue
        a = app if need_online else st_offline()
        if need_online:
            a.modals.clear()  # 状态间互不串门
            if name != "proxies":
                a.msg = None  # 只保留 proxies 那张的切换 toast, 其余钉死无 toast
            fn(a)
        full = os.path.join(HERE, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        render(a.scr, full)


if __name__ == "__main__":
    main()
