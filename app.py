# app.py — Competitor ASIN Grabber (Keepa REST + HTML fallback + Relevance Scoring)
# 功能：
# 1) 输入竞品 ASIN → 通过 Keepa REST（优先）或 HTML 回退抓取 alsoBought/alsoViewed/related ASIN
# 2) 为每个 ASIN 尽量补全 Title/Price/Rating/Reviews/URL
# 3) 相关性打分 + 过滤（包含/排除词、价格、评分、评论阈值）
# 4) 导出两个 CSV：全量抓取、已过滤推荐

import os
import re
import json
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from datetime import datetime  # ✅ 正确导入

# ---------------- 基本设置 ----------------
st.set_page_config(page_title="Competitor ASIN Grabber", layout="wide")
st.title("🕵️ Competitor ASIN Grabber")
st.caption("""
输入一个竞品 ASIN（如 B0D4QMBS75），抓取该商品详情页的 alsoBought / alsoViewed / related，并尽量补全标题/价格/评分/评论。
✅ 若配置 Keepa（Secrets: `KEEPA_API_KEY` 或 `[keepa].api_key`），优先使用 Keepa REST；失败或未配置将回退 HTML 解析。
""")

# ---------------- 读取 Keepa Key（兼容多种写法） ----------------
def get_keepa_key() -> str:
    """
    读取 Keepa API Key（优先 secrets，其次环境变量）。
    支持两种 secrets 写法：
      1) KEEPA_API_KEY = "..."
      2) [keepa] \n api_key = "..."
    """
    try:
        key = (
            st.secrets.get("KEEPA_API_KEY", "") or
            (st.secrets.get("keepa", {}) or {}).get("api_key", "") or
            os.environ.get("KEEPA_API_KEY", "")
        )
        return (key or "").strip()
    except Exception:
        return (os.environ.get("KEEPA_API_KEY", "") or "").strip()

KEEPA_KEY = get_keepa_key()

