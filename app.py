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

# --- 🔒 ログインチェック機能 ---
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if st.session_state.password_correct:
        return True
    st.title("🔑 チーム専用：AI検品ディレクター")
    st.write("このツールは社内専用です。パスワードを入力してください。")
    password_input = st.text_input("パスワード", type="password")
    if st.button("ログイン"):
        if password_input == st.secrets.get("TOOL_PASSWORD"):
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("パスワードが正しくありません")
    return False

if not check_password():
    st.stop()

class SuperSslContextAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.options |= 0x4
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = ctx
        return super(SuperSslContextAdapter, self).init_poolmanager(*args, **kwargs)

# --- ヘルパー関数 ---
@st.cache_resource
def load_ai_model(api_key):
    try:
        genai.configure(api_key=api_key)
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        target = next((m for m in available_models if "gemini-1.5-flash" in m), available_models[0])
        return genai.GenerativeModel(target)
    except: return genai.GenerativeModel('gemini-1.5-flash')

def clean_ai_response(text):
    if not text or "なし" in text or "問題ありません" in text:
        return "✅ 問題なし"
    lines = text.splitlines()
    cleaned_lines = []
    filtered_lines = [l for l in lines if not any(ok in l for ok in ["不整合は見当たりません", "見当たりません", "不備はありません", "問題ありません", "ありませんでした"])]
    for i, line in enumerate(filtered_lines):
        line_s = line.strip()
        if not line_s: continue
        if re.match(r'^(\*\*|【|第?\d+[\.・])', line_s):
            has_content = False
            for j in range(i + 1, len(filtered_lines)):
                next_l = filtered_lines[j].strip()
                if not next_l: continue
                if re.match(r'^(\*\*|【|第?\d+[\.・])', next_l): break
                has_content = True
                break
            if not has_content: continue
        cleaned_lines.append(line)
    final_text = "\n".join(cleaned_lines).strip()
    return final_text if final_text else "✅ 問題なし"

def generate_html_report(results):
    rows_html = ""
    for res in results:
        issue_display = res['issue'].replace('\n', '<br>')
        is_ok = "✅ 問題なし" in res['issue']
        status_class = "" if is_ok else "status-error"
        rows_html += f"<tr><td class='url-cell'><a href='{res['url']}' target='_blank'>{res['url']}</a></td><td><span class='{status_class}'>{issue_display}</span></td></tr>"
    html_template = f"<!DOCTYPE html><html lang='ja'><head><meta charset='UTF-8'><title>検品レポート</title><style>body{{font-family:sans-serif;color:#333;max-width:1100px;margin:30px auto;padding:20px;background:#f4f7f9;}}h1{{border-left:8px solid #3498db;padding-left:15px;font-size:24px;}}table{{width:100%;border-collapse:collapse;background:#fff;table-layout:fixed;}}th,td{{border:1px solid #eee;padding:14px;text-align:left;word-break:break-all;vertical-align:top;}}th{{background:#3498db;color:#fff;}}.url-cell{{font-size:12px;width:30%;}}.status-error{{color:#e74c3c;line-height:1.6;font-size:14px;}}</style></head><body><h1>🔍 Webサイト検品結果レポート</h1><table><thead><tr><th>調査対象ページ</th><th>AI検品 指摘事項</th></tr></thead><tbody>{rows_html}</tbody></table></body></html>"
    return html_template

# --- アプリメイン ---
st.title("🔍 HP納品前 AI自動検品ツール")
st.sidebar.title("🛠 設定")
default_api_key = st.secrets.get("GEMINI_API_KEY", "")
api_key = st.sidebar.text_input("Gemini API Key", value=default_api_key, type="password")
basic_user = st.sidebar.text_input("Basic認証 ユーザー名")
basic_pass = st.sidebar.text_input("Basic認証 パスワード", type="password")

uploaded_file = st.file_uploader("sitemap.xml をアップロード", type="xml")

