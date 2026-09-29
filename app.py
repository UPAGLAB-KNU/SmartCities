import json
from datetime import datetime

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

import smartcity_core as core

st.set_page_config(page_title="스마트도시 서비스 수준", layout="wide")

FORMULA = (
    "Min-Max = 지표별 0~100 환산(역방향 반전), "
    "T = 50 + 10Z, T 합산용 점수 S = clip(100×(T−20)/(80−20), 0, 100). "
    "분야·종합은 L열 가중치의 가중평균으로 산출하고, T 방식은 가중평균 후 다시 T점수화"
)


@st.cache_data(ttl=600, show_spinner="구글시트에서 데이터를 읽는 중...")
def fetch(sheet_id):
    return core.build_base(sheet_id), datetime.now()


# ── 점수 계산 호환 계층
# app.py만 먼저 배포되어 예전 smartcity_core.py가 남아 있어도 앱이 중단되지 않도록
# 시군구 점수 계산에 필요한 함수를 app.py 안에도 보유합니다.
def _safe_z(s):
    s = pd.to_numeric(s, errors="coerce")
    sd = s.std(ddof=1)
    if pd.isna(sd) or sd == 0:
        return pd.Series(np.where(s.notna(), 0.0, np.nan), index=s.index, dtype="float64")
    return (s - s.mean()) / sd


def _add_all_scores_compat(long, t_lo=20.0, t_hi=80.0):
    out = long.copy()
    if "가중치" not in out.columns:
        out["가중치"] = 1.0
    out["가중치"] = pd.to_numeric(out["가중치"], errors="coerce").fillna(1.0).clip(lower=0)
    if "방향" not in out.columns:
        out["방향"] = 1.0
    out["방향"] = pd.to_numeric(out["방향"], errors="coerce").fillna(1.0)

    # 지표별 Min-Max (상수 지표는 중립 50)
    g = out.groupby("지표명", observed=True)["원자료"]
    lo = g.transform("min")
    hi = g.transform("max")
    rng = hi - lo
    mm = 100 * (out["원자료"] - lo) / rng.replace(0, np.nan)
    mm = pd.Series(np.where(out["방향"] < 0, 100 - mm, mm), index=out.index, dtype="float64")
    mm.loc[out["원자료"].notna() & rng.eq(0)] = 50.0
    out["MinMax점수"] = mm.clip(0, 100)
    out["MinMax백분위"] = out.groupby("지표명", observed=True)["MinMax점수"].rank(pct=True) * 100

    # 지표별 T점수 (상수/1개 표본은 중립 50)
    mean = g.transform("mean")
    sd = g.transform("std")
    z = (out["원자료"] - mean) / sd.replace(0, np.nan)
    neutral = out["원자료"].notna() & (sd.isna() | sd.eq(0))
    z.loc[neutral] = 0.0
    out["Z점수"] = z * out["방향"]
    out["T점수"] = 50 + 10 * out["Z점수"]
    out["백분위"] = out.groupby("지표명", observed=True)["T점수"].rank(pct=True) * 100

    # 분야·종합 계산용: T=20→0, 50→50, 80→100, 그 밖은 0~100 절단
    out["T합산점수"] = (100 * (out["T점수"] - t_lo) / (t_hi - t_lo)).clip(0, 100)
    return out


def _weighted_score(g, value_col):
    x = pd.to_numeric(g[value_col], errors="coerce")
    w = pd.to_numeric(g.get("가중치", 1.0), errors="coerce").fillna(1.0)
    ok = x.notna() & w.notna() & (w > 0)
    if not ok.any() or w.loc[ok].sum() <= 0:
        return pd.Series({"점수": np.nan, "지표수": 0, "가중치합": 0.0})
    return pd.Series({"점수": np.average(x.loc[ok], weights=w.loc[ok]),
                      "지표수": int(ok.sum()), "가중치합": float(w.loc[ok].sum())})


def _re_t(df, raw_col, by=None, out_col="T점수"):
    out = df.copy()
    pct_col = out_col[:-2] + "백분위" if out_col.endswith("점수") else out_col + "_백분위"
    if by is None:
        out[out_col] = 50 + 10 * _safe_z(out[raw_col])
        out[pct_col] = out[out_col].rank(pct=True) * 100
        return out
    parts = []
    for _, g in out.groupby(by, observed=True, dropna=False):
        g = g.copy()
        g[out_col] = 50 + 10 * _safe_z(g[raw_col])
        g[pct_col] = g[out_col].rank(pct=True) * 100
        parts.append(g)
    return pd.concat(parts, ignore_index=True) if parts else out


