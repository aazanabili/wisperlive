import os
import time

def paste_text(text):
    if not text:
        return
    # Save the current clipboard content just in case? Or just overwrite.
    # Overwriting is standard for this type of app.
    import pyperclip
    pyperclip.copy(text)

    # Small delay to ensure clipboard is updated and user has released keys
    time.sleep(0.1)

    # Simulate Ctrl+V.
    # Windows uses 'ctrl+v' as it is standard there; on Linux the keyboard
    # package would need root privileges, so synthesise the key press
    # through XTEST instead.
    if os.name == "nt":
        import keyboard
        keyboard.send('ctrl+v')
    else:
        from linux_hotkey import send_paste
        send_paste()
    print("Pasted text into active window.")
