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


def ordered_unique(series):
    """결측·공백을 제외하고 입력 데이터의 최초 등장 순서를 유지한다."""
    vals = []
    seen = set()
    for x in series:
        if pd.isna(x):
            continue
        v = str(x).strip()
        if not v or v in seen:
            continue
        vals.append(v)
        seen.add(v)
    return vals


def sort_by_order(df, col, order):
    """지정한 입력 순서대로 안정적으로 정렬한다. 미등록 값은 뒤에 둔다."""
    if col not in df.columns or not order:
        return df
    rank = {v: i for i, v in enumerate(order)}
    out = df.copy()
    out["_입력순서"] = out[col].astype(str).map(rank).fillna(len(rank))
    out = out.sort_values("_입력순서", kind="stable").drop(columns="_입력순서")
    return out


# 선택·표·그래프에서 공통으로 사용할 입력 데이터 순서
CAT_ORDER = ordered_unique(raw["대분류"])
IND_ORDER = ordered_unique(raw["지표명"])
SIDO_ORDER = ordered_unique(raw["시도명"])
REGION_ORDER = ordered_unique(raw["지역"])
SGG_ORDER_BY_SIDO = {
    sido: ordered_unique(raw.loc[raw["시도명"].astype(str) == sido, "시군구명"])
    for sido in SIDO_ORDER
}
IND_NO = {name: i + 1 for i, name in enumerate(IND_ORDER)}

h1, h2, h3 = st.columns([5, 1.2, 1.4])
with h1:
    st.title("스마트도시 서비스 수준 대시보드")
    st.caption(f"데이터 기준: {ts:%Y-%m-%d %H:%M:%S}")
with h2:
    if st.button("🔄 새로 읽기", use_container_width=True):
        fetch.clear()
        st.rerun()
with h3:
    if st.button("🧹 캐시 초기화", use_container_width=True,
                 help="원자료·점수 계산·지도 캐시를 모두 지우고 다시 계산합니다."):
        st.cache_data.clear()
        st.session_state.pop("last_good", None)
        st.rerun()
if err is not None:
    st.warning(f"시트 읽기 실패 — 마지막 정상 데이터({ts:%H:%M:%S}) 표시 중. 사유: {err}")

# ── 1행: 시각화 항목 · 지표/부문 · 값 기준
c0, c1, c2, c3 = st.columns([1.5, 2, 3, 1.5])
metric_type = c0.selectbox("시각화 항목", ["세부지표", "부문점수", "종합점수"])

if metric_type == "세부지표":
    cat = c1.selectbox("대분류", ["전체"] + CAT_ORDER)
    pool = raw if cat == "전체" else raw[raw["대분류"] == cat]
    pool_inds = [x for x in IND_ORDER if x in set(pool["지표명"].astype(str))]
    ind = c2.selectbox("지표", pool_inds)
    metric_label = ind
    mode_options = ["T점수", "Min-Max", "원자료"]
elif metric_type == "부문점수":
    cat = c1.selectbox("부문", CAT_ORDER)
    ind = None
    c2.caption(f"{cat} 부문에 포함된 세부지표를 결합한 점수")
    metric_label = f"{cat} 부문점수"
    mode_options = ["T점수", "Min-Max"]
else:
    cat = "전체"
    ind = None
    c1.caption("전체 부문")
    c2.caption("전체 세부지표를 결합한 종합점수")
    metric_label = "종합점수"
    mode_options = ["T점수", "Min-Max"]

mode = c3.radio("값 기준", mode_options, horizontal=True)

# ── 2행: 집계 수준
level = st.selectbox("집계 수준",
                     ["시군구별", "시도별", "도시규모별", "수도권-비수도권"])
level_col = core.LEVELS[level]
is_sgg = level_col is None

# ── 3행: 지역·비교집단·시각화 대상
c4, c5, c6, c7 = st.columns([2, 2, 2, 2])
if is_sgg:
    sido = c4.selectbox("시도", ["전체"] + SIDO_ORDER)
    sgg_opts = ["전체"] + (SGG_ORDER_BY_SIDO.get(sido, []) if sido != "전체" else [])
    sgg = c5.selectbox("시군구", sgg_opts, disabled=(sido == "전체"))
    target = f"{sido} {sgg}" if (sido != "전체" and sgg != "전체") else None
    label = sgg
    group = c6.selectbox("비교집단",
                         ["전국", "동일 시도", "특광역시-도", "시-군",
                          "인구규모 유사지역"])

    # 비교집단은 점수 산정 기준, 시각화 대상은 화면에 표시할 지역 필터
    if group == "동일 시도":
        visual_options = ["전체"] + SIDO_ORDER
    elif group == "특광역시-도":
        visual_options = ["전체", "특광역시", "도"]
    elif group == "시-군":
        visual_options = ["전체", "시·구", "군"]
    elif group == "인구규모 유사지역":
        bins = [str(x) for x in raw["인구규모군"].dropna().unique()]
        preferred = [x for x in getattr(core, "POP_LABELS", []) if x in bins]
        visual_options = ["전체"] + preferred + [x for x in bins if x not in preferred]
    else:
        visual_options = ["전체"]
    visual_target = c7.selectbox("시각화 대상", visual_options)
else:
    if level_col == "시도명":
        units = SIDO_ORDER
    elif level_col == "인구규모군":
        present = set(raw[level_col].dropna().astype(str))
        units = [x for x in getattr(core, "POP_LABELS", []) if x in present]
        units += [x for x in ordered_unique(raw[level_col]) if x not in units]
    else:
        units = ordered_unique(raw[level_col])
    target = c4.selectbox(level.replace("별", ""), ["전체"] + list(units))
    target = None if target == "전체" else target
    label = target
    c5.empty()
    group = "전체"
    visual_target = "전체"
    c6.caption(f"{level} 집계 · 비교집단은 전체 {len(units)}개 단위")
    c7.empty()

# ── 집계 및 표준화
# 집계수준·비교집단·원자료가 같으면 결과를 재사용한다.
# 시각화 항목(세부지표/부문/종합), 선택 지표·부문, 값 기준은
# 이 계산 함수의 입력이 아니므로 그 선택만 바꿀 때는 재계산하지 않는다.
@st.cache_data(show_spinner=False)
def build_scored_base(raw_df, denom_df, sido_actual_df, level_col_arg, group_arg):
    agg_df = core.aggregate(raw_df, level_col_arg, denom_df, sido_actual_df)
    is_sgg_arg = level_col_arg is None
    score_col = None

    if is_sgg_arg:
        subset_df = agg_df.copy()
        if group_arg == "동일 시도":
            score_col = "시도명"
        elif group_arg == "특광역시-도":
            score_col = "_비교집단"
            metro_mask = subset_df["시도명"].astype(str).str.contains(
                "특별시|광역시|특별자치시", regex=True, na=False)
            subset_df[score_col] = np.where(metro_mask, "특광역시", "도")
        elif group_arg == "시-군":
            score_col = "_비교집단"
            gun_mask = subset_df["시군구명"].astype(str).str.endswith("군", na=False)
            subset_df[score_col] = np.where(gun_mask, "군", "시·구")
        elif group_arg == "인구규모 유사지역":
            score_col = "인구규모군"

        scored_df = core.add_minmax(
            core.add_tscore(subset_df, group_col=score_col),
            group_col=score_col)
    else:
        scored_df = core.add_minmax(core.add_tscore(agg_df))

    return scored_df, score_col


base, score_group_col = build_scored_base(
    raw, denom, sido_actual, level_col, group if is_sgg else "전체")

# 점수는 전체 비교집단 기준으로 계산한 뒤, 시각화 대상은 표시 단계에서만 필터링
display_base = base
if is_sgg and visual_target != "전체":
    if group == "동일 시도":
        display_base = base[base["시도명"].astype(str) == visual_target]
    elif group in ["특광역시-도", "시-군"]:
        display_base = base[base["_비교집단"].astype(str) == visual_target]
    elif group == "인구규모 유사지역":
        display_base = base[base["인구규모군"].astype(str) == visual_target]


@st.cache_data(show_spinner=False)
def build_scores_cached(scored, group_col_arg=None):
    """이미 표준화된 지표에서 부문·종합 점수를 계산하고 결과를 캐시한다."""
    field_scores, total_scores = core.build_field_total_scores(scored)
    if group_col_arg is None:
        return field_scores, total_scores

    region_group = (scored.drop_duplicates("지역")[["지역", group_col_arg]]
                    .dropna(subset=[group_col_arg]))

    field_scores = field_scores.merge(region_group, on="지역", how="left")
    total_scores = total_scores.merge(region_group, on="지역", how="left")

    def _t_within_group(df, item_col=None):
        keys = [group_col_arg] + ([item_col] if item_col else [])
        g = df.groupby(keys, observed=True)["T원점수"]
        mean = g.transform("mean")
        sd = g.transform("std")
        z = (df["T원점수"] - mean) / sd.replace(0, np.nan)
        df["T점수"] = 50 + 10 * z
        zero = sd.eq(0) & df["T원점수"].notna()
        df.loc[zero, "T점수"] = 50.0
        df["백분위_T"] = df.groupby(keys, observed=True)["T점수"].rank(pct=True) * 100
        df["백분위_MinMax"] = df.groupby(keys, observed=True)["Min-Max점수"].rank(pct=True) * 100
        return df

    field_scores = _t_within_group(field_scores, "항목")
    total_scores = _t_within_group(total_scores)
    return field_scores, total_scores


def build_scores_for_current_group(scored):
    """현재 비교집단의 캐시된 부문·종합 점수를 반환한다."""
    group_col_arg = score_group_col if is_sgg else None
    return build_scores_cached(scored, group_col_arg)


# ── 현재 시각화 항목을 공통 형식(sub)으로 구성
# 세부지표는 기존 점수/원자료를 그대로 사용하고, 부문·종합은 이미 계산된
# Min-Max점수와 재표준화 T점수를 현재 그래프/지도 형식에 맞춰 연결한다.
if metric_type == "세부지표":
    sub = display_base[display_base["지표명"] == ind].dropna(subset=[mode]).copy()
else:
    field_scores, total_scores = build_scores_for_current_group(base)
    score_df = (field_scores[field_scores["항목"] == cat].copy()
                if metric_type == "부문점수" else total_scores.copy())

    # 시각화 대상은 점수 계산 후 표시 단계에서만 적용
    if is_sgg and visual_target != "전체":
        show_regions = set(display_base["지역"].dropna().astype(str))
        score_df = score_df[score_df["지역"].astype(str).isin(show_regions)]

    score_df["Min-Max"] = score_df["Min-Max점수"]
    score_df["백분위"] = (score_df["백분위_T"]
                          if mode == "T점수" else score_df["백분위_MinMax"])
    score_df["원자료"] = np.nan
    score_df["출처"] = "산출점수"

    # 지도/그래프와 선택지역 표시를 위해 현재 단위의 메타정보를 붙인다.
    meta_cols = [c for c in ["지역", "시도명", "시군구명", "인구규모군",
                              level_col, "구성지역수"]
                 if c is not None and c in base.columns]
    if meta_cols:
        region_meta = base[meta_cols].drop_duplicates("지역").copy()
        add_cols = [c for c in region_meta.columns if c != "지역" and c not in score_df.columns]
        if add_cols:
            score_df = score_df.merge(region_meta[["지역"] + add_cols], on="지역", how="left")
    if "구성지역수" not in score_df.columns:
        score_df["구성지역수"] = 1

    sub = score_df.dropna(subset=[mode]).copy()

cap = (f"{level} · {metric_label} · 비교집단 {group} 기준 · "
       f"시각화 대상 {visual_target} · 유효 {len(sub)}개 / 전체 {base['지역'].nunique()}개")

# 세부지표의 상위단위 집계일 때만 원자료 집계방식과 진단을 표시한다.
if metric_type == "세부지표" and not is_sgg and "출처" in sub.columns:
    n_real = int((sub["출처"] == "실측").sum())
    if n_real:
        cap += f" · 실측 {n_real}개"
    how = sub["집계방식"].iloc[0] if len(sub) else ""
    cap += f" · 집계방식: {how}"

    # 집계 진단: 선택 지표가 정의 시트에서 어떤 집계방식/분모로 읽혔는지와
    # 분모 시트 연결 상태를 함께 표시한다. 계산 로직은 변경하지 않는다.
    _meta = raw[raw["지표명"] == ind]
    if not _meta.empty:
        _def_how = str(_meta["집계방식"].iloc[0]) if "집계방식" in _meta.columns else "(컬럼 없음)"
        _def_den = str(_meta["집계_분모"].iloc[0]) if "집계_분모" in _meta.columns else "(컬럼 없음)"
        _diag = f"정의읽기={_def_how}, 분모={_def_den or '(비어 있음)'}"

        if _def_how == "가중평균":
            _denom_error = (denom.attrs.get("load_error", "")
                            if isinstance(denom, pd.DataFrame) else "")
            if _denom_error:
                _diag += f", 분모시트오류={_denom_error}"
            elif denom is None:
                _diag += ", 분모시트=읽기 실패"
            elif not _def_den:
                _diag += ", 분모명=비어 있음"
            elif _def_den not in denom.columns:
                _diag += f", 분모열=없음"
            else:
                _regions = _meta[["지역"]].drop_duplicates()
                _chk = _regions.merge(denom[["지역", _def_den]], on="지역", how="left")
                _matched = int(pd.to_numeric(_chk[_def_den], errors="coerce").notna().sum())
                _diag += f", 분모열=있음, 지역매칭={_matched}/{len(_chk)}"

        cap += f" · 진단: {_diag}"
elif metric_type != "세부지표":
    cap += " · 세부지표 결합점수"
st.caption(cap)

if sub.empty:
    st.info("현재 선택한 항목은 이 값 기준으로 표시할 값이 없습니다.")
    st.stop()

# ── 요약통계
q = sub[mode].describe()
cols = st.columns(7 if target else 6)
for i, (lab, v) in enumerate([("최소", q["min"]), ("25%", q["25%"]), ("중앙값", q["50%"]),
                              ("75%", q["75%"]), ("최대", q["max"]), ("평균", q["mean"])]):
    cols[i].metric(lab, f"{v:,.1f}" if mode in ["T점수", "Min-Max"] else f"{v:,.4g}")

mine = sub[sub["지역"] == target] if target else pd.DataFrame()
if target and not mine.empty:
    val, pct = float(mine[mode].iloc[0]), mine["백분위"].iloc[0]
    cols[6].metric(label, f"{val:,.1f}" if mode in ["T점수", "Min-Max"] else f"{val:,.4g}",
                   f"상위 {100-pct:.0f}%")
elif target:
    cols[6].metric(label, "값 없음")
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
        # Min-Max/부문·종합 점수는 현재 표시값 자체의 백분위를 사용한다.
        # 원자료 모드일 때만 원자료 기준으로 계산된다.
        CUT = float(sub[mode].quantile(pctl / 100))
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
        if level_col == "시도명":
            graph_order = SIDO_ORDER
        elif level_col == "인구규모군":
            graph_order = units
        else:
            graph_order = units
        d = sort_by_order(sub, "지역", graph_order)
        colors = ["#D62728" if r == target else
                  ("#1F4E9C" if v >= (50 if mode == "T점수" else d[mode].median())
                   else "#9BB8DE")
                  for r, v in zip(d["지역"], d[mode])]
        fig = go.Figure(go.Bar(
            x=d[mode], y=d["지역"], orientation="h", marker_color=colors,
            text=[f"{v:,.1f}" if mode in ["T점수", "Min-Max"] else f"{v:,.4g}" for v in d[mode]],
            textposition="outside",
            customdata=np.stack([d["구성지역수"], d["출처"]], axis=-1),
            hovertemplate="<b>%{y}</b><br>%{x:.2f}"
                          "<br>구성 %{customdata[0]}곳 · %{customdata[1]}<extra></extra>"))
        if mode == "T점수":
            fig.add_vline(x=50, line_dash="dash", line_color="gray")
        fig.update_layout(height=max(400, 45 * len(d) + 80), xaxis_title=mode,
                          yaxis=dict(categoryorder="array",
                                     categoryarray=d["지역"].astype(str).tolist(),
                                     autorange="reversed"),
                          showlegend=False, margin=dict(t=40, b=40, l=10))
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("공간분포")
    geo = load_geo()

    if metric_type == "세부지표":
        cols_need = ["지역", mode, "원자료", "T점수", "백분위"]
    else:
        cols_need = ["지역", mode, "Min-Max", "T점수", "백분위"]
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

    if metric_type == "세부지표":
        map_custom = np.stack([pmap["원자료"], pmap["T점수"], pmap["백분위"]], axis=-1)
        map_hover = ("<b>%{location}</b><br>원자료 %{customdata[0]:.2f}"
                     "<br>T점수 %{customdata[1]:.1f}"
                     "<br>백분위 %{customdata[2]:.0f}<extra></extra>")
    else:
        map_custom = np.stack([pmap["Min-Max"], pmap["T점수"], pmap["백분위"]], axis=-1)
        map_hover = ("<b>%{location}</b><br>Min-Max %{customdata[0]:.1f}"
                     "<br>T점수 %{customdata[1]:.1f}"
                     "<br>백분위 %{customdata[2]:.0f}<extra></extra>")
           
    fig3 = go.Figure(go.Choropleth(
        geojson=geo, locations=pmap["지역"], z=pmap[mode].clip(zmin, zmax),
        featureidkey="properties.지역",
        colorscale="Blues", zmin=zmin, zmax=zmax,
        marker_line_color="#8c8c8c", marker_line_width=0.5,
        colorbar=dict(title=mode, thickness=12, len=0.6, x=0.93, y=0.35),
        customdata=map_custom, hovertemplate=map_hover))

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

# ── 클래스별 시군구 분포 히트맵 (시도별·도시규모별·수도권-비수도권)
if not is_sgg:
    st.divider()
    st.subheader(f"{level} 시군구 분포")

    # 시군구 분포는 현재 시각화 항목에 대응하는 시군구 수준 값을 사용한다.
    # 세부지표의 T/Min-Max는 전국 시군구 기준이며, 부문·종합 점수도
    # 전국 시군구 점수를 결합한 뒤 부문/종합 T를 재표준화해서 사용한다.
    if metric_type == "세부지표":
        if mode == "T점수":
            sgg_view = core.add_tscore(raw)
        elif mode == "Min-Max":
            sgg_view = core.add_minmax(raw)
        else:
            sgg_view = raw.copy()
        hv = sgg_view[sgg_view["지표명"] == ind].dropna(subset=[mode]).copy()
    else:
        # 전국 시군구 기준 점수도 캐시하여 시각화 항목 변경 시 재계산하지 않는다.
        sgg_scored, _ = build_scored_base(raw, denom, sido_actual, None, "전국")
        sgg_field, sgg_total = build_scores_cached(sgg_scored, None)
        hv = (sgg_field[sgg_field["항목"] == cat].copy()
              if metric_type == "부문점수" else sgg_total.copy())
        hv["Min-Max"] = hv["Min-Max점수"]
        hv["백분위"] = hv["백분위_T"] if mode == "T점수" else hv["백분위_MinMax"]
        # 상위단위 분류(시도/도시규모/권역)를 시군구 지역키로 붙인다.
        class_meta = raw.drop_duplicates("지역")[["지역", level_col]]
        hv = hv.merge(class_meta, on="지역", how="left").dropna(subset=[mode])

    hv = hv[hv[level_col].notna()]
    hv["클래스"] = hv[level_col].astype(str)

    hc1, hc2, hc3 = st.columns(3)
    if mode == "T점수":                                   # T점수는 정수 폭으로 급간 설정
        bw = hc1.select_slider("급간 폭 (T점수)", options=[1, 2, 5, 10], value=2)
    else:
        nbin = hc1.select_slider("급간 수", options=[10, 15, 20, 30, 40], value=20)
    norm = hc2.radio("색상 기준", ["시군구 수", "클래스 내 비율(%)"], horizontal=True)
    if mode == "T점수":
        hcut = float(hc3.select_slider("상한 (T점수)",
                                       options=[55, 60, 65, 70, 75, 80], value=70))
    else:
        hp = hc3.select_slider("상한 (상위 백분위)",
                               options=[80, 85, 90, 95, 99, 100], value=95)
        hcut = float(hv[mode].quantile(hp / 100))

    hx = hv[mode].clip(upper=hcut)
    if mode == "T점수":                                   # 상한에서 거꾸로 정수 폭만큼 → 경계가 모두 정수
        h_hi = hcut
        nbin = max(1, int(np.ceil((h_hi - float(hx.min())) / bw)))
        h_lo = h_hi - nbin * bw
        edges = h_lo + bw * np.arange(nbin + 1, dtype=float)
    else:
        h_lo = float(hx.min())
        h_hi = hcut if hcut > h_lo else h_lo + 1
        edges = np.linspace(h_lo, h_hi, nbin + 1)
    hv["급간"] = pd.cut(hx, edges, include_lowest=True, labels=False)
    n_clip = int((hv[mode] > hcut).sum())

    rows = [str(r) for r in d["지역"]]                    # 막대그래프와 같은 순서
    missing_rows = [x for x in units if x in set(hv["클래스"]) and x not in set(rows)]
    rows = missing_rows + rows

    ct = (hv.groupby(["클래스", "급간"]).size().unstack(fill_value=0)
          .reindex(index=rows, columns=range(nbin), fill_value=0))
    cnt = ct.values.astype(float)
    tot = cnt.sum(axis=1, keepdims=True)
    pct_m = np.divide(cnt * 100, tot, out=np.zeros_like(cnt), where=tot > 0)
    zval = cnt if norm == "시군구 수" else pct_m

    fe = (lambda v: f"{v:.0f}") if mode == "T점수" else (lambda v: f"{v:,.4g}")
    hover = [[f"<b>{rows[i]}</b><br>{fe(edges[j])} ~ {fe(edges[j + 1])}"
              + (" (상한 초과 포함)" if j == nbin - 1 and n_clip else "")
              + f"<br>{int(cnt[i, j])}곳 · 클래스 내 {pct_m[i, j]:.0f}%"
              for j in range(nbin)] for i in range(len(rows))]
    cell = [[("" if cnt[i, j] == 0 else
              (f"{int(cnt[i, j])}" if norm == "시군구 수" else f"{pct_m[i, j]:.0f}"))
             for j in range(nbin)] for i in range(len(rows))]

    hfig = go.Figure(go.Heatmap(
        z=zval, x=(edges[:-1] + edges[1:]) / 2, y=rows,
        colorscale="Greys", zmin=0,
        text=cell, texttemplate="%{text}", textfont=dict(size=9),
        hovertext=hover, hovertemplate="%{hovertext}<extra></extra>",
        colorbar=dict(title=dict(text=norm, side="right"), thickness=10)))

    if mode == "T점수" and h_lo <= 50 <= h_hi:
        hfig.add_vline(x=50, line_dash="dash", line_color="gray", line_width=1,
                       annotation_text="평균 50", annotation_position="top")
    if target and str(target) in rows:
        k = rows.index(str(target))
        hfig.add_shape(type="rect", x0=edges[0], x1=edges[-1], y0=k - 0.5, y1=k + 0.5,
                       line=dict(color="#D62728", width=2))

    GRID = dict(color="#d0d0d0", width=0.6)
    nr = len(rows)
    for e in edges[1:-1]:                                  # 세로 격자선 (안쪽만)
        hfig.add_shape(type="line", x0=e, x1=e, y0=-0.5, y1=nr - 0.5, line=GRID, layer="above")
    for k in range(nr - 1):                                # 가로 격자선 (안쪽만)
        hfig.add_shape(type="line", x0=edges[0], x1=edges[-1], y0=k + 0.5, y1=k + 0.5,
                       line=GRID, layer="above")
    hfig.add_shape(type="rect", x0=edges[0], x1=edges[-1], y0=-0.5, y1=nr - 0.5,
                   line=GRID, layer="above")               # 외곽선도 같은 굵기

    ROW_H = 18                                             # 행 1개 높이(px)
    hfig.update_layout(height=ROW_H * nr + 90,
                       xaxis=dict(title=f"{mode} (시군구 값)", showgrid=False, zeroline=False,
                                  showline=False, range=[edges[0], edges[-1]]),
                       yaxis=dict(type="category", showgrid=False, showline=False,
                                  # rows는 입력 데이터 순서(시도: 서울→…→제주).
                                  # Plotly Heatmap은 첫 행을 아래쪽에 두므로 축을 뒤집어
                                  # 입력 순서의 첫 항목이 화면 맨 위에 오도록 한다.
                                  range=[nr - 0.5, -0.5]),
                       plot_bgcolor="rgba(0,0,0,0)",
                       font=dict(size=11),
                       margin=dict(l=10, t=24, b=36))
    st.plotly_chart(hfig, use_container_width=True)
    st.caption("칸의 색 = 해당 급간에 속한 시군구 수. "
               + ("T점수는 전국 229개 시군구 기준으로 표준화한 값입니다. " if mode == "T점수" else "")
               + (f"마지막 급간에는 상한 초과 {n_clip}곳이 포함됩니다. " if n_clip else "")
               + "클래스마다 시군구 수가 달라 비교가 어려우면 '클래스 내 비율'로 바꿔 보세요.")

# ── 지역 진단
if target and not mine.empty:
    st.divider()
    st.subheader(f"{target} 진단")
    # 분야점수는 위에서 선택한 방식과 동일한 산식으로 계산한다.
    # 원자료 보기에서는 기존 T점수 기반 진단을 그대로 유지한다.
    field_scores, total_scores = build_scores_for_current_group(base)
    mine_all = base[base["지역"] == target].dropna(subset=["T점수"])

    if cat == "전체" and mode in ["T점수", "Min-Max"]:
        plot = field_scores[field_scores["지역"] == target].copy()
        score_col = "T점수" if mode == "T점수" else "Min-Max점수"
        pct_col = "백분위_T" if mode == "T점수" else "백분위_MinMax"
        plot = plot[["항목", score_col, pct_col, "지표수"]].rename(
            columns={score_col: "점수", pct_col: "백분위"})
        axis_title = mode
    else:
        if cat == "전체":
            plot = (mine_all.groupby("대분류")
                    .agg(Z=("Z점수", "mean"), 백분위=("백분위", "mean"),
                         지표수=("지표명", "count"))
                    .reset_index().rename(columns={"대분류": "항목"}))
            plot["점수"] = 50 + 10 * plot["Z"]
        else:
            score_col = "Min-Max" if mode == "Min-Max" else "T점수"
            plot = (mine_all[mine_all["대분류"] == cat][["지표명", score_col, "백분위"]]
                    .rename(columns={"지표명": "항목", score_col: "점수"}))
            plot["지표수"] = 1
        axis_title = "T점수" if mode == "원자료" else mode

    order = st.radio("정렬", ["높은 값 순", "낮은 값 순"], horizontal=True)
    plot = plot.sort_values("점수", ascending=(order == "낮은 값 순"))

    fig2 = go.Figure(go.Bar(
        x=plot["점수"], y=plot["항목"], orientation="h",
        marker_color=np.where(plot["점수"] >= 50, "#1F4E9C", "#9BB8DE"),
        text=[f"{v:.0f}" for v in plot["점수"]], textposition="outside"))
    fig2.add_vline(x=50, line_dash="dash", line_color="gray")
    b_lo, b_hi = plot["점수"].min(), plot["점수"].max()
    pad = max(3, (b_hi - b_lo) * 0.25)
    fig2.update_layout(height=max(300, 45 * len(plot)), xaxis_title=axis_title,
                       xaxis_range=[b_lo - pad, b_hi + pad],
                       margin=dict(l=10, t=30, b=40))
    st.plotly_chart(fig2, use_container_width=True)

    pos = f"{group if is_sgg else level} 내 위치"
    plot[pos] = plot["백분위"].apply(
        lambda p: "중간" if 40 <= p <= 60 else
        (f"상위 {100-p:.0f}%" if p > 60 else f"하위 {p:.0f}%"))
    st.dataframe(plot[["항목", "점수", pos, "지표수"]].style.format({"점수": "{:.1f}"}),
                 use_container_width=True, hide_index=True)
else:
    st.info("지역을 선택하면 상세 진단이 표시됩니다.")

# ── 세부지표 전체 보기
st.divider()
with st.expander("세부지표 전체 보기", expanded=False):
    view = display_base if cat == "전체" else display_base[display_base["대분류"] == cat]
    view = view.copy()
    if "인구규모군" in view.columns:
        view["도시규모유형"] = view["인구규모군"]

    if target:
        c = ["시도명", "도시규모유형", "대분류", "지표명", "원자료", "Min-Max", "T점수", "백분위"]
        if not is_sgg:
            c += ["집계방식", "구성지역수", "출처"]
        c = [x for x in c if x in view.columns]
        tbl = view[view["지역"] == target][c].copy()
        tbl.insert(0, "번호", tbl["지표명"].map(IND_NO))
        tbl = tbl.sort_values("번호", kind="stable")
        st.caption(f"{target} · {len(tbl)}개 지표 · 번호는 입력 데이터의 지표 순서")
    else:
        index_cols = [x for x in ["시도명", "도시규모유형", "지역"] if x in view.columns]
        present_inds = [x for x in IND_ORDER if x in set(view["지표명"].astype(str))]
        tbl = (view.pivot_table(index=index_cols, columns="지표명", values=mode, sort=False)
               .reindex(columns=present_inds)
               .round(1).reset_index())
        # 행도 입력 데이터의 지역/상위단위 순서를 유지한다.
        if "지역" in tbl.columns:
            if is_sgg:
                tbl = sort_by_order(tbl, "지역", REGION_ORDER)
            elif level_col == "시도명":
                tbl = sort_by_order(tbl, "지역", SIDO_ORDER)
            else:
                tbl = sort_by_order(tbl, "지역", units)
        # 정렬 기능을 사용해도 원래 지표 순서를 확인할 수 있도록 번호를 열 이름에 붙인다.
        tbl = tbl.rename(columns={x: f"{IND_NO.get(x, 0):02d}. {x}" for x in present_inds})
        n_indicator_cols = len(present_inds)
        st.caption(f"{len(tbl)}개 단위 × {n_indicator_cols}개 지표 · 값 기준 {mode} · 지표 번호는 입력 순서")
    st.dataframe(tbl, use_container_width=True, hide_index=True, height=520)
    st.download_button("CSV 내려받기",
                       tbl.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"smartcity_{level}_{cat}_{mode}.csv",
                       mime="text/csv")

# ── 분야별·종합 점수 보기 (시군구 수준)
if is_sgg:
    with st.expander("분야별·종합 점수 보기", expanded=False):
        field_scores, total_scores = build_scores_for_current_group(base)
        score_tbl = pd.concat([field_scores, total_scores], ignore_index=True)
        if target:
            score_tbl = score_tbl[score_tbl["지역"] == target]
            st.caption(f"{target} · 지표 가중치 적용 (현재 기본값 1)")
        else:
            if visual_target != "전체":
                show_regions = set(display_base["지역"].dropna().unique())
                score_tbl = score_tbl[score_tbl["지역"].isin(show_regions)]
            st.caption(f"{group} 비교집단 · 시각화 대상 {visual_target} · 지표 가중치 적용 (현재 기본값 1)")

        # 지역 메타정보 추가: 시도명, 도시규모유형
        region_meta_cols = [c for c in ["지역", "시도명", "인구규모군"] if c in base.columns]
        region_meta = base.drop_duplicates("지역")[region_meta_cols].copy()
        if "인구규모군" in region_meta.columns:
            region_meta = region_meta.rename(columns={"인구규모군": "도시규모유형"})
        score_tbl = score_tbl.merge(region_meta, on="지역", how="left")
        score_tbl = score_tbl[[c for c in ["시도명", "도시규모유형", "지역", "항목",
                                                  "Min-Max점수", "T점수", "지표수", "가중치합"]
                                     if c in score_tbl.columns]]

        # wide 형식: 지역을 행으로, 분야 및 종합을 열로 표시
        field_present = set(field_scores["항목"].dropna().astype(str))
        item_order = [x for x in CAT_ORDER if x in field_present] + ["종합"]
        index_cols = [c for c in ["시도명", "도시규모유형", "지역"] if c in score_tbl.columns]
        wide = score_tbl.pivot(index=index_cols, columns="항목", values=["Min-Max점수", "T점수"])
        wide = wide.reindex(columns=pd.MultiIndex.from_product(
            [["Min-Max점수", "T점수"], [c for c in item_order if c in score_tbl["항목"].values]]
        ))
        wide.columns = [f"{score_type} | {item}" for score_type, item in wide.columns]
        wide = wide.reset_index()
        if "지역" in wide.columns:
            wide = sort_by_order(wide, "지역", REGION_ORDER)

        fmt = {c: "{:.1f}" for c in wide.columns if c not in index_cols}
        st.dataframe(
            wide.style.format(fmt),
            use_container_width=True, hide_index=True, height=520)

st.divider()
st.caption(f"{FORMULA} · 데이터 기준 {ts:%Y-%m-%d %H:%M:%S}")
