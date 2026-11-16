"""One APK workflow, manual voice repair and encrypted key maintenance."""

from __future__ import annotations

import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
import winsound
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk

from maintenance.core.process_lock import AlreadyRunning, process_lock
from maintenance.model_config import ModelConfig, load_model_config, save_model_config
from maintenance.paths import ROOT
from maintenance.release import validate_version_inputs
from maintenance.tencent_asr_config import (
    TencentAsrConfig,
)
from maintenance.tencent_asr_config import (
    load as load_tencent_asr,
)
from maintenance.tencent_asr_config import (
    save as save_tencent_asr,
)
from maintenance.voice.app_review import item_by_id, submit
from maintenance.voice.playback import prepare as prepare_audio

MODEL_PATH = ROOT / "data/model_config.dpapi"
CHAT_PATH = ROOT / "data/couple_chat.db"
TENCENT_ASR_PATH = ROOT / "data/tencent_asr_config.dpapi"
MAINTENANCE_LOCK = ROOT / "data/maintenance.lock"


def initial_window_size(screen_width: int, screen_height: int) -> tuple[int, int]:
    # Tk reports logical pixels, already accounting for Windows display scaling.
    # Reserve space for the title bar, taskbar and a margin around the window.
    return min(940, max(1, screen_width - 100)), min(740, max(1, screen_height - 100))


def worker_python() -> str:
    """Use the same environment, with standard streams even under pythonw."""
    executable = Path(sys.executable)
    console = executable.with_name("python.exe")
    if executable.name.lower() == "pythonw.exe" and console.is_file():
        return str(console)
    return str(executable)


def replacement_model_config(current: ModelConfig | None, entered_key: str) -> ModelConfig:
    key = entered_key.strip() or (current.api_key if current else "")
    return ModelConfig(
        key,
        current.base_url if current else "https://api.deepseek.com",
        current.model if current else "deepseek-flash",
    )


def suggested_version(root: Path = ROOT) -> str:
    """Suggest the archive date without ever changing the user's entered value."""
    software = "0.1"
    try:
        match = re.search(
            r"(?m)^version:\s*(\d+\.\d+)\.", (root / "app/pubspec.yaml").read_text(encoding="utf-8")
        )
        if match:
            software = match[1]
    except OSError:
        pass
    date = datetime.now(timezone(timedelta(hours=8)))
    database = root / "data/couple_chat.db"
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            timestamp = db.execute("SELECT max(ts) FROM messages").fetchone()[0]
            if timestamp:
                date = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8)))
    except OSError, sqlite3.Error, ValueError, OverflowError:
        pass
    return f"{software}.{date:%y%m%d}"


def repair_request(item: dict, decision: str, transcript: str, listened: bool) -> dict:
    """Preserve the reviewed-audio proof without fabricating an automatic review."""
    body = {key: item[key] for key in ("msg_id", "expected_text", "expected_audio_sha256")}
    candidate_fields = ("candidate_model", "candidate_parameters", "expected_candidate")
    if item.get("candidate_model") is not None and all(key in item for key in candidate_fields):
        body.update({key: item[key] for key in candidate_fields})
    return body | {
        "decision": decision,
        "transcript": transcript.strip() if decision == "verified" else None,
        "listened": listened,
        "review_mode": "assisted",
    }


class MaintenanceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._busy = False
        self._closed = False
        self._events = queue.Queue()
        self._buttons = []
        self._redactions = set()
        self.voice_item = None
        self._poll_id = None
        root.title("7u · 本机维护")
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        width, height = initial_window_size(screen_width, screen_height)
        root.minsize(min(760, width), min(620, height))
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2 - 20)
        root.geometry(f"{width}x{height}+{left}+{top}")
        root.protocol("WM_DELETE_WINDOW", self._close)
        try:
            self.model_config = load_model_config(MODEL_PATH)
        except ValueError, OSError:
            self.model_config = None
        try:
            self.tencent_config = load_tencent_asr(TENCENT_ASR_PATH)
        except ValueError, OSError:
            self.tencent_config = None
        self._remember_secrets()

        header = ttk.Frame(root, padding=(18, 15, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text="7u · 本机维护", font=("", 18, "bold")).pack(anchor="w")
        ttk.Label(header, text="更新资料、修正语音，再生成可以直接发给她的安装包。").pack(
            anchor="w", pady=(5, 0)
        )
        tabs = ttk.Notebook(root)
        tabs.pack(fill="both", expand=True, padx=16, pady=8)
        self.release_tab = ttk.Frame(tabs, padding=18)
        self.voice_tab = ttk.Frame(tabs, padding=18)
        self.key_tab = ttk.Frame(tabs, padding=18)
        for frame, title in (
            (self.release_tab, "1  构建新版 APK"),
            (self.voice_tab, "2  按 ID 修复语音"),
            (self.key_tab, "3  更换 API Key"),
        ):
            tabs.add(frame, text=title)
        self.version_name = tk.StringVar(value=suggested_version())
        self._build_release()
        self._build_voice()
        self._build_key()

        logs = ttk.LabelFrame(root, text="执行进度", padding=10)
        logs.pack(side="bottom", before=tabs, fill="x", padx=16, pady=(0, 16))
        self.task_status = tk.StringVar(value="就绪")
        ttk.Label(logs, textvariable=self.task_status, wraplength=740).pack(anchor="w", pady=(0, 6))
        log_body = ttk.Frame(logs)
        log_body.pack(fill="both", expand=True)
        self.task_output = tk.Text(log_body, height=6, wrap="word", state="disabled")
        self.task_output.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_body, command=self.task_output.yview)
        scrollbar.pack(side="right", fill="y")
        self.task_output.configure(yscrollcommand=scrollbar.set)
        self._poll_id = root.after(80, self._drain_events)

    @staticmethod
    def _heading(parent, title, description):
        ttk.Label(parent, text=title, font=("", 16, "bold")).pack(anchor="w")
        description_label = ttk.Label(parent, text=description, wraplength=720, justify="left")
        description_label.pack(anchor="w", fill="x", pady=(7, 16))
        description_label.bind(
            "<Configure>", lambda event: description_label.configure(wraplength=max(200, event.width))
        )

    @staticmethod
    def _entry(parent, label, variable, show=None):
        ttk.Label(parent, text=label).pack(anchor="w")
        entry = ttk.Entry(parent, textvariable=variable, show=show)
        entry.pack(fill="x", pady=(4, 10))
        return entry

    def _button(self, parent, text, command, **pack):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(**({"anchor": "w"} | pack))
        self._buttons.append(button)
        return button

    def _build_tencent(self, parent):
        self.tencent_status = tk.StringVar(
            value="腾讯语音：已配置" if self.tencent_config else "腾讯语音：尚未配置"
        )
        ttk.Label(parent, textvariable=self.tencent_status).pack(anchor="w")
        self.tencent_toggle = ttk.Button(parent, text="展开腾讯语音配置 ▾", command=self._toggle_tencent)
        self.tencent_toggle.pack(anchor="w", pady=(6, 0))
        self.tencent_frame = ttk.Frame(parent, padding=(0, 10, 0, 0))
        self.tencent_id = tk.StringVar()
        self.tencent_key = tk.StringVar()
        self.tencent_region = tk.StringVar(
            value=self.tencent_config.region if self.tencent_config else "ap-shanghai"
        )
        self._entry(self.tencent_frame, "SecretId（留空保留现有值）", self.tencent_id, show="•")
        self._entry(self.tencent_frame, "SecretKey（留空保留现有值）", self.tencent_key, show="•")
        self._entry(self.tencent_frame, "地域", self.tencent_region)
        self._button(self.tencent_frame, "保存腾讯配置", self._save_tencent)
        if self.tencent_config is None:
            self._toggle_tencent()

    def _toggle_tencent(self):
        if self.tencent_frame.winfo_manager():
            self.tencent_frame.pack_forget()
            self.tencent_toggle.configure(text="展开腾讯语音配置 ▾")
        else:
            self.tencent_frame.pack(fill="x")
            self.tencent_toggle.configure(text="收起腾讯语音配置 ▴")

    def _save_tencent(self):
        if not self._ensure_idle():
            return False
        try:
            with process_lock(MAINTENANCE_LOCK):
                current = load_tencent_asr(TENCENT_ASR_PATH)
                config = TencentAsrConfig(
                    self.tencent_id.get().strip() or (current.secret_id if current else ""),
                    self.tencent_key.get().strip() or (current.secret_key if current else ""),
                    self.tencent_region.get().strip(),
                )
                self.tencent_config = save_tencent_asr(TENCENT_ASR_PATH, config)
            self._remember_secrets()
            self.tencent_id.set("")
            self.tencent_key.set("")
            self.tencent_status.set("腾讯语音：配置已加密保存")
            return True
        except Exception as exc:
            messagebox.showerror("无法保存腾讯配置", self._error_text(exc))
            return False

    def _build_release(self):
        # This page can grow when credentials are expanded; keep it scrollable
        # instead of allowing them to push the logs outside a small PC screen.
        canvas = tk.Canvas(self.release_tab, highlightthickness=0, background=self.root.cget("background"))
        scrollbar = ttk.Scrollbar(self.release_tab, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self._heading(
            body,
            "一键构建新版 APK",
            "确保 QQ 已同步到最新记录，原音可在本机找到。一次完成：① 提取 QQ ② 自动转写语音 ③ 生成全新每日短句 ④ 构建正式 APK。任何一步失败都会停止，不会把旧包当成新包。",
        )
        self._entry(body, "完整版本号（major.minor.YYMMDD；内部构建号自动递增）", self.version_name)
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        self._button(actions, "一键构建新版 APK", self._build_apk, side="left")
        ttk.Button(actions, text="打开安装包文件夹", command=self._open_releases).pack(side="left", padx=10)
        ttk.Label(
            body, text="资料会自动备份。安装新 APK 后生效，原应用的问答历史保留。", wraplength=690
        ).pack(anchor="w", pady=(10, 14))
        self._build_tencent(body)

    def _build_command(self):
        version = self.version_name.get().strip()
        validate_version_inputs(version_name=version)
        return [worker_python(), "-m", "maintenance.build", "--version-name", version]

    def _save_entered_tencent(self):
        if (
            self.tencent_id.get().strip()
            or self.tencent_key.get().strip()
            or (self.tencent_config and self.tencent_region.get().strip() != self.tencent_config.region)
        ):
            return self._save_tencent()
        return True

    def _build_apk(self):
        if not self._ensure_idle():
            return
        try:
            command = self._build_command()
            self.model_config = load_model_config(MODEL_PATH)
            self._remember_secrets()
        except Exception as exc:
            messagebox.showerror("无法开始构建", self._error_text(exc))
            return
        if self.model_config is None:
            messagebox.showwarning("请先配置 Key", "到“更换 API Key”填写 Key，并点击保存与生成。")
            return
        if self._save_entered_tencent():
            self._run("构建新版 APK", command)

    def _open_releases(self):
        destination = ROOT / "releases"
        if not destination.is_dir():
            messagebox.showinfo("还没有安装包", "先生成一次正式 APK，完成后这里会显示安装包。")
            return
        os.startfile(str(destination))

    def _build_key(self):
        self._heading(
            self.key_tab,
            "更换问答 API Key",
            "输入新 Key 后加密保存，再运行相同的完整构建流程：同步 QQ、自动语音、全新每日短句、正式 APK。手机安装新版后生效，旧版本不会热更新。",
        )
        self.model_key = tk.StringVar()
        self._entry(self.key_tab, "新的 API Key（留空保留现有 Key）", self.model_key, show="•")
        self._entry(self.key_tab, "完整版本号（与构建页同步）", self.version_name)
        self.model_status = tk.StringVar(
            value="问答 Key：已配置" if self.model_config else "问答 Key：尚未配置"
        )
        ttk.Label(self.key_tab, textvariable=self.model_status).pack(anchor="w", pady=(0, 12))
        self._button(self.key_tab, "保存 Key 并生成新版 APK", self._change_key)

    def _change_key(self):
        if not self._ensure_idle():
            return
        try:
            command = self._build_command()
            if not self._save_entered_tencent():
                return
            with process_lock(MAINTENANCE_LOCK):
                current = load_model_config(MODEL_PATH)
                config = replacement_model_config(current, self.model_key.get())
                self.model_config = save_model_config(MODEL_PATH, config)
            self._remember_secrets()
            self.model_key.set("")
            self.model_status.set("Key 已加密保存；正在生成使用此 Key 的安装包。")
        except Exception as exc:
            messagebox.showerror("无法保存 Key", self._error_text(exc))
            return
        self._run("更换 Key 并构建新版 APK", command)

    def _build_voice(self):
        self._heading(
            self.voice_tab,
            "按消息 ID 修正语音",
            "发现某条语音不准确时，在这里打开、播放并改正；听不清的也可以舍弃。修正会写回本机资料，下次生成 APK 时带入手机。",
        )
        row = ttk.Frame(self.voice_tab)
        row.pack(fill="x")
        ttk.Label(row, text="消息 ID").pack(side="left")
        self.voice_id = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.voice_id, width=28)
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda _: self._load_voice_id())
        self._button(row, "打开语音", self._load_voice_id, side="left")
        self.voice_meta = tk.StringVar(value="输入消息 ID 后，会显示日期、说话人和现有文字。")
        ttk.Label(self.voice_tab, textvariable=self.voice_meta, wraplength=720).pack(anchor="w", pady=(9, 5))
        ttk.Label(self.voice_tab, text="当前入库文字").pack(anchor="w")
        self.voice_original = tk.Text(self.voice_tab, height=2, wrap="word", state="disabled")
        self.voice_original.pack(fill="x", pady=(3, 6))
        ttk.Label(self.voice_tab, text="修正后的文字").pack(anchor="w")
        self.voice_text = tk.Text(self.voice_tab, height=4, wrap="word")
        self.voice_text.pack(fill="both", expand=True, pady=(3, 4))
        self.voice_listened = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.voice_tab, text="我已实际听完这条原音", variable=self.voice_listened).pack(
            anchor="w", pady=4
        )
        actions = ttk.Frame(self.voice_tab)
        actions.pack(fill="x")
        self._button(actions, "播放原音", self._play_voice, side="left")
        self._button(actions, "保存修正", lambda: self._save_voice("verified"), side="left", padx=8)
        self._button(actions, "舍弃这条语音", lambda: self._save_voice("rejected"), side="left")
        self.voice_status = tk.StringVar(value="这里只用于主动修正，不需要完成复核队列。")
        ttk.Label(self.voice_tab, textvariable=self.voice_status, wraplength=720).pack(
            anchor="w", pady=(6, 0)
        )

    def _load_voice_id(self):
        if not self._ensure_idle():
            return
        identifier = self.voice_id.get().strip()
        if not re.fullmatch(r"[0-9]{1,19}", identifier):
            messagebox.showwarning("请输入消息 ID", "消息 ID 应是聊天记录中的数字编号。")
            return
        self.voice_item = None
        winsound.PlaySound(None, winsound.SND_PURGE)
        self.voice_listened.set(False)
        self.voice_status.set("正在载入…")
        self._operation("载入语音", lambda: item_by_id(CHAT_PATH, identifier), self._show_voice)

    def _show_voice(self, result):
        self.voice_item = result.get("item")
        self.voice_listened.set(False)
        self.voice_text.delete("1.0", "end")
        self.voice_original.configure(state="normal")
        self.voice_original.delete("1.0", "end")
        if not self.voice_item:
            self.voice_original.configure(state="disabled")
            self.voice_status.set(result.get("message", "没有找到这条语音。"))
            self.voice_meta.set("")
            return
        item = self.voice_item
        current_text = item.get("current_text")
        self.voice_original.insert("1.0", current_text or "（当前没有入库文字）")
        self.voice_original.configure(state="disabled")
        self.voice_text.insert(
            "1.0", current_text if current_text is not None else (item.get("candidate") or "")
        )
        # PC archive is giver-facing. Only the APK exporter flips the roles.
        speaker = {"self": "你", "partner": "她"}.get(item.get("speaker"), "未知说话人")
        self.voice_id.set(item["msg_id"])
        self.voice_meta.set(f"{item['occurred_at_beijing']}（北京时间） · {speaker} · 消息 {item['msg_id']}")
        self.voice_status.set("请播放原音，听完后勾选确认，再保存或舍弃。")

    def _play_voice(self):
        if not self._ensure_idle() or not self.voice_item:
            return
        identifier = self.voice_item["msg_id"]
        self.voice_listened.set(False)

        def prepare():
            with closing(sqlite3.connect(CHAT_PATH)) as db:
                return prepare_audio(db, identifier, ROOT / "data/playback")

        def play(wav):
            winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
            self.voice_status.set("正在播放；听完后请勾选“我已实际听完这条原音”。")

        self._operation("准备原音", prepare, play)

    def _save_voice(self, decision):
        if not self._ensure_idle() or not self.voice_item:
            return
        if not self.voice_listened.get():
            messagebox.showwarning("请先听原音", "请播放原音并勾选已经听完，再保存或舍弃。")
            return
        text = self.voice_text.get("1.0", "end-1c")
        if decision == "verified" and not text.strip():
            messagebox.showwarning("没有修正文字", "请填写准确文字；没有可用内容时可以选择舍弃。")
            return
        body = repair_request(self.voice_item, decision, text, self.voice_listened.get())
        identifier = self.voice_item["msg_id"]

        def save():
            with process_lock(MAINTENANCE_LOCK):
                submit(CHAT_PATH, body)
                return item_by_id(CHAT_PATH, identifier)

        def saved(result):
            self._show_voice(result)
            self.voice_status.set(
                "修正已保存，下次生成 APK 后生效。"
                if decision == "verified"
                else "已人工舍弃，下次生成 APK 时不会收录这条语音。"
            )

        self._operation("保存语音修正" if decision == "verified" else "舍弃语音", save, saved)

    def _ensure_idle(self):
        if self._busy:
            self.task_status.set("当前任务还在执行，请完成后再进行下一项操作。")
            return False
        return True

    def _remember_secrets(self):
        if self.model_config:
            self._redactions.add(self.model_config.api_key)
        if self.tencent_config:
            self._redactions.update((self.tencent_config.secret_id, self.tencent_config.secret_key))

    def _redact(self, text):
        for secret in self._redactions:
            if secret:
                text = text.replace(secret, "••••")
        return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "••••", text)

    def _error_text(self, error):
        if isinstance(error, AlreadyRunning):
            return "另一个维护任务正在更新资料或打包，请完成后再试。"
        return self._redact(str(error))

    def _append_log(self, text):
        self.task_output.configure(state="normal")
        self.task_output.insert("end", self._redact(text))
        if int(self.task_output.index("end-1c").split(".")[0]) > 1600:
            self.task_output.delete("1.0", "400.0")
        self.task_output.see("end")
        self.task_output.configure(state="disabled")

    def _operation(self, label, operation, on_success=None):
        if not self._ensure_idle():
            return
        self._busy = True
        for button in self._buttons:
            button.configure(state="disabled")
        self.task_status.set(label + "正在执行…")
        self._append_log(f"\n{label}\n")

        def work():
            try:
                result = operation()
                self._events.put(("done", label, result, on_success))
            except Exception as exc:
                self._events.put(("failed", label, self._error_text(exc), None))

        threading.Thread(target=work, daemon=True).start()

    def _run(self, label, command):
        def execute():
            environment = os.environ.copy()
            environment.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            with subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ) as process:
                for line in process.stdout:
                    self._events.put(("log", line))
                code = process.wait()
            if code:
                raise RuntimeError(f"任务退出码 {code}；请查看上方日志中的原因。")

        self._operation(label, execute)

    def _drain_events(self):
        if self._closed:
            return
        for _ in range(150):
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "log":
                self._append_log(event[1])
                if re.match(r"^\[[1-4]/4\]", event[1]):
                    self.task_status.set(self._redact(event[1].strip()))
                continue
            kind, label, result, callback = event
            self._busy = False
            for button in self._buttons:
                button.configure(state="normal")
            if kind == "failed":
                self.task_status.set(label + "失败")
                self._append_log("失败：" + result + "\n")
                if label in ("载入语音", "准备原音", "保存语音修正", "舍弃语音"):
                    self.voice_status.set(self._redact("操作失败：" + result))
            else:
                try:
                    if callback is not None:
                        callback(result)
                    self.task_status.set(label + "完成")
                    self._append_log("完成。\n")
                except Exception as exc:
                    self.task_status.set(label + "未完成")
                    self._append_log(self._redact(str(exc)) + "\n")
        self._poll_id = self.root.after(80, self._drain_events)

    def _close(self):
        if self._busy:
            messagebox.showinfo("任务仍在运行", "请等待当前任务结束后再关闭维护窗口；进度会持续显示在下方。")
            return
        self._closed = True
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
        winsound.PlaySound(None, winsound.SND_PURGE)
        self.root.destroy()


def main():
    root = tk.Tk()
    MaintenanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
