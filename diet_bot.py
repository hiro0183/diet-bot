import sys, os, sqlite3, re
from datetime import datetime, date, timedelta
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler

# 設定（環境変数から取得）
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DB_PATH = os.environ.get("DB_PATH", "/tmp/diet_records.db")

# 知識ベース読み込み
with open('diet_knowledge.txt', 'r', encoding='utf-8') as f:
    KNOWLEDGE = f.read()
with open('molecular_nutrition.md', 'r', encoding='utf-8') as f:
    MOLECULAR_KNOWLEDGE = f.read()

# ========== データベース ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        gender TEXT,
        target_weight REAL,
        notify_hour INTEGER DEFAULT 21,
        notify_minute INTEGER DEFAULT 0,
        setup_step TEXT DEFAULT 'done',
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        record_date TEXT,
        morning_weight REAL,
        evening_weight REAL,
        water_ml INTEGER,
        bowel INTEGER DEFAULT 0,
        menstruation INTEGER DEFAULT 0,
        sleep_hours REAL,
        mood TEXT,
        UNIQUE(user_id, record_date)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        record_date TEXT,
        meal_type TEXT,
        content TEXT,
        recorded_at TEXT
    )''')
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {'user_id':row[0],'name':row[1],'gender':row[2],'target_weight':row[3],
                'notify_hour':row[4],'notify_minute':row[5],'setup_step':row[6]}
    return None

def upsert_user(user_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    user = get_user(user_id)
    if not user:
        c.execute('INSERT INTO users (user_id, created_at) VALUES (?, ?)',
                  (user_id, datetime.now().isoformat()))
    for k, v in kwargs.items():
        c.execute(f'UPDATE users SET {k}=? WHERE user_id=?', (v, user_id))
    conn.commit()
    conn.close()

def get_today_record(user_id):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM daily_records WHERE user_id=? AND record_date=?', (user_id, today))
    row = c.fetchone()
    conn.close()
    if row:
        return {'morning_weight':row[3],'evening_weight':row[4],'water_ml':row[5],
                'bowel':row[6],'menstruation':row[7],'sleep_hours':row[8],'mood':row[9]}
    return {}

def update_today_record(user_id, **kwargs):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO daily_records (user_id, record_date) VALUES (?, ?)', (user_id, today))
    for k, v in kwargs.items():
        c.execute(f'UPDATE daily_records SET {k}=? WHERE user_id=? AND record_date=?', (v, user_id, today))
    conn.commit()
    conn.close()

def add_meal(user_id, meal_type, content):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO meals (user_id, record_date, meal_type, content, recorded_at) VALUES (?,?,?,?,?)',
              (user_id, today, meal_type, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_today_meals(user_id):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT meal_type, content FROM meals WHERE user_id=? AND record_date=? ORDER BY recorded_at', (user_id, today))
    rows = c.fetchall()
    conn.close()
    return rows

def get_yesterday_record(user_id):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM daily_records WHERE user_id=? AND record_date=?', (user_id, yesterday))
    row = c.fetchone()
    conn.close()
    if row:
        return {'morning_weight':row[3],'evening_weight':row[4]}
    return {}

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE setup_step=?', ('done',))
    rows = c.fetchall()
    conn.close()
    return [{'user_id':r[0],'name':r[1],'gender':r[2],'target_weight':r[3],
             'notify_hour':r[4],'notify_minute':r[5]} for r in rows]

# ========== 体重分析 ==========
def analyze_weight(user_id):
    today = get_today_record(user_id)
    yesterday = get_yesterday_record(user_id)
    messages = []

    # 朝→夜の差（本日）
    if today.get('morning_weight') and today.get('evening_weight'):
        diff = today['evening_weight'] - today['morning_weight']
        if 0.4 <= diff <= 1.0:
            messages.append(f"朝→夜の体重差：+{diff:.1f}kg（理想範囲✓）")
        elif diff < 0.4:
            messages.append(f"朝→夜の体重差：+{diff:.1f}kg（少なめ。水分・食事量を確認しましょう）")
        else:
            messages.append(f"朝→夜の体重差：+{diff:.1f}kg（多め。食事内容や塩分を確認しましょう）")

    # 夜→朝の差（昨日夜→今朝）
    if yesterday.get('evening_weight') and today.get('morning_weight'):
        diff = yesterday['evening_weight'] - today['morning_weight']
        if 0.6 <= diff <= 1.0:
            messages.append(f"夜→朝の体重差：-{diff:.1f}kg（理想範囲✓）")
        elif diff < 0.6:
            messages.append(f"夜→朝の体重差：-{diff:.1f}kg（減りが少なめ。睡眠・むくみを確認しましょう）")
        else:
            messages.append(f"夜→朝の体重差：-{diff:.1f}kg（多め。水分不足や体調変化を確認しましょう）")

    return messages

# ========== メッセージ解析 ==========
def parse_message(user_id, text):
    text = text.strip()
    user = get_user(user_id)

    # マイIDはいつでも取得可能
    if text in ['マイID', 'myid', '自分のID', 'ID確認', 'マイid']:
        return f"あなたのUser IDは：\n{user_id}"

    # ===== 初回セットアップ =====
    if not user or user['setup_step'] != 'done':
        return handle_setup(user_id, text, user)

    # 設定変更
    if text in ['設定変更', '設定']:
        upsert_user(user_id, setup_step='name')
        return "設定を変更します。\nまずお名前を教えてください。"

    # 通知時刻変更
    m = re.search(r'通知.?(\d{1,2})[：:時](\d{0,2})', text)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        upsert_user(user_id, notify_hour=h, notify_minute=mi)
        reschedule_notifications()
        return f"通知時刻を{h:02d}:{mi:02d}に変更しました✓"

    # 朝体重
    m = re.search(r'朝.{0,3}?(\d{2,3}\.?\d*)\s*kg?', text)
    if m:
        w = float(m.group(1))
        update_today_record(user_id, morning_weight=w)
        yesterday = get_yesterday_record(user_id)
        msg = f"朝の体重 {w}kg を記録しました✓"
        if yesterday.get('evening_weight'):
            diff = yesterday['evening_weight'] - w
            if 0.6 <= diff <= 1.0:
                msg += f"\n夜→朝：-{diff:.1f}kg（理想範囲✓）"
            elif diff < 0.6:
                msg += f"\n夜→朝：-{diff:.1f}kg（少し減りが少ないですね）"
            else:
                msg += f"\n夜→朝：-{diff:.1f}kg（いつもより多めです）"
        return msg

    # 夜体重
    m = re.search(r'夜.{0,3}?(\d{2,3}\.?\d*)\s*kg?', text)
    if m:
        w = float(m.group(1))
        update_today_record(user_id, evening_weight=w)
        today = get_today_record(user_id)
        msg = f"夜の体重 {w}kg を記録しました✓"
        if today.get('morning_weight'):
            diff = w - today['morning_weight']
            if 0.4 <= diff <= 1.0:
                msg += f"\n朝→夜：+{diff:.1f}kg（理想範囲✓）"
            elif diff < 0.4:
                msg += f"\n朝→夜：+{diff:.1f}kg（少なめです）"
            else:
                msg += f"\n朝→夜：+{diff:.1f}kg（多めです）"
        return msg

    # 水分
    m = re.search(r'水.{0,5}?(\d+)\s*(ml|cc|杯|リットル|L)', text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit == '杯': amount = amount * 200
        elif unit in ['リットル', 'L']: amount = amount * 1000
        update_today_record(user_id, water_ml=amount)
        msg = f"水分 {amount}ml を記録しました✓"
        if amount < 1500:
            msg += "\nもう少し水分を増やしましょう（目標1500〜2000ml）"
        elif amount >= 2000:
            msg += "\nしっかり水分が摂れています！"
        return msg

    # 便
    if re.search(r'便(あり|あった|出た|○|◯)', text):
        update_today_record(user_id, bowel=1)
        return "排便あり を記録しました✓"
    if re.search(r'便(なし|なかった|出ない|×)', text):
        update_today_record(user_id, bowel=0)
        return "排便なし を記録しました"

    # 生理
    if user.get('gender') == '女性':
        if re.search(r'生理(あり|中|きた|始まった|○|◯)', text):
            update_today_record(user_id, menstruation=1)
            return "生理あり を記録しました✓\n生理中は体重が増えやすい時期ですが、気にしすぎず継続しましょう！"
        if re.search(r'生理(なし|終わった|×)', text):
            update_today_record(user_id, menstruation=0)
            return "生理なし を記録しました✓"

    # 睡眠
    m = re.search(r'睡眠.{0,5}?(\d+\.?\d*)\s*(時間|h)', text)
    if m:
        hours = float(m.group(1))
        update_today_record(user_id, sleep_hours=hours)
        msg = f"睡眠 {hours}時間 を記録しました✓"
        if hours < 6:
            msg += "\n睡眠が少し短めです。睡眠不足はダイエットの大敵なので、休める日は早めに休みましょう💤"
        elif hours >= 7:
            msg += "\nしっかり眠れていますね！睡眠はダイエットの土台です😊"
        return msg

    # 気持ち・今日の一言
    if re.search(r'^(気持ち|今日の気持ち|メモ|一言|気分)[：: 　]', text):
        mood = re.sub(r'^(気持ち|今日の気持ち|メモ|一言|気分)[：: 　]*', '', text).strip()
        update_today_record(user_id, mood=mood)
        # Claudeに共感返答させる
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        res = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="あなたは優しいダイエットコーチです。ユーザーの気持ちに寄り添い、否定せず、共感と励ましの言葉を100文字以内で伝えてください。どんな気持ちも受け止め、正直に話してくれたことに感謝を示してください。",
            messages=[{"role": "user", "content": f"今日の気持ち：{mood}"}]
        )
        return f"気持ちを記録しました✓\n\n{res.content[0].text}"

    # 食事
    meal_map = {'朝': '朝食', '昼': '昼食', '夜': '夕食', '夕': '夕食', '間食': '間食', 'おやつ': '間食', 'スナック': '間食'}
    for key, label in meal_map.items():
        if text.startswith(key):
            content = re.sub(r'^' + key + r'[ごはんめし食：: 　]*', '', text).strip()
            if content:
                add_meal(user_id, label, content)
                return f"{label}「{content}」を記録しました✓"

    # 今日の記録確認
    if text in ['今日の記録', '記録確認', '確認']:
        return format_today_summary(user_id)

    # 自分のID確認
    if text in ['マイID', 'myid', '自分のID', 'ID確認']:
        return f"あなたのUser IDは：\n{user_id}"

    # ヘルプ
    if text in ['ヘルプ', 'help', '使い方']:
        return get_help_message(user)

    # 通常の質問はClaudeが回答
    return ask_claude(text)

def handle_setup(user_id, text, user):
    step = user['setup_step'] if user else 'name'
    if step == 'name':
        upsert_user(user_id, name=text, setup_step='gender')
        return f"{text}さん、はじめまして！\n性別を教えてください。\n「男性」または「女性」と送ってください。"
    elif step == 'gender':
        if text in ['男性', '女性']:
            upsert_user(user_id, gender=text, setup_step='target')
            return "目標体重を教えてください。\n例：「55」や「55kg」"
        return "「男性」または「女性」と送ってください。"
    elif step == 'target':
        m = re.search(r'(\d+\.?\d*)', text)
        if m:
            upsert_user(user_id, target_weight=float(m.group(1)), setup_step='notify')
            return "毎日のサポートメッセージを送る時間を教えてください。\n例：「21時」「22:30」「9時」"
        return "数字で入力してください。例：「55」"
    elif step == 'notify':
        m = re.search(r'(\d{1,2})[時：:](\d{0,2})', text)
        if m:
            h, mi = int(m.group(1)), int(m.group(2)) if m.group(2) else 0
            upsert_user(user_id, notify_hour=h, notify_minute=mi, setup_step='done')
            reschedule_notifications()
            u = get_user(user_id)
            gender_note = "（生理の記録もできます）" if u['gender'] == '女性' else ""
            return f"""設定完了です！{u['name']}さん、一緒に頑張りましょう✨

【記録できること】
・朝体重 65.5
・夜体重 64.8
・水 1500ml
・便あり／便なし
{'・生理あり／生理なし' + gender_note if u['gender'] == '女性' else ''}
・朝ごはん 内容
・昼ごはん 内容
・夜ごはん 内容

毎日{h:02d}:{mi:02d}にサポートメッセージをお送りします！
「ヘルプ」と送ると使い方を確認できます。"""
        return "時間を入力してください。例：「21時」「22:30」"
    else:
        upsert_user(user_id, setup_step='name')
        return "はじめまして！\nまずお名前を教えてください。"

def format_today_summary(user_id):
    user = get_user(user_id)
    today = get_today_record(user_id)
    meals = get_today_meals(user_id)
    weight_msgs = analyze_weight(user_id)

    lines = [f"📋 {date.today().strftime('%m/%d')}の記録"]
    lines.append(f"朝体重：{today.get('morning_weight','未記録')}kg")
    lines.append(f"夜体重：{today.get('evening_weight','未記録')}kg")
    if weight_msgs:
        lines.extend(weight_msgs)
    lines.append(f"水分：{today.get('water_ml','未記録')}ml")
    lines.append(f"排便：{'あり✓' if today.get('bowel') else '未記録/なし'}")
    lines.append(f"睡眠：{str(today.get('sleep_hours','未記録')) + '時間' if today.get('sleep_hours') else '未記録'}")
    if user.get('gender') == '女性':
        lines.append(f"生理：{'あり' if today.get('menstruation') else '未記録/なし'}")
    if today.get('mood'):
        lines.append(f"気持ち：{today.get('mood')}")
    if meals:
        lines.append("\n【食事】")
        for meal_type, content in meals:
            lines.append(f"{meal_type}：{content}")
    if user.get('target_weight') and today.get('morning_weight'):
        diff = today['morning_weight'] - user['target_weight']
        lines.append(f"\n目標まであと{diff:.1f}kg")
    return '\n'.join(lines)

def get_help_message(user):
    gender = user.get('gender', '') if user else ''
    msg = """【記録の送り方】
朝体重 65.5
夜体重 64.8
水 1500ml
便あり／便なし
朝ごはん 内容
昼ごはん 内容
夜ごはん 内容
間食 内容"""
    if gender == '女性':
        msg += "\n生理あり／生理なし"
    msg += """

【その他】
「今日の記録」→記録確認
「通知 21時」→通知時刻変更
「設定変更」→プロフィール変更
ダイエットの質問もOKです！"""
    return msg

def ask_claude(text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system = f"""あなたはオンラインダイエットプログラムの専門アドバイザーです。
食事・栄養・生活習慣の改善でサポートします。施術・治療行為には触れないでください。
回答は200文字以内で簡潔に。

【知識ベース】
{KNOWLEDGE[:30000]}

【分子栄養学】
{MOLECULAR_KNOWLEDGE[:5000]}"""
    res = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": text}]
    )
    return res.content[0].text

# ========== 日次サポートメッセージ ==========
def send_daily_support(user_id):
    user = get_user(user_id)
    today = get_today_record(user_id)
    meals = get_today_meals(user_id)
    weight_msgs = analyze_weight(user_id)

    meal_text = '\n'.join([f"{t}：{c}" for t, c in meals]) if meals else "記録なし"
    weight_text = '\n'.join(weight_msgs) if weight_msgs else "体重データなし"

    # 未記録項目チェック
    missing = []
    if not today.get('morning_weight'): missing.append('朝体重')
    if not today.get('evening_weight'): missing.append('夜体重')
    if not today.get('water_ml'): missing.append('水分量')
    if not today.get('bowel') and today.get('bowel') != 0: missing.append('排便')
    if not meals: missing.append('食事')
    if not today.get('sleep_hours'): missing.append('睡眠時間')
    if not today.get('mood'): missing.append('今日の気持ち')
    missing_text = '・'.join(missing) if missing else 'なし（すべて記録済み✓）'

    prompt = f"""
以下は{user['name']}さん（{user.get('gender','不明')}・目標{user.get('target_weight','不明')}kg）の本日の記録です。

【体重分析】
{weight_text}
朝体重：{today.get('morning_weight','未記録')}kg
夜体重：{today.get('evening_weight','未記録')}kg

【水分】{today.get('water_ml','未記録')}ml
【排便】{'あり' if today.get('bowel') else 'なし/未記録'}
【睡眠】{today.get('sleep_hours','未記録')}時間
{'【生理】あり' if today.get('menstruation') else ''}
【今日の気持ち】{today.get('mood') or '未記録'}

【今日の食事】
{meal_text}

【未記録の項目】{missing_text}

この記録をもとに以下の内容で温かいサポートメッセージを350文字以内で送ってください。
・今日の記録への共感・ねぎらい
・体重・食事・水分への具体的アドバイス
・気持ちへの寄り添い（否定は絶対にしない）
・未記録項目があればやんわりと「よかったら明日も記録してみてね」程度に促す
・明日への励まし
個人差があることも踏まえ、押しつけにならない優しい表現で。
"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system = f"""あなたはオンラインダイエットの専門コーチです。
施術・治療には触れず、食事・栄養・生活習慣のアドバイスをしてください。
【知識ベース】{KNOWLEDGE[:20000]}
【分子栄養学】{MOLECULAR_KNOWLEDGE[:5000]}"""

    res = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    message = f"🌙 {date.today().strftime('%m/%d')}のサポート\n\n" + res.content[0].text

    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(PushMessageRequest(
            to=user_id,
            messages=[TextMessage(text=message)]
        ))

# ========== スケジューラー ==========
scheduler = BackgroundScheduler(timezone='Asia/Tokyo')

def reschedule_notifications():
    for job in scheduler.get_jobs():
        if job.id.startswith('notify_'):
            scheduler.remove_job(job.id)
    users = get_all_users()
    for user in users:
        h = user.get('notify_hour', 21)
        mi = user.get('notify_minute', 0)
        uid = user['user_id']
        scheduler.add_job(
            send_daily_support,
            'cron',
            hour=h,
            minute=mi,
            id=f'notify_{uid}',
            args=[uid],
            replace_existing=True
        )
    print(f"通知スケジュール更新: {len(users)}名")

# ========== Flask ==========
app = Flask(__name__)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text

    # 受信したIDを常にファイルに記録
    with open('line_users.txt', 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now().isoformat()} | {user_id} | {text}\n")

    user = get_user(user_id)
    if not user:
        upsert_user(user_id, setup_step='name')

    reply = parse_message(user_id, text)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )

if __name__ == "__main__":
    init_db()
    reschedule_notifications()
    scheduler.start()
    print("ダイエットBot起動中... ポート5000")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
