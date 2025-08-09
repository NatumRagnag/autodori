import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import queue
import threading
from PIL import Image, ImageTk

# 导入重构后的运行器和共享的应用状态
from autodori import AutomationRunner, app_state, configure_log
from util import get_available_servers

class AppGUI:
    """AutoDori 的主图形用户界面应用窗口。"""
    def __init__(self, root, state):
        self.root = root
        self.state = state
        self.automation_thread = None

        self.root.title("AutoDori 控制面板")
        self.root.geometry("850x750") # 稍微加宽以容纳新组件
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        # --- 主布局框架 ---
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        controls_frame = ttk.LabelFrame(main_frame, text="控制", padding="10")
        controls_frame.pack(fill=tk.X, padx=5, pady=5)

        output_frame = ttk.Frame(main_frame)
        output_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        log_frame = ttk.LabelFrame(output_frame, text="控制台输出", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(0, 5))

        self.debug_frame = ttk.LabelFrame(output_frame, text="调试信息", padding="10")

        # --- 控制组件 ---
        self.server_list = get_available_servers()
        default_server = self.server_list[0] if self.server_list else ""

        self.param_vars = {
            'server_name': tk.StringVar(value=self.state.config['server_name'] or default_server),
            'difficulty': tk.StringVar(value=self.state.config['difficulty']),
            'livemode': tk.StringVar(value=self.state.config['livemode']),
            'min_liveboost': tk.IntVar(value=self.state.config['min_liveboost']),
            'debug': tk.BooleanVar(value=self.state.config['debug']),
        }

        # 参数网格
        params_grid = ttk.Frame(controls_frame)
        params_grid.pack(fill=tk.X, expand=True, side=tk.LEFT, padx=(0, 20))

        # 第0行
        ttk.Label(params_grid, text="服务器:").grid(row=0, column=0, sticky=tk.W, pady=2)
        server_combo = ttk.Combobox(params_grid, textvariable=self.param_vars['server_name'], values=self.server_list, state="readonly")
        server_combo.grid(row=0, column=1, sticky=tk.EW, pady=2)

        ttk.Label(params_grid, text="最低体力:").grid(row=0, column=2, sticky=tk.W, pady=2, padx=(10, 0))
        boost_spin = ttk.Spinbox(params_grid, from_=0, to=10, textvariable=self.param_vars['min_liveboost'], width=10)
        boost_spin.grid(row=0, column=3, sticky=tk.W, pady=2)

        # 第1行
        ttk.Label(params_grid, text="难度:").grid(row=1, column=0, sticky=tk.W, pady=2)
        diff_combo = ttk.Combobox(params_grid, textvariable=self.param_vars['difficulty'], values=["easy", "normal", "hard", "expert", "special"], state="readonly")
        diff_combo.grid(row=1, column=1, sticky=tk.EW, pady=2)
        
        debug_check = ttk.Checkbutton(params_grid, text="启用调试视图", variable=self.param_vars['debug'], command=self.toggle_debug_frame)
        debug_check.grid(row=1, column=2, columnspan=2, sticky=tk.W, padx=(10, 0))

        # 第2行
        ttk.Label(params_grid, text="Live 模式:").grid(row=2, column=0, sticky=tk.W, pady=2)
        mode_combo = ttk.Combobox(params_grid, textvariable=self.param_vars['livemode'], values=["freelive", "challengelive"], state="readonly")
        mode_combo.grid(row=2, column=1, sticky=tk.EW, pady=2)

        params_grid.columnconfigure(1, weight=1)
        params_grid.columnconfigure(3, weight=1)

        # 操作按钮
        action_buttons_frame = ttk.Frame(controls_frame)
        action_buttons_frame.pack(side=tk.RIGHT)
        self.start_button = ttk.Button(action_buttons_frame, text="开始", command=self.start_automation, width=15)
        self.start_button.pack(pady=2)
        self.stop_button = ttk.Button(action_buttons_frame, text="停止", command=self.stop_automation, state=tk.DISABLED, width=15)
        self.stop_button.pack(pady=2)
        
        # --- 日志和调试组件 ---
        self.log_text = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, bg="#1E1E1E", fg="#D4D4D4")
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.debug_image_label = ttk.Label(self.debug_frame, text="无调试图像", anchor=tk.CENTER)
        self.debug_image_label.pack(pady=5)
        self.debug_text_label = ttk.Label(self.debug_frame, text="OCR: 无 | 匹配: 无", wraplength=380)
        self.debug_text_label.pack(pady=5)

        self.toggle_debug_frame()

    def toggle_debug_frame(self):
        if self.param_vars['debug'].get():
            self.debug_frame.pack(fill=tk.BOTH, expand=True, side=tk.RIGHT)
        else:
            self.debug_frame.pack_forget()

    def _update_state_from_gui(self):
        for key, var in self.param_vars.items():
            self.state.config[key] = var.get()

    def start_automation(self):
        self._update_state_from_gui()
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.log_text.config(state='normal')
        self.log_text.delete('1.0', tk.END)
        self.log_text.config(state='disabled')
        self.automation_thread = AutomationRunner(self.state)
        self.automation_thread.start()
        self.root.after(100, self.poll_queues)

    def stop_automation(self):
        if self.automation_thread and self.automation_thread.is_alive():
            self.automation_thread.stop_automation()
        self.stop_button.config(state=tk.DISABLED)

    def poll_queues(self):
        self.log_text.config(state='normal')
        while not self.state.log_queue.empty():
            try:
                self.log_text.insert(tk.END, self.state.log_queue.get_nowait() + '\n')
                self.log_text.see(tk.END)
            except queue.Empty:
                break
        self.log_text.config(state='disabled')

        while not self.state.debug_queue.empty():
            try:
                self.update_debug_info(self.state.debug_queue.get_nowait())
            except queue.Empty:
                break

        if self.automation_thread and self.automation_thread.is_alive():
            self.root.after(100, self.poll_queues)
        else:
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)

    def update_debug_info(self, data):
        if 'image' in data:
            img = data['image']
            img.thumbnail((350, 200), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.debug_image_label.config(image=photo, text="")
            self.debug_image_label.image = photo
        else:
            self.debug_image_label.config(image=None, text="无调试图像")
            self.debug_image_label.image = None

        text_info = []
        if 'text' in data: text_info.append(f"OCR: {data['text']}")
        if 'match' in data and data['match']: text_info.append(f"匹配: {data['match'][0]} ({data['match'][1]}%)")
        if 'similarity' in data: text_info.append(f"相似度: {data['similarity']:.2f}")
        self.debug_text_label.config(text=" | ".join(text_info) if text_info else "无")

    def _on_closing(self):
        if self.automation_thread and self.automation_thread.is_alive():
            if messagebox.askokcancel("退出", "自动化仍在运行。您想停止它并退出吗？"):
                self.stop_automation()
                self.root.destroy()
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()
