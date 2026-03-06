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

# --- 2. セキュリティ（ログイン画面） ---
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

# --- 3. ネットワーク通信設定 ---
def create_robust_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504]
    )
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

# --- 4. AIモデル設定（Gemini Pro優先） ---
@st.cache_resource
def load_ai_model(api_key):
    try:
        genai.configure(api_key=api_key)
        # 1.5 Pro(Advanced相当)を優先、失敗時はFlashへ
        for model_name in ['gemini-1.5-pro', 'gemini-1.5-flash']:
            try:
                model = genai.GenerativeModel(model_name)
                # 疎通テスト
                model.generate_content("test", generation_config={"max_output_tokens": 1})
                return model
            except:
                continue
        return None
    except Exception as e:
        st.error(f"AI設定エラー: {e}")
        return None

# --- 5. 検品エンジン（1ページ分） ---
def inspect_page(url, model, session, auth_info, reported_dead_assets, global_checked_assets):
    try:
        res = session.get(url, auth=auth_info, timeout=20, verify=False)
        res.encoding = res.apparent_encoding
