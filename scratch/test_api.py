import requests

res = requests.post('http://127.0.0.1:8000/api/admin/login', json={'username': 'admin', 'password': 'Minhquan@130599'})
print('Login status:', res.status_code)
token = res.json().get('token')

accs = requests.get('http://127.0.0.1:8000/api/admin/accounts', headers={'X-Admin-Key': token}).json()
print('Total accounts:', len(accs.get('accounts', [])))
for a in accs.get('accounts', []):
    print(f"Account {a['name']}: busy={a['busy']}, browser_open={a['browser_open']}, worker_running={a.get('worker_running')}")
