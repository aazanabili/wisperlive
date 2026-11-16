"""SecOps read-only audit helper (run evidence): AST purity + import-graph acyclicity + secret sweep.

Read-only against the repository; prints findings to stdout. Retained as run evidence
for the T-2026-11-16-dictation-modes SecOps gate.
"""
import ast
import os
import re

os.chdir(r"H:\projects\wisperlive")

pure = ['dictation_config.py', 'llm_errors.py', 'quota_ledger.py', 'model_manifest.py',
        'dictation_types.py', 'session_state.py', 'pcm_stream.py', 'vad_segmenter.py']
allowed = {
    'dictation_config.py': set(),
    'llm_errors.py': set(),
    'quota_ledger.py': {'math', 'datetime', 'dataclasses', 'types', 'typing'},
    'model_manifest.py': {'hashlib', 'hmac', 're', 'dataclasses', 'typing', 'urllib'},
    'dictation_types.py': {'dataclasses', 'types', 'typing', 'providers'},
    'session_state.py': {'enum'},
    'pcm_stream.py': {'queue', 'threading', 'wave', 'collections', 'io'},
    'vad_segmenter.py': {'math', 'dataclasses', 'typing'},
}
bad = []
for f in pure:
    tree = ast.parse(open(f, encoding='utf-8').read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split('.')[0])
    extra = mods - allowed.get(f, mods)
    print("PURITY", f, sorted(mods))
    if extra:
        bad.append((f, sorted(extra)))
print('PURITY_VIOLATIONS:', bad)

nodes = {f[:-3] for f in os.listdir('.') if f.endswith('.py')}
graph = {}
for f in os.listdir('.'):
    if not f.endswith('.py'):
        continue
    tree = ast.parse(open(f, encoding='utf-8').read())
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            deps |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            deps.add(node.module.split('.')[0])
    graph[f[:-3]] = {d for d in deps if d in nodes}

cycles = set()


def dfs(n, stack):
    for d in sorted(graph.get(n, ())):
        if d in stack:
            cycles.add(tuple(stack[stack.index(d):] + [d]))
            continue
        dfs(d, stack + [d])


for n in sorted(graph):
    dfs(n, [n])
print('CYCLES:', cycles if cycles else 'NONE')

# secret-pattern sweep over production modules + manifest + artifacts
patterns = [
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"(dpapi|base64):[A-Za-z0-9+/=]{16,}"),
]
targets = [f for f in os.listdir('.') if f.endswith('.py')] + ['assets/models_manifest.json']
hits = []
for f in targets:
    text = open(f, encoding='utf-8', errors='replace').read()
    for p in patterns:
        for m in p.findall(text):
            hits.append((f, str(m)[:24]))
for root, _dirs, files in os.walk('artifacts'):
    for name in files:
        path = os.path.join(root, name)
        try:
            text = open(path, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        for p in patterns:
            for m in p.findall(text):
                hits.append((path, str(m)[:24]))
print('SECRET_HITS:', hits if hits else 'NONE')