def _build_sgg_scores_compat(long):
    detail = _add_all_scores_compat(long)

    mmf = (detail.groupby(["지역", "대분류"], observed=True)
           .apply(lambda g: _weighted_score(g, "MinMax점수"))
           .reset_index()
           .rename(columns={"점수":"MinMax분야점수", "지표수":"MinMax지표수", "가중치합":"MinMax가중치합"}))
    tf = (detail.groupby(["지역", "대분류"], observed=True)
          .apply(lambda g: _weighted_score(g, "T합산점수"))
          .reset_index()
          .rename(columns={"점수":"T분야원점수", "지표수":"T지표수", "가중치합":"T가중치합"}))
    field = mmf.merge(tf, on=["지역", "대분류"], how="outer")
    field = _re_t(field, "T분야원점수", by="대분류", out_col="T분야점수")

    id_cols = [c for c in ["지역","시도명","시군구명","인구규모","인구규모군","유형구분","권역"] if c in long.columns]
    ids = long.drop_duplicates("지역")[id_cols]
    field = field.merge(ids, on="지역", how="left")

    mmt = (detail.groupby("지역", observed=True)
           .apply(lambda g: _weighted_score(g, "MinMax점수"))
           .reset_index()
           .rename(columns={"점수":"MinMax종합점수", "지표수":"MinMax지표수", "가중치합":"MinMax가중치합"}))
    tt = (detail.groupby("지역", observed=True)
          .apply(lambda g: _weighted_score(g, "T합산점수"))
          .reset_index()
          .rename(columns={"점수":"T종합원점수", "지표수":"T지표수", "가중치합":"T가중치합"}))
    total = mmt.merge(tt, on="지역", how="outer")
    total = _re_t(total, "T종합원점수", out_col="T종합점수")
    total = total.merge(ids, on="지역", how="left")
    return detail.reset_index(drop=True), field.reset_index(drop=True), total.reset_index(drop=True)


def add_all_scores_any(long):
    if hasattr(core, "add_all_scores"):
        return core.add_all_scores(long)
    return _add_all_scores_compat(long)


@st.cache_data(show_spinner=False)
def score_sgg(raw):
    if hasattr(core, "build_sgg_scores"):
        return core.build_sgg_scores(raw)
    return _build_sgg_scores_compat(raw)


def load(sheet_id):
    try:
        pack, ts = fetch(sheet_id)
        st.session_state["last_good"] = (pack, ts)
        return pack, ts, None
    except Exception as e:
        if "last_good" in st.session_state:
            pack, ts = st.session_state["last_good"]
            return pack, ts, e
        return None, None, e


@st.cache_data
def load_geo():
    with open("sgg_korea.geojson", encoding="utf-8") as f:
        return json.load(f)


def filter_sgg_group(df, raw, group, sido, target):
    """점수 자체는 전국 기준으로 고정하고, 비교집단은 화면 표시용 필터로만 사용."""
    if group == "전국":
        return df
    if group == "동일 시도":
        return df[df["시도명"] == sido] if sido != "전체" else df
    if group == "인구규모 유사지역":
        if target:
            hit = raw.loc[raw["지역"] == target, "인구규모군"]
            if not hit.empty:
                return df[df["인구규모군"] == hit.iloc[0]]
        return df
    return df[df["유형구분"] == group]


def fmt_score(v):
    return "-" if pd.isna(v) else f"{v:,.1f}"


sheet_id = st.secrets.get("SHEET_ID", "")
if not sheet_id:
    st.error("Streamlit Secrets에 SHEET_ID를 등록하세요.")
    st.stop()

pack, ts, err = load(sheet_id)
if pack is None:
    st.error(f"시트 읽기 실패 — 사유: {err}")
    st.stop()
raw, denom, sido_actual = pack

# 시군구 점수는 전국 시군구 전체를 기준으로 한 번만 계산
sgg_detail, sgg_field, sgg_total = score_sgg(raw)

h1, h2 = st.columns([5, 1])
with h1:
    st.title("스마트도시 서비스 수준 대시보드")
    st.caption(f"데이터 기준: {ts:%Y-%m-%d %H:%M:%S}")
with h2:
    if st.button("🔄 새로 읽기", use_container_width=True):
        fetch.clear()
        score_sgg.clear()
        st.rerun()
