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
st.write("sitemap.xml をアップロードして、精密検品を開始します。")

uploaded_file = st.file_uploader("sitemap.xml をアップロード", type="xml")

if uploaded_file and api_key:
    model = load_ai_model(api_key)
    session = requests.Session()
    session.mount('https://', SuperSslContextAdapter())
    session.verify = False
    auth_info = (basic_user, basic_pass) if basic_user else None

    soup = BeautifulSoup(uploaded_file, 'xml')
    loc_tags = soup.find_all(re.compile(r'loc', re.I))
    url_list = list(set([t.text.strip() for t in loc_tags if t.text.strip().startswith('http')]))

    if st.button(f"{len(url_list)} ページの検品を開始"):
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
       for i, url in enumerate(url_list):
            status_text.text(f"⏳ {i+1}/{len(url_list)} ページ目を解析中: {url}")
            try:
                # ページを取得
                res = session.get(url, auth=auth_info, timeout=20, verify=False)
                res.encoding = res.apparent_encoding
                
                # --- ① ステータスコードの判定（401エラー等のノイズ除去） ---
                if res.status_code == 401:
                    issue = "⚠️ ウェブサイトにアクセスできませんでした。（ベーシック認証の入力漏れ・誤りの可能性があります）"
                elif res.status_code == 404:
                    issue = "⚠️ ウェブサイトにアクセスできませんでした。（ページが見つかりません/404）"
                elif res.status_code != 200:
                    issue = f"⚠️ ウェブサイトにアクセスできませんでした。（Status: {res.status_code}）"
                else:
                    # --- ② 正常(200)な場合のみ詳細な解析を実行 ---
                    html_text = res.text
                    
                    # AI解析用のプロンプト（Ver 19.0 準拠）
                    now = datetime.datetime.now()
                    prompt = f"""現在は{now.strftime('%Y年%m月')}。
                    URL: {url} の問題点を報告せよ。
                    1. 文字品質: ®、①、㈱等の環境依存文字、文字化け、誤字脱字。
                    2. 電話番号不整合: 番号違い。
                    3. 鮮度: 未来の日付（{now.year}年{now.month}月より先）、他社名。
                    4. メタ情報: descriptionの内容乖離。
                    不備がなければ「なし」と回答。"""
                    
                    ai_response = model.generate_content(prompt + "\n\nソース:\n" + html_text[:15000])
                    issue = ai_response.text.strip()
                    
                    # 指摘がない場合は「問題なし」に変換
                    if not issue or "なし" in issue or "問題ありません" in issue:
                        issue = "✅ 問題なし"
                
                # 結果をリストに追加
                results.append({"url": url, "issue": issue})

            except Exception as e:
                results.append({"url": url, "issue": f"⚠️ エラー: {str(e)}"})
            
            # 進捗を更新
            progress_bar.progress((i + 1) / len(url_list))

        st.success("全ての検品が完了しました！")
        
        # 画面に結果表示
        df = pd.DataFrame(results)
        st.table(df)

        # HTMLレポートダウンロード
        report_html = generate_html_report(results)
        st.download_button(
            label="📄 検品レポート(HTML)をダウンロード",
            data=report_html,
            file_name=f"check_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.html",
            mime="text/html"
        )

