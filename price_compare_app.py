# -*- coding: utf-8 -*-
"""
MOMO × PChome 商品比價系統 (Streamlit)
執行方式：streamlit run price_compare_app.py
功能：關鍵字/頁數查詢、雙平台比價、同款商品配對、圖片牆瀏覽、多種圖表、CSV 匯出
"""
import html
import re
import time

import numpy as np
import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from curl_cffi import requests as cffi_requests

# ======================================================================
# 設定
# ======================================================================
st.set_page_config(page_title="MOMO × PChome 比價系統", page_icon="🛒", layout="wide")

PLATFORM_MOMO = "MOMO"
PLATFORM_PCHOME = "PChome"
COLOR_MAP = {PLATFORM_MOMO: "#E6007E", PLATFORM_PCHOME: "#F37021"}

# --- MOMO ---
MOMO_API = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
MOMO_HOME = "https://www.momoshop.com.tw/"
MOMO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.momoshop.com.tw",
    "Referer": "https://www.momoshop.com.tw/",
}

# --- PChome ---
PCHOME_API = "https://ecshweb.pchome.com.tw/search/v3.3/all/results"
PCHOME_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PCHOME_IMG_BASE = "https://img.pchome.com.tw/cs"
PCHOME_SORT = {
    "銷量高到低": "sale/dc",
    "價格低到高": "prc/ac",
    "價格高到低": "prc/dc",
    "相關度": "rnk/dc",
}

COLUMNS = ["平台", "品名", "價格", "圖片", "連結", "頁數"]
PLACEHOLDER = (
    "data:image/svg+xml;utf8,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 "
    "width=%27200%27 height=%27200%27%3E%3Crect width=%27100%25%27 height=%27100%25%27 "
    "fill=%27%23eeeeee%27/%3E%3Ctext x=%2750%25%27 y=%2750%25%27 fill=%27%23999999%27 "
    "font-size=%2714%27 text-anchor=%27middle%27%3ENo Image%3C/text%3E%3C/svg%3E"
)


# ======================================================================
# 爬蟲：MOMO
# ======================================================================
def _momo_payload(keyword: str, page: int) -> dict:
    return {
        "host": "momoshop",
        "flag": "searchEngine",
        "data": {
            "searchValue": keyword,
            "searchType": "1",
            "cateLevel": "-1",
            "curPage": str(page),
            "priceS": "0",
            "priceE": "9999999",
            "showType": "chessboardType",
            "isFuzzy": "0",
            "rtnCateDatainfo": {
                "cateCode": "",
                "cateLv": "-1",
                "keyword": keyword,
                "curPage": str(page),
                "historyDoPush": "false",
                "timestamp": int(time.time() * 1000),
            },
            "flag": 2018,
            "serviceCode": "MT01",
            "addressSearchData": {},
        },
    }


@st.cache_resource(show_spinner=False)
def _momo_session():
    """模擬 Chrome TLS 指紋，並先取得 cookie（全程共用一個 session）。"""
    s = cffi_requests.Session(impersonate="chrome")
    s.get(MOMO_HOME, headers=MOMO_HEADERS, timeout=20)
    return s


def _fix_url(url: str, base: str = "") -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base + url
    return url


@st.cache_data(ttl=600, show_spinner=False)
def fetch_momo_page(keyword: str, page: int) -> list[dict]:
    resp = _momo_session().post(
        MOMO_API, headers=MOMO_HEADERS, json=_momo_payload(keyword, page), timeout=20
    )
    if resp.status_code != 200:
        raise RuntimeError(f"MOMO 第 {page} 頁請求失敗：HTTP {resp.status_code}")
    goods = resp.json().get("rtnSearchData", {}).get("goodsInfoList", []) or []
    rows = []
    for p in goods:
        code = p.get("goodsCode", "")
        img = p.get("imgUrl") or p.get("goodsImgUrl") or p.get("goodsImage") or ""
        rows.append(
            {
                "平台": PLATFORM_MOMO,
                "品名": p.get("goodsName", ""),
                "價格": p.get("goodsPrice", ""),
                "圖片": _fix_url(img),
                "連結": f"https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code={code}" if code else "",
                "頁數": page,
            }
        )
    return rows


