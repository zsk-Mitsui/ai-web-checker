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

# --- 初期設定 ---
st.set_page_config(page_title="AI Web検品 Pro", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if st.session_state.password_correct:
        return True
    st.title("🔑 チーム専用：AI検品ディレクター Pro")
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

# --- 通信セッションの構築 ---
def create_robust_session():
    session = requests.Session()
    retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    class SSLAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs['ssl_context'] = ctx
            return super().init_poolmanager(*args, **kwargs)
    adapter = SSLAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# --- AIモデルのロード（エラー回避ロジック付） ---
@st.cache_resource
def load_ai_model(api_key):
    try:
        genai.configure(api_key=api_key)
        # Advancedユーザー向けに性能重視でproを指定しつつ、失敗時はflashへ
        try:
            return genai.GenerativeModel('gemini-1.5-pro')
        except:
            return genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        st.error(f"AIモデルのロードに失敗しました: {e}")
        return None

def call_ai_inspection(model, url, html_content):
    now = datetime.datetime.now()
    prompt = f"""現在は{now.strftime('%Y年%m月')}。
    URL: {url} を【極めて厳格】に検品し、不備のみを報告せよ。
    1. 文字品質（最優先）: 誤字脱字、不要なスペース、環境依存文字。
    2. 電話番号不整合: 各所の番号違い。
    3. コンテンツ整合性: 他社名の混入。
    4. メタ情報: descriptionに無関係な他社名がある場合のみ。
    【厳守】不備がなければ『なし』とだけ回答。"""
    try:
        response = model.generate_content(prompt + "\n\nソース:\n" + html_content[:15000])
        text = response.text.strip()
        if not text or "なし" in text or "問題ありません" in text: return ""
        return "\n".join([l for l in text.splitlines() if "問題ありません" not in l]).strip()
    except Exception as e:
        return f"⚠️ AI解析エラー: {str(e)}"

# --- 物理チェック（1ページ分） ---
def inspect_page(url, model, session, auth_info, reported_dead_assets, global_checked_assets):
    try:
        res = session.get(url, auth=auth_info, timeout=20, verify=False)
        res.encoding = res.apparent_encoding
        if res.status_code != 200:
            return {"url": url, "issue": f"⚠️ ページアクセス失敗 ({res.status_code})"}
        
        soup = BeautifulSoup(res.text, 'html.parser')
        base_tag = soup.find('base', href=True)
        effective_base = urljoin(res.url, base_tag['href']) if base_tag else res.url
        
        # アセット抽出
        potential = set()
        for t, a in [('img','src'),('link','href'),('script','src')]:
            for item in soup.find_all(t, **{a: True}): potential.add(urljoin(effective_base, item[a]))
        for meta in soup.find_all('meta', content=True):
            c = meta['content']
            if c.startswith(('http', '/', '.')) or any(ext in c.lower() for ext in ['.jpg','.png','.webp','.svg','.ico']):
                potential.add(urljoin(effective_base, c))
        
        dead_links = []
        for a_url in potential:
            if a_url not in global_checked_assets:
                try:
                    a_res = session.get(a_url, auth=auth_info, timeout=10, verify=False, stream=True)
                    global_checked_assets[a_url] = a_res.status_code
                    a_res.close()
                except: global_checked_assets[a_url] = 999
            
            if global_checked_assets[a_url] >= 400:
                if a_url not in reported_dead_assets:
                    dead_links.append(f"❌ リンク切れ({global_checked_assets[a_url]}): {a_url}")
                    reported_dead_assets.add(a_url)
        
        ai_issue = call_ai_inspection(model, url, res.text)
        final = []
        if dead_links: final.append("**物理エラー（リンク切れ）**\n" + "\n".join(dead_links))
        if ai_issue: final.append(ai_issue)
        return {"url": url, "issue": "\n\n".join(final) if final else "✅ 問題なし"}
    except Exception as e:
        return {"url": url, "issue": f"⚠️ エラー: {str(e)}"}

# --- UI ---
st.sidebar.title("🛠 設定")
basic_user = st.sidebar.text_input("Basic認証 ユーザー名")
basic_pass = st.sidebar.text_input("Basic認証 パスワード", type="password")

uploaded_file = st.file_uploader("sitemap.xml をアップロード", type="xml")

if uploaded_file and INTERNAL_API_KEY:
    # ファイル名からレポート名を生成
    base_filename = os.path.splitext(uploaded_file.name)[0]
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    output_filename = f"{base_filename}_report_{today_str}.html"

    model = load_ai_model(INTERNAL_API_KEY)
    session = create_robust_session()
    auth_info = (basic_user, basic_pass) if basic_user else None

    soup_xml = BeautifulSoup(
