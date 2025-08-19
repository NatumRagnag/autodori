import os
import sys
import json
import signal
import time
import subprocess
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except Exception:
    PIL_OK = False

# Optional util for server list (fallback if import fails)
try:
    from util import get_available_servers
except Exception:
    def get_available_servers():
        return ["CN", "TW", "JP", "EN"]

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONTROL_FILE = APP_DIR / "data" / "ui_control.json"
DEFAULT_CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_control():
    if DEFAULT_CONTROL_FILE.exists():
        try:
            with open(DEFAULT_CONTROL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_control(patch: dict):
    data = load_control()
    data.update(patch)
    # sanitize
    if "first_note_delay_ms" in data:
        try:
            data["first_note_delay_ms"] = int(float(data["first_note_delay_ms"]))
        except Exception:
            data["first_note_delay_ms"] = 0
    if "manual_comp_ms" in data:
        try:
            v = int(float(data["manual_comp_ms"]))
            # clamp to sane range
            if v < -1000: v = -1000
            if v > 1000: v = 1000
            data["manual_comp_ms"] = v
        except Exception:
            data["manual_comp_ms"] = 0
    with open(DEFAULT_CONTROL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

class ProcessManager:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def is_running(self):
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def start(self, args, new_console=True):
        with self.lock:
            if self.is_running():
                raise RuntimeError("已有进程在运行")
            # reset ready flag
            save_control({"ready": False})
            creationflags = 0
            preexec_fn = None
            if os.name == "nt":
                if new_console:
                    creationflags |= subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                preexec_fn = os.setsid  # new process group
            self.proc = subprocess.Popen(args, cwd=str(APP_DIR),
                                         creationflags=creationflags,
                                         preexec_fn=preexec_fn)

    def stop(self, force_after=1.5):
        with self.lock:
            if not self.is_running():
                return
            if os.name == "nt":
                try:
                    os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
                except Exception:
                    try:
                        os.kill(self.proc.pid, signal.CTRL_C_EVENT)
                    except Exception:
                        pass
            else:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
                except Exception:
                    try:
                        self.proc.send_signal(signal.SIGINT)
                    except Exception:
                        pass
            # graceful wait
            deadline = time.time() + force_after
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
            if self.proc.poll() is None:
                try:
                    if os.name == "nt":
                        self.proc.kill()
                    else:
                        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self.proc = None

class ROISelector(tk.Toplevel):
    """Toplevel window to select a rectangular ROI on an image."""
    def __init__(self, master, image_path, on_ok):
        super().__init__(master)
        self.title("选择 OCR 区域")
        self.image_path = image_path
        self.on_ok = on_ok
        self.roi = None
        if not PIL_OK:
            ttk.Label(self, text="未安装 Pillow，无法显示截图。").pack(padx=16, pady=16)
            ttk.Button(self, text="关闭", command=self.destroy).pack(pady=8)
            return

        self.img = Image.open(image_path)
        self.tkimg = ImageTk.PhotoImage(self.img)

        self.canvas = tk.Canvas(self, width=self.tkimg.width(), height=self.tkimg.height(), cursor="cross")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas_img = self.canvas.create_image(0, 0, image=self.tkimg, anchor="nw")
        self.rect = None
        self.start = None

        self.info = ttk.Label(self, text="拖拽选择一个矩形区域")
        self.info.pack(fill=tk.X, padx=8, pady=(2,6))

        btnbar = ttk.Frame(self); btnbar.pack(fill=tk.X, padx=8, pady=(0,8))
        ttk.Button(btnbar, text="确认", command=self.confirm).pack(side=tk.RIGHT, padx=(8,0))
        ttk.Button(btnbar, text="取消", command=self.destroy).pack(side=tk.RIGHT)

        self.canvas.bind("<ButtonPress-1>", self.on_down)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_up)

    def on_down(self, e):
        self.start = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        if self.rect is not None:
            self.canvas.delete(self.rect); self.rect = None

    def on_move(self, e):
        if self.start is None: return
        x0, y0 = self.start
        x1, y1 = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        if self.rect is not None:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(x0, y0, x1, y1, outline="red", width=2)
        w, h = abs(int(x1-x0)), abs(int(y1-y0))
        self.info.config(text=f"ROI: {min(int(x0),int(x1))},{min(int(y0),int(y1))},{w},{h}")

    def on_up(self, e):
        if self.start is None: return
        x0, y0 = self.start
        x1, y1 = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        x, y = int(min(x0, x1)), int(min(y0, y1))
        w, h = int(abs(x1 - x0)), int(abs(y1 - y0))
        self.roi = (x, y, w, h)
        self.info.config(text=f"选区完成：{x},{y},{w},{h}")

    def confirm(self):
        if not self.roi:
            messagebox.showwarning("提示", "请先拖出一个选区")
            return
        self.on_ok(self.image_path, self.roi)
        self.destroy()

class AppGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AutoDori 启动器")
        self.geometry("980x640")
        self.minsize(920, 560)

        self.procman = ProcessManager()

        servers = get_available_servers() or []
        default_server = servers[0] if servers else "CN"
        self.var_server = tk.StringVar(value=default_server)
        self.var_difficulty = tk.StringVar(value="expert")
        self.livemode_choices = ["freelive", "challengelive"]
        self.var_livemode = tk.StringVar(value=self.livemode_choices[0])
        self.var_debug = tk.BooleanVar(value=False)

        self.var_first_delay = tk.StringVar(value=str(load_control().get("first_note_delay_ms", 0)))
        self.var_manual_comp = tk.StringVar(value=str(load_control().get("manual_comp_ms", 0)))

        # Top controls
        top = ttk.Frame(self); top.pack(fill=tk.X, padx=12, pady=8)
        ttk.Label(top, text="区服:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(top, values=servers, textvariable=self.var_server, width=10, state="readonly").grid(row=0, column=1, padx=(4,16), sticky="w")

        ttk.Label(top, text="难度:").grid(row=0, column=2, sticky="w")
        ttk.Combobox(top, values=["easy","normal","hard","expert","special"], textvariable=self.var_difficulty, width=10, state="readonly").grid(row=0, column=3, padx=(4,16), sticky="w")

        ttk.Label(top, text="模式:").grid(row=0, column=4, sticky="w")
        ttk.Combobox(top, values=self.livemode_choices, textvariable=self.var_livemode, width=10, state="readonly").grid(row=0, column=5, padx=(4,16), sticky="w")

        ttk.Checkbutton(top, text="Debug", variable=self.var_debug, command=self._on_debug_toggle).grid(row=0, column=6, padx=(4,16), sticky="w")

        ttk.Button(top, text="开始 (流程)", command=self.on_start_pipeline).grid(row=0, column=7, padx=(4,8))
        ttk.Button(top, text="开始 (直打)", command=self.on_start_direct).grid(row=0, column=8, padx=(4,8))
        ttk.Button(top, text="停止", command=self.on_stop).grid(row=0, column=9, padx=(4,8))

        # Debug frame (shown only when Debug checked)
        self.debug_frame = ttk.LabelFrame(self, text="调试 / 即时控制")
        self.debug_visible = False

        ttk.Label(self.debug_frame, text="首音延迟(ms):").grid(row=0, column=0, sticky="w", padx=(8,4), pady=6)
        ttk.Entry(self.debug_frame, textvariable=self.var_first_delay, width=8).grid(row=0, column=1, sticky="w")
        ttk.Button(self.debug_frame, text="−5", width=4, command=lambda:self.bump_delay(self.var_first_delay, -5)).grid(row=0, column=2, padx=2)
        ttk.Button(self.debug_frame, text="+5", width=4, command=lambda:self.bump_delay(self.var_manual_comp, +5) if False else self.bump_delay(self.var_first_delay, +5)).grid(row=0, column=3, padx=2)

        ttk.Label(self.debug_frame, text="手动补偿(ms):").grid(row=0, column=4, sticky="w", padx=(16,4))
        ttk.Entry(self.debug_frame, textvariable=self.var_manual_comp, width=8).grid(row=0, column=5, sticky="w")
        ttk.Button(self.debug_frame, text="−5", width=4, command=lambda:self.bump_delay(self.var_manual_comp, -5)).grid(row=0, column=6, padx=2)
        ttk.Button(self.debug_frame, text="+5", width=4, command=lambda:self.bump_delay(self.var_manual_comp, +5)).grid(row=0, column=7, padx=2)

        ttk.Button(self.debug_frame, text="写入控制文件", command=self.on_write_control).grid(row=0, column=8, padx=(16,8))
        ttk.Button(self.debug_frame, text="Ready（开始识别首音）", command=self.on_ready).grid(row=0, column=9, padx=(16,8))
        ttk.Button(self.debug_frame, text="OCR 选歌", command=self.on_ocr_song).grid(row=0, column=10, padx=(8,8))
        ttk.Button(self.debug_frame, text="快速重对齐", command=self.on_recalibrate).grid(row=0, column=11, padx=(8,8))

        # Log frame
        self.log_frame = ttk.LabelFrame(self, text="提示 / 记录")
        self.log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0,12))
        self.txt_log = scrolledtext.ScrolledText(self.log_frame, height=14, wrap="word")
        self.txt_log.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._on_debug_toggle()  # init

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- helpers ----------
    def log(self, s: str):
        try:
            self.txt_log.insert(tk.END, time.strftime("[%H:%M:%S] ") + s + "\n")
            self.txt_log.see(tk.END)
        except Exception:
            pass

    # ---------- process ops ----------
    def on_start_pipeline(self):
        if self.procman.is_running():
            messagebox.showinfo("提示", "已有进程在运行。请先停止。")
            return
        diff = self.var_difficulty.get()
        if diff == "master":
            diff = "special"
        argv = [sys.executable, str(APP_DIR / "autodori.py"),
                "--server", self.var_server.get(),
                "--difficulty", diff,
                "--livemode", self.var_livemode.get()]
        self.log("启动主流程… " + " ".join(argv))
        try:
            self.procman.start(argv, new_console=True)
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def on_start_direct(self):
        if self.procman.is_running():
            messagebox.showinfo("提示", "已有进程在运行。请先停止。")
            return
        song = simpledialog.askstring("直打模式", "请输入歌曲名（可模糊匹配）：", parent=self)
        if not song:
            return
        diff = self.var_difficulty.get()
        if diff == "master":
            diff = "special"
        argv = [sys.executable, str(APP_DIR / "autodori.py"),
                "--mode", "direct",
                "--song", song,
                "--difficulty", diff,
                "--server", self.var_server.get(),
                "--hold-for-ready",
                "--control-file", str(DEFAULT_CONTROL_FILE)]
        self.log("直打模式待命… " + " ".join(argv))
        try:
            self.procman.start(argv, new_console=True)
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def on_stop(self):
        if not self.procman.is_running():
            return
        self.log("发送停止信号…")
        self.procman.stop()
        self.log("已停止。")
        save_control({"ready": False})

    # ---------- debug / control ----------
    def _on_debug_toggle(self):
        want = self.var_debug.get()
        if want and not getattr(self, "debug_visible", False):
            self.debug_frame.pack(fill=tk.X, padx=12, pady=(0,8))
            self.debug_visible = True
        elif (not want) and getattr(self, "debug_visible", False):
            self.debug_frame.pack_forget()
            self.debug_visible = False

    def bump_delay(self, var: tk.StringVar, delta: int):
        try:
            v = int(float(var.get()))
        except Exception:
            v = 0
        v += delta
        var.set(str(v))
        # write-through for responsiveness
        self.on_write_control()

    def on_write_control(self):
        try:
            fd = int(float(self.var_first_delay.get()))
        except Exception:
            fd = 0
        try:
            mc = int(float(self.var_manual_comp.get()))
        except Exception:
            mc = 0
        save_control({"first_note_delay_ms": fd, "manual_comp_ms": mc})
        self.log(f"写入控制：首音延迟={fd}ms，手动补偿={mc}ms")

    def on_ready(self):
        save_control({"ready": True})
        self.log("已发送 Ready，主程序可进入首音识别。")

    def on_recalibrate(self):
        save_control({"recalibrate_now": True})
        self.log("请求快速重对齐（recalibrate_now=true）")

    # ---------- OCR flow ----------
    def on_ocr_song(self):
        if not PIL_OK:
            messagebox.showwarning("缺少依赖", "未安装 Pillow，无法显示截图与框选。请先安装 pillow。")
            return
        outdir = APP_DIR / "tmp"; outdir.mkdir(exist_ok=True)
        shot = outdir / "screen.png"
        argv = [sys.executable, str(APP_DIR / "autodori.py"), "--capture-screen", str(shot)]
        self.log("请求主程序截屏…")
        try:
            ret = subprocess.run(argv, cwd=str(APP_DIR))
            if ret.returncode != 0 or not shot.exists():
                messagebox.showerror("失败", "截屏失败，请确认模拟器连接正常。")
                return
        except Exception as e:
            messagebox.showerror("失败", f"截屏异常：{e}")
            return

        def after_roi(image_path, roi):
            self.log(f"OCR ROI={roi}")
            ox, oy, ow, oh = map(int, roi)
            argv2 = [sys.executable, str(APP_DIR / "autodori.py"),
                     "--ocr-from-file", str(image_path),
                     "--roi", f"{ox},{oy},{ow},{oh}"]
            try:
                result = subprocess.run(argv2, cwd=str(APP_DIR), capture_output=True, text=True, encoding="utf-8")
                txt = result.stdout.strip()
                if not txt:
                    raise RuntimeError("OCR 无输出")
            except Exception as e:
                messagebox.showerror("OCR失败", f"{e}")
                return

            song = None
            # Try JSON first
            try:
                jd = json.loads(txt)
                if isinstance(jd, dict):
                    song = jd.get("text") or jd.get("name") or jd.get("song") or ""
                elif isinstance(jd, list) and jd:
                    song = jd[0]
            except Exception:
                # Fallback to first non-empty line
                for line in txt.splitlines():
                    if line.strip():
                        song = line.strip()
                        break
            if not song:
                song = txt.splitlines()[0] if txt else ""

            top = tk.Toplevel(self)
            top.title("确认歌曲名")
            ttk.Label(top, text="识别到的歌曲名（可修改）：").pack(anchor="w", padx=12, pady=(12,6))
            var_name = tk.StringVar(value=song)
            ttk.Entry(top, textvariable=var_name, width=48).pack(fill=tk.X, padx=12)
            ttk.Label(top, text="难度将采用主界面选择的档位，无需再点‘选择难度’。").pack(anchor="w", padx=12, pady=(6,6))

            def confirm_start():
                name = var_name.get().strip()
                if not name:
                    messagebox.showwarning("提示", "歌曲名不能为空")
                    return
                top.destroy()
                if self.procman.is_running():
                    self.on_stop()
                diff = self.var_difficulty.get()
                if diff == "master":
                    diff = "special"
                argv3 = [sys.executable, str(APP_DIR / "autodori.py"),
                         "--mode", "direct",
                         "--song", name,
                         "--difficulty", diff,
                         "--server", self.var_server.get(),
                         "--hold-for-ready",
                         "--control-file", str(DEFAULT_CONTROL_FILE)]
                self.log("直打（OCR）待命… " + " ".join(argv3))
                try:
                    self.procman.start(argv3, new_console=True)
                except Exception as e:
                    messagebox.showerror("启动失败", str(e))

            btnbar = ttk.Frame(top); btnbar.pack(fill=tk.X, pady=12)
            ttk.Button(btnbar, text="确认并开始（待命）", command=confirm_start).pack(side=tk.RIGHT, padx=(8,12))
            ttk.Button(btnbar, text="取消", command=top.destroy).pack(side=tk.RIGHT)

        ROISelector(self, str(shot), on_ok=after_roi)

    # ---------- window ----------
    def on_close(self):
        if self.procman.is_running():
            if messagebox.askyesno("退出", "仍有任务在运行，是否先停止？"):
                self.on_stop()
            else:
                return
        self.destroy()

def main():
    app = AppGUI()
    app.mainloop()

if __name__ == "__main__":
    main()