# ---------------- 工具函数 ----------------
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
    失败时也返回基本结构以保证流水线不中断。
    """
    url = f"https://{domain}/gp/aw/d/{asin}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Mobile Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
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

def _scrape_related_asins_from_dp(asin, domain="amazon.co.uk", max_items=120):
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
        r = requests.get(url, headers=headers, timeout=15)
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

# ---------------- Keepa REST：获取关联 ASIN ----------------
def _keepa_fetch_related_rest(asin, domain="amazon.co.uk", api_key=None, max_items=200):
    """
    直接使用 Keepa REST API，不依赖 keepa SDK。兼容性最好。
    返回：([ASIN...], 错误/提示消息或 None)
    """
    if not api_key:
        return [], "未检测到 Keepa API Key，已回退 HTML 解析模式。"

    domain_map = {
        "amazon.co.uk": 2,
        "amazon.com": 1,
        "amazon.de": 3,
        "amazon.fr": 8,
        "amazon.it": 10,
        "amazon.es": 9
    }
    dom = domain_map.get(domain, 2)

    url = "https://api.keepa.com/product"
    params = {
        "key": api_key,
        "domain": dom,
        "asin": asin,
        "history": 0
    }

    try:
        r = requests.get(url, params=params, timeout=25)
        data = r.json()

        if "error" in data and data["error"]:
            return [], f"Keepa API 错误: {data['error']}"

        if "products" not in data or not data["products"]:
            return [], "Keepa 返回为空，可能 ASIN 无数据或数据不足。"

        p = data["products"][0]
        related = set()

        for k in ("alsoBought", "alsoViewed", "frequentlyBoughtTogether", "related"):
            for x in (p.get(k, []) or []):
                xu = str(x).upper()
                if re.fullmatch(r"B0[A-Z0-9]{8}", xu):
                    related.add(xu)

        related.discard(asin.upper())
        out = list(related)[:max_items]

        if not out:
            return out, "Keepa 已连接，但未返回关联 ASIN（可能是新品或数据不足）。"

        return out, None

    except Exception as e:
        return [], f"Keepa API 请求失败：{e}"

# ---------------- 相关性打分 + 过滤 ----------------
def score_and_filter(df: pd.DataFrame,
                     include_terms=None,
                     exclude_terms=None,
                     price_min=None,
                     price_max=None,
                     rating_min=None,
                     reviews_min=None):
    """
    对抓到的 ASIN 做“相关性打分 + 过滤”
    规则：
      - 标题命中 include_terms 加分（多命中多加）
      - 命中 exclude_terms 直接剔除
      - 价格/评分/评论阈值过滤（不达标剔除）
    返回：df_kept（含 RelevanceScore）、df_dropped（被剔除项）
    """
    if df is None or df.empty:
        return df, pd.DataFrame()

    work = df.copy()
    # 规范化
    work["title"] = work.get("title", "").fillna("").astype(str).str.lower()

    def contains_any(text, terms):
        text = text or ""
        for t in (terms or []):
            t = t.strip().lower()
            if not t:
                continue
            if t in text:
                return True
        return False

    include_terms = [t.strip().lower() for t in (include_terms or []) if t.strip()]
    exclude_terms = [t.strip().lower() for t in (exclude_terms or []) if t.strip()]

    scores, drops_mask = [], []
    for _, row in work.iterrows():
        title = row.get("title", "")

        # 1) 命中排除词 → 丢弃
        if contains_any(title, exclude_terms):
            scores.append(0)
            drops_mask.append(True)
            continue

        # 2) include 计分
        score = 0
        for t in include_terms:
            if t in title:
                score += 1

        # 3) 阈值过滤
        price = row.get("price", None)
        rating = row.get("rating", None)
        reviews = row.get("reviews", None)

        drop = False
        if price_min is not None and isinstance(price, (int, float)) and price < price_min:
            drop = True
        if price_max is not None and isinstance(price, (int, float)) and price > price_max:
            drop = True
        if rating_min is not None and (rating is None or (isinstance(rating, (int, float)) and rating < rating_min)):
            drop = True
        if reviews_min is not None and (reviews is None or (isinstance(reviews, (int, float)) and reviews < reviews_min)):
            drop = True

        drops_mask.append(drop)
        scores.append(score)

    work["RelevanceScore"] = scores
    dropped = work.loc[drops_mask].copy()

    kept = work.loc[[not x for x in drops_mask]].copy()
    kept = kept.sort_values(
        by=["RelevanceScore", "reviews", "rating"],
        ascending=[False, False, False],
        kind="mergesort"
    )

    return kept, dropped

# ---------------- UI：输入区 ----------------
with st.container():
    cols = st.columns([1,1,1,1])
    with cols[0]:
        domain = st.selectbox("Marketplace", ["amazon.co.uk","amazon.com","amazon.de","amazon.fr","amazon.it","amazon.es"], index=0)
    with cols[1]:
        seed_asin = st.text_input("输入竞品 ASIN（如 B0D4QMBS75）").strip()
    with cols[2]:
        max_items = st.number_input("最多抓取数量", 10, 500, 120, 10)
    with cols[3]:
        prefer_keepa = st.toggle("优先使用 Keepa (REST)", value=True, help="需在 Secrets 配置 KEEPA_API_KEY 或 [keepa].api_key；无则自动回退 HTML 解析。")

st.caption(f"🔐 Keepa Key 状态：{'✅ 已检测到' if KEEPA_KEY else '❌ 未配置，将使用 HTML 回退'}")

# ---------------- UI：相关性过滤器 ----------------
with st.expander("🧠 相关性过滤器（建议开启）", expanded=True):
    default_includes = "brew, brewing, airlock, ferment, demijohn, bung, grommet, wine, cider, mead, kombucha, heat belt, heat mat, heat pad, fermentation"
    default_excludes = "reptile, terrarium, seed, seedling, plant, pet, dog, cat, car, 12v, water tank, aquarium, vivarium, sous vide, coffee, tea pot"

    colf1, colf2 = st.columns(2)
    with colf1:
        include_terms_str = st.text_input("包含关键词（命中加分，逗号分隔）", value=default_includes)
        price_min = st.number_input("最低价格(£)", min_value=0.0, value=10.0, step=0.5)
        rating_min = st.number_input("最低评分", min_value=0.0, max_value=5.0, value=3.8, step=0.1)
    with colf2:
        exclude_terms_str = st.text_input("排除关键词（命中直接剔除，逗号分隔）", value=default_excludes)
        price_max = st.number_input("最高价格(£)", min_value=0.0, value=60.0, step=0.5)
        reviews_min = st.number_input("最低评论数", min_value=0, value=20, step=5)

# ---------------- 动作 ----------------
if st.button("🚀 开始抓取", use_container_width=True):
    # 校验 ASIN 格式
    if not re.fullmatch(r"[Bb]0[A-Za-z0-9]{8}", seed_asin or ""):
        st.error("请输入合法的 ASIN（以 B0 开头，共10字符）。")
        st.stop()
    seed_asin = seed_asin.upper()

    # 1) Keepa REST 优先
    related_asins, msg = [], None
    if prefer_keepa and KEEPA_KEY:
        with st.status("🔗 正在通过 Keepa REST 获取关联 ASIN …", expanded=False):
            related_asins, msg = _keepa_fetch_related_rest(seed_asin, domain=domain, api_key=KEEPA_KEY, max_items=max_items)
            if msg:
                st.write(msg)

    # 2) 回退：HTML 解析详情页推荐位
    if not related_asins:
        with st.status("🔎 正在解析竞品详情页推荐位（HTML 回退）…", expanded=False):
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

    # 4) 相关性打分 + 过滤
    include_terms = [x.strip() for x in include_terms_str.split(",")]
    exclude_terms = [x.strip() for x in exclude_terms_str.split(",")]
    kept, dropped = score_and_filter(
        df,
        include_terms=include_terms,
        exclude_terms=exclude_terms,
        price_min=price_min, price_max=price_max,
        rating_min=rating_min, reviews_min=reviews_min
    )

    st.success(f"抓取完成：共 {len(df)} 条，过滤后建议投放 {len(kept)} 条，剔除 {len(dropped)} 条。")
    st.subheader("✅ 建议投放（已按相关性得分排序）")
    st.dataframe(kept, use_container_width=True)

    with st.expander("🗃️ 被剔除（供复核）"):
        st.dataframe(dropped, use_container_width=True)

    # 5) 导出 CSV（全量 & 已过滤）
    csv_full = df.to_csv(index=False).encode("utf-8-sig")
    csv_kept = kept.to_csv(index=False).encode("utf-8-sig")
    today = datetime.now().strftime("%Y%m%d")

    st.download_button("📥 下载【全量抓取】CSV",
                       data=csv_full,
                       file_name=f"asin_competitors_full_{seed_asin}_{today}.csv",
                       mime="text/csv",
                       use_container_width=True)
    st.download_button("✅ 下载【已过滤推荐】CSV",
                       data=csv_kept,
                       file_name=f"asin_competitors_filtered_{seed_asin}_{today}.csv",
                       mime="text/csv",
                       use_container_width=True)

st.markdown("---")
st.caption("提示：Keepa REST 更稳定；未配置 Keepa 时使用 HTML 回退可能抓到赞助位/跨类目，建议使用上方相关性过滤器收敛。")
