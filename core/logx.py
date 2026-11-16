# -*- coding: utf-8 -*-
"""
Companion Lite · 错误日志

- 写 data/logs/app.log，5 MB 自动轮转，保留 3 份
- 线程安全
- 未捕获异常（主线程/子线程）都会落盘
- 退出时把 stdout/stderr 接进日志（打包成 exe 后没有控制台，print 会丢）
"""
import os
import sys
import threading
import time
import traceback


class Log:
    def __init__(self, path, max_bytes=5 * 1024 * 1024, backups=3):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _rotate(self):
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                for i in range(self.backups - 1, 0, -1):
                    src = f"{self.path}.{i}"
                    dst = f"{self.path}.{i + 1}"
                    if os.path.exists(src):
                        os.replace(src, dst)
                os.replace(self.path, self.path + ".1")
        except Exception:
            pass

    def write(self, level, tag, msg, exc=None):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level:<5}] [{tag}] {msg}"
        if exc:
            line += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        line += "\n"
        with self.lock:
            try:
                self._rotate()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass
        # 开发模式下也打到控制台
        try:
            if sys.stderr and sys.stderr.isatty():
                sys.stderr.write(line)
        except Exception:
            pass

    def info(self, tag, msg):
        self.write("INFO", tag, msg)

    def warn(self, tag, msg):
        self.write("WARN", tag, msg)

    def error(self, tag, msg, exc=None):
        self.write("ERROR", tag, msg, exc)

    def tail(self, n=200):
        try:
            if not os.path.exists(self.path):
                return []
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            return [x.rstrip("\n") for x in lines[-n:]]
        except Exception:
            return []

    def install_hooks(self):
        """未捕获异常 + 标准输出重定向"""
        def excepthook(exc_type, exc, tb):
            self.write("ERROR", "uncaught", f"{exc_type.__name__}: {exc}",
                       exc if isinstance(exc, BaseException) else None)
        sys.excepthook = excepthook

        def thook(args):
            self.write("ERROR", "thread", f"{args.thread.name if args.thread else '?'}: {args.exc_type.__name__}: {args.exc_value}",
                       args.exc_value if isinstance(args.exc_value, BaseException) else None)
        try:
            threading.excepthook = thook
        except Exception:
            pass

    def redirect_std(self):
        """把 print 也写进日志（exe 没有控制台）"""
        log_self = self

        class Stream:
            def write(self, s):
                s = str(s).rstrip()
                if s:
                    log_self.write("INFO", "stdout", s)

            def flush(self):
                pass

            def isatty(self):
                return False

        sys.stdout = Stream()
        sys.stderr = Stream()
