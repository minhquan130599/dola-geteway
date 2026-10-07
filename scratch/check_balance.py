import json
import requests
import sys
import io
import re

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
ids = [c.get('conversation', {}).get('conversation_id') or c.get('id') for c in cells if c.get('id')]
print('Conversations:', ids)

batch_payload = {
    'cmd': 3101,
    'uplink_body': {'batch_pull_singe_chain_uplink_body': {
        'conversation_type': 3, 'direction': 3, 'limit': 1,
        'params': [{'conversation_id': cid} for cid in ids],
        'evaluate_ab_params': '', 'evaluate_common_params': '', 'ext': {}
    }},
    'sequence_id': 'test', 'channel': 2, 'version': '1'
}
batch_resp = requests.post('https://www.dola.com/im/chain/batch_single', params=params, json=batch_payload, cookies=cookies, headers=headers, timeout=15)
raw_text = batch_resp.text
for m in re.finditer(r'Bạn (?:vẫn )?còn \d+[^"\n]+', raw_text):
    print('Found balance match:', m.group(0))
for m in re.finditer(r'Tôi không thể[^"\n]+', raw_text):
    print('Found fail match:', m.group(0))
