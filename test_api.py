import requests
import os
import json

# Manually load .env
try:
    with open('.env', 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                key, value = line.split('=', 1)
                os.environ[key] = value
except FileNotFoundError:
    print("Warning: .env file not found")

API_URL = os.getenv("API_URL")
API_TOKEN = os.getenv("API_TOKEN")

headers = {"Authorization": f"Token {API_TOKEN}"}

print(f"Testing API at: {API_URL}")

def test_endpoint(path):
    url = f"{API_URL}{path}"
    print(f"\n--- Testing {url} ---")
    try:
        response = requests.get(url, headers=headers, timeout=5)
        print(f"Status Code: {response.status_code}")
        try:
            data = response.json()
            print("Response JSON (truncated):")
            print(json.dumps(data, indent=2)[:500] + "..." if len(str(data)) > 500 else json.dumps(data, indent=2))
            return data
        except json.JSONDecodeError:
            print("Response is not JSON")
            print(response.text[:200])
            return None
    except Exception as e:
        print(f"Error: {e}")
        return None

# Test Root API
test_endpoint("")

# Test Search Parameters
target_id = "K1Uw_YVgCBsww"
print(f"\n--- Testing Search Params for {target_id} ---")

# Test Page Size
print(f"\n--- Testing Page Size ---")

sizes = [12, 50, 100]

for size in sizes:
    url = f"/video/?page_size={size}"
    print(f"Testing {url}...")
    data = test_endpoint(url)
    if data and isinstance(data, dict) and 'data' in data:
        count = len(data['data'])
        print(f"Requested {size}, got {count} items.")
        if 'paginate' in data:
            print(f"Pagination meta: {data['paginate']}")
