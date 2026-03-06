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
st.set_page_config(page_title="AI Web検品 Pro", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 2. ログイン機能 ---
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if st.session_state.password_correct:
        return True
    st.title("🔑 チーム専用：AI検品 Pro")
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
        if not target: return None, "利用可能なモデルがありません"
        model = genai.GenerativeModel(target)
        return model, None
    except Exception as e:
        return None, str(e)

# --- 5. 検品エンジン ---
def inspect_single_page(url, model, session, auth_info, reported_dead_assets, global_checked_assets):
    try:
        res = session.get(url, auth=auth_info, timeout=20, verify=False)
        res.encoding = res.apparent_encoding
        if res.status_code != 200:
            return {"url": url, "issue": f"⚠️ 読込失敗 ({res.status_code})"}

        soup = BeautifulSoup(res.text, 'html.parser')
        effective_base = urljoin(res.url, soup.find('base', href=True)['href']) if soup.find('base', href=True) else res.url
        
        # 物理アセット死活監視
        assets = set()
        for tag, attr in [('img','src'),('link','href'),('script','src')]:
            for item in soup.find_all(tag, **{attr: True}):
                assets.add(urljoin(effective_base, item[attr]))
        for meta in soup.find_all('meta', content=True):
            content = meta['content']
            if content.startswith(('http', '/', '.')) or any(ext in content.lower() for ext in ['.jpg','.png','.webp','.svg']):
                assets.add(urljoin(effective_base, content))

        dead_results = []
        for a_url in assets:
            if a_url not in global_checked_assets:
                try:
                    with session.get(a_url, auth=auth_info, timeout=10, verify=False, stream=True) as a_res:
                        global_checked_assets[a_url] = a_res.status_code
                except: global_checked_assets[a_url] = 999
            if global_checked_assets[a_url] >= 400:
                if a_url not in reported_dead_assets:
                    dead_results.append(f"❌ リンク切れ({global_checked_assets[a_url]}): {a_url}")
                    reported_dead_assets.add(a_url)

        # --- AIプロンプト（デバッグ特化） ---
        prompt = f"""あなたは「Webサイトの物理的欠陥」を探すデバッグ・プログラムです。URL: {url} のソースを解析し、以下の「機械的ミス」のみを報告せよ。

        【最優先の検知対象】
        1. 文字品質: 誤字（お引きたえ 等）、送り仮名、環境依存文字（～, ①, ㈱等）、文字化け。
        2. 不要な空白: 文中や文末に混入した不要な半角・全角スペース。
        3. 電話番号不整合: ページ内で電話番号の記述が食い違っている場合のみ報告。
        4. コピペ残骸: 他社名や無関係なサービス名の記述（現在のサイト内容と明らかに矛盾する場合）。

        【注意・禁止事項】
        ・「文章の表現」「信頼性」「根拠」などの主観的アドバイスは【一切不要】。
        ・メタ情報は、明らかに他社のコピペとわかる場合のみ指摘し、共通紹介文なら無視せよ。
        ・「問題ありません」「適切です」といった正常報告、見出しのみの出力は禁止。
        ・HTMLタグ（<h1>など）を回答に含めないこと。
        ・不備がなければ『なし』と回答。"""
        
        try:
            ai_res = model.generate_content(prompt + "\n\nHTMLソース:\n" + res.text[:15000])
            ai_issue = re.sub(r'<[^>]+>', '', ai_res.text.strip()) # HTMLタグを強制排除
            if any(ok in ai_issue for ok in ["なし", "問題ありません", "適切です"]): ai_issue = ""
        except: ai_issue = "⚠️ AI解析エラー"

        final = []
        if dead_results: final.append("**物理エラー**\n" + "\n".join(dead_results))
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
    model, error_msg = load_ai_model(INTERNAL_API_KEY)
    if error_msg: st.error(error_msg); st.stop()

    content = uploaded_file.read().decode("utf-8")
    urls = [loc.text.strip().rstrip('/') for loc in BeautifulSoup(content, 'xml').find_all(re.compile(r'loc', re.I))] if uploaded_file.name.endswith(".xml") else [line.strip().rstrip('/') for line in content.splitlines()]
    unique_urls = list(dict.fromkeys([u for u in urls if u.startswith('http')]))

    if st.button(f"{len(unique_urls)} ページの並列検品を開始"):
        results = []
        reported_dead, checked_cache = set(), {}
        prog, status = st.progress(0), st.empty()
        with ThreadPoolExecutor(max_workers=5) as executor:
            tasks = {executor.submit(inspect_single_page, u, model, get_session(), (b_user, b_pass) if b_user else None, reported_dead, checked_cache): u for u in unique_urls}
            for i, future in enumerate(as_completed(tasks)):
                res_data = future.result()
                results.append(res_data); prog.progress((i + 1) / len(unique_urls)); status.text(f"完了: {i+1}/{len(unique_urls)} - {res_data['url']}")

        st.success("検品完了！")
        st.table(pd.DataFrame(results))
        html_rows = "".join([f"<tr><td style='font-size:12px;width:30%;'><a href='{r['url']}' target='_blank'>{r['url']}</a></td><td><span style='color:{'#e74c3c' if '✅' not in r['issue'] else '#333'};'>{r['issue'].replace('\n','<br>')}</span></td></tr>" for r in results])
        full_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{{font-family:sans-serif;padding:20px;background:#f4f7f9;}}table{{width:100%;border-collapse:collapse;background:#fff;}}th,td{{border:1px solid #eee;padding:12px;text-align:left;vertical-align:top;}}th{{background:#3498db;color:#fff;}}</style></head><body><h1>🔍 {sitemap_stem} レポート</h1><table><thead><tr><th>URL</th><th>指摘事項</th></tr></thead><tbody>{html_rows}</tbody></table></body></html>"
        st.download_button(f"📄 レポート保存", data=full_html, file_name=report_name, mime="text/html")
