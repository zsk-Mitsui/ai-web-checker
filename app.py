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
import html as html_lib
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
st.caption("Ver. 55.0 | 具体的引用モード ＆ 行番号表示 ＆ 日付・メタ資産監視")

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
        
        # 資産抽出
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

        # 行番号付与
        numbered_lines = [f"{i+1}: {line}" for i, line in enumerate(raw_html.splitlines())]
        numbered_html = "\n".join(numbered_lines[:2000])

        # --- AIプロンプト（Ver. 55.0 具体的引用徹底仕様） ---
        prompt = f"""あなたは冷徹なWebデバッグ・プログラムです。URL: {url} のソースを行番号付きで解析し、不備を報告せよ。

        【デバッグ項目と報告形式】
        指摘は必ず以下の形式を守り、問題の箇所をソースから直接「引用」して説明せよ：
        [L:行番号] カテゴリ名: 「引用テキスト」は〜という不備です。

        1. 物理的な文字バグ:
           ・誤字（例：お引きたえ、綿密→念密 等）。
           ・文章内の不自然な空白（例：「発 生」）。
           ・助詞の重複や脱字（例：「〜をを」、「自分せい」）。
           ・環境依存文字。
        2. 表記の不統一: 「お問い合わせ」と「お問合せ」の混在等。
        3. 物理的不整合:
           ・電話番号不一致。
           ・HTMLの `datetime` 属性と、画面表示日付の「年月日」の食い違い（未来の日付自体は許容）。
        4. コピペの痕跡: alt属性や本文の他社名残り。

        【スペース判定ルール】
        ・「 | 」や「 - 」で区切られたタイトル・パンくず内のスペースはデザイン意図としてスルー。
        ・住所（番地とビル名の間）のスペースは正常としてスルー。

        【禁止事項】
        ・アドバイス、正常報告（問題ありません等）、空の見出し出力は禁止。
        ・不備がなければ『なし』。"""
        
        ai_issue = ""
        try:
            ai_res = model.generate_content(prompt + "\n\n行番号付きHTMLソース:\n" + numbered_html[:25000])
            ai_issue = html_lib.escape(ai_res.text.strip())
            if any(ok in ai_issue for ok in ["なし", "問題ありません"]): ai_issue = ""
        except: ai_issue = "⚠️ AI解析エラー"

        final = []
        if dead_list: final.append("**物理エラー**\n" + "\n".join(dead_list))
        if ai_issue: final.append("**検品指摘**\n" + ai_issue)
        return {"url": url, "issue": "\n\n".join(final) if final else "✅ 問題なし"}
    except Exception as e:
        return {"url": url, "issue": f"⚠️ 解析エラー: {str(e)}"}

# --- 6. UI ---
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

    if st.button(f"{len(unique_urls)} ページの並列検品を開始"):
        results = []
        reported_dead, checked_cache = set(), {}
        prog, status_box = st.progress(0), st.empty()
        with ThreadPoolExecutor(max_workers=5) as executor:
            tasks = {executor.submit(inspect_single_page, u, model, get_session(), (b_user, b_pass) if b_user else None, reported_dead, checked_cache): u for u in unique_urls}
            for i, future in enumerate(as_completed(tasks)):
                res_data = future.result()
                results.append(res_data); prog.progress((i + 1) / len(unique_urls)); status_box.text(f"完了: {i+1}/{len(unique_urls)} - {res_data['url']}")

        st.success("検品完了！")
        
        html_rows = ""
        for r in results:
            color = "#e74c3c" if "✅" not in r['issue'] else "#333"
            safe_url = html_lib.escape(r['url'])
            # 改行を<br>に変換（指摘事項自体はinspect_single_page内でescape済み）
            formatted_issue = r['issue'].replace('\n', '<br>')
            html_rows += f"<tr><td style='font-size:12px;width:30%;padding:12px;border:1px solid #eee;'><a href='{safe_url}' target='_blank'>{safe_url}</a></td>"
            html_rows += f"<td style='padding:12px;border:1px solid #eee;'><span style='color:{color};white-space:pre-wrap;'>{formatted_issue}</span></td></tr>"
        
        full_html_table = f"""
        <div style="background:#fff; padding:20px; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,0.05); overflow-x: auto;">
            <table style="width:100%; border-collapse:collapse; font-family:sans-serif; min-width: 800px;">
                <thead style="background:#3498db; color:#fff;">
                    <tr><th style="padding:12px; text-align:left;">URL</th><th style="padding:12px; text-align:left;">指摘事項</th></tr>
                </thead>
                <tbody>{html_rows}</tbody>
            </table>
        </div>
        """
        st.write("### 🔍 検品結果プレビュー")
        st.write(full_html_table, unsafe_allow_html=True)
        
        download_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{{font-family:sans-serif;padding:20px;background:#f4f7f9;}}table{{width:100%;border-collapse:collapse;background:#fff;}}th,td{{border:1px solid #eee;padding:12px;text-align:left;vertical-align:top;}}th{{background:#3498db;color:#fff;}}</style></head><body><h1>🔍 {sitemap_stem} レポート</h1><table><thead><tr><th>URL</th><th>指摘事項</th></tr></thead><tbody>{html_rows}</tbody></table></body></html>"
        st.download_button(label=f"📄 {report_name} をダウンロード", data=download_html, file_name=report_name, mime="text/html")
