"""QA probe (scratch): drive settings_view.show_settings with a withdrawn root.

Not part of unittest discovery (does not match test_*.py). Owned by
qa-test-assurance; safe to delete with the run directory.
"""
import queue
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

import tkinter as tk  # noqa: E402

import main  # noqa: E402
import settings_view  # noqa: E402

root = tk.Tk()
root.withdraw()
app = main.WhisperLiveApp.__new__(main.WhisperLiveApp)
app.root = root
app.config = {"api_key": "", "provider": "custom", "provider_variant": "",
              "provider_base_url": "https://legacy.example/v1", "model": "old-model",
              "transcription_model": "", "target_language": "English"}
app.is_exiting = False
app.is_processing = False
app._ui_queue = queue.Queue()
app.set_status = mock.MagicMock()

with mock.patch.object(settings_view, "save_config", return_value=True) as sc:
    app.show_settings()
    w = app.settings_widgets
    print("dialog exists:", w["dialog"].winfo_exists())
    print("base_entry state:", w["base_entry"].cget("state"))
    for k in ("variant_row", "mimo_rows", "model_row", "base_hint"):
        print(k, "manager:", w[k].winfo_manager())
    print("provider values:", w["provider_combo"].cget("values"))
    print("base_var:", w["base_var"].get(), "| model_var:", w["model_var"].get())

    w["provider_var"].set("Xiaomi MiMo")
    w["provider_combo"].event_generate("<<ComboboxSelected>>")
    root.update()
    print("--- MiMo ---")
    for k in ("variant_row", "mimo_rows", "model_row", "base_hint"):
        print(k, "manager:", w[k].winfo_manager())
    print("base state:", w["base_entry"].cget("state"), "| base_var:", w["base_var"].get())
    print("variant_var:", w["variant_var"].get(), "| values:", w["variant_combo"].cget("values"))
    print("transcription_var:", w["transcription_var"].get(), "| model_var:", w["model_var"].get())
    print("mimo combo values:", w["transcription_combo"].cget("values"), w["post_combo"].cget("values"))

    w["variant_var"].set("Token Plan — Singapore (SGP)")
    w["variant_combo"].event_generate("<<ComboboxSelected>>")
    root.update()
    print("--- SGP --- base_var:", w["base_var"].get())

    w["provider_var"].set("Custom (OpenAI-compatible URL)")
    w["provider_combo"].event_generate("<<ComboboxSelected>>")
    root.update()
    print("--- Custom --- state:", w["base_entry"].cget("state"), "| base_var:", w["base_var"].get())
    print("mimo_rows:", w["mimo_rows"].winfo_manager(), "| model_row:", w["model_row"].winfo_manager())

    w["provider_var"].set("Groq")
    w["provider_combo"].event_generate("<<ComboboxSelected>>")
    root.update()
    print("--- Groq --- base_var:", w["base_var"].get(), "| state:", w["base_entry"].cget("state"))
    w["model_var"].set("llama-3.3-70b")
    w["save_btn"].invoke()
    root.update()
    print("save called:", sc.call_count, "| config:", app.config)
    print("widgets after save:", app.settings_widgets)
    print("set_status:", app.set_status.call_args)

root.destroy()
print("destroyed; _default_root:", tk._default_root)
print("PROBE OK")