if uploaded_file and api_key:
    model = load_ai_model(api_key)
    session = requests.Session()
    session.mount('https://', SuperSslContextAdapter())
    session.verify = False
    auth_info = (basic_user, basic_pass) if basic_user else None

    soup = BeautifulSoup(uploaded_file, 'xml')
    loc_tags = soup.find_all(re.compile(r'loc', re.I))
    url_map = {}
    for t in loc_tags:
        raw_url = t.text.strip()
        if raw_url.startswith('http'):
            url_map[raw_url.rstrip('/')] = raw_url
    url_list = list(url_map.values())

    if st.button(f"{len(url_list)} ページの検品を開始"):
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        global_checked_assets = {}

        for i, url in enumerate(url_list):
            status_text.text(f"⏳ {i+1}/{len(url_list)} ページ解析中: {url}")
            try:
                res = session.get(url, auth=auth_info, timeout=20, verify=False)
                res.encoding = res.apparent_encoding
                if res.status_code != 200:
                    issue = f"⚠️ アクセス失敗(Status: {res.status_code})"
                else:
                    html_text = res.text
                    soup_p = BeautifulSoup(html_text, 'html.parser')
                    
                    # リンク切れチェック (meta/og含む)
                    dead_assets = []
                    potential_assets = []
                    for img in soup_p.find_all('img', src=True): potential_assets.append(img['src'])
                    for link in soup_p.find_all('link', href=True): potential_assets.append(link['href'])
                    for script in soup_p.find_all('script', src=True): potential_assets.append(script['src'])
                    for meta in soup_p.find_all('meta', content=True):
                        content = meta['content']
                        if content.startswith(('http', '/')) or any(ext in content.lower() for ext in ['.jpg', '.png', '.webp', '.svg']):
                            potential_assets.append(content)

                    for asset_path in set(potential_assets):
                        asset_url = urljoin(url, asset_path)
                        if asset_url not in global_checked_assets:
                            try:
                                a_res = session.head(asset_url, auth=auth_info, timeout=5, verify=False)
                                global_checked_assets[asset_url] = a_res.status_code
                            except: global_checked_assets[asset_url] = 999
                        if global_checked_assets[asset_url] >= 400:
                            dead_assets.append(f"❌ リンク切れ({global_checked_assets[asset_url]}): {asset_url}")

                    # --- AIプロンプト：内容の整合性チェックを追加 ---
                    prompt = f"""URL: {url} の不備のみを報告せよ。
                    1. コンテンツの整合性（コピペ残骸の検出）:
                       ・本文中に、現在のサイトとは無関係な「他社名」「他サービス名」「別プロジェクトの記述」が混じっていないか。
                       ・テンプレートのダミーテキストや、明らかに他サイトからの流用と思われる違和感のある文章はないか。
                    2. 文字品質: 
                       ・環境依存文字（®、①、㈱、～等）の使用。
                       ・文中や文末の「不要な半角スペース」。
                       ・誤字脱字、送り仮名のミス（例：「お引きたえ」はNG）。
                    3. 電話番号不整合: 各所の番号違い。
                    4. メタ情報: descriptionが内容と【明らかに無関係】（他サイトの記述のまま等）。
                    5. 物理エラー(自動検出済み): {", ".join(dead_assets) if dead_assets else "なし"}

                    【厳守ルール】
                    ・不備がないカテゴリーの見出しは出力しない。
                    ・「問題ありません」等の正常報告は不要。
                    ・不備がなければ『なし』とだけ回答。"""
                    
                    ai_response = model.generate_content(prompt + "\n\nソース:\n" + html_text[:15000])
                    issue = clean_ai_response(ai_response.text.strip())
                
                results.append({"url": url, "issue": issue})
            except Exception as e:
                results.append({"url": url, "issue": f"⚠️ エラー: {str(e)}"})
            progress_bar.progress((i + 1) / len(url_list))

        st.success("検品完了！")
        st.table(pd.DataFrame(results))
        st.download_button("📄 HTMLレポートをダウンロード", generate_html_report(results), file_name="check_report.html", mime="text/html")
