
# app.py — Competitor ASIN Grabber (Streamlit standalone)
import os, re, requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from datetime import datetime
import requests

# 读取 API Key
API_KEY = st.secrets["keepa"]["api_key"]

def search_products_by_keyword(keyword, domain=3, page=0):
    """调用 Keepa 搜索功能，抓取关键词下的竞品 ASIN"""
    url = "https://api.keepa.com/query"
    params = {
        "key": API_KEY,
        "domain": domain,  # 3 = UK, 1 = US, 4 = DE ...
        "type": "product",
        "term": keyword,
        "page": page
    }
    res = requests.get(url, params=params).json()
    if "products" not in res:
        st.error("❌ Keepa 返回错误: " + str(res))
        return pd.DataFrame()
    
    # 解析数据
    products = []
    for p in res["products"]:
        products.append({
            "ASIN": p.get("asin"),
            "Title": p.get("title"),
            "Brand": p.get("brand"),
            "Category": p.get("rootCategory"),
            "BuyBoxPrice": p.get("buyBoxPrice"),
            "SalesRank": p.get("salesRankDrops30"),  # 30天销量排名波动（越多越好）
        })
    return pd.DataFrame(products)

# 📊 Streamlit UI
st.title("🔎 Keepa 竞品分析模块")

keyword = st.text_input("输入关键词（例如：airlock 或 brewing heat pad）")

if st.button("抓取竞品"):
    if not keyword:
        st.warning("请输入关键词")
    else:
        df = search_products_by_keyword(keyword)
        st.dataframe(df)
        st.download_button("⬇️ 下载 CSV", df.to_csv(index=False), "keepa_results.csv")


st.set_page_config(page_title="Competitor ASIN Grabber", layout="wide")
st.title("🕵️ Competitor ASIN Grabber")
st.caption("""
输入一个竞品 ASIN（如 B0D4QMBS75），抓取该商品详情页的相似/相关推荐位的 ASIN，尽量补全标题/价格/评分/评论，并导出 CSV。
如果配置了 Keepa API Key（`KEEPA_API_KEY`），会优先使用 Keepa 获取 alsoBought/alsoViewed 关系。
""")

# ----------------- Utils -----------------
def _format_price(txt):
    if txt is None: return None
    v = re.sub(r"[^\d\.,]", "", str(txt)).replace(",", "")
    try: return float(v)
    except: return None

def _fetch_mobile_product_snapshot(asin, domain="amazon.co.uk"):
    """
    访问亚马逊移动简页尽量取到：标题/价格/星级/评论数。
    仅做补充，不保证100%获取。
    """
    url = f"https://{domain}/gp/aw/d/{asin}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Mobile Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return {"asin": asin, "title": None, "price": None, "rating": None, "reviews": None, "url": f"https://{domain}/dp/{asin}"}
        soup = BeautifulSoup(r.text, "html.parser")

        title_el = soup.select_one("#title")
        title = title_el.get_text(strip=True) if title_el else None

        price = None
        price_el = soup.select_one(".a-color-price") or soup.select_one("#priceblock_ourprice")
        if price_el: price = _format_price(price_el.get_text())

        rating = None
        rating_el = soup.select_one(".acr-stars-text") or soup.find(string=re.compile(r"out of 5"))
        if rating_el:
            text = rating_el if isinstance(rating_el, str) else rating_el.get_text()
            m = re.search(r"([\d\.]+)\s*out of 5", text)
            if m:
                try: rating = float(m.group(1))
                except: rating = None

        reviews = None
        rev_el = soup.select_one("#acrCustomerReviewText") or soup.find(string=re.compile(r"ratings"))
        if rev_el:
            text = rev_el if isinstance(rev_el, str) else rev_el.get_text()
            m = re.search(r"([\d,]+)", text)
            if m: reviews = int(m.group(1).replace(",", ""))

        return {"asin": asin, "title": title, "price": price, "rating": rating, "reviews": reviews, "url": f"https://{domain}/dp/{asin}"}
    except Exception:
        return {"asin": asin, "title": None, "price": None, "rating": None, "reviews": None, "url": f"https://{domain}/dp/{asin}"}

def _scrape_related_asins_from_dp(asin, domain="amazon.co.uk", max_items=100):
    """
    直接抓取竞品详情页，解析推荐位里的 ASIN（sponsored/related/also viewed等）
    """
    url = f"https://{domain}/dp/{asin}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")

        asins = set([asin.upper()])
        # data-asin 容器
        for tag in soup.find_all(attrs={"data-asin": True}):
            candidate = str(tag.get("data-asin")).strip().upper()
            if re.fullmatch(r"B0[A-Z0-9]{8}", candidate):
                asins.add(candidate)
        # 链接形式 /dp/B0XXXXXXX/
        for a in soup.find_all("a", href=True):
            m = re.search(r"/dp/(B0[A-Z0-9]{8})", a["href"].upper())
            if m: asins.add(m.group(1))

        asins.discard(asin.upper())
        out = list(asins)
        return out[:max_items]
    except Exception:
        return []

