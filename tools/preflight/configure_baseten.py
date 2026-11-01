"""Local credential entry. No network, shell, logging or key output."""
from pathlib import Path
import json
import os
import re
import tkinter as tk
from tkinter import ttk, messagebox

ROOT = Path(__file__).resolve().parents[2]


def save_key(value: str, path: Path) -> None:
    value = value.strip()
    if not value or any(c in value for c in '\r\n\x00'):
        raise ValueError('Enter a single non-empty API key.')
    original = path.read_text(encoding='utf-8-sig') if path.exists() else ''
    line = 'BASETEN_API_KEY=' + json.dumps(value)
    pattern = r'(?m)^\s*(?:export\s+)?BASETEN_API_KEY\s*=.*$'
    updated = re.sub(pattern, lambda _: line, original) if re.search(pattern, original) else original.rstrip() + '\n' + line + '\n'
    temp = path.with_name('.env.setup-tmp')
    try:
        temp.write_text(updated.lstrip('\n'), encoding='utf-8')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def main() -> None:
    window = tk.Tk()
    window.title('AURA — Baseten setup')
    window.geometry('530x240')
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Baseten API key', font=('Segoe UI', 15)).pack(anchor='w')
    ttk.Label(frame, text='Paste with Ctrl+V. Saved only to AURA/.env.\nThis window makes no API requests.').pack(anchor='w', pady=(10, 14))
    entry = ttk.Entry(frame, show='\u2022', width=62)
    entry.pack(fill='x')
    entry.focus_set()
    def save() -> None:
        try:
            save_key(entry.get(), ROOT / '.env')
        except Exception:
            messagebox.showerror('Not saved', 'Check that the key is non-empty and the AURA folder is writable.')
            return
        entry.delete(0, 'end')
        messagebox.showinfo('Saved', 'Key saved locally. Start or restart AURA, then open My apps to check a connected application.')
        window.destroy()
    ttk.Button(frame, text='Save locally', command=save).pack(anchor='e', pady=18)
    window.mainloop()


if __name__ == '__main__':
    main()
