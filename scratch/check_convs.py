import json
import requests
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open('accounts/fb_615912137265/cookies.json', 'r', encoding='utf-8') as f:
    cookie_list = json.load(f)

cookies = {c['name']: c['value'] for c in cookie_list}
headers = {
    'Accept': '*/*',
    'Content-Type': 'application/json; encoding=utf-8',
    'agw-js-conv': 'str',
    'Origin': 'https://www.dola.com',
    'Referer': 'https://www.dola.com/chat',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
}
params = {
    'version_code': '20800', 'language': 'vi', 'device_platform': 'web',
    'doubao_device_platform': 'web', 'aid': '495671', 'real_aid': '495671',
    'pkg_type': 'release_version', 'pc_version': '3.32.62', 'doubao_pc_version': '3.32.62',
    'region': 'JP', 'sys_region': 'JP', 'samantha_web': '1', 'web_platform': 'browser',
    'use-olympus-account': '1', 'web_tab_id': 'test'
}
if 'msToken' in cookies: params['msToken'] = cookies['msToken']
if 's_v_web_id' in cookies: params['fp'] = cookies['s_v_web_id']

for cid in ['38418259535793425', '38417999217120017']:
    print('='*60)
    print(f"CONVERSATION: {cid}")
    p = {
        'cmd': 3100,
        'uplink_body': {
            'pull_singe_chain_uplink_body': {
                'conversation_id': str(cid),
                'anchor_index': 999999999999999,
                'conversation_type': 3,
                'direction': 1,
                'limit': 20,
                'ext': {},
                'filter': {'index_list': []},
            }
        },
        'sequence_id': 'test_single', 'channel': 2, 'version': '1'
    }
    r = requests.post('https://www.dola.com/im/chain/single', params=params, json=p, cookies=cookies, headers=headers, timeout=15)
    msgs = r.json().get('downlink_body', {}).get('pull_singe_chain_downlink_body', {}).get('messages', [])
    for m in msgs:
        ext = m.get('ext', {})
        res_code = ext.get('ai_creation_res_code')
        clarifying = ext.get('is_creation_clarifying')
        print(f"\n[Sender: {m.get('sender_type')} | res_code: {res_code} | clarifying: {clarifying}]")
        content_raw = m.get('content', '')
        try:
            cj = json.loads(content_raw)
        except:
            cj = content_raw
        if isinstance(cj, list):
            for block in cj:
                tb = block.get('content', {}).get('text_block', {}).get('text')
                cb = block.get('content', {}).get('creation_block')
                if tb: print(f"  TEXT: {tb}")
                if cb: 
                    creations = cb.get('creations', [])
                    print(f"  CREATIONS count: {len(creations)}")
                    for cre in creations:
                        print(f"    type={cre.get('type')}, download_url={cre.get('video', {}).get('download_url')[:60] if cre.get('video', {}).get('download_url') else None}")
        else:
            print(f"  RAW: {cj}")
