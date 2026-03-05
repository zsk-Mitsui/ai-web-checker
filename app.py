import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
import time
import google.generativeai as genai
import re
import html as html_lib
import pandas as pd
import io
import urllib3
import ssl
import datetime

# --- 初期設定 ---
st.set_page_config(page_title="AI Web検品ディレクター", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SuperSslContextAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.options |= 0x4
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = ctx
        return super(SuperSslContextAdapter, self).init_poolmanager(*args, **kwargs)

# --- サイドバー設定（修正版） ---
st.sidebar.title("🛠 設定")

# Secretsにキーがあるか確認し、あればそれを使う。なければ入力欄を出す。
default_api_key = st.secrets.get("GEMINI_API_KEY", "")
api_key = st.sidebar.text_input("Gemini API Key", value=default_api_key, type="password")

basic_user = st.sidebar.text_input("Basic認証 ユーザー名")
basic_pass = st.sidebar.text_input("Basic認証 パスワード", type="password")

# --- アプリメイン ---
st.title("🔍 HP納品前 AI自動検品ツール")
st.write("sitemap.xml をアップロードして、AIによる精密検品を開始します。")

uploaded_file = st.file_uploader("sitemap.xml をアップロード", type="xml")

# --- 修正後のモデル作成部分 ---
if uploaded_file and api_key:
    genai.configure(api_key=api_key)
    
    # 利用可能なモデルの中から 'flash' を含むものを自動で探す
    def get_working_model():
        try:
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    if 'gemini-1.5-flash' in m.name:
                        return genai.GenerativeModel(m.name)
            # 見つからない場合の最終バックアップ
            return genai.GenerativeModel('gemini-1.5-flash')
        except:
            return genai.GenerativeModel('gemini-1.5-flash')

    model = get_working_model()
# ---------------------------
    
    session = requests.Session()
    session.mount('https://', SuperSslContextAdapter())
    session.verify = False
    auth_info = (basic_user, basic_pass) if basic_user else None

    # URL抽出ロジック（Ver 18.0 継承）
    soup = BeautifulSoup(uploaded_file, 'xml')
    loc_tags = soup.find_all(re.compile(r'loc', re.I))
    unique_urls = {}
    for t in loc_tags:
        url = t.text.strip()
        if url.startswith('http'):
            norm_key = url.rstrip('/')
            if norm_key not in unique_urls: unique_urls[norm_key] = url
    url_list = list(unique_urls.values())

    if st.button(f"{len(url_list)} ページの検品を開始"):
        progress_bar = st.progress(0)
        results = []
        checked_assets = {}
        reported_dead_assets = set()

        for i, url in enumerate(url_list):
            st.write(f"🔎 検品中: {url}")
            
            # --- ページ取得 & アセットチェック ---
            try:
                res = session.get(url, auth=auth_info, timeout=20, verify=False)
                res.encoding = res.apparent_encoding
                html_text = res.text
                
                # アセット抽出と死活監視（Ver 14.1 / 16.2 継承）
                dead_assets = []
                soup_p = BeautifulSoup(html_text, 'html.parser')
                asset_base = urljoin(url, soup_p.find('base')['href']) if soup_p.find('base') else url
                
                # 画像, CSS, JS, Iconの抽出
                tags = {'img': 'src', 'link': 'href', 'script': 'src'}
                found_assets = []
                for tag, attr in tags.items():
                    for item in soup_p.find_all(tag, **{attr: True}):
                        if tag == 'link' and not any(r in str(item.get('rel')).lower() for r in ['icon', 'stylesheet']): continue
                        found_assets.append(urljoin(asset_base, item[attr]))

                for a_url in set(found_assets):
                    if a_url not in checked_assets:
                        try:
                            h_res = session.head(a_url, auth=auth_info, timeout=5, verify=False)
                            checked_assets[a_url] = h_res.status_code
                        except: checked_assets[a_url] = 999
                    if checked_assets[a_url] >= 400 and a_url not in reported_dead_assets:
                        dead_assets.append(f"未検出({checked_assets[a_url]}): {a_url.split('/')[-1]}")
                        reported_dead_assets.add(a_url)

                # --- AI解析（Ver 18.0 継承） ---
                now = datetime.datetime.now()
                prompt = f"""現在は{now.strftime('%Y年%m月')}。{now.year-1}年は過去。
                URL: {url} の問題点を報告せよ。
                1. 文字品質: ®、①、㈱等の環境依存文字、文字化け（等）、誤字脱字。
                2. 電話番号不整合: 各所の番号違い。
                3. 鮮度: 未来の日付（{now.year}年{now.month}月より先）、他社名。
                4. リンク切れ: {", ".join(dead_assets) if dead_assets else "なし"}
                ※不備がない項目は出力厳禁。全て正常なら「なし」と回答。"""
                
                ai_response = model.generate_content(prompt + "\n\nソース:\n" + html_text[:15000])
                issue = ai_response.text.strip() if "なし" not in ai_response.text else ""
                results.append({"url": url, "issue": issue})

            except Exception as e:
                results.append({"url": url, "issue": f"⚠️ エラー: {str(e)}"})

            progress_bar.progress((i + 1) / len(url_list))
            time.sleep(1)

        # --- レポート表示 ---
        st.success("検品完了！")

        st.table(results)




