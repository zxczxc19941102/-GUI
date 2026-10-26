# -*- coding: utf-8 -*-
"""RiverNexus 自动签到：Tkinter GUI、北京时间随机定时及标准库 HTTP API。"""
from __future__ import annotations

import configparser
import http.cookiejar
import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from tkinter import END, StringVar, Tk, TclError, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_TITLE = "RiverNexus 自动签到工具"
SITE_HOME = "https://rivernexus.vip/"
WELFARE_URL = "https://rivernexus.vip/welfare"
LOGIN_API = "/api/user/login"
CHECKIN_API = "/api/user/checkin"
PROXY_URL = "http://127.0.0.1:10808"
API_TIMEOUT_SECONDS = 8
MAX_GUI_LOG_LINES = 3000
try:
    BJ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    BJ = timezone(timedelta(hours=8), name="Asia/Shanghai")

ROOT = Path(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)))
CONFIG_PATH = ROOT / "config.ini"
STATE_PATH = ROOT / "state.json"
LOG_DIR = ROOT / "logs"
EMPTY_STATE = {"last_sign_date": "", "last_sign_time": "", "last_amount": "", "next_sign_time": ""}
CONFIG_TEMPLATE = """[account]\nusername =\npassword =\n\n[settings]\nretry_interval_minutes = 30\n"""


def now_bj() -> datetime:
    """返回北京时间当前时刻，不读取系统本地时区。"""
    return datetime.now(BJ)


def fmt(value: datetime | None) -> str:
    """格式化北京时间，精确到秒。"""
    return value.astimezone(BJ).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def parse_bj(value: str) -> datetime | None:
    """解析 state.json 中保存的北京时间。"""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ) if value else None
    except (TypeError, ValueError):
        return None


