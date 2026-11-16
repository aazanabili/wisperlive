"""QA probe (scratch): verify prompts moved verbatim from HEAD llm_api.py."""
import io
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
old = subprocess.run(
    ["git", "show", "HEAD:llm_api.py"], cwd=REPO,
    capture_output=True, text=True, encoding="utf-8", check=True,
).stdout
new = (REPO / "llm_payloads.py").read_text(encoding="utf-8")


def grab(src, name):
    m = re.search(re.escape(name) + r' = """(.*?)"""', src, re.S)
    return m.group(1) if m else None


o1, n1 = grab(old, "_TRANSCRIBE_PROMPT"), grab(new, "_TRANSCRIBE_PROMPT")
o2, n2 = grab(old, "_POST_PROCESS_PROMPT_TEMPLATE"), grab(new, "_POST_PROCESS_PROMPT_TEMPLATE")
print("transcribe verbatim:", o1 == n1, "| old none:", o1 is None, "| new none:", n1 is None)
print("postprocess verbatim:", o2 == n2, "| old none:", o2 is None, "| new none:", n2 is None)
sys.exit(0 if (o1 == n1 and o2 == n2) else 1)
