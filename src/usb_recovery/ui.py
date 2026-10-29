"""Chinese native UI. Importing this module never creates a window.

The controller owns device access, output validation and background jobs. This
module only presents controller events and makes calls after a user's action.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import os
from pathlib import Path
import queue
import threading
from typing import Any, Mapping
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


COUNT_KEYS = ("structure_checked", "repaired_candidate", "partial", "unverified",
              "historical", "temporary")
STAGES = {
    "idle": "等待开始", "starting": "正在准备任务", "imaging": "正在只读制作镜像",
    "acquiring": "正在只读制作镜像", "recovering": "正在从镜像恢复",
    "inventory": "正在读取目录", "extracting": "正在提取文件",
    "scanning": "正在搜索候选文件", "deep_scan": "正在深度搜索Office文件",
    "validating": "正在检查文件结构", "cancelling": "正在安全停止，请等待",
    "cancelled": "已停止，保留当前结果", "error": "发生错误，保留当前结果",
    "failed": "发生错误，保留当前结果", "completed": "任务已结束，请核对结果",
    "finished": "任务已结束，请核对结果", "paused": "任务已暂停，可续接",
}
TERMINAL_STAGES = {"cancelled", "error", "failed", "completed", "finished", "paused"}


def format_bytes(value: Any) -> str:
    """Unknown or invalid lengths must not look like a zero-byte device."""
    try:
        size = float(value)
    except (TypeError, ValueError, OverflowError):
        return "未知"
    if not math.isfinite(size) or size < 0:
        return "未知"
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        size /= 1024
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}"
    return "未知"


def device_label(device: Mapping[str, Any]) -> str:
    name = str(device.get("label") or "未命名设备")
    model = str(device.get("model") or "")
    drive = str(device.get("drive_letter") or "无盘符")
    disk = device.get("disk_number", "未知")
    return f"{drive} · {name}{(' / ' + model) if model and model != name else ''} · {format_bytes(device.get('size'))} · 物理磁盘 {disk}"


def partition_label(volume: Mapping[str, Any]) -> str:
    text = (f"{volume.get('label') or '未命名卷'} · {volume.get('filesystem') or '文件系统未知'}"
            f" · {format_bytes(volume.get('size'))} · 偏移 {volume.get('offset', '未知')} 字节")
    warnings = volume.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    if warnings:
        text += " · 提示：" + "；".join(str(item) for item in warnings)
    return text


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def count_labels(counts: Mapping[str, Any] | None) -> list[tuple[str, int]]:
    counts = counts or {}
    return [
        ("结构检查通过", _count(counts.get("structure_checked", 0))),
        ("修复候选", _count(counts.get("repaired_candidate", 0))),
        ("残缺或未确认", _count(counts.get("partial", 0)) + _count(counts.get("unverified", 0))),
        ("历史候选", _count(counts.get("historical", 0))),
        ("临时或系统文件", _count(counts.get("temporary", 0))),
    ]


@dataclass
class UiState:
    running: bool = False
    stage: str = "idle"
    message: str = "选择源设备或已有镜像后开始。首次打开不会自动读取U盘。"
    progress: float | None = None
    job_dir: str = ""
    report_path: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    can_resume: bool = False
    complete: bool | None = None
    can_recover: bool = False
    image_path: str = ""
    map_path: str = ""


def reduce_event(state: UiState, event: Mapping[str, Any]) -> UiState:
    """Reduce events without treating scan progress or good structure as recovery."""
    changes: dict[str, Any] = {}
    event_type = event.get("type", "state")
    for key in ("stage", "message", "job_dir", "report_path", "image_path", "map_path"):
        if event.get(key) is not None:
            changes[key] = str(event[key])
    for key in ("can_resume", "can_recover", "running"):
        if isinstance(event.get(key), bool):
            changes[key] = event[key]
    if "progress" in event:
        try:
            number = float(event["progress"])
            changes["progress"] = max(0.0, min(100.0, number)) if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            changes["progress"] = None
    if isinstance(event.get("counts"), Mapping):
        changes["counts"] = {key: _count(event["counts"].get(key, 0)) for key in COUNT_KEYS}
    stage = changes.get("stage", state.stage)
    if event_type == "error":
        changes.update(running=False, stage="error", complete=False)
    elif event_type == "result":
        changes["running"] = False
        changes["complete"] = event.get("complete") if isinstance(event.get("complete"), bool) else None
        if stage not in TERMINAL_STAGES:
            changes["stage"] = "finished"
    elif stage in TERMINAL_STAGES and event_type == "state":
        changes["running"] = False
        if stage in {"cancelled", "error", "failed", "paused"}:
            changes["complete"] = False
    elif state.running and state.stage == "cancelling" and stage not in TERMINAL_STAGES:
        # A queued acquisition progress event may arrive after the stop click.
        changes["stage"] = "cancelling"
    return replace(state, **changes)


def summary_text(state: UiState) -> str:
    if state.running:
        return STAGES.get(state.stage, "正在处理任务")
    if state.stage in {"error", "failed", "cancelled", "paused"}:
        return STAGES[state.stage]
    if state.complete is False:
        return "部分结果已保留，任务尚未完成"
    return STAGES.get(state.stage, "任务已结束，请核对结果")


class RecoveryWindow:
    """All Tk interaction stays on the main thread; controller calls may wait."""

    def __init__(self, root: tk.Tk, controller: Any):
        self.root = root
        self.controller = controller
        self.state = UiState()
        self._replies: queue.Queue = queue.Queue()
        self._devices: list[dict[str, Any]] = []
        self._volumes: list[dict[str, Any]] = []
        self._pending = False
        self._stopping = False
        self._cancel_after_start = False
        self._closing = False
        self._dead = False
        self._indeterminate = False
        self._log_lines = 0
        self._inputs: list[Any] = []
        self._action_buttons: list[Any] = []
        self._build()
        # status() is bookkeeping only. Discovery requires an explicit refresh.
        try:
            status = controller.status()
            if isinstance(status, Mapping):
                self.state = reduce_event(self.state, {"type": "state", **status})
        except Exception as error:
            self._append_log(f"读取任务状态失败：{error}")
        self._render()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

    def _build(self) -> None:
        root = self.root
        root.title("U盘恢复助手")
        root.geometry(f"{min(1100, root.winfo_screenwidth() - 60)}x{min(850, root.winfo_screenheight() - 100)}")
        root.minsize(900, 620)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Treeview", rowheight=29)
        style.configure("Count.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        # Keep every control reachable at smaller resolutions / larger scaling.
        viewport = ttk.Frame(root)
        viewport.pack(fill="both", expand=True)
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        canvas = tk.Canvas(viewport, highlightthickness=0, borderwidth=0,
                           background=style.lookup("TFrame", "background") or "#f0f0f0")
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)
        outer = ttk.Frame(canvas, padding=(20, 15, 20, 12))
        frame_id = canvas.create_window(0, 0, window=outer, anchor="nw")

        def resize_content(event: Any) -> None:
            canvas.itemconfigure(frame_id, width=event.width,
                                 height=max(event.height, outer.winfo_reqheight()))
            canvas.configure(scrollregion=canvas.bbox("all"))

        canvas.bind("<Configure>", resize_content)
        outer.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))

        def wheel(event: Any) -> str | None:
            if event.widget.winfo_class() in {"Text", "Treeview", "TCombobox"}:
                return None
            if outer.winfo_height() > canvas.winfo_height():
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
            return None

        root.bind("<MouseWheel>", wheel)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="U盘恢复助手", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="本机处理 · 只读保全 · 原盘不写入", style="Hint.TLabel").pack(side="right")
        ttk.Label(outer, text="先把源U盘保全为镜像，再从镜像提取和检查文件。输出必须放在另一块物理磁盘。",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 12))
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=2, column=0, sticky="ew")
        self.usb_tab = ttk.Frame(self.tabs, padding=14)
        self.image_tab = ttk.Frame(self.tabs, padding=14)
        self.resume_tab = ttk.Frame(self.tabs, padding=14)
        self.tabs.add(self.usb_tab, text="  ① U盘只读保全  ")
        self.tabs.add(self.image_tab, text="  ② 已有镜像恢复  ")
        self.tabs.add(self.resume_tab, text="  续接已有任务  ")
        self._build_usb()
        self._build_image()
        self._build_resume()
        self._build_status(outer)
        ttk.Label(outer, text="支持范围：exFAT目录恢复；Office新格式（xlsx、docx、pptx等）连续完整候选深搜。"
                  "文件结构通过仍需用WPS/Office打开核对，历史候选不保证原名、时间或内容归属。",
                  style="Hint.TLabel", wraplength=1000).grid(row=4, column=0, sticky="ew", pady=(9, 0))

    def _button(self, parent: Any, text: str, command: Any, *, action: bool = False) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command)
        (self._action_buttons if action else self._inputs).append(button)
        return button

    def _path_row(self, parent: Any, row: int, label: str, variable: tk.StringVar,
                  command: Any, button_text: str = "选择…") -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        self._inputs.append(entry)
        self._button(parent, button_text, command).grid(row=row, column=2, sticky="e", padx=(8, 0), pady=5)

    def _build_usb(self) -> None:
        tab = self.usb_tab
        tab.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="源设备（只读）", style="Heading.TLabel").pack(side="left")
        self._button(toolbar, "刷新USB设备", self._refresh_devices).pack(side="right")
        columns = ("drive", "name", "size", "disk", "kind")
        self.devices_tree = ttk.Treeview(tab, columns=columns, show="headings", height=4, selectmode="browse")
        for key, title, width in (("drive", "盘符", 75), ("name", "名称 / 型号", 365),
                                  ("size", "容量", 115), ("disk", "物理磁盘", 100), ("kind", "范围", 95)):
            self.devices_tree.heading(key, text=title)
            self.devices_tree.column(key, width=width, minwidth=50, stretch=key == "name")
        self.devices_tree.grid(row=1, column=0, sticky="ew", pady=7)
        self.devices_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_device_selection())
        self.device_text = tk.StringVar(value="尚未刷新设备。点击上方按钮后，请按名称、容量和物理磁盘编号核对。")
        ttk.Label(tab, textvariable=self.device_text, style="Hint.TLabel", wraplength=940).grid(row=2, column=0, sticky="w")
        paths = ttk.Frame(tab)
        paths.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        paths.columnconfigure(1, weight=1)
        self.usb_destination = tk.StringVar()
        self._path_row(paths, 0, "镜像保存到", self.usb_destination,
                       lambda: self._choose_directory(self.usb_destination, "选择另一块物理磁盘上的保存文件夹"))
        buttons = ttk.Frame(tab)
        buttons.grid(row=4, column=0, sticky="ew", pady=(9, 0))
        self.start_image_button = self._button(buttons, "开始只读制作镜像", self._start_image, action=True)
        self.start_image_button.pack(side="left")
        self.admin_button = self._button(buttons, "权限不足时：以管理员身份重新打开", self._restart_admin)
        self.admin_button.pack(side="right")
        ttk.Label(tab, text="软件会核对源设备身份及目标物理磁盘。空间不足、同盘输出或身份无法确认时拒绝开始。",
                  style="Hint.TLabel", wraplength=940).grid(row=5, column=0, sticky="w", pady=(9, 0))

    def _build_image(self) -> None:
        tab = self.image_tab
        tab.columnconfigure(1, weight=1)
        self.image_path = tk.StringVar()
        self.map_path = tk.StringVar()
        self.recovery_destination = tk.StringVar()
        self._path_row(tab, 0, "源镜像文件", self.image_path, self._choose_image)
        self._path_row(tab, 1, "读取记录（可选）", self.map_path, self._choose_map)
        hints = ttk.Frame(tab)
        hints.grid(row=2, column=1, columnspan=2, sticky="ew")
        ttk.Label(hints, text="无ddrescue map时，镜像的读取完整度未知。", style="Hint.TLabel").pack(side="left")
        self._button(hints, "清除读取记录", lambda: self.map_path.set("")).pack(side="right")
        ttk.Label(tab, text="镜像中的卷").grid(row=3, column=0, sticky="w", pady=(10, 5))
        self.volume_combo = ttk.Combobox(tab, state="readonly", values=())
        self.volume_combo.grid(row=3, column=1, sticky="ew", pady=(10, 5))
        self.volume_combo.bind("<<ComboboxSelected>>", lambda _event: self._render_controls())
        self._button(tab, "识别分区", self._probe_image).grid(row=3, column=2, padx=(8, 0), pady=(10, 5))
        self.volume_hint = tk.StringVar(value="选好镜像和读取记录后，点击“识别分区”，再明确选择要恢复的卷。")
        ttk.Label(tab, textvariable=self.volume_hint, style="Hint.TLabel", wraplength=940).grid(row=4, column=0, columnspan=3, sticky="ew")
        self._path_row(tab, 5, "恢复结果保存到", self.recovery_destination,
                       lambda: self._choose_directory(self.recovery_destination, "选择恢复结果保存文件夹"))
        options = ttk.Frame(tab)
        options.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(5, 8))
        self.include_history = tk.BooleanVar(value=True)
        self.deep_scan = tk.BooleanVar(value=False)
        for text, variable in (("包含历史候选（单独标记，不保证内容归属）", self.include_history),
                               ("同时深搜Office新格式（耗时较长，原名未知）", self.deep_scan)):
            checkbox = ttk.Checkbutton(options, text=text, variable=variable)
            checkbox.pack(anchor="w", pady=2)
            self._inputs.append(checkbox)
        self.start_recovery_button = self._button(tab, "开始从镜像恢复", self._start_recovery, action=True)
        self.start_recovery_button.grid(row=7, column=0, columnspan=3, sticky="w")
        self.image_path.trace_add("write", self._invalidate_volumes)
        self.map_path.trace_add("write", self._invalidate_volumes)

    def _build_resume(self) -> None:
        tab = self.resume_tab
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text="选择之前保留的任务文件夹", style="Heading.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self.resume_path = tk.StringVar()
        self._path_row(tab, 1, "任务文件夹", self.resume_path,
                       lambda: self._choose_directory(self.resume_path, "选择已有任务文件夹"))
        ttk.Label(tab, text="任务文件夹包含软件保存的任务记录。续接U盘镜像时会重新核对设备身份，"
                  "盘符相同不代表是同一设备；校验失败不会继续读取。",
                  style="Hint.TLabel", wraplength=900).grid(row=2, column=0, columnspan=3, sticky="ew", pady=14)
        self.resume_button = self._button(tab, "核对并续接任务", self._resume, action=True)
        self.resume_button.grid(row=3, column=0, columnspan=3, sticky="w")
        self.use_current_button = self._button(tab, "填入刚才的任务", self._use_current_job)
        self.use_current_button.grid(row=4, column=0, columnspan=3, sticky="w", pady=12)

    def _build_status(self, outer: Any) -> None:
        panel = ttk.LabelFrame(outer, text="当前任务与结果", padding=12)
        panel.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(5, weight=1)
        row = ttk.Frame(panel)
        row.grid(row=0, column=0, sticky="ew")
        self.status_text = tk.StringVar()
        ttk.Label(row, textvariable=self.status_text, style="Status.TLabel").pack(side="left")
        self.stop_button = ttk.Button(row, text="停止任务并保留结果", command=self._cancel)
        self.stop_button.pack(side="right")
        progress_row = ttk.Frame(panel)
        progress_row.grid(row=1, column=0, sticky="ew", pady=(8, 5))
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.percent_text = tk.StringVar(value="—")
        ttk.Label(progress_row, textvariable=self.percent_text, width=9, anchor="e").grid(row=0, column=1)
        self.message_text = tk.StringVar()
        ttk.Label(panel, textvariable=self.message_text, wraplength=970).grid(row=2, column=0, sticky="ew", pady=(2, 8))
        counts = ttk.Frame(panel)
        counts.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.count_vars: dict[str, tk.StringVar] = {}
        for index, (label, _) in enumerate(count_labels({})):
            counts.columnconfigure(index, weight=1)
            cell = ttk.Frame(counts)
            cell.grid(row=0, column=index, sticky="w")
            variable = tk.StringVar(value="0")
            self.count_vars[label] = variable
            ttk.Label(cell, textvariable=variable, style="Count.TLabel").pack(anchor="w")
            ttk.Label(cell, text=label, style="Hint.TLabel").pack(anchor="w")
        result_row = ttk.Frame(panel)
        result_row.grid(row=4, column=0, sticky="ew", pady=(0, 7))
        result_row.columnconfigure(0, weight=1)
        self.job_text = tk.StringVar(value="任务位置：尚未开始")
        ttk.Label(result_row, textvariable=self.job_text, style="Hint.TLabel", wraplength=600).grid(row=0, column=0, sticky="w")
        self.open_button = ttk.Button(result_row, text="打开结果文件夹", command=self._open_results)
        self.open_button.grid(row=0, column=1, padx=(8, 0))
        self.report_button = ttk.Button(result_row, text="查看核对报告", command=self._open_report)
        self.report_button.grid(row=0, column=2, padx=(8, 0))
        self.recover_image_button = ttk.Button(result_row, text="用此镜像恢复", command=self._use_result_image)
        self.recover_image_button.grid(row=0, column=3, padx=(8, 0))
        log_frame = ttk.Frame(panel)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=5, wrap="word", state="disabled", relief="solid", borderwidth=1,
                           font=("Microsoft YaHei UI", 9), background="#fafafa")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _choose_directory(self, variable: tk.StringVar, title: str) -> None:
        path = filedialog.askdirectory(parent=self.root, title=title, mustexist=True)
        if path:
            variable.set(path)

    def _choose_image(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="选择已有镜像（不会写入）",
                                         filetypes=[("磁盘镜像", "*.img *.dd *.raw *.bin"), ("所有文件", "*.*")])
        if path:
            self.image_path.set(path)

    def _choose_map(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="选择与此镜像对应的ddrescue读取记录",
                                         filetypes=[("ddrescue读取记录", "*.map *.mapfile *.log"), ("所有文件", "*.*")])
        if path:
            self.map_path.set(path)

    def _invalidate_volumes(self, *_args: Any) -> None:
        self._volumes = []
        self.volume_combo.configure(values=())
        self.volume_combo.set("")
        self.volume_hint.set("镜像或读取记录已变更，请重新识别分区后选择要恢复的卷。")
        self._render_controls()

    def _launch(self, work: Any, done: Any, label: str) -> None:
        if self._pending or self.state.running:
            return
        self._pending = True
        self._append_log(label)
        self._render_controls()

        def worker() -> None:
            try:
                result = work()
            except Exception as error:
                self._replies.put((False, error, done))
            else:
                self._replies.put((True, result, done))

        threading.Thread(target=worker, name="usb-ui-request", daemon=True).start()

    def _refresh_devices(self) -> None:
        self._launch(self.controller.devices, self._show_devices, "正在只读刷新USB设备列表…")

    def _show_devices(self, devices: Any) -> None:
        self._devices = list(devices)
        self.devices_tree.delete(*self.devices_tree.get_children())
        for index, device in enumerate(self._devices):
            name = str(device.get("label") or "未命名")
            model = str(device.get("model") or "")
            if model and model != name:
                name += " / " + model
            kind = {"physical": "整盘", "disk": "整盘", "partition": "分区", "volume": "分区"}.get(device.get("source_kind"), "设备")
            self.devices_tree.insert("", "end", iid=str(index), values=(device.get("drive_letter") or "无盘符",
                                     name, format_bytes(device.get("size")), device.get("disk_number", "未知"), kind))
        self.device_text.set("请选择并核对源设备。" if self._devices else "未找到可用USB设备。请检查连接后再刷新；软件不会访问源盘内容。")
        self._append_log(f"设备列表已刷新：{len(self._devices)} 项。尚未开始读取镜像。")

    def _selected_device(self) -> dict[str, Any] | None:
        selected = self.devices_tree.selection()
        if not selected:
            return None
        try:
            return self._devices[int(selected[0])]
        except (IndexError, ValueError):
            return None

    def _update_device_selection(self) -> None:
        device = self._selected_device()
        if device:
            self.device_text.set("已选源设备：" + device_label(device))
        self._render_controls()

    def _probe_image(self) -> None:
        image = self.image_path.get().strip()
        map_path = self.map_path.get().strip() or None
        if not image:
            messagebox.showinfo("先选择镜像", "请选择已有镜像文件，再识别分区。", parent=self.root)
            return
        self._launch(lambda: self.controller.probe_image(image, map_path=map_path), self._show_volumes,
                     "正在识别所选镜像中的分区…")

    def _show_volumes(self, volumes: Any) -> None:
        self._volumes = list(volumes)
        self.volume_combo.configure(values=[partition_label(volume) for volume in self._volumes])
        # Selecting even a single volume is an explicit user decision.
        self.volume_combo.set("")
        if self._volumes:
            self.volume_hint.set(f"识别到 {len(self._volumes)} 个卷。请选择一个；非exFAT卷的目录恢复不在本版范围内，可勾选Office深搜。")
        else:
            self.volume_hint.set("未识别到可选择的卷。请核对镜像和读取记录；本版不能据此推断源盘为空。")
        self._append_log(self.volume_hint.get())

    def _begin_job(self, work: Any, label: str) -> None:
        if self._pending or self.state.running:
            return
        self.state = UiState(stage="starting", message=label)
        self._stopping = False
        self._cancel_after_start = False
        # _launch guards active work; set running only once the worker is queued.
        self._launch(work, self._job_started, label)
        self.state = replace(self.state, running=True)
        self._render()

    def _start_image(self) -> None:
        device = self._selected_device()
        destination = self.usb_destination.get().strip()
        if not device or not destination:
            messagebox.showinfo("请补全来源与位置", "请选择源USB设备及另一块物理磁盘上的保存文件夹。", parent=self.root)
            return
        self._begin_job(lambda: self.controller.start_image(device, destination), "正在核对源设备及目标位置…")

    def _start_recovery(self) -> None:
        image = self.image_path.get().strip()
        destination = self.recovery_destination.get().strip()
        selected = self.volume_combo.current()
        if not image or not destination or not 0 <= selected < len(self._volumes):
            messagebox.showinfo("请补全恢复选项", "请选择镜像、识别并选择卷，以及恢复结果保存文件夹。", parent=self.root)
            return
        map_path = self.map_path.get().strip() or None
        offset = self._volumes[selected].get("offset")
        deep = bool(self.deep_scan.get())
        include_history = bool(self.include_history.get())
        self._begin_job(lambda: self.controller.start_recovery(image, destination, map_path=map_path,
                        deep=deep, include_history=include_history, offset=offset), "正在准备从镜像恢复…")

    def _resume(self) -> None:
        job_dir = self.resume_path.get().strip()
        if not job_dir:
            messagebox.showinfo("先选择任务", "请选择软件先前保存的任务文件夹。", parent=self.root)
            return
        self._begin_job(lambda: self.controller.resume(job_dir), "正在核对任务记录和源身份…")

    def _job_started(self, job_dir: Any) -> None:
        if job_dir is not None:
            self.state = replace(self.state, job_dir=str(job_dir))
            self.resume_path.set(str(job_dir))
        self._append_log("任务已提交。具体进度和结果以后台记录为准。")
        if self._cancel_after_start:
            self._cancel_after_start = False
            try:
                self.controller.cancel()
            except Exception as error:
                self._stopping = False
                self._append_log(f"停止请求失败：{error}")
                messagebox.showerror("停止请求失败", str(error), parent=self.root)

    def _use_current_job(self) -> None:
        if self.state.job_dir:
            self.resume_path.set(self.state.job_dir)

    def _use_result_image(self) -> None:
        if self.state.running or not self.state.can_recover or not self.state.image_path:
            return
        self.image_path.set(self.state.image_path)
        self.map_path.set(self.state.map_path)
        self.tabs.select(self.image_tab)
        self.volume_hint.set("已填入镜像。请点击“识别分区”，核对后选择要恢复的卷。")

    def _restart_admin(self) -> None:
        self._launch(self.controller.restart_as_admin, self._admin_restarted, "正在请求Windows管理员授权…")

    def _admin_restarted(self, result: Any) -> None:
        if result is False:
            self._append_log("未启动管理员窗口。可以保持当前窗口，继续已有镜像的恢复。")
        else:
            self._append_log("已请求管理员窗口，请在Windows提示中确认；当前窗口没有自动开始读取设备。")

    def _cancel(self) -> None:
        if not self.state.running or self._stopping:
            return
        if self._pending:
            # start_* may still be validating before its cancellation token exists.
            self._cancel_after_start = True
        else:
            try:
                self.controller.cancel()
            except Exception as error:
                messagebox.showerror("停止请求失败", str(error), parent=self.root)
                self._append_log(f"停止请求失败：{error}")
                return
        self._stopping = True
        self.state = replace(self.state, stage="cancelling", message="已请求安全停止，等待后台退出并保存当前结果。")
        self._render()

    def _open_path(self, path: str) -> None:
        if not path:
            return
        try:
            os.startfile(str(Path(path)))
        except (OSError, AttributeError) as error:
            messagebox.showerror("无法打开", f"无法打开此位置：\n{path}\n\n{error}", parent=self.root)

    def _open_results(self) -> None:
        self._open_path(self.state.job_dir)

    def _open_report(self) -> None:
        self._open_path(self.state.report_path)

    def _append_log(self, text: Any) -> None:
        # Bound both event size and retained UI history; full logs belong to jobs.
        value = str(text).replace("\x00", "")[:16000]
        self.log.configure(state="normal")
        self.log.insert("end", value + "\n")
        self._log_lines += value.count("\n") + 1
        if self._log_lines > 1200:
            self.log.delete("1.0", f"{self._log_lines - 1000 + 1}.0")
            self._log_lines = 1000
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        if self._dead:
            return
        for _ in range(100):
            try:
                ok, value, done = self._replies.get_nowait()
            except queue.Empty:
                break
            self._pending = False
            if ok:
                try:
                    done(value)
                except Exception as error:
                    self._show_error(error)
            else:
                self._show_error(value)
        for _ in range(150):
            try:
                event = self.controller.events.get_nowait()
            except queue.Empty:
                break
            if not isinstance(event, Mapping):
                continue
            self.state = reduce_event(self.state, event)
            if event.get("message"):
                self._append_log(event["message"])
            if event.get("type") in {"error", "result"} or self.state.stage in TERMINAL_STAGES:
                self._stopping = False
        self._render()
        if self._closing and not self.state.running and not self._pending:
            self._destroy()
            return
        self.root.after(100, self._poll)

    def _show_error(self, error: Any) -> None:
        text = str(error)
        self.state = reduce_event(self.state, {"type": "error", "message": text})
        self._stopping = False
        self._cancel_after_start = False
        self._append_log("错误：" + text)
        if isinstance(error, PermissionError):
            self._append_log("设备权限不足时，可点击“以管理员身份重新打开”，再重新选择设备。")
        messagebox.showerror("操作未完成", text + "\n\n已有任务文件不会因此被删除。", parent=self.root)

    def _render(self) -> None:
        self.status_text.set(summary_text(self.state))
        self.message_text.set(self.state.message)
        self.job_text.set("任务位置：" + (self.state.job_dir or "尚未开始"))
        for label, count in count_labels(self.state.counts):
            self.count_vars[label].set(str(count))
        unknown_progress = self.state.running and self.state.progress is None
        if unknown_progress != self._indeterminate:
            self.progress.stop()
            self.progress.configure(mode="indeterminate" if unknown_progress else "determinate")
            self._indeterminate = unknown_progress
            if unknown_progress:
                self.progress.start(15)
        if not unknown_progress:
            self.progress.configure(value=self.state.progress if self.state.progress is not None else 0)
        self.percent_text.set("处理中…" if unknown_progress else
                              (f"{self.state.progress:.1f}%" if self.state.progress is not None else "—"))
        self._render_controls()

    def _render_controls(self) -> None:
        # This is also called by variable traces while the form is being built.
        if not hasattr(self, "stop_button"):
            return
        busy = self._pending or self.state.running
        for widget in self._inputs + self._action_buttons:
            widget.configure(state="disabled" if busy else "normal")
        self.volume_combo.configure(state="disabled" if busy or not self._volumes else "readonly")
        self.devices_tree.configure(selectmode="none" if busy else "browse")
        if not busy:
            self.start_image_button.configure(state="normal" if self._selected_device() else "disabled")
            self.start_recovery_button.configure(state="normal" if self._volumes and self.volume_combo.current() >= 0 else "disabled")
            self.use_current_button.configure(state="normal" if self.state.job_dir else "disabled")
        self.stop_button.configure(state="normal" if self.state.running and not self._stopping else "disabled")
        self.open_button.configure(state="normal" if self.state.job_dir else "disabled")
        self.report_button.configure(state="normal" if self.state.report_path else "disabled")
        self.recover_image_button.configure(state="normal" if not busy and self.state.can_recover and self.state.image_path else "disabled")

    def _on_close(self) -> None:
        if self.state.running:
            if not messagebox.askyesno("停止后关闭", "当前任务仍在处理。停止并保存当前结果后关闭窗口？\n\n"
                                       "镜像未读范围和部分结果会如实保留。", parent=self.root):
                return
            self._closing = True
            self._cancel()
        elif self._pending:
            messagebox.showinfo("正在处理请求", "请等待当前请求结束后再关闭。", parent=self.root)
        else:
            self._destroy()

    def _destroy(self) -> None:
        self._dead = True
        self.progress.stop()
        self.root.destroy()


def run(controller: Any) -> None:
    """The sole window-creation entry point; called by the application's launcher."""
    root = tk.Tk()
    RecoveryWindow(root, controller)
    root.mainloop()