# ======================================================================
# 爬蟲：PChome
# ======================================================================
@st.cache_data(ttl=600, show_spinner=False)
def fetch_pchome_page(keyword: str, page: int, sort: str) -> list[dict]:
    resp = requests.get(
        PCHOME_API,
        params={"q": keyword, "page": page, "sort": sort},
        headers=PCHOME_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    prods = resp.json().get("prods", []) or []
    rows = []
    for p in prods:
        pid = p.get("Id", "")
        img = p.get("picB") or p.get("picS") or ""
        rows.append(
            {
                "平台": PLATFORM_PCHOME,
                "品名": p.get("name", ""),
                "價格": p.get("price", ""),
                "圖片": _fix_url(img, PCHOME_IMG_BASE),
                "連結": f"https://24h.pchome.com.tw/prod/{pid}" if pid else "",
                "頁數": page,
            }
        )
    return rows


# ======================================================================
# 多頁彙整
# ======================================================================
def crawl_platform(name, fetch_fn, pages, delay, progress, offset, total_steps):
    """依序抓多頁；失敗時回傳已取得的資料與錯誤訊息。"""
    rows, error = [], None
    for i in range(1, pages + 1):
        progress.progress(
            min((offset + i - 1) / total_steps, 1.0), text=f"{name}：正在抓取第 {i} / {pages} 頁..."
        )
        try:
            page_rows = fetch_fn(i)
        except Exception as e:  # noqa: BLE001  單一平台失敗不影響另一個平台
            error = f"{name} 抓取失敗：{e}"
            break
        if not page_rows:
            break
        rows.extend(page_rows)
        if i < pages:
            time.sleep(delay)
    return rows, error


def clean_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS)
    if df.empty:
        return df
    df["價格"] = df["價格"].astype(str).str.replace(r"[^\d]", "", regex=True)
    df = df[(df["價格"] != "") & (df["品名"].astype(str).str.strip() != "")].copy()
    df["價格"] = df["價格"].astype(int)
    df = df.drop_duplicates(subset=["平台", "品名", "價格"]).reset_index(drop=True)
    return df


# ======================================================================
# 資料處理
# ======================================================================
def add_price_range(df: pd.DataFrame, bins: int) -> pd.DataFrame:
    """兩平台共用同一組價格區間；以 95 百分位數為上限避免離群值把區間撐爆。"""
    out = df.copy()
    lo, hi = float(out["價格"].min()), float(out["價格"].max())
    if lo == hi:
        out["價格區間"] = f"{int(lo):,}"
        return out
    cap = max(float(out["價格"].quantile(0.95)), lo + 1)
    edges = np.unique(np.round(np.linspace(lo, cap, bins + 1)).astype(int)).tolist()
    if len(edges) < 2:
        out["價格區間"] = f"{int(lo):,}+"
        return out
    # 區間為 [e_i, e_{i+1})；最後一格為「大於上限」的離群區
    full_edges = edges + [float("inf")]
    labels = [f"{edges[i]:,}~{edges[i + 1] - 1:,}" for i in range(len(edges) - 1)]
    labels.append(f"{edges[-1]:,}+")
    cat = pd.cut(out["價格"], bins=full_edges, labels=labels, right=False, include_lowest=True)
    out["價格區間"] = cat.astype(str)
    out.attrs["range_order"] = labels
    return out


def _clean_name(name: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(name).lower())


def _bigrams(s: str) -> set:
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def match_products(df: pd.DataFrame, threshold: float, limit: int = 200) -> pd.DataFrame:
    """以字元 bigram 的 Dice 相似度，將 MOMO 商品配對到最相近的 PChome 商品。"""
    a = df[df["平台"] == PLATFORM_MOMO].head(limit).reset_index(drop=True)
    b = df[df["平台"] == PLATFORM_PCHOME].head(limit).reset_index(drop=True)
    if a.empty or b.empty:
        return pd.DataFrame()
    grams_b = [_bigrams(_clean_name(n)) for n in b["品名"]]
    results = []
    for _, ra in a.iterrows():
        ga = _bigrams(_clean_name(ra["品名"]))
        best_i, best_s = -1, 0.0
        for j, gb in enumerate(grams_b):
            inter = len(ga & gb)
            if inter == 0:
                continue
            s = 2 * inter / (len(ga) + len(gb))
            if s > best_s:
                best_i, best_s = j, s
        if best_i >= 0 and best_s >= threshold:
            rb = b.iloc[best_i]
            diff = int(ra["價格"]) - int(rb["價格"])
            results.append(
                {
                    "相似度": round(best_s, 2),
                    "MOMO 品名": ra["品名"],
                    "MOMO 價格": int(ra["價格"]),
                    "PChome 品名": rb["品名"],
                    "PChome 價格": int(rb["價格"]),
                    "價差(MOMO-PChome)": diff,
                    "較便宜": PLATFORM_PCHOME if diff > 0 else (PLATFORM_MOMO if diff < 0 else "相同"),
                    "MOMO 連結": ra["連結"],
                    "PChome 連結": rb["連結"],
                }
            )
    out = pd.DataFrame(results)
    if not out.empty:
        out = out.sort_values("相似度", ascending=False).reset_index(drop=True)
    return out


