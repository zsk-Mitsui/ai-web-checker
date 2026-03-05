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

# --- サイドバー設定 ---
st.sidebar.title("🛠 設定")
default_api_key = st.secrets.get("GEMINI_API_KEY", "")
api_key = st.sidebar.text_input("Gemini API Key", value=default_api_key, type="password")
basic_user = st.sidebar.text_input("Basic認証 ユーザー名")
basic_pass = st.sidebar.text_input("Basic認証 パスワード", type="password")

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
    """AIの回答から空の見出しや正常報告を削除する"""
    if not text or "なし" in text or "問題ありません" in text:
        return "✅ 問題なし"
    
    lines = text.splitlines()
    cleaned_lines = []
    
    # 正常系ワードを含む行を削除
    filtered_lines = [l for l in lines if not any(ok in l for ok in ["不整合は見当たりません", "見当たりません", "不備はありません", "問題ありません", "ありませんでした"])]
    
    # 中身のない見出しを削除
    for i, line in enumerate(filtered_lines):
        line_s = line.strip()
        if not line_s: continue
        
        # 見出し行（**数字. または 【）の判定
        if re.match(r'^(\*\*|【|第?\d+[\.・])', line_s):
            has_content = False
            for j in range(i + 1, len(filtered_lines)):
                next_l = filtered_lines[j].strip()
                if not next_l: continue
                if re.match(r'^(\*\*|【|第?\d+[\.・])', next_l): break # 次の見出しが来たら終了
                has_content = True
                break
            if not has_content: continue # 内容がない見出しはスキップ
            
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

    html_template = f"""<!DOCTYPE html><html lang='ja'><head><meta charset='UTF-8'><title>検品レポート</title><style>body{{font-family:sans-serif;color:#333;max-width:1100px;margin:30px auto;padding:20px;background:#f4f7f9;}}h1{{border-left:8px solid #3498db;padding-left:15px;font-size:24px;}}table{{width:100%;border-collapse:collapse;background:#fff;table-layout:fixed;}}th,td{{border:1px solid #eee;padding:14px;text-align:left;word-break:break-all;vertical-align:top;}}th{{background:#3498db;color:#fff;}}.url-cell{{font-size:12px;width:30%;}}.status-error{{color:#e74c3c;line-height:1.6;font-size:14px;}}</style></head><body><h1>🔍 Webサイト検品結果レポート</h1><table><thead><tr><th>調査対象ページ</th><th>AI検品 指摘事項</th></tr></thead><tbody>{rows_html}</tbody></table></body></html>"""
    return html_template

# --- アプリメイン ---
st.title("🔍 HP納品前 AI自動検品ツール")
uploaded_file = st.file_uploader("sitemap.xml をアップロード", type="xml")

if uploaded_file and api_key:
    model = load_ai_model(api_key)
    session = requests.Session()
    session.mount('https://', SuperSslContextAdapter())
    session.verify = False
    auth_info = (basic_user, basic_pass) if basic_user else None

    # URLの抽出と正規化（重複排除）
    soup = BeautifulSoup(uploaded_file, 'xml')
    loc_tags = soup.find_all(re.compile(r'loc', re.I))
    # 末尾スラッシュを削除したものをキーにして重複を排除
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
        
        for i, url in enumerate(url_list):
            status_text.text(f"⏳ {i+1}/{len(url_list)} ページ解析中: {url}")
            try:
                res = session.get(url, auth=auth_info, timeout=20, verify=False)
                res.encoding = res.apparent_encoding
                
                if res.status_code != 200:
                    issue = f"⚠️ アクセス失敗(Status: {res.status_code})"
                    if res.status_code == 401: issue = "⚠️ 認証エラー(Basic認証の入力漏れ)"
                else:
                    now = datetime.datetime.now()
                    prompt = f"""現在は{now.strftime('%Y年%m月')}。
                    URL: {url} の問題点（不備）のみを報告せよ。
                    1. 文字品質: ®、①、㈱等の環境依存文字、文字化け、誤字脱字。
                    2. 電話番号不整合: 番号違い。
                    3. 鮮度: 未来の日付（{now.year}年{now.month}月より先）、他社名。
                    4. メタ情報: descriptionの内容乖離。

                    【厳守ルール】
                    ・指摘事項がないカテゴリーの『見出し』や『箇条書き』は絶対に出力しないでください。
                    ・「問題ありません」「見当たりません」といった正常報告は一切不要です。
                    ・全体として不備が1つもなければ『なし』とだけ回答してください。"""
                    
                    ai_response = model.generate_content(prompt + "\n\nソース:\n" + res.text[:15000])
                    issue = clean_ai_response(ai_response.text.strip())
                
                results.append({"url": url, "issue": issue})
            except Exception as e:
                results.append({"url": url, "issue": f"⚠️ エラー: {str(e)}"})
            
            progress_bar.progress((i + 1) / len(url_list))

        st.success("検品完了！")
        st.table(pd.DataFrame(results))
        st.download_button("📄 HTMLレポートをダウンロード", generate_html_report(results), file_name="check_report.html", mime="text/html")
