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
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- 初期設定 & セキュリティ ---
st.set_page_config(page_title="AI Web検品ディレクター Pro", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if st.session_state.password_correct:
        return True
    st.title("🔑 チーム専用：AI検品ディレクター Pro")
    password_input = st.text_input("パスワードを入力してください", type="password")
    if st.button("ログイン"):
        if password_input == st.secrets.get("TOOL_PASSWORD"):
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("パスワードが正しくありません")
    return False

if not check_password():
    st.stop()

INTERNAL_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# --- ネットワーク通信の最適化 ---
def create_robust_session():
    session = requests.Session()
    # リトライ戦略の設定（接続エラーや一時的な503等に対応）
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    # SSLエラーを無視するカスタムアダプター
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

# --- AI検品ロジック ---
@st.cache_resource
def load_ai_model(api_key):
    try:
        genai.configure(api_key=api_key)
        # Gemini 1.5 Flashを使用（速度重視）
        return genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        st.error(f"AIモデルのロードに失敗しました: {e}")
        return None

def call_ai_inspection(model, url, html_content):
    now = datetime.datetime.now()
    prompt = f"""現在は{now.strftime('%Y年%m月')}。
    URL: {url} を【極めて厳格】に検品し、以下の項目に該当する不備のみを報告せよ。

    1. 文字品質（最優先）: 
       ・「お引きたえ」「Abobe」等の微細な誤字、送り仮名ミス、不要なスペース、環境依存文字の使用。
    2. 電話番号不整合: サイト内の各所で番号が異なっていないか。
    3. コンテンツ整合性: 別のサイトの使い回し、無関係な他社名の混入。
    4. メタ情報: descriptionに無関係な他社名がある場合。

    【厳守】不備がなければ『なし』とだけ回答せよ。"""
    
    try:
        response = model.generate_content(prompt + "\n\nソース:\n" + html_content[:15000])
        text = response.text.strip()
        if not text or "なし" in text or "問題ありません" in text:
            return ""
        # 不要な正常報告行をフィルタリング
        lines = [l for l in text.splitlines() if not any(ok in l for ok in ["不整合は見当たりません", "見当たりません", "問題ありません"])]
        return "\n".join(lines).strip()
    except Exception as e:
        return f"⚠️ AI解析エラー: {str(e)}"

# --- 物理アセット検品 ---
def check_asset(session, asset_url, auth_info):
    try:
        res = session.get(asset_url, auth=auth_info, timeout=10, verify=False, stream=True)
        status = res.status_code
        res.close()
        return status
    except:
        return 999

# --- メイン処理ユニット（1ページ分） ---
def inspect_page(url, model, session, auth_info, reported_dead_assets, global_checked_assets):
    try:
        res = session.get(url, auth=auth_info, timeout=20, verify=False)
        res.encoding = res.apparent_encoding
        if res.status_code != 200:
            return {"url": url, "issue": f"⚠️ ページアクセス失敗 (Status: {res.status_code})"}
        
        html_text = res.text
        soup = BeautifulSoup(html_text, 'html.parser')
        
        # URL解決用のベースURL設定
        base_tag = soup.find('base', href=True)
        effective_base = urljoin(res.url, base_tag['href']) if base_tag else res.url
        
        # アセット抽出
        potential_assets = set()
        for tag, attr in [('img','src'), ('link','href'), ('script','src')]:
            for item in soup.find_all(tag, **{attr: True}):
                potential_assets.add(urljoin(effective_base, item[attr]))
        for meta in soup.find_all('meta', content=True):
            c = meta['content']
            if c.startswith(('http', '/', '.')) or any(ext in c.lower() for ext in ['.jpg','.png','.webp','.svg','.ico']):
                potential_assets.add(urljoin(effective_base, c))
        
        # アセット死活監視
        dead_links = []
        for a_url in potential_assets:
            if a_url not in global_checked_assets:
                global_checked_assets[a_url] = check_asset(session, a_url, auth_info)
            
            if global_checked_assets[a_url] >= 400:
                if a_url not in reported_dead_assets:
                    dead_links.append(f"❌ リンク切れ({global_checked_assets[a_url]}): {a_url}")
                    reported_dead_assets.add(a_url)
        
        # AI検品
        ai_issue = call_ai_inspection(model, url, html_text)
        
        # 結果の合成
        final_report = []
        if dead_links:
            final_report.append("**物理エラー（リンク切れ）**\n" + "\n".join(dead_links))
        if ai_issue:
            final_report.append(ai_issue)
            
        return {"url": url, "issue": "\n\n".join(final_report) if final_report else "✅ 問題なし"}

    except Exception as e:
        return {"url": url, "issue": f"⚠️ 検品中断エラー: {str(e)}"}

# --- UI表示 ---
st.title("🔍 Web検品 Pro (Gemini Advanced Refactored)")
st.sidebar.title("🛠 オプション")
basic_user = st.sidebar.text_input("Basic認証 ユーザー名")
basic_pass = st.sidebar.text_input("Basic認証 パスワード", type="password")

uploaded_file = st.file_uploader("sitemap.xml をアップロード", type="xml")

if uploaded_file and INTERNAL_API_KEY:
    model = load_ai_model(INTERNAL_API_KEY)
    session = create_robust_session()
    auth_info = (basic_user, basic_pass) if basic_user else None

    # Sitemap解析
    soup = BeautifulSoup(uploaded_file, 'xml')
    url_list = [t.text.strip() for t in soup.find_all(re.compile(r'loc', re.I)) if t.text.strip().startswith('http')]
    # 重複URL排除
    url_list = list(dict.fromkeys([u.rstrip('/') for u in url_list]))

    if st.button(f"{len(url_list)} ページの並列検品を開始"):
        results = []
        reported_dead_assets = set()
        global_checked_assets = {}
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # --- 並列実行の核心部 ---
        # 同時実行数は5〜10程度がGemini APIのレートリミット的に安全
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_url = {executor.submit(inspect_page, url, model, session, auth_info, reported_dead_assets, global_checked_assets): url for url in url_list}
            
            for i, future in enumerate(as_completed(future_to_url)):
                res_data = future.result()
                results.append(res_data)
                progress_bar.progress((i + 1) / len(url_list))
                status_text.text(f"完了: {i+1}/{len(url_list)} - {res_data['url']}")

        st.success("全ての並列検品が完了しました！")
        
        # 結果表示
        df = pd.DataFrame(results)
        st.table(df)
        
        # レポート生成
        rows_html = ""
        for r in results:
            issue_html = r['issue'].replace('\n', '<br>')
            status_cls = "status-error" if "✅" not in r['issue'] else ""
            rows_html += f"<tr><td style='font-size:12px; width:30%;'><a href='{r['url']}' target='_blank'>{r['url']}</a></td><td><span class='{status_cls}' style='color: {'#e74c3c' if status_cls else '#333'};'>{issue_html}</span></td></tr>"
        
        html_report = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{{font-family:sans-serif;padding:20px;background:#f4f7f9;}}table{{width:100%;border-collapse:collapse;background:#fff;}}th,td{{border:1px solid #eee;padding:12px;text-align:left;vertical-align:top;}}th{{background:#3498db;color:#fff;}}</style></head><body><h1>🔍 検品結果</h1><table><thead><tr><th>URL</th><th>指摘事項</th></tr></thead><tbody>{rows_html}</tbody></table></body></html>"
        
        st.download_button("📄 HTMLレポートを保存", html_report, file_name=f"report_{datetime.date.today()}.html", mime="text/html")
