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

# --- 4. AIモデル設定 (モデル自動探索機能) ---
@st.cache_resource
def load_ai_model(api_key):
    if not api_key:
        return None, "APIキーが設定されていません。"
    try:
        genai.configure(api_key=api_key)
        
        # 利用可能なモデルをリストアップ
        available_models = []
        try:
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    available_models.append(m.name)
        except Exception as e:
            return None, f"モデルリストの取得に失敗しました: {str(e)}"

        if not available_models:
            return None, "使用可能なモデルが見つかりませんでした。APIキーの有効性やリージョンを確認してください。"

        # 優先順位: 1.5 Pro -> 1.5 Flash -> 最初に見つかったもの
        target_model = None
        for pref in ["models/gemini-1.5-pro", "models/gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.5-flash"]:
            if pref in available_models:
                target_model = pref
                break
        
        if not target_model:
            target_model = available_models[0]

        # モデルの初期化
        model = genai.GenerativeModel(target_model)
        # 接続テスト
        model.generate_content("test", generation_config={"max_output_tokens": 1})
        return model, None
        
    except Exception as e:
        return None, f"AI設定エラー: {str(e)}"

# --- 5. 個別ページ検品エンジン ---
def inspect_single_page(url, model, session, auth_info, reported_dead_assets, global_checked_assets):
    try:
        res = session.get(url, auth=auth_info, timeout=20, verify=False)
        res.encoding = res.apparent_encoding
        if res.status_code != 200:
            return {"url": url, "issue": f"⚠️ ページ読込失敗 ({res.status_code})"}

        soup = BeautifulSoup(res.text, 'html.parser')
        base_tag = soup.find('base', href=True)
        effective_base = urljoin(res.url, base_tag['href']) if base_tag else res.url
        
        # 資産の抽出
        assets = set()
        for tag, attr in [('img','src'),('link','href'),('script','src')]:
            for item in soup.find_all(tag, **{attr: True}):
                assets.add(urljoin(effective_base, item[attr]))
        for meta in soup.find_all('meta', content=True):
            content = meta['content']
            if content.startswith(('http', '/', '.')) or any(ext in content.lower() for ext in ['.jpg','.png','.webp','.svg']):
                assets.add(urljoin(effective_base, content))

        # 物理リンク切れチェック
        dead_results = []
        for a_url in assets:
            if a_url not in global_checked_assets:
                try:
                    with session.get(a_url, auth=auth_info, timeout=10, verify=False, stream=True) as a_res:
                        global_checked_assets[a_url] = a_res.status_code
                except:
                    global_checked_assets[a_url] = 999
            
            if global_checked_assets[a_url] >= 400:
                if a_url not in reported_dead_assets:
                    dead_results.append(f"❌ リンク切れ({global_checked_assets[a_url]}): {a_url}")
                    reported_dead_assets.add(a_url)

        # AI解析プロンプト (Gemini Advancedの性能をフル活用)
        now_str = datetime.datetime.now().strftime('%Y年%m月')
        prompt = f"現在は{now_str}。URL: {url} を極めて厳格に検品せよ。\n" \
                 "1.文字品質:誤字脱字(お引きたえ等)、不要なスペース(半角・全角)、環境依存文字。\n" \
                 "2.不整合:電話番号不一致、他社名混入。\n" \
                 "不備がなければ『なし』とだけ回答せよ。"
        
        ai_issue = ""
        try:
            response = model.generate_content(prompt + "\n\nHTMLソース:\n" + res.text[:15000])
            ai_issue = response.text.strip()
            if "なし" in ai_issue or "問題ありません" in ai_issue:
                ai_issue = ""
        except Exception as e:
            ai_issue = f"⚠️ AIエラー: {str(e)}"

        # 合流
        final = []
        if dead_results:
            final.append("**物理エラー**\n" + "\n".join(dead_results))
        if ai_issue:
            final.append("**検品指摘**\n" + ai_issue)
            
        return {"url": url, "issue": "\n\n".join(final) if final else "✅ 問題なし"}
    except Exception as e:
        return {"url": url, "issue": f"⚠️ 解析失敗: {str(e)}"}

# --- 6. メインUI ---
st.sidebar.title("🛠 設定")
b_user = st.sidebar.text_input("Basic認証 ユーザー名")
b_pass = st.sidebar.text_input("Basic認証 パスワード", type="password")

uploaded_file = st.file_uploader("URLリスト (.xml または .txt) をアップロード", type=["xml", "txt"])

if uploaded_file and INTERNAL_API_KEY:
    # ファイル名からレポート名を動的に生成
    sitemap_stem = os.path.splitext(uploaded_file.name)[0]
    date_label = datetime.date.today().strftime('%Y-%m-%d')
    report_name = f"{sitemap_stem}_report_{date_label}.html"

    # モデルの動的ロード
    model, error_msg = load_ai_model(INTERNAL_API_KEY)
    if error_msg:
        st.error(f"AIモデルの初期化に失敗しました: {error_msg}")
        st.stop()

    session = get_session()
    auth = (b_user, b_pass) if b_user else None

    # ファイル解析
    raw_content = uploaded_file.read().decode("utf-8")
    unique_urls = []
    if uploaded_file.name.endswith(".xml"):
        xml_soup = BeautifulSoup(raw_content, 'xml')
        for loc in xml_soup.find_all(re.compile(r'loc', re.I)):
            u = loc.text.strip().rstrip('/')
            if u.startswith('http') and u not in unique_urls:
                unique_urls.append(u)
    else:
        for line in raw_content.splitlines():
            u = line.strip().rstrip('/')
            if u.startswith('http') and u not in unique_urls:
                unique_urls.append(u)

    if st.button(f"{len(unique_urls)} ページの並列検品を開始"):
        results = []
        reported_dead = set()
        checked_cache = {}
        prog = st.progress(0)
        status = st.empty()
        
        # マルチスレッドによる並列実行
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_url = {executor.submit(inspect_single_page, u, model, session, auth, reported_dead, checked_cache): u for u in unique_urls}
            for i, future in enumerate(as_completed(future_to_url)):
                data = future.result()
                results.append(data)
                prog.progress((i + 1) / len(unique_urls))
                status.text(f"完了: {i+1}/{len(unique_urls)} - {data.get('url')}")

        st.success("検品完了！")
        st.table(pd.DataFrame(results))
        
        # HTMLレポート作成
        html_rows = ""
        for r in results:
            text_color = "#e74c3c" if "✅" not in r['issue'] else "#333"
            html_rows += f"<tr><td style='font-size:12px;width:30%;'><a href='{r['url']}' target='_blank'>{r['url']}</a></td>"
            html_rows += f"<td><span style='color:{text_color};'>{r['issue'].replace('\n','<br>')}</span></td></tr>"
        
        full_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><style>" \
                    f"body{{font-family:sans-serif;padding:20px;background:#f4f7f9;}}" \
                    f"table{{width:100%;border-collapse:collapse;background:#fff;}}" \
                    f"th,td{{border:1px solid #eee;padding:12px;text-align:left;vertical-align:top;}}" \
                    f"th{{background:#3498db;color:#fff;}}</style></head><body>" \
                    f"<h1>🔍 {sitemap_stem} 検品結果レポート</h1>" \
                    f"<table><thead><tr><th>URL</th><th>指摘事項</th></tr></thead><tbody>{html_rows}</tbody></table>" \
                    f"</body></html>"
        
        st.download_button(label=f"📄 {report_name} をダウンロード", data=full_html, file_name=report_name, mime="text/html")