if err is not None:
    st.warning(f"시트 읽기 실패 — 마지막 정상 데이터({ts:%H:%M:%S}) 표시 중. 사유: {err}")

# ── 1행: 분야·지표·표준화 방식
c1, c2, c3 = st.columns([2, 3, 2])
cat = c1.selectbox("대분류", ["전체"] + sorted(raw["대분류"].dropna().unique()))
pool = raw if cat == "전체" else raw[raw["대분류"] == cat]
ind = c2.selectbox("지표", sorted(pool["지표명"].unique()))
score_method = c3.radio("점수 표준화 방식", ["Min-Max", "T점수"], horizontal=True)
score_col = "MinMax점수" if score_method == "Min-Max" else "T점수"
pct_col = "MinMax백분위" if score_method == "Min-Max" else "백분위"

# ── 2행: 집계 수준
level = st.selectbox("집계 수준",
                     ["시군구별", "시도별", "도시규모별", "수도권-비수도권"])
level_col = core.LEVELS[level]
is_sgg = level_col is None

if not is_sgg:
    st.info("시도·도시규모·수도권/비수도권의 최종 점수 집계 방식은 다음 단계에서 개편 예정입니다. "
            "현재 화면은 기존 원자료 집계값에 선택한 표준화 방식을 적용한 임시 비교입니다.")

# ── 3행: 지역·비교집단
c4, c5, c6 = st.columns([2, 2, 2])
if is_sgg:
    sido = c4.selectbox("시도", ["전체"] + sorted(raw["시도명"].dropna().unique()))
    sgg_opts = ["전체"] + (sorted(raw[raw["시도명"] == sido]["시군구명"].dropna().unique())
                          if sido != "전체" else [])
    sgg = c5.selectbox("시군구", sgg_opts, disabled=(sido == "전체"))
    target = f"{sido} {sgg}" if (sido != "전체" and sgg != "전체") else None
    label = sgg
    group = c6.selectbox("비교집단",
                         ["전국", "동일 시도", "특별·광역시", "시 지역", "군 지역",
                          "인구규모 유사지역"])
else:
    units = [u for u in raw[level_col].dropna().unique()]
    target = c4.selectbox(level.replace("별", ""), ["전체"] + list(units))
    target = None if target == "전체" else target
    label = target
    c5.empty()
    group = "전체"
    c6.caption(f"{level} 집계 · 비교집단은 전체 {len(units)}개 단위")

# ── 점수 데이터 준비
if is_sgg:
    base_all = sgg_detail
    subset = filter_sgg_group(base_all, raw, group, sido, target)
    if target and target not in subset["지역"].values:
        st.warning(f"{target}는 '{group}'에 없어 전국 표시로 전환합니다.")
        subset, group = base_all, "전국"
    base = subset
else:
    agg = core.aggregate(raw, level_col, denom, sido_actual)
    base_all = add_all_scores_any(agg)
    base = base_all

sub = base[base["지표명"] == ind].dropna(subset=[score_col])

cap = f"{level} · 비교집단 {group} · 유효 {len(sub)}개 / 전체 {base['지역'].nunique()}개"
if is_sgg:
    cap += " · 점수 산정 기준: 전국 시군구"
else:
    if "출처" in sub.columns:
        n_real = int((sub["출처"] == "실측").sum())
        if n_real:
            cap += f" · 실측 {n_real}개"
        how = sub["집계방식"].iloc[0] if len(sub) else ""
        cap += f" · 집계방식: {how}"
st.caption(cap)

if sub.empty:
    st.info("이 지표는 현재 값 기준으로 표시할 점수가 없습니다.")
    st.stop()

# ── 요약통계
q = sub[score_col].describe()
cols = st.columns(6 if target else 5)
for i, (lab, v) in enumerate([("최소", q["min"]), ("25%", q["25%"]), ("중앙값", q["50%"]),
                              ("75%", q["75%"]), ("평균", q["mean"])]):
    cols[i].metric(lab, f"{v:,.1f}")

mine = sub[sub["지역"] == target] if target else pd.DataFrame()
if target and not mine.empty:
    val = float(mine[score_col].iloc[0])
    pct = float(mine[pct_col].iloc[0])
    cols[5].metric(label, f"{val:,.1f}", f"상위 {100-pct:.0f}%")
elif target:
    cols[5].metric(label, "값 없음")
    val = None
else:
    val = None

# ── 그래프 표시 상한
if score_method == "T점수":
    CUT = st.select_slider("그래프 상한 묶기 기준 (T점수)",
                           options=[55, 60, 65, 70, 75, 80, 90, 100], value=70)
    cut_label = f"{CUT}"
else:
    CUT = 100.0
    cut_label = "100"

# ── 분포 + 지도
left, right = st.columns([1, 1.4])

with left:
    st.subheader("전국 분포" if is_sgg else f"{level} 비교")

    if is_sgg:
        plot_x = sub[score_col].clip(upper=CUT)
        n_over = int((sub[score_col] > CUT).sum())
        lo, hi = float(plot_x.min()), float(plot_x.max())
        step = (hi - lo) / 60 or 1
        fig = go.Figure()
        fig.add_histogram(x=plot_x, marker_color="#B7CDEB", autobinx=False,
                          xbins=dict(start=lo, end=hi + step, size=step))
        if score_method == "T점수":
            fig.add_vline(x=50, line_dash="dash", line_color="gray",
                          annotation_text="평균 50")
        if n_over:
            fig.add_annotation(x=CUT, y=1, yref="paper", yanchor="bottom",
                               text=f"{cut_label}↑ {n_over}곳", showarrow=False,
                               font=dict(size=11, color="#666"))
        if val is not None:
            fig.add_vline(x=min(val, CUT), line_color="#1F4E9C", line_width=3,
                          annotation_text=label, annotation_position="top")
        fig.update_layout(height=640, bargap=0.05, showlegend=False,
                          xaxis_title=score_method, yaxis_title="지역 수",
                          margin=dict(t=40, b=40))
    else:
        d = sub.sort_values(score_col, ascending=True)
        pivot = 50 if score_method == "T점수" else d[score_col].median()
        colors = ["#D62728" if r == target else
                  ("#1F4E9C" if v >= pivot else "#9BB8DE")
                  for r, v in zip(d["지역"], d[score_col])]
        fig = go.Figure(go.Bar(
            x=d[score_col], y=d["지역"], orientation="h", marker_color=colors,
            text=[f"{v:,.1f}" for v in d[score_col]], textposition="outside",
            customdata=np.stack([d["구성지역수"], d["출처"]], axis=-1),
            hovertemplate="<b>%{y}</b><br>%{x:.2f}"
                          "<br>구성 %{customdata[0]}곳 · %{customdata[1]}<extra></extra>"))
        if score_method == "T점수":
            fig.add_vline(x=50, line_dash="dash", line_color="gray")
        fig.update_layout(height=max(400, 45 * len(d) + 80), xaxis_title=score_method,
                          showlegend=False, margin=dict(t=40, b=40, l=10))
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("공간분포")
    geo = load_geo()

    cols_need = ["지역", score_col, "원자료", "MinMax점수", "T점수", pct_col]
    cols_need = list(dict.fromkeys(cols_need))

    if is_sgg:
        pmap = sub[cols_need].copy()
        pmap["단위"] = pmap["지역"]
    else:
        key = (raw.drop_duplicates("지역")[["지역", level_col]]
               .rename(columns={"지역": "시군구", level_col: "단위"}))
        pmap = (key.merge(sub[cols_need].rename(columns={"지역": "단위"}),
                          on="단위", how="inner")
                .rename(columns={"시군구": "지역"}))

    zmax = CUT
    zmin = 20 if score_method == "T점수" else 0
    fig3 = go.Figure(go.Choropleth(
        geojson=geo, locations=pmap["지역"], z=pmap[score_col].clip(zmin, zmax),
        featureidkey="properties.지역",
        colorscale="Blues", zmin=zmin, zmax=zmax,
        marker_line_color="#8c8c8c", marker_line_width=0.5,
        colorbar=dict(title=score_method, thickness=12, len=0.6, x=0.93, y=0.35),
        customdata=np.stack([pmap["원자료"], pmap["MinMax점수"], pmap["T점수"], pmap[pct_col]], axis=-1),
        hovertemplate="<b>%{location}</b><br>원자료 %{customdata[0]:.2f}"
                      "<br>Min-Max %{customdata[1]:.1f}"
                      "<br>T점수 %{customdata[2]:.1f}"
                      "<br>백분위 %{customdata[3]:.0f}<extra></extra>"))

    miss = set(f["properties"]["지역"] for f in geo["features"]) - set(pmap["지역"])
    if miss:
        fig3.add_trace(go.Choropleth(
            geojson=geo, locations=list(miss), z=[0] * len(miss),
            featureidkey="properties.지역",
            colorscale=[[0, "#ffffff"], [1, "#ffffff"]], showscale=False,
            marker_line_color="#8c8c8c", marker_line_width=0.5,
            hovertemplate="<b>%{location}</b><br>자료 없음<extra></extra>"))

    if target:
        hl = pmap.loc[pmap["단위"] == target, "지역"].tolist()
        if hl:
            fig3.add_trace(go.Choropleth(
                geojson=geo, locations=hl, z=[1] * len(hl),
                featureidkey="properties.지역",
                colorscale=[[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,0)"]],
                showscale=False, marker_line_color="#D62728",
                marker_line_width=2.5, hoverinfo="skip"))

    fig3.update_geos(visible=False, projection_type="transverse mercator",
                     projection_rotation_lon=127.5,
                     lonaxis_range=[124.5, 131.0], lataxis_range=[33.0, 38.7],
                     domain=dict(x=[0, 1], y=[0, 1]), bgcolor="rgba(0,0,0,0)")
    fig3.update_layout(height=640, margin=dict(l=0, r=0, t=40, b=0))
    st.plotly_chart(fig3, use_container_width=True)

# ── 시군구 분야·종합 점수표
if is_sgg:
    st.divider()
    st.subheader("분야별·종합 점수")
    st.caption("Min-Max와 T 방식은 항상 함께 계산됩니다. L열 가중치가 분야점수와 종합점수에 직접 적용되며, "
               "현재 가중치가 모두 1이면 단순평균과 같습니다. 종합점수는 분야 평균이 아니라 전체 개별지표의 가중평균입니다.")

    if target:
        ft = sgg_field[sgg_field["지역"] == target].copy()
        tt = sgg_total[sgg_total["지역"] == target].copy()
        score_rows = pd.DataFrame({
            "구분": ft["대분류"],
            "Min-Max 점수": ft["MinMax분야점수"],
            "T 합산원점수(0~100)": ft["T분야원점수"],
            "T 점수": ft["T분야점수"],
            "지표수": ft["T지표수"],
            "가중치합": ft["T가중치합"],
        })
        if not tt.empty:
            r = tt.iloc[0]
            score_rows = pd.concat([score_rows, pd.DataFrame([{
                "구분": "종합",
                "Min-Max 점수": r["MinMax종합점수"],
                "T 합산원점수(0~100)": r["T종합원점수"],
                "T 점수": r["T종합점수"],
                "지표수": r["T지표수"],
                "가중치합": r["T가중치합"],
            }])], ignore_index=True)

        sort_order = {name: i for i, name in enumerate(sorted(ft["대분류"].dropna().unique()))}
        sort_order["종합"] = 999
        score_rows["_order"] = score_rows["구분"].map(sort_order).fillna(998)
        score_rows = score_rows.sort_values("_order").drop(columns="_order")

        overall = tt.iloc[0] if not tt.empty else None
        m1, m2, m3 = st.columns(3)
        if overall is not None:
            m1.metric("Min-Max 종합점수", fmt_score(overall["MinMax종합점수"]))
            m2.metric("T 종합 원점수", fmt_score(overall["T종합원점수"]))
            m3.metric("T 종합점수", fmt_score(overall["T종합점수"]))
        st.dataframe(score_rows.style.format({
            "Min-Max 점수": "{:.1f}", "T 합산원점수(0~100)": "{:.1f}",
            "T 점수": "{:.1f}", "가중치합": "{:.1f}"
        }), use_container_width=True, hide_index=True)
    else:
        totals = (sgg_total[["지역", "시도명", "시군구명", "MinMax종합점수",
                             "T종합원점수", "T종합점수", "T종합백분위",
                             "T지표수", "T가중치합"]]
                  .sort_values("T종합점수", ascending=False))
        st.caption("지역을 선택하면 분야별 점수가 표시됩니다. 아래는 전체 시군구 종합점수입니다.")
        st.dataframe(totals.style.format({
            "MinMax종합점수": "{:.1f}", "T종합원점수": "{:.1f}",
            "T종합점수": "{:.1f}", "T종합백분위": "{:.1f}", "T가중치합": "{:.1f}"
        }), use_container_width=True, hide_index=True, height=430)

# ── 클래스별 시군구 분포 히트맵 (임시 비교 화면)
if not is_sgg:
    st.divider()
    st.subheader(f"{level} 시군구 분포")

    hv = sgg_detail[sgg_detail["지표명"] == ind].dropna(subset=[score_col]).copy()
    hv = hv[hv[level_col].notna()]
    hv["클래스"] = hv[level_col].astype(str)

    hc1, hc2, hc3 = st.columns(3)
    if score_method == "T점수":
        bw = hc1.select_slider("급간 폭 (T점수)", options=[1, 2, 5, 10], value=2)
    else:
        nbin = hc1.select_slider("급간 수", options=[10, 15, 20, 30, 40], value=20)
    norm = hc2.radio("색상 기준", ["시군구 수", "클래스 내 비율(%)"], horizontal=True)
    if score_method == "T점수":
        hcut = float(hc3.select_slider("상한 (T점수)",
                                       options=[55, 60, 65, 70, 75, 80, 90], value=70))
    else:
        hcut = 100.0
        hc3.caption("Min-Max 상한 100")

    hx = hv[score_col].clip(upper=hcut)
    if score_method == "T점수":
        h_hi = hcut
        nbin = max(1, int(np.ceil((h_hi - float(hx.min())) / bw)))
        h_lo = h_hi - nbin * bw
        edges = h_lo + bw * np.arange(nbin + 1, dtype=float)
    else:
        h_lo, h_hi = 0.0, 100.0
        edges = np.linspace(h_lo, h_hi, nbin + 1)
    hv["급간"] = pd.cut(hx, edges, include_lowest=True, labels=False)
    n_clip = int((hv[score_col] > hcut).sum())

    class_order = sorted(set(hv["클래스"]))
    ct = (hv.groupby(["클래스", "급간"]).size().unstack(fill_value=0)
          .reindex(index=class_order, columns=range(nbin), fill_value=0))
    cnt = ct.values.astype(float)
    tot = cnt.sum(axis=1, keepdims=True)
    pct_m = np.divide(cnt * 100, tot, out=np.zeros_like(cnt), where=tot > 0)
    zval = cnt if norm == "시군구 수" else pct_m

    hover = [[f"<b>{class_order[i]}</b><br>{edges[j]:.1f} ~ {edges[j + 1]:.1f}"
              + (" (상한 초과 포함)" if j == nbin - 1 and n_clip else "")
              + f"<br>{int(cnt[i, j])}곳 · 클래스 내 {pct_m[i, j]:.0f}%"
              for j in range(nbin)] for i in range(len(class_order))]
    cell = [[("" if cnt[i, j] == 0 else
              (f"{int(cnt[i, j])}" if norm == "시군구 수" else f"{pct_m[i, j]:.0f}"))
             for j in range(nbin)] for i in range(len(class_order))]

    hfig = go.Figure(go.Heatmap(
        z=zval, x=(edges[:-1] + edges[1:]) / 2, y=class_order,
        colorscale="Greys", zmin=0,
        text=cell, texttemplate="%{text}", textfont=dict(size=9),
        hovertext=hover, hovertemplate="%{hovertext}<extra></extra>",
        colorbar=dict(title=dict(text=norm, side="right"), thickness=10)))
    if score_method == "T점수" and h_lo <= 50 <= h_hi:
        hfig.add_vline(x=50, line_dash="dash", line_color="gray", line_width=1,
                       annotation_text="평균 50", annotation_position="top")
    hfig.update_layout(height=max(260, 26 * len(class_order) + 100),
                       xaxis_title=f"{score_method} (시군구 전국 기준)",
                       margin=dict(l=10, t=24, b=36))
    st.plotly_chart(hfig, use_container_width=True)

# ── 지역 진단
if is_sgg and target and not mine.empty:
    st.divider()
    st.subheader(f"{target} 진단")

    if cat == "전체":
        plot = sgg_field[sgg_field["지역"] == target].copy()
        if score_method == "T점수":
            plot = plot[["대분류", "T분야점수", "T분야백분위", "T지표수"]].rename(
                columns={"대분류": "항목", "T분야점수": "점수", "T분야백분위": "백분위", "T지표수": "지표수"})
        else:
            allf = sgg_field.copy()
            allf["MinMax분야백분위"] = allf.groupby("대분류")["MinMax분야점수"].rank(pct=True) * 100
            plot = allf[allf["지역"] == target][["대분류", "MinMax분야점수", "MinMax분야백분위", "MinMax지표수"]].rename(
                columns={"대분류": "항목", "MinMax분야점수": "점수", "MinMax분야백분위": "백분위", "MinMax지표수": "지표수"})
    else:
        plot = sgg_detail[(sgg_detail["지역"] == target) & (sgg_detail["대분류"] == cat)][
            ["지표명", score_col, pct_col]].rename(columns={"지표명": "항목", score_col: "점수", pct_col: "백분위"})
        plot["지표수"] = 1

    order = st.radio("정렬", ["높은 값 순", "낮은 값 순"], horizontal=True)
    plot = plot.sort_values("점수", ascending=(order == "낮은 값 순"))

    fig2 = go.Figure(go.Bar(
        x=plot["점수"], y=plot["항목"], orientation="h",
        marker_color=np.where(plot["점수"] >= 50, "#1F4E9C", "#9BB8DE"),
        text=[f"{v:.1f}" for v in plot["점수"]], textposition="outside"))
    fig2.add_vline(x=50, line_dash="dash", line_color="gray")
    b_lo, b_hi = plot["점수"].min(), plot["점수"].max()
    pad = max(3, (b_hi - b_lo) * 0.25)
    fig2.update_layout(height=max(300, 45 * len(plot)), xaxis_title=score_method,
                       xaxis_range=[max(0, b_lo - pad), b_hi + pad],
                       margin=dict(l=10, t=30, b=40))
    st.plotly_chart(fig2, use_container_width=True)

    plot["전국 내 위치"] = plot["백분위"].apply(
        lambda p: "중간" if 40 <= p <= 60 else
        (f"상위 {100-p:.0f}%" if p > 60 else f"하위 {p:.0f}%"))
    st.dataframe(plot[["항목", "점수", "전국 내 위치", "지표수"]].style.format({"점수": "{:.1f}"}),
                 use_container_width=True, hide_index=True)
elif is_sgg:
    st.info("시도와 시군구를 선택하면 분야별 상세 진단이 표시됩니다.")

# ── 세부지표 전체 보기
st.divider()
with st.expander("세부지표 전체 보기", expanded=False):
    view = base_all if cat == "전체" else base_all[base_all["대분류"] == cat]
    if target:
        cols_show = ["대분류", "지표명", "원자료", "가중치", "MinMax점수", "T점수", "T합산점수",
                     "MinMax백분위", "백분위"]
        if not is_sgg:
            cols_show += ["집계방식", "구성지역수", "출처"]
        cols_show = [c for c in cols_show if c in view.columns]
        tbl = view[view["지역"] == target][cols_show].sort_values(["대분류", "지표명"])
        st.caption(f"{target} · {len(tbl)}개 지표")
    else:
        tbl = (view.pivot_table(index="지역", columns="지표명", values=score_col)
               .round(1).reset_index())
        st.caption(f"{len(tbl)}개 단위 × {len(tbl.columns)-1}개 지표 · {score_method}")
    st.dataframe(tbl, use_container_width=True, hide_index=True, height=520)
    st.download_button("CSV 내려받기",
                       tbl.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"smartcity_{level}_{cat}_{score_method}.csv",
                       mime="text/csv")

if is_sgg:
    with st.expander("시군구 분야·종합 점수 전체 내려받기", expanded=False):
        field_out = sgg_field[["지역", "대분류", "MinMax분야점수", "T분야원점수", "T분야점수",
                               "T분야백분위", "T지표수", "T가중치합"]].copy()
        total_out = sgg_total[["지역", "MinMax종합점수", "T종합원점수", "T종합점수",
                               "T종합백분위", "T지표수", "T가중치합"]].copy()
        st.dataframe(total_out, use_container_width=True, hide_index=True, height=320)
        cdl1, cdl2 = st.columns(2)
        cdl1.download_button("분야점수 CSV",
                             field_out.to_csv(index=False).encode("utf-8-sig"),
                             file_name="smartcity_sgg_field_scores.csv", mime="text/csv",
                             use_container_width=True)
        cdl2.download_button("종합점수 CSV",
                             total_out.to_csv(index=False).encode("utf-8-sig"),
                             file_name="smartcity_sgg_total_scores.csv", mime="text/csv",
                             use_container_width=True)

st.divider()
st.caption(f"{FORMULA} · 데이터 기준 {ts:%Y-%m-%d %H:%M:%S}")
