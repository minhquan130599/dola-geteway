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

payload = {
    'cmd': 3200,
    'uplink_body': {'pull_recent_conv_chain_uplink_body': {
        'limit': 10, 'message_count_per_conv': 10, 'api_version': 1, 'conv_version': 0,
        'direction': 3,
        'option': {'not_need_message': False, 'need_complete_conversation': True,
                   'need_coco_conversation': True, 'need_coco_bot': True,
                   'need_pc_pin_chain': True, 'pc_pin_query_type': 0}
    }},
    'sequence_id': 'test', 'channel': 2, 'version': '1'
}

resp = requests.post('https://www.dola.com/im/chain/recent_conv', params=params, json=payload, cookies=cookies, headers=headers, timeout=15)
data = resp.json()
cells = data.get('downlink_body', {}).get('pull_recent_conv_chain_downlink_body', {}).get('cells', [])
print(f'Total convs: {len(cells)}')

for c in cells:
    conv = c.get('conversation', {})
    cid = conv.get('conversation_id') or c.get('id')
    print('='*50)
    print(f"Conversation ID: {cid} | Name: {conv.get('name')}")
    # Now pull messages from /im/chain/single
    p_single = {
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
                'evaluate_ab_params': '',
                'evaluate_common_params': '',
            }
        },
        'sequence_id': 'test_single', 'channel': 2, 'version': '1'
    }
    r_single = requests.post('https://www.dola.com/im/chain/single', params=params, json=p_single, cookies=cookies, headers=headers, timeout=15)
    msgs = r_single.json().get('downlink_body', {}).get('pull_singe_chain_downlink_body', {}).get('messages', [])
    for m in msgs:
        sender = m.get('sender_type')
        ext = m.get('ext', {})
        content_raw = m.get('content', '')
        try:
            content_json = json.loads(content_raw)
        except:
            content_json = content_raw
        print(f"\n[Sender {sender} | index {m.get('index')} | ext: {ext}]")
        if isinstance(content_json, list):
            for block in content_json:
                btype = block.get('block_type')
                tb = block.get('content', {}).get('text_block', {}).get('text')
                cb = block.get('content', {}).get('creation_block')
                if tb:
                    print(f"  TEXT: {tb}")
                if cb:
                    print(f"  CREATION: {cb}")
                if not tb and not cb:
                    print(f"  BLOCK ({btype}): {block}")
        else:
            print(f"  RAW: {content_json}")
