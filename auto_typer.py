import pyperclip
import keyboard
import time

def paste_text(text):
    if not text:
        return
    # Save the current clipboard content just in case? Or just overwrite.
    # Overwriting is standard for this type of app.
    pyperclip.copy(text)
    
    # Small delay to ensure clipboard is updated and user has released keys
    time.sleep(0.1)
    
    # Simulate Ctrl+V
    # We use 'ctrl+v' as it is standard on Windows
    keyboard.send('ctrl+v')
    print("Pasted text into active window.")
