import os
import re
import time
import threading
import queue
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
import tkinter as tk
from tkinter import ttk, messagebox

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

APP_NAME = 'LiveGuarder1026'
VERSION = '4.1-dev'
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']
WARNING_THRESHOLD = 6
LOG_RETENTION_DAYS = 3
ENABLE_YOUTUBE_ACTION = False

ROOT_DIR = APP_NAME
COMMENT_LOG_DIR = os.path.join(ROOT_DIR, 'コメント記録')
WARNING_LOG_DIR = os.path.join(ROOT_DIR, '警告記録')
TIMEOUT_LOG_DIR = os.path.join(ROOT_DIR, 'オートタイムアウト記録')
for d in (COMMENT_LOG_DIR, WARNING_LOG_DIR, TIMEOUT_LOG_DIR):
    os.makedirs(d, exist_ok=True)


def cleanup_old_logs():
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for folder in (COMMENT_LOG_DIR, WARNING_LOG_DIR):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass




def delete_all_records():
    """Delete all saved moderation logs while keeping the folders.

    This does not touch token.json, client_secret.json, settings, or
    other authentication files.
    """
    deleted = 0
    errors = []
    for folder in (COMMENT_LOG_DIR, WARNING_LOG_DIR, TIMEOUT_LOG_DIR):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                deleted += 1
            except OSError as e:
                errors.append(f"{name}: {e}")
    return deleted, errors

def append_log(folder, entry):
    name = datetime.now().strftime('%Y-%m-%d') + '.txt'
    path = os.path.join(folder, name)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(entry + '\n')


def save_comment_record(username, channel_id, comment, result):
    append_log(COMMENT_LOG_DIR, (
        f"[{datetime.now().strftime('%H:%M:%S')}] {username} | {channel_id}\n"
        f"コメント: {comment}\n"
        f"スコア: {result['total_score']}\n"
        f"判定: {result['judgment']}\n"
        f"AI使用: {result['ai_used']}\n"
        + '-' * 60
    ))


def save_warning_record(username, channel_id, comment, result):
    append_log(WARNING_LOG_DIR, (
        f"[{datetime.now().strftime('%H:%M:%S')}] {username} | {channel_id}\n"
        f"コメント: {comment}\n"
        f"スコア: {result['total_score']}\n"
        f"判定: {result['judgment']}\n"
        + '-' * 60
    ))


def save_timeout_record(username, channel_id, comment, result):
    append_log(TIMEOUT_LOG_DIR, (
        f"[{datetime.now().strftime('%H:%M:%S')}] {username} | {channel_id}\n"
        f"コメント: {comment}\n"
        f"スコア: {result['total_score']}\n"
        f"判定: {result['judgment']}\n"
        + '-' * 60
    ))


def get_video_id(url):
    url = (url or '').strip()
    if not url:
        return None
    p = urlparse(url)
    host = (p.hostname or '').lower()
    if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}:
        q = parse_qs(p.query)
        if q.get('v'):
            return q['v'][0]
        parts = [x for x in p.path.split('/') if x]
        if len(parts) >= 2 and parts[0] == 'live':
            return parts[1]
    if host == 'youtu.be':
        return p.path.strip('/') or None
    return None


def get_youtube():
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError('token.json が見つかりません。')
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())
    return build('youtube', 'v3', credentials=creds)


def get_live_info(youtube, video_id):
    response = youtube.videos().list(
        part='snippet,liveStreamingDetails', id=video_id
    ).execute()
    items = response.get('items', [])
    if not items:
        raise ValueError('指定された動画が見つかりません。')
    item = items[0]
    chat_id = item.get('liveStreamingDetails', {}).get('activeLiveChatId')
    if not chat_id:
        raise ValueError('現在取得可能なライブチャットがありません。')
    return item.get('snippet', {}).get('title', '不明'), chat_id


def get_channel_info(youtube, channel_id):
    response = youtube.channels().list(part='snippet', id=channel_id).execute()
    items = response.get('items', [])
    if not items:
        return None
    snippet = items[0].get('snippet', {})
    published_at = snippet.get('publishedAt')
    age_hours = 999999
    if published_at:
        created = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    return {'channel_name': snippet.get('title', '不明'), 'account_age_hours': age_hours}


def first_stage_check(text, account_age_hours, is_first_observed):
    text = text.strip()
    signals = []
    if is_first_observed and account_age_hours <= 24:
        signals.append('new_account_first_comment')
    pigeon_patterns = [
        r'あっちの配信.*行って', r'あっちの配信.*見て',
        r'向こうの配信.*行って', r'向こうの配信.*見て',
        r'別の配信.*行って', r'別の配信.*見て',
        r'みんな.*あっち.*行', r'みんな.*向こう.*行',
        r'みんな.*あっち.*見', r'みんな.*向こう.*見'
    ]
    if any(re.search(p, text, re.IGNORECASE) for p in pigeon_patterns):
        signals.append('possible_pigeon')
    semi_patterns = [
        r'あっちの配信では今', r'向こうの配信では今',
        r'別の配信では今', r'あっちのライブでは今',
        r'向こうのライブでは今', r'別のライブでは今'
    ]
    if any(re.search(p, text, re.IGNORECASE) for p in semi_patterns):
        signals.append('possible_semi_pigeon')
    ad_patterns = [
        r'買ってみて', r'登録して', r'フォローして', r'チャンネル登録して',
        r'販売中', r'絶賛発売', r'格安', r'無料配布', r'セール中', r'こちらからどうぞ'
    ]
    if any(re.search(p, text, re.IGNORECASE) for p in ad_patterns):
        signals.append('possible_advertising')
    other_live = ['あっちの配信', '向こうの配信', '別の配信', 'あっちのライブ', '向こうのライブ', '別のライブ']
    if any(w in text for w in other_live):
        signals.append('other_live_reference')
    ai_related = [s for s in signals if s != 'new_account_first_comment']
    return {'signals': signals, 'need_ai': bool(ai_related)}


def rule_fallback_score(stage1):
    s = 0
    sig = stage1['signals']
    if 'new_account_first_comment' in sig:
        s += 2
    if 'possible_pigeon' in sig:
        s += 3
    elif 'possible_semi_pigeon' in sig:
        s += 2
    if 'possible_advertising' in sig:
        s += 3
    return s


def calculate_account_score(age, first):
    return 2 if first and age <= 24 else 0


def calculate_ai_score(ai):
    s = 0
    if ai.get('advertising'): s += 3
    if ai.get('context'): s += 3
    if ai.get('similarity'): s += 3
    if ai.get('pigeon'): s += 3
    elif ai.get('semi_pigeon'): s += 2
    return s


def judge_score(score):
    if score >= WARNING_THRESHOLD: return '荒らし'
    if score >= 3: return '警告候補'
    return '通常'


def analyze_comment_with_ai(comment_text, past_problem_comments=None):
    return {
        'available': False, 'advertising': False, 'context': False,
        'similarity': False, 'semi_pigeon': False, 'pigeon': False,
        'reason': 'AIサービス未接続'
    }


def run_two_stage_judgement(comment_text, account_age_hours, is_first_observed, ai_enabled):
    stage1 = first_stage_check(comment_text, account_age_hours, is_first_observed)
    if not stage1['need_ai']:
        score = calculate_account_score(account_age_hours, is_first_observed)
        return {'stage1': stage1, 'ai_used': False, 'total_score': score,
                'judgment': judge_score(score), 'ai_result': None}
    if not ai_enabled:
        score = rule_fallback_score(stage1)
        return {'stage1': stage1, 'ai_used': False, 'total_score': score,
                'judgment': judge_score(score), 'ai_result': None}
    ai = analyze_comment_with_ai(comment_text, [])
    if not ai.get('available', False):
        score = rule_fallback_score(stage1)
        return {'stage1': stage1, 'ai_used': False, 'total_score': score,
                'judgment': judge_score(score), 'ai_result': ai}
    score = calculate_account_score(account_age_hours, is_first_observed) + calculate_ai_score(ai)
    return {'stage1': stage1, 'ai_used': True, 'total_score': score,
            'judgment': judge_score(score), 'ai_result': ai}


class LiveGuarderApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f'{APP_NAME} v{VERSION}')
        self.root.geometry('1200x760')
        self.root.minsize(720, 560)
        self.youtube = None
        self.live_chat_id = None
        self.monitoring = False
        self.stop_event = threading.Event()
        self.mode = 'WARNING'
        self.ai_enabled = True
        self.seen_ids = set()
        self.user_history = {}
        self.processed_users = set()
        self.ui_queue = queue.Queue()
        self.comment_buffer = []
        self.warning_buffer = []
        self.stats = {'comments': 0, 'alerts': 0, 'warnings': 0, 'timeouts': 0}
        cleanup_old_logs()
        self.build_ui()
        self.root.after(200, self.flush_ui)

    def build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass

        self.root.option_add('*Font', ('Yu Gothic UI', 10))

        title = ttk.Label(
            self.root,
            text=f'🛡 {APP_NAME}',
            font=('Yu Gothic UI', 20, 'bold')
        )
        title.pack(anchor='w', padx=18, pady=(14, 2))

        subtitle = ttk.Label(
            self.root,
            text='YouTube Live Moderation',
            font=('Yu Gothic UI', 10)
        )
        subtitle.pack(anchor='w', padx=20, pady=(0, 10))

        # Top bar
        top = ttk.Frame(self.root)
        top.pack(fill='x', padx=16, pady=(0, 8))

        self.layout_var = tk.BooleanVar(value=False)
        self.layout_toggle_top = ttk.Button(
            top,
            text='◀ 左だけ',
            command=self.toggle_left_only,
            width=14
        )
        self.layout_toggle_top.pack(side='right', padx=(0, 4))

        # Main layout: left = comments + warnings, right = controls
        self.main = ttk.Frame(self.root)
        self.main.pack(fill='both', expand=True, padx=16, pady=(0, 12))
        self.main.columnconfigure(0, weight=3)
        self.main.columnconfigure(1, weight=2)
        self.main.rowconfigure(0, weight=1)

        self.left_panel = ttk.Frame(self.main)
        self.left_panel.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.left_panel.rowconfigure(0, weight=3)
        self.left_panel.rowconfigure(1, weight=1)
        self.left_panel.columnconfigure(0, weight=1)

        self.right_panel = ttk.Frame(self.main)
        self.right_panel.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        self.right_panel.rowconfigure(0, weight=0)
        self.right_panel.rowconfigure(1, weight=0)
        self.right_panel.rowconfigure(2, weight=1)
        self.right_panel.columnconfigure(0, weight=1)

        # Comments: approximately 2 parts
        cf = ttk.LabelFrame(self.left_panel, text='💬 コメント')
        cf.grid(row=0, column=0, sticky='nsew', pady=(0, 8))
        cf.rowconfigure(0, weight=1)
        cf.columnconfigure(0, weight=1)

        self.comment_text = tk.Text(
            cf,
            wrap='word',
            state='disabled',
            font=('Yu Gothic UI', 10),
            undo=False
        )
        self.comment_text.grid(row=0, column=0, sticky='nsew')
        comment_scroll = ttk.Scrollbar(
            cf,
            orient='vertical',
            command=self.comment_text.yview
        )
        comment_scroll.grid(row=0, column=1, sticky='ns')
        self.comment_text.configure(yscrollcommand=comment_scroll.set)

        # Warnings: approximately 1 part
        wf = ttk.LabelFrame(self.left_panel, text='⚠️ 警告・検出')
        wf.grid(row=1, column=0, sticky='nsew', pady=(0, 0))
        wf.rowconfigure(0, weight=1)
        wf.columnconfigure(0, weight=1)

        self.warning_text = tk.Text(
            wf,
            wrap='word',
            state='disabled',
            font=('Yu Gothic UI', 10),
            undo=False
        )
        self.warning_text.grid(row=0, column=0, sticky='nsew')
        warning_scroll = ttk.Scrollbar(
            wf,
            orient='vertical',
            command=self.warning_text.yview
        )
        warning_scroll.grid(row=0, column=1, sticky='ns')
        self.warning_text.configure(yscrollcommand=warning_scroll.set)

        # Right controls
        settings = ttk.LabelFrame(self.right_panel, text='⚙ 設定')
        settings.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(2, weight=1)

        ttk.Label(settings, text='YouTubeライブURL').grid(
            row=0, column=0, columnspan=3,
            sticky='w', padx=8, pady=(8, 4)
        )
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(settings, textvariable=self.url_var)
        self.url_entry.grid(
            row=1, column=0, columnspan=3,
            sticky='ew', padx=8, pady=(0, 8)
        )

        self.connect_btn = ttk.Button(
            settings,
            text='🔐 YouTubeと接続',
            command=self.connect_youtube
        )
        self.connect_btn.grid(row=2, column=0, sticky='ew', padx=8, pady=6)

        self.check_btn = ttk.Button(
            settings,
            text='🔎 ライブを確認',
            command=self.check_live
        )
        self.check_btn.grid(row=2, column=1, columnspan=2, sticky='ew', padx=8, pady=6)

        self.connection_status = ttk.Label(
            settings,
            text='🔴 YouTube：未接続'
        )
        self.connection_status.grid(
            row=3, column=0, columnspan=3,
            sticky='w', padx=8, pady=3
        )

        self.live_status = ttk.Label(
            settings,
            text='⚪ ライブ：未確認'
        )
        self.live_status.grid(
            row=4, column=0, columnspan=3,
            sticky='w', padx=8, pady=(3, 8)
        )

        opts = ttk.LabelFrame(self.right_panel, text='🤖 動作設定')
        opts.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        opts.columnconfigure(1, weight=1)
        opts.columnconfigure(2, weight=1)

        ttk.Label(opts, text='モード').grid(
            row=0, column=0, sticky='w', padx=8, pady=8
        )
        self.mode_var = tk.StringVar(value='警告のみ')
        ttk.Radiobutton(
            opts, text='警告のみ', value='警告のみ',
            variable=self.mode_var, command=self.mode_changed
        ).grid(row=0, column=1, sticky='w', padx=4, pady=8)
        ttk.Radiobutton(
            opts, text='自動タイムアウト', value='自動タイムアウト',
            variable=self.mode_var, command=self.mode_changed
        ).grid(row=0, column=2, sticky='w', padx=4, pady=8)

        ttk.Label(opts, text='AI判定').grid(
            row=1, column=0, sticky='w', padx=8, pady=8
        )
        self.ai_var = tk.StringVar(value='ON')
        ttk.Radiobutton(
            opts, text='ON', value='ON',
            variable=self.ai_var, command=self.ai_changed
        ).grid(row=1, column=1, sticky='w', padx=4, pady=8)
        ttk.Radiobutton(
            opts, text='OFF', value='OFF',
            variable=self.ai_var, command=self.ai_changed
        ).grid(row=1, column=2, sticky='w', padx=4, pady=8)

        self.ai_status = ttk.Label(
            opts,
            text='🤖 AI：🟡 未接続'
        )
        self.ai_status.grid(
            row=2, column=0, columnspan=3,
            sticky='w', padx=8, pady=(0, 8)
        )

        controls = ttk.Frame(opts)
        controls.grid(row=3, column=0, columnspan=3, sticky='ew', padx=8, pady=(0, 8))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        ttk.Button(
            controls,
            text='▶ 監視開始',
            command=self.start_monitoring
        ).grid(row=0, column=0, sticky='ew', padx=(0, 4))

        ttk.Button(
            controls,
            text='🛑 監視停止',
            command=self.stop_monitoring
        ).grid(row=0, column=1, sticky='ew', padx=(4, 0))

        # Stats on right bottom
        sf = ttk.LabelFrame(self.right_panel, text='📊 今日の状況')
        sf.grid(row=2, column=0, sticky='nsew')
        sf.columnconfigure(0, weight=1)

        self.stats_label = ttk.Label(
            sf,
            text=self.stats_text(),
            justify='left'
        )
        self.stats_label.grid(
            row=0, column=0,
            sticky='nw', padx=10, pady=10
        )

        self.delete_records_btn = ttk.Button(
            sf,
            text='🗑 記録を全削除',
            command=self.delete_all_records_confirm
        )
        self.delete_records_btn.grid(
            row=1, column=0,
            sticky='ew', padx=10, pady=(0, 10)
        )

        self.layout_toggle_btn = ttk.Button(
            sf,
            text='◀ 左側だけ表示',
            command=self.toggle_left_only,
            width=14
        )
        self.layout_toggle_btn.grid(
            row=2, column=0,
            sticky='ew', padx=10, pady=(0, 10)
        )

        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    def toggle_left_only(self):
        """Toggle compact left-only monitoring view."""
        compact = getattr(self, "_left_only", False)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        if not compact:
            # Save current normal window size before entering compact mode.
            self._full_geometry = self.root.geometry().split("+")[0]
            self._left_only = True

            # Hide the entire right-side settings/statistics panel.
            self.right_panel.grid_remove()
            self.main.columnconfigure(0, weight=1)
            self.main.columnconfigure(1, weight=0)

            # About 1/3 of the screen width and 1/2 of the screen height.
            compact_w = max(420, screen_w // 3)
            compact_h = max(420, screen_h // 2)
            self.root.minsize(compact_w, compact_h)
            self.root.geometry(f"{compact_w}x{compact_h}")

            self.layout_toggle_top.config(text="▶ 全画面に戻す")
            self.layout_toggle_btn.config(text="▶ 全画面に戻す")
        else:
            self._left_only = False

            # Restore the full layout and normal minimum size.
            self.main.columnconfigure(0, weight=3)
            self.main.columnconfigure(1, weight=2)
            self.right_panel.grid()
            self.root.minsize(950, 620)

            geometry = getattr(self, "_full_geometry", "1200x760")
            self.root.geometry(geometry)

            self.layout_toggle_top.config(text="◀ 左だけ")
            self.layout_toggle_btn.config(text="◀ 左だけ")

        self.root.update_idletasks()

    def append_warning_at_top(self, text):
        """Insert latest warning at the top without changing older warning order."""
        self.warning_text.config(state='normal')
        self.warning_text.insert('1.0', text + '\n' + '-' * 45 + '\n\n')
        self.warning_text.config(state='disabled')
        self.warning_text.see('1.0')

    def delete_all_records_confirm(self):
        if self.monitoring:
            messagebox.showwarning(
                '監視中',
                '監視中は記録の全削除を実行できません。\n先に監視を停止してください。'
            )
            return

        answer = messagebox.askyesno(
            '記録を全削除',
            'コメント・警告・オートタイムアウトの記録を\n'
            'すべて削除します。\n\n'
            'この操作は元に戻せません。実行しますか？'
        )
        if not answer:
            return

        deleted, errors = delete_all_records()

        # 画面上の記録もリセット
        self.comment_buffer.clear()
        self.warning_buffer.clear()
        self.user_history.clear()
        self.processed_users.clear()
        self.clear_views()
        self.stats = {'comments': 0, 'alerts': 0, 'warnings': 0, 'timeouts': 0}
        self.update_stats()

        if errors:
            messagebox.showwarning(
                '一部削除完了',
                f'{deleted}件の記録を削除しました。\n\n'
                '一部のファイルを削除できませんでした。'
            )
        else:
            messagebox.showinfo(
                '削除完了',
                f'{deleted}件の記録をすべて削除しました。'
            )

    def mode_changed(self):
        if self.mode_var.get() == '自動タイムアウト':
            if messagebox.askyesno('自動タイムアウト確認','自動タイムアウトを選択しました。\n現在は実際のYouTube操作は無効です。\n\nこの設定を有効にしますか？'):
                self.mode = 'AUTO_TIMEOUT'
            else:
                self.mode_var.set('警告のみ'); self.mode = 'WARNING'
        else:
            self.mode = 'WARNING'

    def ai_changed(self):
        self.ai_enabled = self.ai_var.get() == 'ON'
        self.update_ai_status()

    def update_ai_status(self):
        self.ai_status.config(text='🤖 AI：⚪ OFF' if not self.ai_enabled else '🤖 AI：🟡 未接続')

    def connect_youtube(self):
        try:
            self.youtube = get_youtube()
            items = self.youtube.channels().list(part='snippet',mine=True).execute().get('items',[])
            if not items: raise RuntimeError('YouTubeチャンネル情報を取得できませんでした。')
            name = items[0]['snippet'].get('title','不明')
            self.connection_status.config(text=f'🟢 YouTube：接続済み（{name}）')
        except Exception as e:
            self.youtube = None; self.connection_status.config(text='🔴 YouTube：接続失敗'); messagebox.showerror('YouTube接続エラー',str(e))

    def check_live(self):
        if self.youtube is None:
            messagebox.showwarning('未接続','先に「YouTubeと接続」を押してください。'); return
        vid = get_video_id(self.url_var.get())
        if not vid:
            messagebox.showwarning('URLエラー','YouTubeライブURLを認識できません。'); return
        try:
            title, chat = get_live_info(self.youtube, vid); self.live_chat_id = chat
            self.live_status.config(text=f'🟢 ライブ：確認済み | {title}')
        except Exception as e:
            self.live_chat_id = None; self.live_status.config(text='🔴 ライブ：確認失敗'); messagebox.showerror('ライブ確認エラー',str(e))

    def start_monitoring(self):
        if self.monitoring: return
        if self.youtube is None:
            messagebox.showwarning('未接続','先にYouTubeと接続してください。'); return
        if not self.live_chat_id:
            self.check_live()
            if not self.live_chat_id: return
        self.monitoring = True; self.stop_event.clear(); self.seen_ids.clear(); self.user_history.clear(); self.processed_users.clear()
        self.comment_buffer.clear(); self.warning_buffer.clear()
        self.stats = {'comments':0,'alerts':0,'warnings':0,'timeouts':0}
        self.clear_views(); self.update_stats()
        threading.Thread(target=self.monitor_worker, daemon=True).start()
        self.enqueue_warning('監視を開始しました。','success')

    def stop_monitoring(self):
        self.stop_event.set(); self.monitoring = False; self.enqueue_warning('監視を停止しました。','info')

    def monitor_worker(self):
        token = None
        while self.monitoring and not self.stop_event.is_set():
            try:
                response = self.youtube.liveChatMessages().list(liveChatId=self.live_chat_id,part='id,snippet,authorDetails',maxResults=200,pageToken=token).execute()
                for item in response.get('items',[]):
                    mid=item.get('id')
                    if not mid or mid in self.seen_ids: continue
                    self.seen_ids.add(mid); self.process_comment(item)
                token = response.get('nextPageToken')
                if response.get('offlineAt'):
                    self.enqueue_warning('ライブ配信が終了しました。','success'); break
                time.sleep(max(response.get('pollingIntervalMillis',5000)/1000,1))
            except Exception as e:
                self.enqueue_warning(f'監視エラー：{type(e).__name__}: {e}','danger'); time.sleep(5)
        self.monitoring=False

    def process_comment(self,item):
        author=item.get('authorDetails',{}); snip=item.get('snippet',{})
        username=author.get('displayName','不明'); cid=author.get('channelId'); comment=snip.get('displayMessage','')
        if not cid or not comment: return
        age=999999
        try:
            info=get_channel_info(self.youtube,cid)
            if info: age=info['account_age_hours']
        except Exception: pass
        first=cid not in self.user_history
        result=run_two_stage_judgement(comment,age,first,self.ai_enabled)
        self.user_history.setdefault(cid,[]).append({'comment':comment,'score':result['total_score']})
        self.stats['comments']+=1
        if result['total_score']>=WARNING_THRESHOLD: self.stats['alerts']+=1
        save_comment_record(username,cid,comment,result)
        self.enqueue_comment(username,comment,result['total_score'],result['judgment'],result['ai_used'])
        if result['total_score']>=WARNING_THRESHOLD and cid not in self.processed_users:
            self.processed_users.add(cid); self.enqueue_warning(f'⚠️ {username}\nスコア：{result["total_score"]}点\nコメント：{comment}','danger')
            if self.mode=='WARNING':
                self.stats['warnings']+=1; save_warning_record(username,cid,comment,result)
            elif self.mode=='AUTO_TIMEOUT':
                if ENABLE_YOUTUBE_ACTION:
                    # Deliberately kept disabled in this development build.
                    pass
                else:
                    save_timeout_record(username,cid,comment,result)

    def enqueue_comment(self,username,comment,score,judgment,ai_used):
        self.comment_buffer.append((username,comment,score,judgment,ai_used))

    def enqueue_warning(self,text,level='info'):
        self.warning_buffer.append((text,level))

    def flush_ui(self):
        while self.comment_buffer:
            u,c,s,j,a=self.comment_buffer.pop(0)
            ai='AI使用' if a else 'AI不使用'
            text=f'👤 {u}\n💬 {c}\n📊 {s}点 / {j} / {ai}\n'+'-'*45+'\n\n'
            self.comment_text.config(state='normal')
            self.comment_text.insert('end', text)
            self.comment_text.config(state='disabled')
            self.comment_text.see('end')

        while self.warning_buffer:
            text,level=self.warning_buffer.pop(0)
            self.append_warning_at_top(text)

        self.update_stats()
        self.root.after(200,self.flush_ui)

    def stats_text(self):
        return f"💬 コメント：{self.stats['comments']}\n⚠️ 注意報：{self.stats['alerts']}\n🟡 警告：{self.stats['warnings']}\n🔴 タイムアウト：{self.stats['timeouts']}"

    def update_stats(self): self.stats_label.config(text=self.stats_text())
    def clear_views(self):
        for w in (self.comment_text,self.warning_text):
            w.config(state='normal'); w.delete('1.0','end'); w.config(state='disabled')
        self.comment_text.see('end')
        self.warning_text.see('1.0')
    def on_close(self): self.stop_event.set(); self.monitoring=False; self.root.destroy()


if __name__ == '__main__':
    root=tk.Tk(); LiveGuarderApp(root); root.mainloop()
