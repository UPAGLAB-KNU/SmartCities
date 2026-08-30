import json
from datetime import datetime

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

import smartcity_core as core

st.set_page_config(page_title="스마트도시 서비스 수준", layout="wide")

FORMULA = ("Z = (원자료 − 평균) ÷ 표준편차 × 방향, T = 50 + 10Z, "
           "백분위 = Z 순위 백분율 (지표별·비교집단 내)")


@st.cache_data(ttl=600, show_spinner="구글시트에서 데이터를 읽는 중...")
def fetch(sheet_id):
    return core.build_base(sheet_id), datetime.now()


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


sheet_id = st.secrets.get("SHEET_ID", "")
if not sheet_id:
    st.error("Streamlit Secrets에 SHEET_ID를 등록하세요.")
    st.stop()

pack, ts, err = load(sheet_id)
if pack is None:
    st.error(f"시트 읽기 실패 — 사유: {err}")
    st.stop()
raw, denom, sido_actual = pack

h1, h2 = st.columns([5, 1])
with h1:
    st.title("스마트도시 서비스 수준 대시보드")
    st.caption(f"데이터 기준: {ts:%Y-%m-%d %H:%M:%S}")
with h2:
    if st.button("🔄 새로 읽기", use_container_width=True):
        fetch.clear()
        st.rerun()
if err is not None:
    st.warning(f"시트 읽기 실패 — 마지막 정상 데이터({ts:%H:%M:%S}) 표시 중. 사유: {err}")

# ── 1행: 지표
c1, c2, c3 = st.columns([2, 3, 1.5])
cat = c1.selectbox("대분류", ["전체"] + sorted(raw["대분류"].dropna().unique()))
pool = raw if cat == "전체" else raw[raw["대분류"] == cat]
ind = c2.selectbox("지표", sorted(pool["지표명"].unique()))
mode = c3.radio("값 기준", ["T점수", "원자료"], horizontal=True)

# ── 2행: 집계 수준
level = st.selectbox("집계 수준",
                     ["시군구별", "시도별", "도시규모별", "수도권-비수도권"])
level_col = core.LEVELS[level]
is_sgg = level_col is None

# ── 3행: 지역·비교집단
c4, c5, c6 = st.columns([2, 2, 2])
if is_sgg:
    sido = c4.selectbox("시도", ["전체"] + sorted(raw["시도명"].unique()))
    sgg_opts = ["전체"] + (sorted(raw[raw["시도명"] == sido]["시군구명"].unique())
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

# ── 집계 및 표준화
agg = core.aggregate(raw, level_col, denom, sido_actual)

if is_sgg:
    if group == "전국":
        subset = agg
    elif group == "동일 시도":
        subset = agg[agg["시도명"] == sido] if sido != "전체" else agg
    elif group == "인구규모 유사지역":
        if target:
            myg = agg.loc[agg["지역"] == target, "인구규모군"].iloc[0]
            subset = agg[agg["인구규모군"] == myg]
        else:
            subset = agg
    else:
        subset = agg[agg["유형구분"] == group]
    if target and target not in subset["지역"].values:
        st.warning(f"{target}는 '{group}'에 없어 전국 기준으로 표시합니다.")
        subset, group = agg, "전국"
else:
    subset = agg

base = core.add_tscore(subset)
sub = base[base["지표명"] == ind].dropna(subset=[mode])

cap = f"{level} · 비교집단 {group} · 유효 {len(sub)}개 / 전체 {base['지역'].nunique()}개"
if not is_sgg and "출처" in sub.columns:
    n_real = int((sub["출처"] == "실측").sum())
    if n_real:
        cap += f" · 실측 {n_real}개"
    how = sub["집계방식"].iloc[0] if len(sub) else ""
    cap += f" · 집계방식: {how}"
st.caption(cap)

if sub.empty:
    st.info("이 지표는 현재 값 기준으로 표시할 값이 없습니다.")
    st.stop()

# ── 요약통계
q = sub[mode].describe()
cols = st.columns(6 if target else 5)
for i, (lab, v) in enumerate([("최소", q["min"]), ("25%", q["25%"]), ("중앙값", q["50%"]),
                              ("75%", q["75%"]), ("평균", q["mean"])]):
    cols[i].metric(lab, f"{v:,.1f}" if mode == "T점수" else f"{v:,.4g}")

mine = sub[sub["지역"] == target] if target else pd.DataFrame()
if target and not mine.empty:
    val, pct = float(mine[mode].iloc[0]), mine["백분위"].iloc[0]
    cols[5].metric(label, f"{val:,.1f}" if mode == "T점수" else f"{val:,.4g}",
                   f"상위 {100-pct:.0f}%")
elif target:
    cols[5].metric(label, "값 없음")
    val = None
else:
    val = None

# ── 극단값 기준 (시군구별 + 히스토그램일 때만)
if is_sgg:
    if mode == "T점수":
        CUT = st.select_slider("극단값 묶기 기준 (T점수)",
                               options=[55, 60, 65, 70, 75, 80], value=60)
        cut_label = f"{CUT}"
    else:
        pctl = st.select_slider("극단값 묶기 기준 (상위 백분위)",
                                options=[80, 85, 90, 95, 99, 100], value=95)
        CUT = float(sub["원자료"].quantile(pctl / 100))
        cut_label = f"{CUT:,.4g}"
else:
    CUT = float(sub[mode].max())
    cut_label = ""

# ── 분포 + 지도
left, right = st.columns([1, 1.4])

with left:
    st.subheader("전국 분포" if is_sgg else f"{level} 비교")

    if is_sgg:
        plot_x = sub[mode].clip(upper=CUT)
        n_over = int((sub[mode] > CUT).sum())
        lo, hi = float(plot_x.min()), float(plot_x.max())
        step = (hi - lo) / 60 or 1
        fig = go.Figure()
        fig.add_histogram(x=plot_x, marker_color="#B7CDEB", autobinx=False,
                          xbins=dict(start=lo, end=hi + step, size=step))
        if mode == "T점수":
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
                          xaxis_title=mode, yaxis_title="지역 수",
                          margin=dict(t=40, b=40))
    else:                                        # 단위가 적으면 막대그래프
        d = sub.sort_values(mode, ascending=True)
        colors = ["#D62728" if r == target else
                  ("#1F4E9C" if v >= (50 if mode == "T점수" else d[mode].median())
                   else "#9BB8DE")
                  for r, v in zip(d["지역"], d[mode])]
        fig = go.Figure(go.Bar(
            x=d[mode], y=d["지역"], orientation="h", marker_color=colors,
            text=[f"{v:,.1f}" if mode == "T점수" else f"{v:,.4g}" for v in d[mode]],
            textposition="outside",
            customdata=np.stack([d["구성지역수"], d["출처"]], axis=-1),
            hovertemplate="<b>%{y}</b><br>%{x:.2f}"
                          "<br>구성 %{customdata[0]}곳 · %{customdata[1]}<extra></extra>"))
        if mode == "T점수":
            fig.add_vline(x=50, line_dash="dash", line_color="gray")
        fig.update_layout(height=max(400, 45 * len(d) + 80), xaxis_title=mode,
                          showlegend=False, margin=dict(t=40, b=40, l=10))
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("공간분포")
    geo = load_geo()

    cols_need = ["지역", mode, "원자료", "T점수", "백분위"]
    cols_need = list(dict.fromkeys(cols_need))          # mode 중복 제거

    if is_sgg:
        pmap = sub[cols_need].copy()
        pmap["단위"] = pmap["지역"]
    else:                                        # 집계값을 소속 시군구에 펼침
        key = (raw.drop_duplicates("지역")[["지역", level_col]]
               .rename(columns={"지역": "시군구", level_col: "단위"}))
        pmap = (key.merge(sub[cols_need].rename(columns={"지역": "단위"}),
                          on="단위", how="inner")
                .rename(columns={"시군구": "지역"}))
    zmax = CUT
    zmin = 40 if mode == "T점수" else float(pmap[mode].min())
           
    fig3 = go.Figure(go.Choropleth(
        geojson=geo, locations=pmap["지역"], z=pmap[mode].clip(zmin, zmax),
        featureidkey="properties.지역",
        colorscale="Blues", zmin=zmin, zmax=zmax,
        marker_line_color="white", marker_line_width=0.4,
        colorbar=dict(title=mode, thickness=12, len=0.6, x=0.93, y=0.35),
        customdata=np.stack([pmap["원자료"], pmap["T점수"], pmap["백분위"]], axis=-1),
        hovertemplate="<b>%{location}</b><br>원자료 %{customdata[0]:.2f}"
                      "<br>T점수 %{customdata[1]:.1f}"
                      "<br>상위 %{customdata[2]:.0f}%<extra></extra>"))

    miss = set(f["properties"]["지역"] for f in geo["features"]) - set(pmap["지역"])
    if miss:
        fig3.add_trace(go.Choropleth(
            geojson=geo, locations=list(miss), z=[0] * len(miss),
            featureidkey="properties.지역",
            colorscale=[[0, "#ffffff"], [1, "#ffffff"]], showscale=False,
            marker_line_color="white", marker_line_width=0.4,
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

# ── 지역 진단
if target and not mine.empty:
    st.divider()
    st.subheader(f"{target} 진단")
    st.caption("범주 종합점수는 단위가 다른 지표를 합산하므로 T점수 기준으로만 산출됩니다.")
    mine_all = base[base["지역"] == target].dropna(subset=["T점수"])

    if cat == "전체":
        plot = (mine_all.groupby("대분류")
                .agg(Z=("Z점수", "mean"), 백분위=("백분위", "mean"),
                     지표수=("지표명", "count"))
                .reset_index().rename(columns={"대분류": "항목"}))
        plot["T점수"] = 50 + 10 * plot["Z"]
    else:
        plot = (mine_all[mine_all["대분류"] == cat][["지표명", "T점수", "백분위"]]
                .rename(columns={"지표명": "항목"}))
        plot["지표수"] = 1

    order = st.radio("정렬", ["높은 값 순", "낮은 값 순"], horizontal=True)
    plot = plot.sort_values("T점수", ascending=(order == "낮은 값 순"))

    fig2 = go.Figure(go.Bar(
        x=plot["T점수"], y=plot["항목"], orientation="h",
        marker_color=np.where(plot["T점수"] >= 50, "#1F4E9C", "#9BB8DE"),
        text=[f"{v:.0f}" for v in plot["T점수"]], textposition="outside"))
    fig2.add_vline(x=50, line_dash="dash", line_color="gray")
    b_lo, b_hi = plot["T점수"].min(), plot["T점수"].max()
    pad = max(3, (b_hi - b_lo) * 0.25)
    fig2.update_layout(height=max(300, 45 * len(plot)), xaxis_title="T점수",
                       xaxis_range=[b_lo - pad, b_hi + pad],
                       margin=dict(l=10, t=30, b=40))
    st.plotly_chart(fig2, use_container_width=True)

    pos = f"{group if is_sgg else level} 내 위치"
    plot[pos] = plot["백분위"].apply(
        lambda p: "중간" if 40 <= p <= 60 else
        (f"상위 {100-p:.0f}%" if p > 60 else f"하위 {p:.0f}%"))
    st.dataframe(plot[["항목", "T점수", pos, "지표수"]].style.format({"T점수": "{:.1f}"}),
                 use_container_width=True, hide_index=True)
else:
    st.info("지역을 선택하면 상세 진단이 표시됩니다.")

# ── 세부지표 전체 보기
st.divider()
with st.expander("세부지표 전체 보기", expanded=False):
    view = base if cat == "전체" else base[base["대분류"] == cat]
    if target:
        c = ["대분류", "지표명", "원자료", "T점수", "백분위"]
        if not is_sgg:
            c += ["집계방식", "구성지역수", "출처"]
        tbl = view[view["지역"] == target][c].sort_values(["대분류", "지표명"])
        st.caption(f"{target} · {len(tbl)}개 지표")
    else:
        tbl = (view.pivot_table(index="지역", columns="지표명", values=mode)
               .round(1).reset_index())
        st.caption(f"{len(tbl)}개 단위 × {len(tbl.columns)-1}개 지표 · 값 기준 {mode}")
    st.dataframe(tbl, use_container_width=True, hide_index=True, height=520)
    st.download_button("CSV 내려받기",
                       tbl.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"smartcity_{level}_{cat}_{mode}.csv",
                       mime="text/csv")

st.divider()
st.caption(f"{FORMULA} · 데이터 기준 {ts:%Y-%m-%d %H:%M:%S}")
