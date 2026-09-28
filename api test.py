import os

import requests


api_key = os.getenv("TEXT2STATA_API_KEY_LINE1")
if not api_key:
    raise SystemExit("Please set TEXT2STATA_API_KEY_LINE1 before running this test.")

models_to_try = ["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4o"]
base_urls = [
    os.getenv("TEXT2STATA_API_BASE_URL", "https://globalai.vip"),
    "https://api.globalai.vip",
]

for base_url in dict.fromkeys(base_urls):
    print(f"\nTesting base URL: {base_url}")
    print("=" * 50)

    for model in models_to_try:
        try:
            url = f"{base_url}/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            data = {
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 50,
            }

            response = requests.post(url, headers=headers, json=data, timeout=10)

            if response.status_code == 200:
                result = response.json()
                print(f"Model '{model}' succeeded")
                print(f"  Reply: {result['choices'][0]['message']['content'][:50]}...")
            else:
                error_msg = response.text[:100] if response.text else "no error message"
                print(f"Model '{model}' failed: HTTP {response.status_code}, {error_msg}")

        except Exception as exc:
            print(f"Model '{model}' raised: {str(exc)[:80]}")