# ======================================================================
# 圖片牆
# ======================================================================
GALLERY_CSS = """
<style>
.pc-grid{display:grid;gap:14px;}
.pc-card{border:1px solid rgba(128,128,128,.3);border-radius:12px;overflow:hidden;
  background:rgba(128,128,128,.06);transition:transform .15s ease, box-shadow .15s ease;
  display:flex;flex-direction:column;}
.pc-card:hover{transform:translateY(-4px);box-shadow:0 8px 20px rgba(0,0,0,.25);}
.pc-img{position:relative;aspect-ratio:1/1;background:#fff;overflow:hidden;}
.pc-img img{width:100%;height:100%;object-fit:contain;transition:transform .25s ease;}
.pc-card:hover .pc-img img{transform:scale(1.08);}
.pc-badge{position:absolute;top:8px;left:8px;color:#fff;font-size:12px;font-weight:600;
  padding:2px 8px;border-radius:999px;}
.pc-body{padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;}
.pc-price{font-size:20px;font-weight:700;color:#e5484d;}
.pc-name{font-size:13px;line-height:1.4;height:3.6em;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;}
.pc-name a{color:inherit;text-decoration:none;}
.pc-name a:hover{text-decoration:underline;}
</style>
"""


def render_gallery(df: pd.DataFrame, cols: int):
    cards = []
    for _, r in df.iterrows():
        name = html.escape(str(r["品名"]))
        img = html.escape(str(r["圖片"] or PLACEHOLDER), quote=True)
        link = html.escape(str(r["連結"]), quote=True)
        color = COLOR_MAP.get(r["平台"], "#666")
        title = f'<a href="{link}" target="_blank" rel="noopener">{name}</a>' if link else name
        cards.append(
            f'<div class="pc-card"><div class="pc-img">'
            f'<img src="{img}" loading="lazy" referrerpolicy="no-referrer" alt="" '
            f'onerror="this.onerror=null;this.src=\'{PLACEHOLDER}\'">'
            f'<span class="pc-badge" style="background:{color}">{html.escape(r["平台"])}</span></div>'
            f'<div class="pc-body"><div class="pc-price">NT$ {int(r["價格"]):,}</div>'
            f'<div class="pc-name">{title}</div></div></div>'
        )
    grid = f'<div class="pc-grid" style="grid-template-columns:repeat({cols},1fr)">{"".join(cards)}</div>'
    st.markdown(GALLERY_CSS + grid, unsafe_allow_html=True)


# ======================================================================
# 側邊欄
# ======================================================================
st.title("🛒 MOMO × PChome 商品比價系統")

with st.sidebar:
    st.header("🔎 查詢條件")
    keyword = st.text_input("商品關鍵字", value="行動電源", placeholder="例如：行動電源、冰箱、耳機")
    platforms = st.multiselect(
        "比價平台", [PLATFORM_MOMO, PLATFORM_PCHOME], default=[PLATFORM_MOMO, PLATFORM_PCHOME]
    )
    pages = st.number_input("爬取頁數（每個平台）", min_value=1, max_value=20, value=2, step=1)
    sort_label = st.selectbox("PChome 排序方式", list(PCHOME_SORT.keys()))
    delay = st.slider("每頁間隔秒數", 0.0, 5.0, 1.0, 0.5, help="間隔太短可能被網站暫時封鎖")
    bins = st.slider("價格區間數量", 3, 10, 5)
    run = st.button("🔍 開始查詢", type="primary", use_container_width=True)

