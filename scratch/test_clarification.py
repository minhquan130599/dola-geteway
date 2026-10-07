import sys
sys.path.insert(0, '.')
import re
from video_worker_ui import CLARIFICATION_PATTERN, CANNOT_GENERATE_PATTERN, PROCEEDING_PATTERN, PromptClarificationError

sample_texts = [
    "Video generation currently supports durations from 4 to 15 seconds. I can generate it at the nearest supported duration of 15 seconds.",
    "Video generation currently supports durations from 4 to 15 seconds. Would you like me to generate it with a duration of 15 seconds?",
    "動画生成ツールは 4 秒から 15 秒までの長さをサポートしています。最も近い 15 秒で生成しますか？",
    "Tôi không thể tạo video và không có điểm tín dụng nào được sử dụng cho lần tạo này.",
    "I'll create the video for you now.",
]

print("\n--- Testing Poll Simulation with User Sample ---")
text = "Video generation currently supports durations from 4 to 15 seconds. I can generate it at the nearest supported duration of 15 seconds."
poll = {"texts": [text], "isClarifying": True}

clarify_text = ""
for t in poll.get("texts", []):
    if CLARIFICATION_PATTERN.search(t):
        clarify_text = t
        break

if clarify_text:
    fail_msg = (
        f"Dola yêu cầu điều chỉnh thời lượng/kịch bản: \"{clarify_text.strip()}\". "
        f"Vui lòng điều chỉnh lại prompt (ghi rõ thời lượng mong muốn từ 4-15s ở đầu prompt và tránh chia quá nhiều phân cảnh chi tiết)."
    )
    print("SUCCESS! Successfully caught and constructed fail_msg:")
    print(fail_msg)

