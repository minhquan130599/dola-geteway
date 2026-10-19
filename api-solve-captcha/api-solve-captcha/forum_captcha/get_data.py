import asyncio
import os
from playwright.async_api import async_playwright
from PIL import Image
from io import BytesIO

# Tạo thư mục nếu chưa tồn tại
save_dir = "combined_image"
os.makedirs(save_dir, exist_ok=True)

async def capture_captcha():
    async with async_playwright() as p:
        # 1️⃣ Mở trình duyệt
        browser = await p.chromium.launch(headless=False)  # Hiển thị trình duyệt để tránh bị chặn
        page = await browser.new_page()

        for i in range(408, 1001):  # Lặp 1000 lần để lấy CAPTCHA mới
            print(f"🔄 [{i}] Đang làm mới CAPTCHA...")

            # 2️⃣ Truy cập trang web chứa CAPTCHA
            await page.goto("https://breachforums.st/member?action=login")

            # 3️⃣ Lấy element CAPTCHA và ảnh cũ
            captcha_element = await page.wait_for_selector("#captcha_img")
            old_captcha_src = await captcha_element.get_attribute("src")

            # 4️⃣ Nhấn "Refresh" để tạo CAPTCHA mới
            await page.click("input[name='refresh']")

            # 5️⃣ Chờ CAPTCHA mới xuất hiện (tối đa 5 giây)
            captcha_loaded = False
            for _ in range(10):  # Kiểm tra tối đa 10 lần (tương đương 5 giây)
                await asyncio.sleep(0.5)
                new_captcha_src = await captcha_element.get_attribute("src")
                if new_captcha_src and new_captcha_src != old_captcha_src:
                    captcha_loaded = True
                    break  # Nếu CAPTCHA thay đổi, tiếp tục

            # 6️⃣ Nếu CAPTCHA không load sau 5 giây, mở tab mới và đóng tab cũ
            if not captcha_loaded:
                print(f"⚠️ [{i}] CAPTCHA load quá lâu! Mở tab mới & đóng tab cũ...")
                new_page = await browser.new_page()  # Mở tab mới
                await new_page.goto("https://breachforums.st/member?action=login")
                await page.close()  # Đóng tab cũ
                page = new_page  # Gán tab mới làm trang chính
                await page.wait_for_selector("#captcha_img")
                continue  # Bỏ qua vòng lặp hiện tại để thử lại

            old_captcha_src = new_captcha_src  # Cập nhật ảnh CAPTCHA cũ

            # 7️⃣ Lấy tọa độ CAPTCHA
            box = await captcha_element.bounding_box()

            if box:
                print(f"✅ [{i}] CAPTCHA tọa độ: x={box['x']}, y={box['y']}, width={box['width']}, height={box['height']}")

                # 8️⃣ Chụp màn hình và cắt CAPTCHA
                screenshot = await page.screenshot()
                image = Image.open(BytesIO(screenshot))

                left, top, right, bottom = box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]
                captcha_image = image.crop((left, top, right, bottom))

                # 9️⃣ Lưu ảnh CAPTCHA
                captcha_filename = os.path.join(save_dir, f"combined_image{i}.png")
                captcha_image.save(captcha_filename)
                print(f"✅ [{i}] CAPTCHA đã lưu: {captcha_filename}")

            else:
                print(f"❌ [{i}] Không tìm thấy CAPTCHA.")

        # 🔚 Đóng trình duyệt sau khi hoàn thành 1000 lần
        await browser.close()

# Chạy Playwright
asyncio.run(capture_captcha())

# import requests

# cookies = {
#     '__ddg1_': 'BvLPfpgZxmpbLsVaoIge',
#     'loginattempts': '1',
#     'mybb[announcements]': '0',
#     'sid': '4dfd37d19e629ba47809bd224b1753cf',
#     '__ddg9_': '27.72.145.217',
#     'mybb[lastvisit]': '1741076101',
#     'mybb[lastactive]': '1741080516',
#     'mybbuser': '304801_LRaPNd9h48XM2rE0pmrFya5qTsCVlNMUIfhjL6EbQSH0LILWs3',
#     '__ddg8_': 'flwV528aCyfPN99x',
#     '__ddg10_': '1741081028',
# }

# headers = {
#     'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
#     'accept-language': 'vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5',
#     'priority': 'u=0, i',
#     'referer': 'https://breachforums.st/search.php?action=results&sid=69638c14e34593d865d651f91243c444',
#     'sec-ch-ua': '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
#     'sec-ch-ua-mobile': '?0',
#     'sec-ch-ua-platform': '"Windows"',
#     'sec-fetch-dest': 'document',
#     'sec-fetch-mode': 'navigate',
#     'sec-fetch-site': 'same-origin',
#     'sec-fetch-user': '?1',
#     'upgrade-insecure-requests': '1',
#     'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
#     # 'cookie': '__ddg1_=BvLPfpgZxmpbLsVaoIge; loginattempts=1; mybb[announcements]=0; sid=4dfd37d19e629ba47809bd224b1753cf; __ddg9_=27.72.145.217; mybb[lastvisit]=1741076101; mybb[lastactive]=1741080516; mybbuser=304801_LRaPNd9h48XM2rE0pmrFya5qTsCVlNMUIfhjL6EbQSH0LILWs3; __ddg8_=flwV528aCyfPN99x; __ddg10_=1741081028',
# }

# params = {
#     'action': 'results',
#     'sid': 'f12538e7b808569e7f87551b7c09e336',
#     'sortby':'lastpost',
#     'order':'desc'
# }

# response = requests.get('https://breachforums.st/search.php', params=params, cookies=cookies, headers=headers)
# with open ("capsov.html","w",encoding= 'utf-8') as file:
#     file.write(response.text)
#     file.close()
# print("ok")