# ======================================================================
# 執行查詢
# ======================================================================
if run:
    kw_in = keyword.strip()
    if not kw_in:
        st.warning("請輸入商品關鍵字。")
        st.stop()
    if not platforms:
        st.warning("請至少選擇一個平台。")
        st.stop()

    progress = st.progress(0.0, text="開始抓取資料...")
    total_steps = int(pages) * len(platforms)
    all_rows, errors, step = [], [], 0
    if PLATFORM_MOMO in platforms:
        r, e = crawl_platform(
            "MOMO", lambda p: fetch_momo_page(kw_in, p), int(pages), delay, progress, step, total_steps
        )
        all_rows += r
        errors += [e] if e else []
        step += int(pages)
    if PLATFORM_PCHOME in platforms:
        sort_val = PCHOME_SORT[sort_label]
        r, e = crawl_platform(
            "PChome",
            lambda p: fetch_pchome_page(kw_in, p, sort_val),
            int(pages), delay, progress, step, total_steps,
        )
        all_rows += r
        errors += [e] if e else []
    progress.empty()

    df_new = clean_df(all_rows)
    st.session_state["result"] = {"keyword": kw_in, "df": df_new, "errors": errors}

if "result" not in st.session_state:
    st.info("👈 請在左側輸入關鍵字與爬取頁數，選擇平台後按下「開始查詢」。")
    st.stop()

res = st.session_state["result"]
kw, df_all, errors = res["keyword"], res["df"], res["errors"]

for msg in errors:
    st.warning(msg)
if df_all.empty:
    st.error(f"找不到「{kw}」的商品資料，請換個關鍵字或稍後再試。")
    st.stop()

# ======================================================================
# 結果頁
# ======================================================================
st.subheader(f"「{kw}」查詢結果，共 {len(df_all):,} 筆")

tab_cmp, tab_chart, tab_gallery, tab_match, tab_table = st.tabs(
    ["⚖️ 平台比價", "📊 圖表分析", "🖼️ 圖片瀏覽", "🔗 同款比價", "📋 資料表"]
)

# ---------------------------------------------------------------- 平台比價
with tab_cmp:
    stats = (
        df_all.groupby("平台")["價格"]
        .agg(商品數="count", 平均價="mean", 中位數="median", 最低價="min", 最高價="max")
        .round(0)
        .astype(int)
        .reset_index()
    )
    cols = st.columns(len(stats))
    for col, (_, row) in zip(cols, stats.iterrows()):
        with col:
            st.markdown(f"#### {row['平台']}")
            m1, m2 = st.columns(2)
            m1.metric("商品數", f"{row['商品數']:,}")
            m2.metric("平均價", f"{row['平均價']:,}")
            m3, m4 = st.columns(2)
            m3.metric("最低價", f"{row['最低價']:,}")
            m4.metric("中位數", f"{row['中位數']:,}")

    if len(stats) == 2:
        a, b = stats.iloc[0], stats.iloc[1]
        cheaper = a if a["中位數"] < b["中位數"] else b
        diff = abs(int(a["中位數"]) - int(b["中位數"]))
        if diff == 0:
            st.info("兩平台的中位數價格相同。")
        else:
            st.success(f"以中位數價格來看，**{cheaper['平台']}** 整體較便宜，差距約 NT$ {diff:,}。")

    c_left, c_right = st.columns(2)
    with c_left:
        fig = px.box(
            df_all, x="平台", y="價格", color="平台", color_discrete_map=COLOR_MAP,
            points="outliers", hover_name="品名", title="各平台價格分布（箱型圖）",
        )
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
    with c_right:
        fig = px.histogram(
            df_all, x="價格", color="平台", color_discrete_map=COLOR_MAP,
            barmode="overlay", opacity=0.65, nbins=30, title="價格直方圖",
        )
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("##### 💰 全部商品中最便宜的 10 件")
    cheapest = df_all.nsmallest(10, "價格")[["平台", "品名", "價格", "圖片", "連結"]]
    st.dataframe(
        cheapest, use_container_width=True, hide_index=True,
        column_config={
            "圖片": st.column_config.ImageColumn("圖片", width="small"),
            "連結": st.column_config.LinkColumn("連結", display_text="開啟"),
            "價格": st.column_config.NumberColumn("價格", format="NT$ %d"),
        },
    )

