import requests
import json

# Test Google
r = requests.post('http://127.0.0.1:5000/api/scan', json={'url': 'https://www.google.com'})
d = r.json()
print(f"Google - Status: {r.status_code}, Images: {d.get('count', 'ERROR')}")
if 'error' in d:
    print(f"  Error: {d['error']}")

# Test Apple
r2 = requests.post('http://127.0.0.1:5000/api/scan', json={'url': 'https://www.apple.com'})
d2 = r2.json()
print(f"Apple  - Status: {r2.status_code}, Images: {d2.get('count', 'ERROR')}")
if 'error' in d2:
    print(f"  Error: {d2['error']}")

# Test Unsplash
r3 = requests.post('http://127.0.0.1:5000/api/scan', json={'url': 'https://unsplash.com'})
d3 = r3.json()
print(f"Unsplash - Status: {r3.status_code}, Images: {d3.get('count', 'ERROR')}")
if 'error' in d3:
    print(f"  Error: {d3['error']}")
