import json
import urllib.request

with open('data/events.jsonl') as f:
    events = [json.loads(line) for line in f if line.strip()]

print(f"Loaded {len(events)} total events")

# Send in batches of 500
BATCH_SIZE = 500
total_accepted = 0
total_rejected = 0

for i in range(0, len(events), BATCH_SIZE):
    batch = events[i:i+BATCH_SIZE]
    data = json.dumps({'events': batch}).encode()
    req = urllib.request.Request(
        'http://localhost:8000/events/ingest',
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read().decode())
    total_accepted += result['accepted']
    total_rejected += result['rejected']
    print(f"Batch {i//BATCH_SIZE + 1}: accepted={result['accepted']} rejected={result['rejected']}")

print(f"\nDone! Total accepted: {total_accepted} | Total rejected: {total_rejected}")

# Show final metrics
req2 = urllib.request.Request('http://localhost:8000/stores/STORE_BLR_002/metrics')
resp2 = urllib.request.urlopen(req2)
print("\nFinal Metrics:")
print(json.dumps(json.loads(resp2.read().decode()), indent=2))