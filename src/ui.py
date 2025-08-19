import signal
import subprocess
import sys
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox


class AutoDoriGUI:
    """Simple Tkinter based GUI for launching AutoDori."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AutoDori")

        tk.Label(self.root, text="服务器").grid(row=0, column=0, sticky="w")
        self.server_var = tk.StringVar(value="日服")
        tk.Entry(self.root, textvariable=self.server_var, width=10).grid(
            row=0, column=1, sticky="w"
        )

        tk.Label(self.root, text="难度").grid(row=1, column=0, sticky="w")
        self.diff_var = tk.StringVar(value="hard")
        ttk.Combobox(
            self.root,
            textvariable=self.diff_var,
            values=["easy", "normal", "hard", "expert", "special"],
            width=8,
        ).grid(row=1, column=1, sticky="w")

        tk.Label(self.root, text="首音延迟(ms)").grid(row=2, column=0, sticky="w")
        self.wait_var = tk.DoubleVar(value=0.0)
        self._build_offset_controls(2, self.wait_var)

        tk.Label(self.root, text="补偿延迟(ms)").grid(row=3, column=0, sticky="w")
        self.interval_var = tk.DoubleVar(value=0.0)
        self._build_offset_controls(3, self.interval_var)

        self.debug_var = tk.BooleanVar()
        tk.Checkbutton(
            self.root, text="调试模式", variable=self.debug_var, command=self._toggle_debug
        ).grid(row=4, column=0, sticky="w")

        self.ocr_button = tk.Button(self.root, text="OCR歌曲", command=self._ocr_song)
        self.ocr_button.grid(row=4, column=1, sticky="w")
        self.ocr_button.grid_remove()

        tk.Button(self.root, text="开始", command=self.start).grid(
            row=5, column=0, sticky="ew"
        )
        tk.Button(self.root, text="停止", command=self.stop).grid(
            row=5, column=1, sticky="ew"
        )

        self.proc: subprocess.Popen | None = None

    def _build_offset_controls(self, row: int, var: tk.DoubleVar) -> None:
        entry = tk.Entry(self.root, textvariable=var, width=8)
        entry.grid(row=row, column=1, sticky="w")
        steps = [5, 20, 50]
        for idx, step in enumerate(steps):
            tk.Button(
                self.root,
                text=f"+{step}",
                command=lambda s=step, v=var: v.set(v.get() + s),
            ).grid(row=row, column=2 + idx)
            tk.Button(
                self.root,
                text=f"-{step}",
                command=lambda s=step, v=var: v.set(v.get() - s),
            ).grid(row=row, column=5 + idx)

    def _toggle_debug(self) -> None:
        if self.debug_var.get():
            self.ocr_button.grid()
        else:
            self.ocr_button.grid_remove()

    def _ocr_song(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("OCR Song")
        tk.Label(top, text="识别到的歌曲名称：").pack(padx=10, pady=5)
        name_var = tk.StringVar()
        tk.Entry(top, textvariable=name_var).pack(padx=10, pady=5)
        tk.Button(top, text="确认", command=top.destroy).pack(pady=5)

    def start(self) -> None:
        if self.proc is not None:
            messagebox.showwarning("警告", "主程序已在运行")
            return
        cmd = [
            sys.executable,
            str(Path(__file__).with_name("autodori.py")),
            "--difficulty",
            self.diff_var.get(),
            "--resource",
            self.server_var.get(),
            "--offset-wait",
            str(self.wait_var.get()),
            "--offset-interval",
            str(self.interval_var.get()),
        ]
        self.proc = subprocess.Popen(cmd)

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.send_signal(signal.SIGINT)
        finally:
            self.proc = None

    def run(self) -> None:
        self.root.mainloop()


def launch_gui() -> None:
    gui = AutoDoriGUI()
    gui.run()