def _keepa_fetch_related(asin, domain="amazon.co.uk", api_key=None, max_items=200):
    """
    使用 Keepa API 获取 alsoBought/alsoViewed 等关联ASIN（更稳、更全）
    环境变量：KEEPA_API_KEY；pip install keepa
    """
    try:
        import keepa
    except Exception:
        return [], "Keepa SDK 未安装，已回退 HTML 解析模式。"

    if not api_key:
        return [], "未设置 KEEPA_API_KEY，已回退 HTML 解析模式。"

    try:
        api = keepa.Keepa(api_key)
        domain_map = {"amazon.co.uk": 2, "amazon.com": 1, "amazon.de": 3, "amazon.fr": 8, "amazon.it": 10, "amazon.es": 9}
        dom = domain_map.get(domain, 2)
        products = api.query(asin=asin, domain=dom, history=False)
        if not products:
            return [], "Keepa 未返回产品，回退 HTML 解析模式。"
        p = products[0]
        related = set()
        for k in ("alsoBought", "alsoViewed", "frequentlyBoughtTogether", "related"):
            arr = p.get(k) or []
            for x in arr:
                xu = str(x).upper()
                if re.fullmatch(r"B0[A-Z0-9]{8}", xu): related.add(xu)
        related.discard(asin.upper())
        return list(related)[:max_items], None
    except Exception as e:
        return [], f"Keepa 查询失败（{e}），回退 HTML 解析模式。"

# ----------------- UI -----------------
with st.container():
    cols = st.columns([1,1,1,1])
    with cols[0]:
        domain = st.selectbox("Marketplace", ["amazon.co.uk","amazon.com","amazon.de","amazon.fr","amazon.it","amazon.es"], index=0)
    with cols[1]:
        seed_asin = st.text_input("输入竞品 ASIN（如 B0D4QMBS75）").strip()
    with cols[2]:
        max_items = st.number_input("最多抓取数量", 10, 500, 120, 10)
    with cols[3]:
        use_keepa = st.toggle("优先使用 Keepa API", value=True, help="需在 Secrets 添加 KEEPA_API_KEY；无则自动回退 HTML 解析。")

if st.button("🚀 开始抓取", use_container_width=True):
    if not re.fullmatch(r"[Bb]0[A-Za-z0-9]{8}", seed_asin or ""):
        st.error("请输入合法的 ASIN（以 B0 开头，共10字符）。")
    else:
        seed_asin = seed_asin.upper()

        # 1) 尝试 Keepa
        related_asins, keepa_msg = [], None
        if use_keepa:
            keepa_key = os.environ.get("KEEPA_API_KEY", "").strip()
            related_asins, keepa_msg = _keepa_fetch_related(seed_asin, domain=domain, api_key=keepa_key, max_items=max_items)
        if keepa_msg: st.warning(keepa_msg)

        # 2) 回退 HTML 解析
        if not related_asins:
            with st.status("🔎 正在解析竞品详情页推荐位 …", expanded=False):
                related_asins = _scrape_related_asins_from_dp(seed_asin, domain=domain, max_items=max_items)
                st.write(f"找到候选 ASIN：{len(related_asins)}")

        if not related_asins:
            st.error("未能抓到相关 ASIN，请更换竞品或稍后再试。")
        else:
            rows, progress = [], st.progress(0, text="补充信息中…")
            for i, a in enumerate(related_asins, start=1):
                snap = _fetch_mobile_product_snapshot(a, domain=domain)
                rows.append(snap)
                progress.progress(int(i/len(related_asins)*100), text=f"信息补充 {i}/{len(related_asins)}")
            progress.empty()

            df = pd.DataFrame(rows, columns=["asin","title","price","rating","reviews","url"])
            st.success(f"抓取完成：共 {len(df)} 条。")
            st.dataframe(df, use_container_width=True)

            csv_data = df.to_csv(index=False).encode("utf-8-sig")
            today = datetime.now().strftime("%Y%m%d")
            st.download_button(
                "📥 下载 CSV",
                data=csv_data,
                file_name=f"asin_competitors_{seed_asin}_{today}.csv",
                mime="text/csv",
                use_container_width=True
            )

st.markdown("---")
st.caption("提示：若配置 Keepa，稳定性和覆盖率更好；未配置也能抓到一部分推荐 ASIN（基于页面结构解析）。")
