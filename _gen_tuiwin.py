# -*- coding: utf-8 -*-
"""由 tui.py 生成可独立分发的 tui-win.py (Windows Terminal 专用版).

tui.py 内置 Win32/POSIX 双终端层; 本脚本删除
    # >>> posix >>>  ...  # <<< posix <<<
标记的 POSIX 专属代码, 换上 tui-win 的文件头 docstring 与程序名, 其余原样保留."""
import re

SRC = open("tui.py", encoding="utf-8").read()

NEW_DOC = '''#!/usr/bin/env python3
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
'''


def rep(s, old, new):
    n = s.count(old)
    assert n == 1, "锚点应出现 1 次, 实际 %d 次: %r" % (n, old[:60])
    return s.replace(old, new)


# ──────────── 删除 POSIX 专属代码块 (标记行一并去掉) ────────────

out, n = re.subn(r"(?ms)^[ \t]*# >>> posix >>>[^\n]*\n.*?^[ \t]*# <<< posix <<<[^\n]*\n",
                 "", SRC)
assert n == 2, "posix 标记块应为 2 段 (导入 / 终端层), 实际 %d" % n
out = re.sub(r"\n{4,}", "\n\n\n", out)      # 裁剪处多余空行收敛为两个

# ──────────── 文件头 docstring 换成 tui-win 版, 其余原样 ────────────

out = NEW_DOC + out[out.index("import argparse"):]

out = rep(out, "Term = _WinTerm if IS_WIN else _PosixTerm", "Term = _WinTerm")
out = rep(out, 'prog="tui.py",', 'prog="tui-win.py",')
out = rep(out,
          'description="mihomo 控制台 TUI —— Clash Verge 轻量平替 (纯标准库, 支持鼠标). "',
          'description="mihomo 控制台 TUI —— Windows Terminal 版 (纯标准库, 可独立分发). "')

with open("tui-win.py", "w", encoding="utf-8", newline="\n") as f:
    f.write(out)
print("已生成 tui-win.py")
