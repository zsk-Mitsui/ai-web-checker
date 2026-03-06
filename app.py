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

# --- 3. 通信セッション設定 ---
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
    if not api_key:
        return None, "APIキー未設定"
    try:
        genai.configure(api_key=api_key)
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        target = None
        for pref in ["models/gemini-1.5-pro", "models/gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.5-flash"]:
            if pref in available_models:
                target = pref
                break
        if not target and available_models:
            target = available_models[0]
        
        if not target:
            return None, "モデルが見つかりません"

        model = genai.GenerativeModel(target)
        model.generate_content("ping", generation_config={"max_output_tokens": 1})
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
        base_tag = soup.find('base', href=True)
        effective_base = urljoin(res.url, base_tag['href']) if base_tag else res.url
        
        # 資産抽出
        assets = set()
        for tag, attr in [('img','src'),('link','href'),('script','src')]:
            for item in soup.find_all(tag, **{attr: True}):
                assets.add(urljoin(effective_base, item[attr]))
        for meta in soup.find_all('meta', content=True):
            content = meta['content']
            if content.startswith(('http', '/', '.')) or any(ext in content.lower() for ext in ['.jpg','.png','.webp','.svg']):
                assets.add(urljoin(effective_base, content))

        # リンク切れチェック
        dead_list = []
        for a_url in assets:
            if a_url not in global_checked_assets:
                try:
                    with session.get(a_url, auth=auth_info, timeout=10, verify=False, stream=True) as a_res:
                        global_checked_assets[a_url] = a_res.status_code
                except:
                    global_checked_assets[a_url] = 999
            
            if global_checked_assets[a_url] >= 400:
                if a_url not in reported_dead_assets:
                    dead_list.append(f"❌ リンク切れ({global_checked_assets[a_url]}): {a_url}")
                    reported_dead_assets.add(a_url)

        # --- AIプロンプト：汎用的かつ極めて厳格な校閲基準 ---
        now_str = datetime.datetime.now().strftime('%Y年%m月')
        prompt = f"""あなたは日本最高峰のWebディレクター兼校閲記者です。URL: {url} のソースを解析し、プロの品質基準に満たない「不備」のみを鋭く指摘せよ。

        【検品基準】
        1. 表記の正確性: 1文字のタイポ、送り仮名の誤り、助詞の重複、文字化けを徹底的に摘出せよ。
        2. 記述の整合性: 文脈上の矛盾、数値や単位の不一致、同一ページ内での情報の食い違い。
        3. 専門性・信頼性: ビジネス文書として不自然な口語、過剰または誤った敬語表現、他サイトからのコピペ残骸（他社名・他サービス名）。
        4. メタ情報の整合性: ページ内容と明らかに乖離したdescriptionやOGP情報。

        【出力制限（ノイズ排除）】
        ・問題がない項目については一切言及するな。見出しも出すな。
        ・「〜は見当たりません」「〜は適切です」等の報告は不要。
        ・指摘事項のみを箇条書きで出力せよ。
        ・不備が皆無の場合のみ『なし』と回答せよ。"""
        
        ai_issue = ""
        try:
            response = model.generate_content(prompt + "\n\nHTMLソース:\n" + res.text[:15000])
            ai_issue = response.text.strip()
            # 正常報告ワードの徹底排除
            if any(ok in ai_issue for ok in ["なし", "問題ありません", "不備はありません", "適切です"]):
                ai_issue = ""
        except Exception:
            ai_issue = "⚠️ AI解析一時エラー"

        final = []
        if dead_list:
            final.append("**物理エラー**\n" + "\n".join(dead_list))
        if ai_issue:
            final.append("**検品指摘**\n" + ai_issue)
            
        return {"url": url, "issue": "\n\n".join(final) if final else "✅ 問題なし"}
    except Exception as e:
        return {"url": url, "issue": f"⚠️ 解析エラー: {str(e)}"}

# --- 6. UI ---
st.sidebar.title("🛠 設定")
b_user = st.sidebar.text_input("Basic認証 ユーザー名")
b_pass = st.sidebar.text_input("Basic認証 パスワード", type="password")

uploaded_file = st.file_uploader("URLリスト (.xml または .txt) をアップロード", type=["xml", "txt"])

if uploaded_file and INTERNAL_API_KEY:
    sitemap_stem = os.path.splitext(uploaded_file.name)[0]
    report_name = f"{sitemap_stem}_report_{datetime.date.today()}.html"

    model, error_msg = load_ai_model(INTERNAL_API_KEY)
    if error_msg:
        st.error(f"初期化失敗: {error_msg}")
        st.stop()

    session = get_session()
    auth = (b_user, b_pass) if b_user else None

    content = uploaded_file.read().decode("utf-8")
    urls = []
    if uploaded_file.name.endswith(".xml"):
        soup_xml = BeautifulSoup(content, 'xml')
        urls = [loc.text.strip().rstrip('/') for loc in soup_xml.find_all(re.compile(r'loc', re.I))]
    else:
        urls = [line.strip().rstrip('/') for line in content.splitlines()]
    
    unique_urls = list(dict.fromkeys([u for u in urls if u.startswith('http')]))

    if st.button(f"{len(unique_urls)} ページの並列検品を開始"):
        results = []
        reported_dead = set()
        checked_cache = {}
        prog = st.progress(0)
        status = st.empty()
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_url = {executor.submit(inspect_single_page, u, model, session, auth, reported_dead, checked_cache): u for u in unique_urls}
            for i, future in enumerate(as_completed(future_to_url)):
                res_data = future.result()
                results.append(res_data)
                prog.progress((i + 1) / len(unique_urls))
                status.text(f"完了: {i+1}/{len(unique_urls)} - {res_data.get('url')}")

        st.success("検品完了！")
        st.table(pd.DataFrame(results))
        
        html_rows = ""
        for r in results:
            color = "#e74c3c" if "✅" not in r['issue'] else "#333"
            html_rows += f"<tr><td style='font-size:12px;width:30%;'><a href='{r['url']}' target='_blank'>{r['url']}</a></td>"
            html_rows += f"<td><span style='color:{color};'>{r['issue'].replace('\n','<br>')}</span></td></tr>"
        
        full_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{{font-family:sans-serif;padding:20px;background:#f4f7f9;}}table{{width:100%;border-collapse:collapse;background:#fff;}}th,td{{border:1px solid #eee;padding:12px;text-align:left;vertical-align:top;}}th{{background:#3498db;color:#fff;}}</style></head><body><h1>🔍 {sitemap_stem} 検品レポート</h1><table><thead><tr><th>URL</th><th>指摘事項</th></tr></thead><tbody>{html_rows}</tbody></table></body></html>"
        st.download_button(label=f"📄 {report_name} をダウンロード", data=full_html, file_name=report_name, mime="text/html")
