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

# --- 3. ネットワーク通信クラス ---
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
        # 利用可能なモデルを順に試す
        for m_name in ['gemini-1.5-pro', 'gemini-1.5-flash']:
            try:
                m = genai.GenerativeModel(m_name)
                m.generate_content("ping", generation_config={"max_output_tokens": 1})
                return m
            except:
                continue
        return None
    except:
        return None

# --- 5. 個別ページ検品エンジン ---
def inspect_single_page(url, model, session, auth_info, reported_dead_assets, global_checked_assets):
    try:
        # ページ取得
        res = session.get(url, auth=auth_info, timeout=20, verify=False)
        res.encoding = res.apparent_encoding
        if res.status_code != 200:
            return {"url": url, "issue": f"⚠️ ページアクセスエラー ({res.status_code})"}

        soup = BeautifulSoup(res.text, 'html.parser')
        base_tag = soup.find('base', href=True)
        effective_base = urljoin(res.url, base_tag['href']) if base_tag else res.url
        
        # 物理リンク（画像・JS・CSS・Meta）の抽出
        assets = set()
        for tag, attr in [('img','src'),('link','href'),('script','src')]:
            for item in soup.find_all(tag, **{attr: True}):
                assets.add(urljoin(effective_base, item[attr]))
        for meta in soup.find_all('meta', content=True):
            content = meta['content']
            if content.startswith(('http', '/', '.')) or any(ext in content.lower() for ext in ['.jpg','.png','.webp','.svg']):
                assets.add(urljoin(effective_base, content))

        # リンク切れの死活監視
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

        # AI解析プロンプト
        now_str = datetime.datetime.now().strftime('%Y年%m月')
        prompt = f"現在は{now_str}。URL: {url} を極めて厳格に検品せよ。\n" \
                 "1.文字品質:誤字脱字(お引きたえ等)、不要なスペース、環境依存文字。\n" \
                 "2.不整合:電話番号の違い、他社名混入、他サイトコピペ残骸。\n" \
                 "不備がなければ『なし』とだけ回答せよ。"
        
        ai_issue = ""
        try:
            response = model.generate_content(prompt + "\n\nHTMLソース:\n" + res.text[:15000])
            ai_issue = response.text.strip()
            if "なし" in ai_issue or "問題ありません" in ai_issue:
                ai_issue = ""
        except Exception as e:
            ai_issue = f"⚠️ AIエラー: {str(e)}"

        # レポート合成
        final_list = []
        if dead_results:
            final_list.append("**物理エラー**\n" + "\n".join(dead_results))
        if ai_issue:
            final_list.append("**検品指摘**\n" + ai_issue)
            
        return {"url": url, "issue": "\n\n".join(final_list) if final_list else "✅ 問題なし"}

    except Exception as e:
