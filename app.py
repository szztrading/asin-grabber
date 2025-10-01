# app.py — Competitor ASIN Grabber (Streamlit standalone, fixed)
# 功能：输入一个竞品 ASIN → 通过 Keepa（优先）或 HTML 回退抓取 alsoBought/alsoViewed/related ASIN
# 输出：尽量补全 标题/价格/评分/评论/链接 → 支持 CSV 下载

import os
import re
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from datetime import datetime

import keepa
st.write("Keepa 版本：", keepa.__version__)

st.set_page_config(page_title="Competitor ASIN Grabber", layout="wide")
st.title("🕵️ Competitor ASIN Grabber")
st.caption("""
输入一个竞品 ASIN（如 B0D4QMBS75），抓取该商品详情页的相似/相关推荐位的 ASIN，
并尽量补全标题/价格/评分/评论，最后导出 CSV。

✅ 若配置了 Keepa（Secrets 中设置 `KEEPA_API_KEY` 或 `[keepa].api_key`），将优先通过 Keepa 获取 alsoBought/alsoViewed/related；
❇️ 未配置 Keepa 时会自动回退到 HTML 解析模式。
""")

# ----------------- Keepa Key 读取（兼容多种写法） -----------------
def get_keepa_key() -> str:
    """
    读取 Keepa API Key（优先 secrets，其次环境变量），支持两种 secrets 写法：
    1) KEEPA_API_KEY = "..."
    2) [keepa]
       api_key = "..."
    """
    try:
        key = (
            st.secrets.get("KEEPA_API_KEY", "") or
            (st.secrets.get("keepa", {}) or {}).get("api_key", "") or
            os.environ.get("KEEPA_API_KEY", "")
        )
        return key.strip()
    except Exception:
        return os.environ.get("KEEPA_API_KEY", "").strip()

KEEPA_KEY = get_keepa_key()

# ----------------- 工具函数 -----------------
def _format_price(txt):
    if txt is None:
        return None
    v = re.sub(r"[^\d\.,]", "", str(txt)).replace(",", "")
    try:
        return float(v)
    except Exception:
        return None

def _fetch_mobile_product_snapshot(asin, domain="amazon.co.uk"):
    """
    访问亚马逊移动简页，尽量取到：标题/价格/星级/评论数。
    仅做补充，不保证100%获取（回退也会返回基本结构）。
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
        if price_el:
            price = _format_price(price_el.get_text())

        rating = None
        rating_el = soup.select_one(".acr-stars-text") or soup.find(string=re.compile(r"out of 5"))
        if rating_el:
            text = rating_el if isinstance(rating_el, str) else rating_el.get_text()
            m = re.search(r"([\d\.]+)\s*out of 5", text)
            if m:
                try:
                    rating = float(m.group(1))
                except Exception:
                    rating = None

        reviews = None
        rev_el = soup.select_one("#acrCustomerReviewText") or soup.find(string=re.compile(r"ratings"))
        if rev_el:
            text = rev_el if isinstance(rev_el, str) else rev_el.get_text()
            m = re.search(r"([\d,]+)", text)
            if m:
                reviews = int(m.group(1).replace(",", ""))

        return {"asin": asin, "title": title, "price": price, "rating": rating, "reviews": reviews, "url": f"https://{domain}/dp/{asin}"}
    except Exception:
        return {"asin": asin, "title": None, "price": None, "rating": None, "reviews": None, "url": f"https://{domain}/dp/{asin}"}

def _scrape_related_asins_from_dp(asin, domain="amazon.co.uk", max_items=100):
    """
    直接抓取竞品详情页，解析推荐位里的 ASIN（sponsored/related/also viewed 等）。
    作为 Keepa 不可用时的回退方案。
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

        # 常见 data-asin 容器
        for tag in soup.find_all(attrs={"data-asin": True}):
            candidate = str(tag.get("data-asin")).strip().upper()
            if re.fullmatch(r"B0[A-Z0-9]{8}", candidate):
                asins.add(candidate)

        # 链接形式 /dp/B0XXXXXXX/
        for a in soup.find_all("a", href=True):
            m = re.search(r"/dp/(B0[A-Z0-9]{8})", a["href"].upper())
            if m:
                asins.add(m.group(1))

        asins.discard(asin.upper())
        out = list(asins)
        return out[:max_items]
    except Exception:
        return []