# ---------------------------------------------------------------- 圖表分析
with tab_chart:
    chart_type = st.radio("圖表類型", ["折線圖", "圓餅圖", "旭日圖"], horizontal=True)
    ranged = add_price_range(df_all, bins)
    order = ranged.attrs.get("range_order")

    if chart_type == "折線圖":
        sort_price = st.checkbox("依價格由低到高排序", value=False)
        d = df_all.copy()
        if sort_price:
            d = d.sort_values(["平台", "價格"])
        d["編號"] = d.groupby("平台").cumcount() + 1
        fig = px.line(
            d, x="編號", y="價格", color="平台", color_discrete_map=COLOR_MAP,
            markers=True, hover_name="品名",
            title=f"「{kw}」商品價格走勢" + ("（由低到高）" if sort_price else "（依搜尋順序）"),
        )
        for p_name, g in df_all.groupby("平台"):
            fig.add_hline(
                y=g["價格"].mean(), line_dash="dash", line_color=COLOR_MAP.get(p_name),
                annotation_text=f"{p_name} 平均 {g['價格'].mean():,.0f}",
                annotation_position="top left",
            )
        fig.update_layout(xaxis_title="商品編號", yaxis_title="價格 (NT$)")
        st.plotly_chart(fig, use_container_width=True)

    elif chart_type == "圓餅圖":
        platform_sel = st.selectbox("選擇平台", ["全部"] + sorted(df_all["平台"].unique()))
        src = ranged if platform_sel == "全部" else ranged[ranged["平台"] == platform_sel]
        pie_df = src.groupby("價格區間").size().reset_index(name="商品數")
        if order:
            pie_df["價格區間"] = pd.Categorical(pie_df["價格區間"], categories=order, ordered=True)
            pie_df = pie_df.sort_values("價格區間")
        fig = px.pie(pie_df, names="價格區間", values="商品數", hole=0.3,
                     title=f"「{kw}」價格區間分布（{platform_sel}）")
        fig.update_traces(textinfo="percent+label+value", sort=False)
        st.plotly_chart(fig, use_container_width=True)

    else:
        sun = ranged.copy()
        sun["根"] = f"{kw}（全部）"
        sun["商品"] = sun.index.astype(str) + ". " + sun["品名"].str.slice(0, 18)
        level = st.radio("顯示層級", ["平台 → 價格區間", "平台 → 價格區間 → 商品"], horizontal=True)
        if level == "平台 → 價格區間":
            g = sun.groupby(["根", "平台", "價格區間"]).size().reset_index(name="商品數")
            fig = px.sunburst(g, path=["根", "平台", "價格區間"], values="商品數",
                              color="平台", color_discrete_map=COLOR_MAP,
                              title="平台 → 價格區間（面積＝商品數）")
        else:
            fig = px.sunburst(sun, path=["根", "平台", "價格區間", "商品"], values="價格",
                              color="平台", color_discrete_map=COLOR_MAP,
                              title="平台 → 價格區間 → 商品（面積＝價格）")
        fig.update_layout(height=700)
        st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------- 圖片瀏覽
