import urllib.request
import json
try:
    req = urllib.request.urlopen('https://api.github.com/repos/aazanabili/wisperlive/actions/runs')
    data = json.loads(req.read())
    if 'workflow_runs' in data:
        for r in data['workflow_runs'][:5]:
            print(f"Run ID: {r['id']}, Status: {r['status']}, Conclusion: {r['conclusion']}, Event: {r['event']}, Name: {r['name']}")
    else:
        print(data)
except Exception as e:
    print('Error:', e)