def _keepa_fetch_related(asin, domain="amazon.co.uk", api_key=None, max_items=200):
    """
    使用 Keepa SDK 获取 alsoBought/alsoViewed/related 等关联 ASIN（更稳、更全）。
    需要：pip install keepa；并在 Secrets/环境变量提供 api_key。
    """
    try:
        import keepa
    except Exception:
        return [], "Keepa SDK 未安装（requirements.txt 需包含 keepa>=1.3.0），已回退 HTML 解析模式。"

    if not api_key:
        return [], "未检测到 Keepa API Key，已回退 HTML 解析模式。"

    try:
        api = keepa.Keepa(api_key)
        domain_map = {"amazon.co.uk": 2, "amazon.com": 1, "amazon.de": 3, "amazon.fr": 8, "amazon.it": 10, "amazon.es": 9}
        dom = domain_map.get(domain, 2)
        products = api.query(asin=asin, domain=dom, history=False)
        if not products:
            return [], "Keepa 未返回产品，已回退 HTML 解析模式。"
        p = products[0]
        related = set()
        for k in ("alsoBought", "alsoViewed", "frequentlyBoughtTogether", "related"):
            arr = p.get(k) or []
            for x in arr:
                xu = str(x).upper()
                if re.fullmatch(r"B0[A-Z0-9]{8}", xu):
                    related.add(xu)
        related.discard(asin.upper())
        return list(related)[:max_items], None
    except Exception as e:
        return [], f"Keepa 查询失败（{e}），已回退 HTML 解析模式。"

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
        prefer_keepa = st.toggle("优先使用 Keepa", value=True, help="需在 Secrets 配置 KEEPA_API_KEY 或 [keepa].api_key；无则自动回退 HTML 解析。")

# 小提示显示密钥状态（可注释掉）
st.caption(f"🔐 Keepa Key 状态：{'✅ 已检测到' if KEEPA_KEY else '❌ 未配置，将使用 HTML 回退'}")

if st.button("🚀 开始抓取", use_container_width=True):
    # 校验 ASIN 格式
    if not re.fullmatch(r"[Bb]0[A-Za-z0-9]{8}", seed_asin or ""):
        st.error("请输入合法的 ASIN（以 B0 开头，共10字符）。")
        st.stop()
    seed_asin = seed_asin.upper()

    related_asins, keepa_msg = [], None

    # 1) Keepa 优先
    if prefer_keepa:
        related_asins, keepa_msg = _keepa_fetch_related(seed_asin, domain=domain, api_key=KEEPA_KEY, max_items=max_items)
        if keepa_msg:
            st.warning(keepa_msg)

    # 2) 回退：HTML 解析详情页推荐位
    if not related_asins:
        with st.status("🔎 正在解析竞品详情页的推荐位 …", expanded=False):
            related_asins = _scrape_related_asins_from_dp(seed_asin, domain=domain, max_items=max_items)
            st.write(f"找到候选 ASIN：{len(related_asins)}")

    if not related_asins:
        st.error("未能抓到相关 ASIN，请更换竞品或稍后再试。")
        st.stop()

    # 3) 为每个 ASIN 尽量补信息（移动简页抓取）
    rows = []
    progress = st.progress(0, text="补充信息中…")
    for i, a in enumerate(related_asins, start=1):
        snap = _fetch_mobile_product_snapshot(a, domain=domain)
        rows.append(snap)
        progress.progress(int(i/len(related_asins)*100), text=f"信息补充 {i}/{len(related_asins)}")
    progress.empty()

    df = pd.DataFrame(rows, columns=["asin","title","price","rating","reviews","url"])
    st.success(f"抓取完成：共 {len(df)} 条。")
    st.dataframe(df, use_container_width=True)

    # 4) 导出 CSV
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
st.caption("提示：未配置 Keepa 时将使用 HTML 回退，可能受页面结构影响抓取率较低；建议在 Secrets 配置 Keepa 提升稳定性与覆盖率。")
