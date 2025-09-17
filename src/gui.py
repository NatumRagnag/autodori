import sys
import signal
import subprocess
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext

import autodori


def run_ui():
    autodori.init_maa()
    autodori.init_player_and_mnt()

    root = tk.Tk()
    root.title("Autodori UI")

    difficulty_var = tk.StringVar(value="hard")
    livemode_var = tk.StringVar(value="freelive")
    liveboost_var = tk.IntVar(value=1)
    debug_var = tk.BooleanVar(value=False)

    process = None

    def start():
        nonlocal process
        if process and process.poll() is None:
            return
        cmd = [
            sys.executable,
            str(Path(autodori.__file__).resolve()),
            "--mode",
            "main",
            "--difficulty",
            difficulty_var.get(),
            "--livemode",
            livemode_var.get(),
            "--liveboost",
            str(liveboost_var.get()),
        ]
        if debug_var.get():
            cmd.append("--debug")
        text_area.delete("1.0", tk.END)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE if debug_var.get() else None,
            stderr=subprocess.STDOUT if debug_var.get() else None,
            text=True,
        )
        if debug_var.get():
            def reader():
                for line in process.stdout:
                    text_area.insert(tk.END, line)
                    text_area.see(tk.END)
            threading.Thread(target=reader, daemon=True).start()

    def stop():
        nonlocal process
        if process and process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)
            except Exception:
                process.kill()
        process = None

    def toggle_debug():
        if debug_var.get():
            debug_frame.grid()
        else:
            debug_frame.grid_remove()

    def do_ocr():
        autodori.debug_ocr_song()

    frm = ttk.Frame(root, padding=10)
    frm.grid()

    ttk.Label(frm, text="Difficulty").grid(row=0, column=0, sticky="w")
    ttk.OptionMenu(
        frm,
        difficulty_var,
        difficulty_var.get(),
        "easy",
        "normal",
        "hard",
        "expert",
        "special",
    ).grid(row=0, column=1, sticky="ew")

    ttk.Label(frm, text="Live Mode").grid(row=1, column=0, sticky="w")
    ttk.OptionMenu(
        frm,
        livemode_var,
        livemode_var.get(),
        "freelive",
        "challengelive",
    ).grid(row=1, column=1, sticky="ew")

    ttk.Label(frm, text="Min Live Boost").grid(row=2, column=0, sticky="w")
    ttk.Spinbox(frm, from_=1, to=10, textvariable=liveboost_var, width=5).grid(
        row=2, column=1, sticky="w"
    )

    ttk.Checkbutton(
        frm, text="Debug Mode", variable=debug_var, command=toggle_debug
    ).grid(row=3, column=0, columnspan=2, sticky="w")

    ttk.Button(frm, text="Start", command=start).grid(row=4, column=0, pady=5)
    ttk.Button(frm, text="Stop", command=stop).grid(row=4, column=1, pady=5)

    ttk.Button(frm, text="OCR歌曲", command=do_ocr).grid(
        row=5, column=0, columnspan=2, pady=5
    )

    debug_frame = ttk.Frame(root, padding=10)
    debug_frame.grid(row=1, column=0, sticky="nsew")
    text_area = scrolledtext.ScrolledText(debug_frame, width=80, height=20)
    text_area.pack()
    debug_frame.grid_remove()

    root.mainloop()


if __name__ == "__main__":
    autodori.configure_log(debug=True)
    run_ui()
