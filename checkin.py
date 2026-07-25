# -- coding: utf-8 --
import os
import requests
import re
import sys
import json
from datetime import datetime

def parse_expiry_date(expiry_str):
    """解析过期日期（格式：YYYY-MM-DD），返回剩余天数"""
    if not expiry_str:
        return None
    try:
        expiry = datetime.strptime(expiry_str.strip(), "%Y-%m-%d")
        delta = expiry - datetime.now()
        return delta.days
    except Exception:
        return None

def send_telegram_message(text):
    bot_token = os.getenv('TG_BOT_TOKEN')
    chat_id = os.getenv('TG_CHAT_ID')
    if not bot_token or not chat_id:
        print("未配置 Telegram 通知，跳过。")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }, timeout=10)
        if resp.status_code == 200:
            print("Telegram 通知发送成功。")
        else:
            print(f"Telegram 通知失败: {resp.text}")
    except Exception as e:
        print(f"发送 Telegram 异常: {e}")


def send_magicpush_message(text):
    """推送消息到自建 MagicPush 网关（飞牛 NAS）"""
    mp_url = os.getenv('MAGICPUSH_URL')
    mp_token = os.getenv('MAGICPUSH_TOKEN')
    if not mp_url or not mp_token:
        print("未配置 MagicPush 通知，跳过。")
        return
    # 去掉 Telegram 用的 HTML 标签，MagicPush 用纯文本/Markdown
    plain = text.replace('<b>', '').replace('</b>', '')
    url = f"{mp_url.rstrip('/')}/api/push/{mp_token}"
    payload = {
        "title": "📅 NodeSeek 签到汇总",
        "content": plain,
        "type": "markdown"
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            print("MagicPush 通知发送成功。")
        else:
            print(f"MagicPush 通知失败: {resp.status_code} {resp.text[:120]}")
    except Exception as e:
        print(f"发送 MagicPush 异常: {e}")


# 与生成 cf_clearance 的浏览器保持一致的 UA（否则 Cloudflare 因 UA 不匹配拒绝）
BROWSER_UA = 'Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36'


def checkin(cookie, random_mode=False):
    """签到函数，使用正确的 API 方式：POST /api/attendance?random=true/false，body为空
    改进：先用 Session GET 首页，让 Cloudflare 在 runner 的 IP 上重新下发 cf_clearance，
    避免依赖用户家庭 IP 签发的 clearance（跨 IP 常被拒 403）。"""
    random_param = 'true' if random_mode else 'false'
    url = f"https://www.nodeseek.com/api/attendance?random={random_param}"

    base_headers = {
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh-Hans;q=0.9,en;q=0.8',
        'Content-Type': 'application/json',
        'Origin': 'https://www.nodeseek.com',
        'Referer': 'https://www.nodeseek.com/board',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'User-Agent': BROWSER_UA
    }

    s = requests.Session()
    # 先把用户提供的登录态 cookie 灌进 session
    for kv in cookie.split(';'):
        kv = kv.strip()
        if '=' in kv:
            k, v = kv.split('=', 1)
            s.cookies.set(k.strip(), v.strip())

    # 预热：访问首页，让 Cloudflare 在 runner IP 上重新签发 cf_clearance（覆盖旧的）
    try:
        s.get('https://www.nodeseek.com/board', headers=base_headers, timeout=15)
    except Exception:
        pass

    try:
        resp = s.post(url, headers=base_headers, timeout=15)
    except Exception as e:
        return False, f"请求异常: {e}", 0

    if resp.status_code != 200:
        if resp.status_code == 500:
            if '已签到' in resp.text or '重复' in resp.text:
                return True, "已签到（今日已打卡）", 0
        return False, f"HTTP {resp.status_code}", 0

    try:
        result = resp.json()
    except:
        return False, f"非JSON响应: {resp.text[:100]}", 0

    success = result.get('success', False)
    msg = result.get('message', '')
    state = result.get('state', '')

    if not success and re.search(r'(已完成签到|已签到|重复|already|duplicate)', msg, re.I):
        return True, "已签到（今日已打卡）", 0

    chicken = 0
    if success or state == 'success':
        m = re.search(r'获得(\d+)鸡腿', msg)
        if m:
            chicken = int(m.group(1))
        return True, msg, chicken

    return False, msg, 0

def main():
    cookies_raw = os.getenv('NS_COOKIES')
    if not cookies_raw:
        print("错误: 未设置 NS_COOKIES")
        sys.exit(1)

    random_mode = os.getenv('NS_RANDOM', 'false').strip().lower() == 'true'

    lines = [line.strip() for line in cookies_raw.split('\n') if line.strip()]
    if not lines:
        print("错误: NS_COOKIES 为空")
        sys.exit(1)

    print(f"签到模式: {'试试手气' if random_mode else '固定鸡腿'}")
    print(f"检测到 {len(lines)} 个账号，开始签到...")
    results = []

    # 解析每行，支持格式：用户名|Cookie|到期日期（可选）
    accounts = []
    for line in lines:
        parts = line.split('|')
        if len(parts) >= 3:
            username = parts[0].strip()
            cookie = parts[1].strip()
            expiry_date = parts[2].strip()
        elif len(parts) == 2:
            username = parts[0].strip()
            cookie = parts[1].strip()
            expiry_date = None
        else:
            username = None
            cookie = line
            expiry_date = None
        accounts.append((username, cookie, expiry_date))

    for idx, (username, cookie, expiry_date) in enumerate(accounts, 1):
        display_name = username if username else f"账号 {idx}"

        # 计算该账号的剩余天数
        days_left = parse_expiry_date(expiry_date)
        days_str = f"{days_left} 天" if days_left is not None else "未知"

        success, msg, chicken = checkin(cookie, random_mode)
        status_icon = "✅" if success else "❌"
        if success and chicken == 0:
            numbers = re.findall(r'\d+', msg)
            if numbers:
                chicken = int(numbers[0])

        result_line = f"{display_name}: {status_icon} {msg}\n 获得 {chicken} 鸡腿 \n cookie到期剩余 {days_str} \n https://www.nodeseek.com/"
        results.append(result_line)
        print(result_line)

    final_msg = "<b>📅 NodeSeek 签到汇总</b>\n" + "\n".join(results)
    send_telegram_message(final_msg)
    send_magicpush_message(final_msg)

if __name__ == "__main__":
    main()
