import json
import urllib.request

with open('data/events.jsonl') as f:
    events = [json.loads(line) for line in f if line.strip()]

print(f"Loaded {len(events)} events")

batch = {'events': events[:50]}
data = json.dumps(batch).encode()
req = urllib.request.Request(
    'http://localhost:8000/events/ingest',
    data=data,
    headers={'Content-Type': 'application/json'},
    method='POST'
)
resp = urllib.request.urlopen(req)
print("Ingest response:", resp.read().decode())

# Check metrics
req2 = urllib.request.Request('http://localhost:8000/stores/STORE_BLR_002/metrics')
resp2 = urllib.request.urlopen(req2)
print("Metrics:", resp2.read().decode())