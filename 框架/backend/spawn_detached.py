#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以**脱离会话**的方式启动一条长驻命令，并可靠地拿到孙进程 PID。

为什么需要它（本机实测结论，不是猜测）：
  - `nohup cmd &` 与工具自带的"后台任务"模式，都会在**工具调用结束后被回收**，
    进程在 1~2 分钟内消失，日志停在某一刻、既不报错也无退出码。
  - 只有真正**新建会话**（`os.setsid()`）并重定向标准流、且父进程不等待，
    进程才能跨工具调用长驻。

实现：双重 `fork`。
  父进程 ──fork──> 中间进程 ──fork──> 孙进程（setsid + 重定向 + execvp）
  中间进程立刻 `_exit`，孙进程被 init 收养，不会变成僵尸。
  孙进程启动后**自己把 PID 写入 pid 文件**，父进程轮询读回，因此拿到的
  是真正干活的孙进程 PID，而不是转瞬即逝的中间进程。

用法：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  $PY spawn_detached.py --pid logs/x.pid --log logs/x.log --cwd . -- CMD ARG...
  # 成功时向 stdout 打印孙进程 PID（整数），退出码 0
"""
import argparse
import os
import sys
import time


def spawn(argv, cwd, log_path, pid_path, timeout=15.0):
    # 必须在 fork/chdir **之前**解析成绝对路径：孙进程会先 chdir 到 cwd，
    # 之后相对路径会被错误地二次拼接（曾导致 pid 文件写不出、启动静默失败）。
    cwd = os.path.abspath(cwd)
    log_path = os.path.abspath(log_path)
    pid_path = os.path.abspath(pid_path)
    try:
        os.unlink(pid_path)
    except FileNotFoundError:
        pass
    pid = os.fork()
    if pid == 0:                       # 中间进程
        try:
            os.setsid()                # 成为新会话组长，脱离控制终端
        except OSError:
            os._exit(126)
        if os.fork() == 0:             # 孙进程：真正干活的那个
            try:
                os.chdir(cwd)
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                os.dup2(fd, 1)
                os.dup2(fd, 2)
                os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
                with open(pid_path, "w", encoding="ascii") as f:
                    f.write(str(os.getpid()))
                    f.flush()
                    os.fsync(f.fileno())
                os.execvp(argv[0], argv)
            except BaseException:
                os._exit(127)
        os._exit(0)                    # 中间进程不等待，孙进程被 init 收养
    os.waitpid(pid, 0)                 # 回收中间进程
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(pid_path, encoding="ascii") as f:
                value = f.read().strip()
            if value:
                return int(value)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.05)
    raise RuntimeError("孙进程未在超时内写出 PID")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        ap.error("缺少要启动的命令")
    print(spawn(cmd, a.cwd, a.log, a.pid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
