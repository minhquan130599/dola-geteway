import json
import requests
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open('accounts/fb_615912137265/cookies.json', 'r', encoding='utf-8') as f:
    cookie_list = json.load(f)
cookies = {c['name']: c['value'] for c in cookie_list}
headers = {'Accept': '*/*', 'Content-Type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str', 'Origin': 'https://www.dola.com', 'Referer': 'https://www.dola.com/chat', 'User-Agent': 'Mozilla/5.0'}
params = {'version_code': '20800', 'language': 'vi', 'device_platform': 'web', 'doubao_device_platform': 'web', 'aid': '495671', 'real_aid': '495671', 'pkg_type': 'release_version', 'pc_version': '3.32.62', 'doubao_pc_version': '3.32.62', 'region': 'JP', 'sys_region': 'JP', 'samantha_web': '1', 'web_platform': 'browser', 'use-olympus-account': '1', 'web_tab_id': 'test'}
if 'msToken' in cookies: params['msToken'] = cookies['msToken']
if 's_v_web_id' in cookies: params['fp'] = cookies['s_v_web_id']

payload = {'cmd': 3200, 'uplink_body': {'pull_recent_conv_chain_uplink_body': {'limit': 10, 'message_count_per_conv': 10, 'api_version': 1, 'conv_version': 0, 'direction': 3, 'option': {'not_need_message': False, 'need_complete_conversation': True, 'need_coco_conversation': True, 'need_coco_bot': True, 'need_pc_pin_chain': True, 'pc_pin_query_type': 0}}}, 'sequence_id': 'test', 'channel': 2, 'version': '1'}
resp = requests.post('https://www.dola.com/im/chain/recent_conv', params=params, json=payload, cookies=cookies, headers=headers, timeout=15)
data = resp.json()
cells = data.get('downlink_body', {}).get('pull_recent_conv_chain_downlink_body', {}).get('cells', [])

for c in cells:
    conv = c.get('conversation', {})
    cid = conv.get('conversation_id') or c.get('id')
    print('==================================================')
    print(f'CONVERSATION {cid}: {conv.get("name")}')
    p_single = {'cmd': 3100, 'uplink_body': {'pull_singe_chain_uplink_body': {'conversation_id': str(cid), 'anchor_index': 999999999999999, 'conversation_type': 3, 'direction': 1, 'limit': 30, 'ext': {}, 'filter': {'index_list': []}, 'evaluate_ab_params': '', 'evaluate_common_params': ''}}, 'sequence_id': 'test_single', 'channel': 2, 'version': '1'}
    r_single = requests.post('https://www.dola.com/im/chain/single', params=params, json=p_single, cookies=cookies, headers=headers, timeout=15)
    msgs = r_single.json().get('downlink_body', {}).get('pull_singe_chain_downlink_body', {}).get('messages', [])
    for m in msgs:
        sender = 'BOT' if m.get('sender_type') == 2 else 'USER'
        content_raw = m.get('content', '')
        try:
            content_json = json.loads(content_raw)
        except:
            content_json = []
        if isinstance(content_json, list):
            for block in content_json:
                tb = block.get('content', {}).get('text_block', {}).get('text')
                cb = block.get('content', {}).get('creation_block')
                btype = block.get('block_type')
                if tb:
                    print(f'[{sender} text]: {tb}')
                if cb:
                    creations = cb.get('creations', [])
                    for cr in creations:
                        print(f'[{sender} CREATION]: type={cr.get("type")}, status={cr.get("status")}, video={bool(cr.get("video"))}')
                if not tb and not cb:
                    print(f'[{sender} block {btype}]: {block}')
        else:
            print(f'[{sender} raw]: {content_raw}')
