"""Окна tkinter: вопрос «Разобрать почту?» и окно прогресса."""
import os
import queue
import threading
import traceback

from .common import Stop, make_logger


def dpi_aware():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def ask_dialog() -> bool:
    import tkinter as tk
    from tkinter import messagebox
    dpi_aware()
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    ans = messagebox.askyesno(
        "Разбор почты",
        "Разобрать почту локальной моделью?\n\n"
        "Модель запустится, разберёт новые письма во всех ящиках, откроет отчёт и выключится.",
        parent=root)
    root.destroy()
    return bool(ans)


def run_gui(args, pipeline_fn):
    import tkinter as tk
    from tkinter import scrolledtext

    q = queue.Queue()
    cancel = threading.Event()
    log = make_logger(q)
    state = {"running": True}

    root = tk.Tk()
    root.title("Разбор почты" + (" — тест" if args.dry_run else ""))
    root.geometry("680x380")
    root.attributes("-topmost", True)
    root.after(1500, lambda: root.attributes("-topmost", False))

    status = tk.Label(root, text="Работаю…", font=("Segoe UI", 11, "bold"), anchor="w")
    status.pack(fill="x", padx=10, pady=(10, 4))
    btn = tk.Button(root, text="Отменить", width=14)
    btn.pack(side="bottom", pady=8)
    txt = scrolledtext.ScrolledText(root, font=("Consolas", 9), wrap="word", state="disabled", height=12)
    txt.pack(fill="both", expand=True, padx=10)

    def add_line(s):
        txt.configure(state="normal")
        txt.insert("end", s + "\n")
        txt.see("end")
        txt.configure(state="disabled")

    def on_close():
        if state["running"]:
            cancel.set()
            status.configure(text="Останавливаю… (дождитесь выключения модели)")
            btn.configure(state="disabled")
        else:
            root.destroy()

    btn.configure(command=on_close)
    root.protocol("WM_DELETE_WINDOW", on_close)

    def worker():
        try:
            total, ok = pipeline_fn(args, log, cancel)
            q.put(("done" if ok else "errors", total))
        except Stop as e:
            log(str(e))
            q.put(("fail", str(e).splitlines()[0]))
        except Exception as e:
            log("Непредвиденная ошибка:\n" + traceback.format_exc())
            q.put(("fail", f"Непредвиденная ошибка: {e}"))

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    add_line(val)
                else:
                    state["running"] = False
                    btn.configure(text="Закрыть", state="normal")
                    if kind == "done":
                        status.configure(text="Готово. " + val, fg="#1d6b2f", wraplength=650, justify="left")
                        root.after(30000, root.destroy)  # закрыть само через 30 с
                    else:  # сбой или ошибки в ящиках: окно остаётся, пока его не закроют
                        text = "Готово, есть ошибки (см. отчёт). " + val if kind == "errors" else val
                        status.configure(text=text, fg="#b00020", wraplength=650, justify="left")
                        root.attributes("-topmost", True)
        except queue.Empty:
            pass
        root.after(200, poll)

    threading.Thread(target=worker, daemon=True).start()
    poll()
    root.mainloop()
    # окно закрыли во время работы — ждём, пока модель выключится
    cancel.set()
