import json
import requests

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

with open('scratch/output.txt', 'w', encoding='utf-8') as out:
    for c in cells:
        cid = c.get('id')
        conv_info = c.get('conversation', {})
        out.write(f"\n================== CONV {cid} (name: {conv_info.get('name')}) ==================\n")
        p_single = {'cmd': 3100, 'uplink_body': {'pull_singe_chain_uplink_body': {'conversation_id': str(cid), 'anchor_index': 999999999999999, 'conversation_type': 3, 'direction': 1, 'limit': 30, 'ext': {}, 'filter': {'index_list': []}, 'evaluate_ab_params': '', 'evaluate_common_params': ''}}, 'sequence_id': 'test_single', 'channel': 2, 'version': '1'}
        r_single = requests.post('https://www.dola.com/im/chain/single', params=params, json=p_single, cookies=cookies, headers=headers, timeout=15)
        msgs = r_single.json().get('downlink_body', {}).get('pull_singe_chain_downlink_body', {}).get('messages', [])
        out.write(f"Messages count: {len(msgs)}\n")
        for m in msgs:
            st = m.get('sender_type')
            sender = 'BOT' if st == 2 else ('USER' if st == 1 else f'UNKNOWN_{st}')
            out.write(f"\n--- MSG id={m.get('id')} index={m.get('index')} sender={sender} ---\n")
            ext = m.get('ext', {})
            for k, v in ext.items():
                if k in ['is_creation_clarifying', 'ai_creation_res_code', 'chat_ability', 'input_skill', 'inner_msg_lang_code', 'clarify_type']:
                    out.write(f"  ext[{k}] = {v}\n")
            content_raw = m.get('content', '')
            try:
                content_json = json.loads(content_raw)
            except:
                content_json = content_raw
            if isinstance(content_json, list):
                for b in content_json:
                    tb = b.get('content', {}).get('text_block', {}).get('text')
                    cb = b.get('content', {}).get('creation_block')
                    ab = b.get('content', {}).get('attachment_block')
                    btype = b.get('block_type')
                    if tb: out.write(f"  TEXT: {tb}\n")
                    if cb: out.write(f"  CREATION: {json.dumps(cb, ensure_ascii=False)}\n")
                    if ab: out.write(f"  ATTACHMENT: {len(ab.get('attachments', []))} attachments\n")
                    if not tb and not cb and not ab:
                        out.write(f"  BLOCK {btype}: {json.dumps(b, ensure_ascii=False)[:200]}\n")
            else:
                out.write(f"  CONTENT: {content_json}\n")