def ensure_files() -> None:
    """创建仅含空凭据的本地配置模板和运行目录。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    if not STATE_PATH.exists():
        write_state(EMPTY_STATE)


def load_config() -> configparser.ConfigParser:
    """读取本地配置；账户密码不写入源代码。"""
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8-sig")
    for section in ("account", "settings"):
        if not parser.has_section(section):
            parser.add_section(section)
    for key, value in (("username", ""), ("password", "")):
        if not parser.has_option("account", key):
            parser.set("account", key, value)
    if not parser.has_option("settings", "retry_interval_minutes"):
        parser.set("settings", "retry_interval_minutes", "30")
    return parser


def save_config(parser: configparser.ConfigParser) -> None:
    """原子写入账号配置文件。"""
    temp = CONFIG_PATH.with_suffix(".ini.tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        parser.write(stream)
    os.replace(temp, CONFIG_PATH)


def write_state(state: dict[str, str]) -> None:
    """通过临时文件原子更新签到状态。"""
    value = {key: str(state.get(key, "") or "") for key in EMPTY_STATE}
    temp = STATE_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, STATE_PATH)


def read_state(logger: logging.Logger) -> dict[str, str]:
    """读取本地状态；文件损坏时记录警告并恢复空状态。"""
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("state.json 顶层不是对象")
        return {key: str(value.get(key, "") or "") for key in EMPTY_STATE}
    except FileNotFoundError:
        write_state(EMPTY_STATE)
        return dict(EMPTY_STATE)
    except Exception:
        logger.exception("签到状态文件损坏，正在重建空状态")
        write_state(EMPTY_STATE)
        return dict(EMPTY_STATE)


class BeijingFormatter(logging.Formatter):
    """将日志时间固定为北京时间。"""
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, BJ).strftime(datefmt or "%Y-%m-%d %H:%M:%S")


class DailyFileHandler(logging.Handler):
    """按北京时间日期写入每日 UTF-8 日志。"""
    def __init__(self) -> None:
        super().__init__()
        self.day = ""
        self.stream = None
        self.lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        with self.lock:
            try:
                day = now_bj().strftime("%Y%m%d")
                if day != self.day or self.stream is None:
                    if self.stream:
                        self.stream.close()
                    LOG_DIR.mkdir(parents=True, exist_ok=True)
                    self.stream = (LOG_DIR / f"log_{day}.log").open("a", encoding="utf-8")
                    self.day = day
                self.stream.write(self.format(record) + "\n")
                self.stream.flush()
            except Exception:
                self.handleError(record)

    def close(self) -> None:
        with self.lock:
            if self.stream:
                self.stream.close()
                self.stream = None
        super().close()


class GuiLogHandler(logging.Handler):
    """通过队列把日志发往 Tk 主线程。"""
    def __init__(self, events: queue.Queue[dict[str, Any]]) -> None:
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put({"type": "log", "text": self.format(record)})


def make_logger(events: queue.Queue[dict[str, Any]]) -> logging.Logger:
    """配置文件与 GUI 双通道日志。"""
    logger = logging.getLogger("rivernexus")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    formatter = BeijingFormatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler, gui_handler = DailyFileHandler(), GuiLogHandler(events)
    file_handler.setFormatter(formatter)
    gui_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(gui_handler)
    return logger


class SignError(RuntimeError):
    """登录、接口或签到操作失败。"""


def request_json(opener: Any, path: str, method: str, payload: dict[str, Any] | None,
                 token: str = "", cancel: threading.Event | None = None) -> dict[str, Any]:
    """发送 JSON API 请求，将 HTTP/网络/鉴权错误转成中文异常。"""
    if cancel and cancel.is_set():
        raise SignError("操作已停止")
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
               "Origin": SITE_HOME.rstrip("/"), "Referer": SITE_HOME}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(SITE_HOME.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=API_TIMEOUT_SECONDS) as response:
            status = response.status
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            body_data = json.loads(text)
        except json.JSONDecodeError:
            body_data = {}
        nested = body_data.get("error") if isinstance(body_data.get("error"), dict) else {}
        message = body_data.get("message") or nested.get("message") or text[:300] or f"HTTP {exc.code}"
        if exc.code in (401, 403):
            raise SignError(f"账号或密码错误，或登录已失效：{message}") from exc
        raise SignError(f"服务器错误（HTTP {exc.code}）：{message}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        raise SignError(f"网络连接失败，请检查代理 {PROXY_URL}：{reason}") from exc
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        raise SignError(f"服务器返回非 JSON 数据（HTTP {status}）：{text[:300]}") from exc
    if not isinstance(data, dict):
        raise SignError("服务器响应格式错误：预期 JSON 对象")
    if status >= 400:
        raise SignError(f"服务器错误（HTTP {status}）：{data.get('message') or data.get('error') or '请求失败'}")
    return data


def walk_dicts(obj: Any):
    """深度遍历接口响应中的对象，便于兼容不同字段嵌套。"""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_dicts(value)


def field_value(data: Any, keys: tuple[str, ...], labels: tuple[str, ...]) -> str:
    """从接口数据按字段名或中文标签读取统计值。"""
    normalized = {key.casefold() for key in keys}
    for item in walk_dicts(data):
        for key, value in item.items():
            if str(key).casefold() in normalized and value is not None and not isinstance(value, (dict, list)):
                return str(value)
            if any(label in str(key) for label in labels) and value is not None and not isinstance(value, (dict, list)):
                return str(value)
    return "—"


def extract_reward(data: Any, text: str) -> str:
    """只提取本次签到奖励字段，不把累计额度误报成本次奖励。"""
    for item in walk_dicts(data):
        for key in ("quota_awarded", "awarded_quota", "reward_quota", "reward_amount", "checkin_reward"):
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return f"{value:g} 积分" if isinstance(value, float) else f"{value} 积分"
    match = re.search(r"(?:\u83b7\u5f97|\u5956\u52b1|\u8d60\u9001|\u7b7e\u5230\u6210\u529f[^0-9]*)\s*([0-9]+(?:\.[0-9]+)?)\s*(\u5143|USDT|RMB|\u91d1\u5e01|\u79ef\u5206|GTC)?", text, re.I)
    if not match:
        return "\u672a\u8bc6\u522b"
    return f"{match.group(1)} {match.group(2) or ''}".strip()


def parse_welfare_stats(data: Any) -> dict[str, str]:
    """读取福利接口中累计签到、本月获得、累计获得统计。"""
    return {
        "streak": field_value(data, ("total_checkin_days", "checkin_days", "total_days", "sign_days", "continuous_days", "checkin_count"), ("累计签到",)),
        "month": field_value(data, ("monthly_earned", "month_earned", "month_reward", "monthly_reward", "this_month_reward"), ("本月获得",)),
        "total": field_value(data, ("total_earned", "total_reward", "total_rewards", "cumulative_reward", "all_time_reward"), ("累计获得",)),
    }


def sign_once(username: str, password: str, cancel: threading.Event, logger: logging.Logger) -> dict[str, Any]:
    """登录校验、读取签到统计并通过签到 POST 接口提交签到。"""
    proxy_handler = urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
    opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    logger.info("正在登录 %s", "https://rivernexus.vip/sign-in")
    login = request_json(opener, LOGIN_API, "POST", {"username": username, "password": password}, cancel=cancel)
    if login.get("success") is not True:
        message = login.get("message") or login.get("error") or "服务器拒绝登录"
        raise SignError(f"账号或密码错误/登录失败：{message}")
    login_data = login.get("data") if isinstance(login.get("data"), dict) else {}
    token = str(login_data.get("access_token") or login_data.get("token") or "")
    if not token:
        raise SignError("登录响应未包含有效令牌，无法确认账号密码校验成功")
    logger.info("登录校验成功")

    logger.info("读取福利签到数据：%s", WELFARE_URL)
    status = request_json(opener, CHECKIN_API, "GET", None, token, cancel)
    stats = parse_welfare_stats(status)
    status_data = status.get("data") if isinstance(status.get("data"), dict) else {}
    messages = [str(item.get("message", "")) for item in walk_dicts(status) if item.get("message")]
    checked = any(item.get("checked_in_today") is True or item.get("already_signed") is True for item in walk_dicts(status))
    checked = checked or any(any(word in message for word in ("\u4eca\u65e5\u5df2\u7b7e\u5230", "\u5df2\u7ecf\u7b7e\u5230", "\u5df2\u5b8c\u6210\u7b7e\u5230")) for message in messages)
    if checked:
        message = str(status.get("message") or status_data.get("message") or "今日已签到")
        reward = extract_reward(status, message)
        logger.info("今日已签到：%s", message)
        return {"already": True, "reward": reward, "message": message, "stats": stats}

    if cancel.is_set():
        raise SignError("操作已停止")
    logger.info("执行立即签到接口：POST %s", CHECKIN_API)
    result = request_json(opener, CHECKIN_API, "POST", {}, token, cancel)
    message = str(result.get("message") or result.get("error") or json.dumps(result, ensure_ascii=False))
    success = result.get("success") is True
    already = "已签到" in message or "已经签到" in message
    if not success and not already:
        raise SignError(f"签到失败：{message}")
    reward_data = result.get("data") if result.get("data") is not None else result
    reward = extract_reward(reward_data, message)
    if already:
        logger.info("服务器提示今日已签到：%s", message)
    else:
        logger.info("签到成功，奖励：%s；服务器提示：%s", reward, message)
    refreshed = parse_welfare_stats(result)
    for key, value in stats.items():
        if refreshed[key] == "—":
            refreshed[key] = value
    return {"already": already, "reward": reward, "message": message, "stats": refreshed}


def random_next_day(base: datetime) -> datetime:
    """生成次日 00:00:00 至 11:59:59 的北京时间随机时刻。"""
    midnight = (base.astimezone(BJ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(seconds=random.randint(0, 43199))


def random_today(now: datetime) -> datetime:
    """在今天剩余 00:00-12:00 区间随机排程；窗口已过则立即签到。"""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=12, minute=0, second=0, microsecond=0)
    low = max(int((now + timedelta(minutes=1)).timestamp()), int(start.timestamp()))
    high = int((end - timedelta(seconds=1)).timestamp())
    if now >= end or low > high:
        return now.replace(microsecond=0)
    return datetime.fromtimestamp(random.randint(low, high), BJ)


class App:
    """简单界面控制器；网络和调度在线程运行，控件仅由主线程更新。"""
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.logger = make_logger(self.events)
        self.config = load_config()
        self.state = read_state(self.logger)
        self.guard = threading.RLock()
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.scheduler: threading.Thread | None = None
        self.signing = False
        self.scheduler_enabled = False
        self.closing = False
        self.build_gui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(150, self.consume_events)
        self.root.after(500, self.refresh_countdown)
        self.root.minsize(680, 520)
        self.logger.info("程序启动，所有时间使用北京时间")

    def build_gui(self) -> None:
        """搭建简洁账号、状态统计、定时按钮和日志界面。"""
        self.root.title(APP_TITLE)
        self.root.geometry("760x620")
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        account = ttk.LabelFrame(outer, text="账号", padding=10)
        account.pack(fill="x")
        self.username = StringVar(value=self.config.get("account", "username", fallback=""))
        self.password = StringVar(value=self.config.get("account", "password", fallback=""))
        ttk.Label(account, text="邮箱").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=4)
        ttk.Entry(account, textvariable=self.username, width=32).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(account, text="密码").grid(row=0, column=2, sticky="w", padx=(16, 5), pady=4)
        ttk.Entry(account, textvariable=self.password, width=26, show="*").grid(row=0, column=3, sticky="ew", pady=4)
        account.columnconfigure(1, weight=1)
        account.columnconfigure(3, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=8)
        self.start_btn = ttk.Button(buttons, text="启动定时", command=self.start_scheduler)
        self.start_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(buttons, text="停止定时", command=self.stop_scheduler, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.sign_btn = ttk.Button(buttons, text="立即签到", command=self.manual_sign)
        self.sign_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(buttons, text="立即停止", command=self.immediate_stop, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.status_var = StringVar(value="待机")
        ttk.Label(buttons, textvariable=self.status_var).pack(side="right")

        stats = ttk.LabelFrame(outer, text="签到信息", padding=10)
        stats.pack(fill="x", pady=(0, 8))
        self.today_var = StringVar(value="未知")
        self.reward_var = StringVar(value=self.state.get("last_amount") or "—")
        self.next_var = StringVar(value=self.state.get("next_sign_time") or "未安排")
        self.countdown_var = StringVar(value="未安排")
        self.streak_var = StringVar(value="—")
        self.month_var = StringVar(value="—")
        self.total_var = StringVar(value="—")
        rows = (("今日签到", self.today_var), ("本次奖励", self.reward_var), ("累计签到", self.streak_var),
                ("本月获得", self.month_var), ("累计获得", self.total_var), ("下次签到", self.next_var), ("倒计时", self.countdown_var))
        for idx, (label, var) in enumerate(rows):
            row, col = divmod(idx, 2)
            ttk.Label(stats, text=label + "：").grid(row=row, column=col * 2, sticky="w", padx=(4, 2), pady=3)
            ttk.Label(stats, textvariable=var).grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 15), pady=3)

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log = ScrolledText(log_frame, wrap="word", state="disabled", height=12, bg="#111111", fg="#b7f7b7", insertbackground="#b7f7b7", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    def emit_status(self, **values: Any) -> None:
        self.events.put({"type": "status", **values})

    def save_credentials(self) -> tuple[str, str]:
        """将 GUI 当前凭据单独写入被 Git 忽略的 config.ini。"""
        username, password = self.username.get().strip(), self.password.get()
        if not username or not password:
            raise SignError("请填写账号和密码")
        self.config.set("account", "username", username)
        self.config.set("account", "password", password)
        save_config(self.config)
        return username, password

    def launch_sign(self, source: str) -> None:
        """启动异步签到，成功后更新 GUI 和下一次排程。"""
        if self.signing:
            return
        try:
            username, password = self.save_credentials()
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self.root)
            return
        self.signing = True
        self.sign_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status_var.set("正在签到")
        self.cancel_event.clear()
        threading.Thread(target=self.sign_worker, args=(username, password, source), daemon=True, name="RiverNexusSign").start()

    def manual_sign(self) -> None:
        """立即启动一次签到。"""
        self.launch_sign("手动")

    def sign_worker(self, username: str, password: str, source: str) -> None:
        """后台执行网络登录/签到与每日调度更新。"""
        try:
            result = sign_once(username, password, self.cancel_event, self.logger)
            today = now_bj().strftime("%Y-%m-%d")
            target = random_next_day(now_bj())
            with self.guard:
                self.state.update(last_sign_date=today, last_sign_time=fmt(now_bj()), last_amount=result["reward"], next_sign_time=fmt(target))
                write_state(self.state)
            self.events.put({"type": "signed", "already": result["already"], "reward": result["reward"], "message": result["message"], "stats": result["stats"], "next": fmt(target)})
            if source == "timer":
                self.events.put({"type": "restart_scheduler"})
        except Exception as exc:
            if self.cancel_event.is_set():
                self.logger.info("签到任务已停止")
                self.emit_status(status="已停止")
            else:
                self.logger.exception("签到失败：%s", exc)
                retry_minutes = max(1, self.config.getint("settings", "retry_interval_minutes", fallback=30))
                retry = now_bj() + timedelta(minutes=retry_minutes)
                with self.guard:
                    self.state["next_sign_time"] = fmt(retry)
                    write_state(self.state)
                self.events.put({"type": "failed", "error": str(exc), "next": fmt(retry), "timer": source == "timer"})
                if source == "timer":
                    self.events.put({"type": "restart_scheduler"})
        finally:
            self.events.put({"type": "sign_done"})

    def scheduler_loop(self) -> None:
        """按北京时间随机窗口轮询；失败按重试间隔再次执行。"""
        current = now_bj()
        today = current.strftime("%Y-%m-%d")
        with self.guard:
            signed_today = self.state.get("last_sign_date") == today
            saved = parse_bj(self.state.get("next_sign_time", ""))
        if signed_today:
            target = saved if saved and saved > current else random_next_day(current)
        elif saved and saved > current:
            target = saved
        else:
            target = random_today(current)
        if signed_today:
            with self.guard:
                self.state["next_sign_time"] = fmt(target)
                write_state(self.state)
        self.events.put({"type": "schedule", "next": fmt(target)})
        while not self.stop_event.is_set() and not self.cancel_event.is_set() and not self.closing:
            if now_bj() >= target:
                if self.state.get("last_sign_date") == now_bj().strftime("%Y-%m-%d"):
                    target = random_next_day(now_bj())
                    with self.guard:
                        self.state["next_sign_time"] = fmt(target)
                        write_state(self.state)
                    self.events.put({"type": "schedule", "next": fmt(target)})
                    continue
                self.launch_sign_from_worker()
                return
            self.stop_event.wait(1)

    def launch_sign_from_worker(self) -> None:
        """调度线程发队列消息，由 GUI 主线程安全启动签到工作线程。"""
        self.events.put({"type": "timer_due"})

    def start_scheduler(self) -> None:
        """保存凭据并开始定时线程。"""
        if self.scheduler and self.scheduler.is_alive():
            return
        try:
            self.save_credentials()
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self.root)
            return
        self.stop_event.clear()
        self.cancel_event.clear()
        self.scheduler_enabled = True
        self.scheduler = threading.Thread(target=self.scheduler_loop, daemon=True, name="RiverNexusScheduler")
        self.scheduler.start()
        self.status_var.set("定时运行中")
        self.logger.info("定时任务已启动")

    def stop_scheduler(self) -> None:
        """停止定时轮询，不影响手动签到。"""
        self.scheduler_enabled = False
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.start_btn.configure(state="normal")
        self.status_var.set("已停止")
        self.logger.info("定时任务已停止")

    def immediate_stop(self) -> None:
        """立即请求停止当前签到及定时轮询。"""
        self.cancel_event.set()
        self.stop_event.set()
        self.status_var.set("正在停止")
        self.logger.warning("已请求立即停止")

    def consume_events(self) -> None:
        """由主线程消费后台线程事件并刷新界面。"""
        if self.closing:
            return
        try:
            while True:
                event = self.events.get_nowait()
                kind = event.get("type")
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert(END, event["text"] + "\n")
                    lines = int(self.log.index("end-1c").split(".")[0])
                    if lines > MAX_GUI_LOG_LINES:
                        self.log.delete("1.0", f"{lines - MAX_GUI_LOG_LINES + 1}.0")
                    self.log.see(END)
                    self.log.configure(state="disabled")
                elif kind == "schedule":
                    self.next_var.set(event["next"])
                elif kind == "signed":
                    self.today_var.set("已签到")
                    self.reward_var.set(event["reward"])
                    stats = event["stats"]
                    self.streak_var.set(stats["streak"])
                    self.month_var.set(stats["month"])
                    self.total_var.set(stats["total"])
                    self.next_var.set(event["next"])
                    self.status_var.set("今日已签到")
                    self.logger.info("签到状态：%s；本次奖励：%s；原文：%s", "此前已签到" if event["already"] else "签到成功", event["reward"], event["message"])
                    if not self.stop_event.is_set():
                        self.stop_btn.configure(state="normal")
                elif kind == "failed":
                    self.today_var.set("签到失败")
                    self.status_var.set("等待重试" if event["timer"] else "失败")
                    self.next_var.set(event["next"])
                    self.logger.error("签到失败，将于 %s 重试", event["next"])
                    if not event["timer"]:
                        messagebox.showerror(APP_TITLE, event["error"], parent=self.root)
                elif kind == "sign_done":
                    self.signing = False
                    self.sign_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    if self.stop_event.is_set():
                        self.start_btn.configure(state="normal")
                        self.stop_btn.configure(state="disabled")
                elif kind == "timer_due":
                    if not self.signing and self.scheduler_enabled:
                        self.launch_sign("timer")
                elif kind == "restart_scheduler":
                    if self.scheduler_enabled and not self.closing:
                        self.scheduler = threading.Thread(target=self.scheduler_loop, daemon=True, name="RiverNexusScheduler")
                        self.scheduler.start()
                self.events.task_done()
        except queue.Empty:
            pass
        self.root.after(150, self.consume_events)

    def refresh_countdown(self) -> None:
        """每秒使用北京时间刷新下次签到倒计时。"""
        if self.closing:
            return
        target = parse_bj(self.state.get("next_sign_time", ""))
        if not target:
            self.countdown_var.set("未安排")
        else:
            remaining = max(0, int((target - now_bj()).total_seconds()))
            days, rem = divmod(remaining, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)
            self.countdown_var.set(f"{days}天 {hours:02d}:{minutes:02d}:{seconds:02d}")
        self.root.after(1000, self.refresh_countdown)

    def close(self) -> None:
        """设置停止事件并关闭程序。"""
        self.closing = True
        self.cancel_event.set()
        self.stop_event.set()
        self.logger.info("程序退出")
        for thread in threading.enumerate():
            if thread.name in ("RiverNexusSign", "RiverNexusScheduler") and thread is not threading.current_thread():
                thread.join(timeout=1)
        logging.shutdown()
        self.root.destroy()


def main() -> None:
    """初始化本地运行文件并启动 Tk 主循环。"""
    ensure_files()
    root = Tk()
    try:
        App(root)
        root.mainloop()
    except Exception:
        logging.exception("GUI 启动失败")
        messagebox.showerror(APP_TITLE, "程序启动失败，请检查程序目录权限。")
        raise


if __name__ == "__main__":
    main()