with tab_gallery:
    f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
    g_platforms = f1.multiselect(
        "平台", sorted(df_all["平台"].unique()), default=sorted(df_all["平台"].unique()), key="g_pf"
    )
    g_text = f2.text_input("品名包含", key="g_text", placeholder="輸入關鍵字過濾")
    g_sort = f3.selectbox("排序", ["預設", "價格低到高", "價格高到低"], key="g_sort")
    g_cols = f4.select_slider("每列張數", [3, 4, 5, 6], value=4, key="g_cols")

    lo, hi = int(df_all["價格"].min()), int(df_all["價格"].max())
    if lo < hi:
        g_price = st.slider("價格範圍 (NT$)", lo, hi, (lo, hi), key="g_price")
    else:
        g_price = (lo, hi)

    gdf = df_all[df_all["平台"].isin(g_platforms)]
    gdf = gdf[gdf["價格"].between(*g_price)]
    if g_text.strip():
        gdf = gdf[gdf["品名"].str.contains(g_text.strip(), case=False, regex=False)]
    if g_sort == "價格低到高":
        gdf = gdf.sort_values("價格")
    elif g_sort == "價格高到低":
        gdf = gdf.sort_values("價格", ascending=False)

    if gdf.empty:
        st.info("沒有符合條件的商品。")
    else:
        if gdf["圖片"].eq("").all():
            st.caption("⚠️ 此次查詢沒有取得圖片網址，將顯示佔位圖。")
        p1, p2 = st.columns([1, 1])
        per_page = p1.selectbox("每頁顯示", [12, 24, 48, 96], index=1, key="g_per")
        total_pages = max(1, -(-len(gdf) // per_page))
        cur = p2.number_input(f"頁碼（共 {total_pages} 頁，{len(gdf):,} 件）", 1, total_pages, 1, key="g_cur")
        chunk = gdf.iloc[(cur - 1) * per_page : cur * per_page]
        render_gallery(chunk, g_cols)

        st.divider()
        st.markdown("##### 🔍 放大檢視")
        options = {f"[{r['平台']}] NT$ {r['價格']:,}｜{r['品名'][:40]}": i for i, r in chunk.iterrows()}
        pick = st.selectbox("選擇本頁商品", list(options.keys()), key="g_pick")
        item = chunk.loc[options[pick]]
        d1, d2 = st.columns([1, 2])
        with d1:
            if item["圖片"]:
                st.image(item["圖片"], use_container_width=True)
            else:
                st.info("無圖片")
        with d2:
            st.markdown(f"**{item['品名']}**")
            st.metric(item["平台"], f"NT$ {int(item['價格']):,}")
            if item["連結"]:
                st.link_button("前往商品頁", item["連結"])

# ---------------------------------------------------------------- 同款比價
with tab_match:
    if set(df_all["平台"].unique()) != {PLATFORM_MOMO, PLATFORM_PCHOME}:
        st.info("需要同時有 MOMO 與 PChome 的資料才能做同款配對，請在左側勾選兩個平台後重新查詢。")
    else:
        st.caption("以品名相似度，將每件 MOMO 商品配對到最相近的 PChome 商品。結果為估算，規格不同的商品可能被誤配，請點連結確認。")
        thr = st.slider("相似度門檻", 0.3, 0.95, 0.55, 0.05)
        matched = match_products(df_all, thr)
        if matched.empty:
            st.info("在此門檻下找不到配對，試著調低相似度門檻。")
        else:
            n_momo = int((matched["較便宜"] == PLATFORM_MOMO).sum())
            n_pc = int((matched["較便宜"] == PLATFORM_PCHOME).sum())
            k1, k2, k3 = st.columns(3)
            k1.metric("配對成功", f"{len(matched)} 組")
            k2.metric("MOMO 較便宜", f"{n_momo} 組")
            k3.metric("PChome 較便宜", f"{n_pc} 組")
            st.dataframe(
                matched, use_container_width=True, hide_index=True,
                column_config={
                    "MOMO 連結": st.column_config.LinkColumn("MOMO 連結", display_text="開啟"),
                    "PChome 連結": st.column_config.LinkColumn("PChome 連結", display_text="開啟"),
                    "MOMO 價格": st.column_config.NumberColumn(format="NT$ %d"),
                    "PChome 價格": st.column_config.NumberColumn(format="NT$ %d"),
                    "價差(MOMO-PChome)": st.column_config.NumberColumn(format="%+d"),
                },
            )
            st.download_button(
                "⬇️ 下載同款比價 CSV",
                matched.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"比價_{kw}_{time.strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )

# ---------------------------------------------------------------- 資料表
with tab_table:
    t_pf = st.multiselect("平台", sorted(df_all["平台"].unique()),
                          default=sorted(df_all["平台"].unique()), key="t_pf")
    tdf = df_all[df_all["平台"].isin(t_pf)]
    st.dataframe(
        tdf, use_container_width=True, hide_index=True,
        column_config={
            "圖片": st.column_config.ImageColumn("圖片", width="small"),
            "連結": st.column_config.LinkColumn("連結", display_text="開啟"),
            "價格": st.column_config.NumberColumn("價格", format="NT$ %d"),
        },
    )
    st.download_button(
        "⬇️ 下載 CSV",
        tdf.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"比價資料_{kw}_{time.strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )
