import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
import google.generativeai as genai
import re
import pandas as pd
import urllib3
import ssl
import datetime
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- 1. 基本設定 ---
st.set_page_config(page_title="Web検品ディレクター Pro", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 2. ログイン機能 ---
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if st.session_state.password_correct:
        return True
    st.title("🔑 ログイン：Web検品ディレクター Pro")
    pwd = st.text_input("パスワードを入力してください", type="password")
    if st.button("ログイン"):
        if pwd == st.secrets.get("TOOL_PASSWORD"):
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("パスワードが正しくありません")
    return False

if not check_password():
    st.stop()

st.title("🔍 Web検品ディレクター Pro")
st.caption("Ver. 50.0 | 行番号表示 ＆ 構造的スペース判定モード")

INTERNAL_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# --- 3. ネットワーク設定 ---
def get_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    class SimpleSSLAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs['ssl_context'] = ctx
            return super().init_poolmanager(*args, **kwargs)
    adapter = SimpleSSLAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# --- 4. AIモデル設定 ---
@st.cache_resource
def load_ai_model(api_key):
    try:
        genai.configure(api_key=api_key)
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        target = next((m for m in ["models/gemini-1.5-pro", "models/gemini-1.5-flash"] if m in available_models), available_models[0] if available_models else None)
        return genai.GenerativeModel(target) if target else None
    except: return None

# --- 5. 個別ページ検品エンジン ---
def inspect_single_page(url, model, session, auth_info, reported_dead_assets, global_checked_assets):
    try:
        res = session.get(url, auth=auth_info, timeout=20, verify=False)
        res.encoding = res.apparent_encoding
        if res.status_code != 200:
            return {"url": url, "issue": f"⚠️ 読込失敗 ({res.status_code})"}

        raw_html = res.text
        soup = BeautifulSoup(raw_html, 'html.parser')
        effective_base = urljoin(res.url, soup.find('base', href=True)['href']) if soup.find('base', href=True) else res.url
        
        # 資産抽出（Meta/OGP含む）
        assets = set()
        for tag, attr in [('img','src'),('link','href'),('script','src')]:
            for item in soup.find_all(tag, **{attr: True}):
                assets.add(urljoin(effective_base, item[attr]))
        for meta in soup.find_all('meta', content=True):
            content = meta['content']
            if content.startswith(('http', '/', '.')) or any(ext in content.lower() for ext in ['.jpg','.png','.webp','.svg','.ico']):
                assets.add(urljoin(effective_base, content))

        # リンク切れチェック
        dead_list = []
        for a_url in assets:
            if a_url not in global_checked_assets:
                try:
                    with session.get(a_url, auth=auth_info, timeout=10, verify=False, stream=True) as a_res:
                        global_checked_assets[a_url] = a_res.status_code
                except: global_checked_assets[a_url] = 999
            if global_checked_assets[a_url] >= 400:
                if a_url not in reported_dead_assets:
                    dead_list.append(f"❌ リンク切れ({global_checked_assets[a_url]}): {a_url}")
                    reported_dead_assets.add(a_url)

        # --- AI解析用のソース加工（行番号付与） ---
        numbered_lines = []
        for i, line in enumerate(raw_html.splitlines()):
            numbered_lines.append(f"{i+1}: {line}")
            if i > 1500: break # 解析上限
        numbered_html = "\n".join(numbered_lines)

        # --- AIプロンプト（行番号・構造的スペース対応） ---
        prompt = f"""あなたは冷徹なWebデバッグ・プログラムです。URL: {url} のソースを行番号付きで解析し、不備を報告せよ。

        【デバッグ項目（必ず [L:行番号] を先頭に付けること）】
        1. 物理的な文字バグ:
           ・1文字の誤字（例：お引きたえ、綿密→念密 等）。
           ・文章内の不自然な空白（例：「発 生」）。
           ・助詞の重複や脱字（例：「〜をを」、「自分せい」）。
           ・環境依存文字。
        2. 表記の不統一: 同一ページ内での「お問い合わせ」と「お問合せ」の混在等。
        3. 物理的不整合: 電話番号不一致、datetime属性と表示の年の食い違い。
        4. コピペの痕跡: alt属性や本文に他社名や無関係なサービス名が残っている。

        【スペース判定の特別ルール】
        ・タイトルやパンくずリスト（例: XXX | XXX | XXX）の区切り文字前後のスペースは「意図的なデザイン」とみなし、スルーせよ。
        ・それ以外の、通常の文章内での不要なスペース（例: 文章の途中の全角スペース等）は厳しく指摘せよ。

        【禁止事項】
        ・主観的なアドバイスは一切不要。「問題ありません」等の報告も禁止。
        ・不備がある場合のみ、[L:行番号] を添えて簡潔に報告。
        ・不備がなければ『なし』とだけ回答。"""
        
        ai_issue = ""
        try:
            ai_res = model.generate_content(prompt + "\n\n行番号付きHTMLソース:\n" + numbered_html[:20000])
            ai_issue = re.sub(r'<[^>]+>', '', ai_res.text.strip())
            if any(ok in ai_issue for ok in ["なし", "問題ありません"]): ai_issue = ""
        except: ai_issue = "⚠️ AI解析エラー"

        final = []
        if dead_list: final.append("**物理エラー**\n" + "\n".join(dead_list))
        if ai_issue: final.append("**検品指摘**\n" + ai_issue)
        return {"url": url, "issue": "\n\n".join(final) if final else "✅ 問題なし"}
    except Exception as e:
        return {"url": url, "issue": f"⚠️ 解析エラー: {str(e)}"}

# --- 6. メインUI ---
st.sidebar.title("🛠 設定")
b_user = st.sidebar.text_input("Basic認証 ユーザー名")
b_pass = st.sidebar.text_input("Basic認証 パスワード", type="password")
uploaded_file = st.file_uploader("URLリスト (.xml または .txt)", type=["xml", "txt"])

if uploaded_file and INTERNAL_API_KEY:
    sitemap_stem = os.path.splitext(uploaded_file.name)[0]
    report_name = f"{sitemap_stem}_report_{datetime.date.today()}.html"
    model = load_ai_model(INTERNAL_API_KEY)
    if not model: st.error("AIモデル初期化失敗"); st.stop()

    content = uploaded_file.read().decode("utf-8")
    urls = [loc.text.strip().rstrip('/') for loc in BeautifulSoup(content, 'xml').find_all(re.compile(r'loc', re.I))] if uploaded_file.name.endswith(".xml") else [line.strip().rstrip('/') for line in content.splitlines()]
    unique_urls = list(dict.fromkeys([u for u in urls if u.startswith('http')]))

    if st.button(f"{len(unique_urls)} ページの検品を開始"):
        results = []
        reported_dead, checked_cache = set(), {}
        prog, status_box = st.progress(0), st.empty()
        with ThreadPoolExecutor(max_workers=5) as executor:
            tasks = {executor.submit(inspect_single_page, u, model, get_session(), (b_user, b_pass) if b_user else None, reported_dead, checked_cache): u for u in unique_urls}
            for i, future in enumerate(as_completed(tasks)):
                res_data = future.result()
                results.append(res_data); prog.progress((i + 1) / len(unique_urls)); status_box.text(f"完了: {i+1}/{len(unique_urls)} - {res_data['url']}")

        st.success("検品完了！")
        st.table(pd.DataFrame(results))
        html_rows = "".join([f"<tr><td style='font-size:12px;width:30%;'><a href='{r['url']}' target='_blank'>{r['url']}</a></td><td><span style='color:{'#e74c3c' if '✅' not in r['issue'] else '#333'};'>{r['issue'].replace('\n','<br>')}</span></td></tr>" for r in results])
        full_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{{font-family:sans-serif;padding:20px;background:#f4f7f9;}}table{{width:100%;border-collapse:collapse;background:#fff;}}th,td{{border:1px solid #eee;padding:12px;text-align:left;vertical-align:top;}}th{{background:#3498db;color:#fff;}}</style></head><body><h1>🔍 {sitemap_stem} レポート</h1><table><thead><tr><th>URL</th><th>指摘事項</th></tr></thead><tbody>{html_rows}</tbody></table></body></html>"
        st.download_button(f"📄 レポート保存", data=full_html, file_name=report_name, mime="text/html")
