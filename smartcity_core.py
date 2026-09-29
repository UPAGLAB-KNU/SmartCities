"""공개용 구글시트 → 원자료 긴 테이블 + 시군구 점수화 + 집계 수준별 재집계"""
import io
import urllib.request
import pandas as pd
import numpy as np

DEF_GID = 0                 # 시군구지표정의(첫 시트)
DATA_GID = 23863734         # 가공된 지표정리_요약과 순서 일치
DENOM_GID = 1824252943      # 분모
SIDO_DEF_GID = 263049616    # 시도지표정의 (아직 비어 있을 수 있음)
SIDO_DATA_GID = 726884330   # 시도데이터

POP_BINS = [-np.inf, 100_000, 300_000, 500_000, 1_000_000, np.inf]
POP_LABELS = ["10만 미만", "10만~30만", "30만~50만", "50만~100만", "100만 이상"]
CAPITAL = ["서울특별시", "인천광역시", "경기도"]

LEVELS = {"시군구별": None, "시도별": "시도명",
          "도시규모별": "인구규모군", "수도권-비수도권": "권역"}

# T점수 기반 합산용 점수의 고정 범위
T_AGG_LO = 20.0
T_AGG_HI = 80.0


def fetch_grid(sheet_id, gid):
    """gid로 CSV를 받아 문자열 2차원 배열로 반환"""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=60).read()
    df = pd.read_csv(io.BytesIO(raw), header=None, dtype=str, keep_default_na=False)
    return df.values.tolist()


def col_to_idx(letters):
    """엑셀 열 문자(A, B, ..., AA)를 0-기반 인덱스로"""
    n = 0
    for ch in str(letters).strip().upper():
        if not ch.isalpha():
            return None
        n = n * 26 + (ord(ch) - 64)
    return n - 1 if n else None


def to_num(s):
    return pd.to_numeric(pd.Series(s).astype(str)
                         .str.replace(",", "", regex=False)
                         .str.replace("%", "", regex=False)
                         .str.strip(),
                         errors="coerce")


def load_definitions(raw):
    """지표정의 시트를 읽는다. L열(0기반 11번)을 가중치로 사용하며 기본값은 1."""
    if not raw or len(raw) < 2:
        raise ValueError("지표정의 시트가 비어 있습니다.")

    header = [str(c).strip() for c in raw[0]]
    df = pd.DataFrame(raw[1:], columns=header)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"테이터유형": "데이터유형", "지표 계산": "지표계산",
                            "데이터시트 열번호": "열번호",
                            "합계_분모": "집계_분모", "합계_방식": "집계방식",
                            "집계_분포": "집계_분모"})
    for c in ["집계_분모", "집계방식", "열번호"]:
        if c not in df.columns:
            df[c] = ""

    # 사용자가 시군구지표정의 L열에 가중치를 둔다고 지정.
    # 헤더가 '가중치'가 아니어도 L열 값을 우선적으로 읽을 수 있게 한다.
    if len(header) > 11:
        l_values = pd.Series([row[11] if len(row) > 11 else "" for row in raw[1:]], index=df.index)
        if "가중치" not in df.columns:
            df["가중치"] = l_values
        else:
            # 가중치 열이 있으나 비어 있는 행은 L열 값으로 보완
            blank = df["가중치"].astype(str).str.strip().eq("")
            df.loc[blank, "가중치"] = l_values.loc[blank]
    elif "가중치" not in df.columns:
        df["가중치"] = 1

    df["가중치"] = pd.to_numeric(df["가중치"], errors="coerce").fillna(1.0)
    df.loc[df["가중치"] < 0, "가중치"] = 0.0

    df = df[df["지표명"].astype(str).str.strip() != ""].reset_index(drop=True)
    df["col_idx"] = df["열번호"].map(col_to_idx)
    df = df[df["col_idx"].notna()].copy()
    df["col_idx"] = df["col_idx"].astype(int)
    df = df[df["col_idx"] >= 4]                  # A~D는 식별자, E열부터 지표

    df["방향"] = np.where(df["의미"].str.contains("작을수록", na=False), -1, 1)
    df["유형"] = np.select(
        [df["의미"].str.contains("1은 있음", na=False),
         df["의미"].str.contains("독자시스템", na=False)],
        ["binary", "ordinal"], default="continuous")
    df["집계방식"] = df["집계방식"].astype(str).str.strip().replace("", "단순평균")
    df["집계_분모"] = df["집계_분모"].astype(str).str.strip()
    return df


def load_values(defs, raw):
    data = pd.DataFrame(raw[4:])                 # 5행부터 값
    use = defs[defs["사용여부"] == "O"]

    if use.empty:
        raise ValueError("사용여부='O'인 지표가 없습니다.")

    need = int(use["col_idx"].max()) + 1
    if data.shape[1] < need:
        raise ValueError(f"데이터 시트 열 수 부족: {data.shape[1]}개, 최소 {need}개 필요")

    df = data.iloc[:, [0, 1, 2, 3] + list(use["col_idx"])].copy()
    df.columns = ["시도명", "시군구명", "지역", "인구규모"] + list(use["지표명"])
    df = df[df["지역"].astype(str).str.strip() != ""]

    long = df.melt(id_vars=["시도명", "시군구명", "지역", "인구규모"],
                   var_name="지표명", value_name="원자료")
    long["원자료"] = to_num(long["원자료"])
    long["인구규모"] = to_num(long["인구규모"])

    valid = long.groupby("지표명")["원자료"].transform("count") > 0
    long = long[valid]
    return long.merge(
        use[["지표명", "대분류", "데이터유형", "의미", "방향", "유형",
             "가중치", "집계_분모", "집계방식"]], on="지표명", how="left")


def load_denominators(raw):
    """분모 시트 → 지역 × 분모변수 wide 테이블. 실패하면 None"""
    try:
        df = pd.DataFrame(raw[1:], columns=[c.strip() for c in raw[0]])
        key = df.columns[0]                      # 첫 열이 '시도시군구'
        df = df[df[key].astype(str).str.strip() != ""].copy()
        out = pd.DataFrame({"지역": df[key].str.strip()})
        for c in df.columns[1:]:
            if c.strip():
                out[c.strip()] = to_num(df[c])
        return out.loc[:, ~out.columns.duplicated()]
    except Exception:
        return None


def add_groups(long):
    """비교집단·집계 수준용 파생 컬럼"""
    out = long.copy()
    out["유형구분"] = np.where(
        out["시도명"].str.contains("특별시|광역시|특별자치시", na=False), "특별·광역시",
        np.where(out["시군구명"].str.endswith("군", na=False), "군 지역", "시 지역"))
    out["권역"] = np.where(out["시도명"].isin(CAPITAL), "수도권", "비수도권")

    pop = out.drop_duplicates("지역")[["지역", "인구규모"]].copy()
    pop["인구규모군"] = pd.cut(pop["인구규모"], POP_BINS, labels=POP_LABELS)
    return out.merge(pop[["지역", "인구규모군"]], on="지역", how="left")


def load_sido_actual(sheet_id):
    """시도 실측 시트. 없거나 비어 있으면 None"""
    try:
        sdefs = load_definitions(fetch_grid(sheet_id, SIDO_DEF_GID))
        raw = fetch_grid(sheet_id, SIDO_DATA_GID)
        data = pd.DataFrame(raw[4:])
        use = sdefs[sdefs["사용여부"] == "O"]
        if use.empty or data.empty:
            return None
        df = data.iloc[:, [0] + list(use["col_idx"])].copy()
        df.columns = ["시도명"] + list(use["지표명"])
        df = df[df["시도명"].astype(str).str.strip() != ""]
        if df.empty:
            return None
        long = df.melt(id_vars=["시도명"], var_name="지표명", value_name="실측값")
        long["실측값"] = to_num(long["실측값"])
        return long.dropna(subset=["실측값"])
    except Exception:
        return None


def aggregate(long, level_col, denom=None, sido_actual=None):
    """집계 수준별 재집계. level_col=None이면 시군구 원본 그대로."""
    if level_col is None:
        return long.copy()

    df = long.copy()
    rows = []

    for (grp, ind), g in df.groupby([level_col, "지표명"], observed=True):
        meta = g.iloc[0]
        how = meta["집계방식"]
        v = g["원자료"]

        n_valid = int(v.notna().sum())
        detail = ""

        if how == "비율":                              # 이진 지표 도입률
            val = v.mean() * 100 if n_valid else np.nan
            detail = f"{n_valid}곳 중 {int(v.sum())}곳" if n_valid else ""
        elif how == "합산":
            val = v.sum() if n_valid else np.nan
        elif how == "가중평균":
            w = None
            dname = meta["집계_분모"]
            if denom is not None and dname and dname in denom.columns:
                w = g[["지역"]].merge(denom[["지역", dname]], on="지역",
                                      how="left")[dname].values
            if w is not None and np.nansum(w) > 0:
                m = v.notna().values & ~pd.isna(w)
                val = np.nansum(v.values[m] * w[m]) / np.nansum(w[m]) if m.any() else np.nan
            else:
                val = v.mean()                          # 분모 없으면 단순평균
                how = "단순평균(분모 없음)"
        else:
            val = v.mean() if n_valid else np.nan

        rows.append({level_col: grp, "지역": grp, "지표명": ind, "원자료": val,
                     "대분류": meta["대분류"], "데이터유형": meta["데이터유형"],
                     "의미": meta["의미"], "방향": meta["방향"], "유형": meta["유형"],
                     "가중치": meta.get("가중치", 1.0),
                     "집계방식": how, "집계상세": detail, "구성지역수": n_valid,
                     "출처": "집계"})

    out = pd.DataFrame(rows)

    # 시도 실측이 있으면 그 값으로 대체
    if level_col == "시도명" and sido_actual is not None and not out.empty:
        out = out.merge(sido_actual.rename(columns={"시도명": "지역"}),
                        on=["지역", "지표명"], how="left")
        hit = out["실측값"].notna()
        out.loc[hit, "원자료"] = out.loc[hit, "실측값"]
        out.loc[hit, "출처"] = "실측"
        out = out.drop(columns=["실측값"])
    return out


def _safe_standardize(values):
    """유효값의 표본표준편차가 0이면 Z=0, 아니면 일반 Z. 결측은 결측 유지."""
    s = pd.Series(values, index=getattr(values, "index", None), dtype="float64")
    mean = s.mean()
    std = s.std(ddof=1)
    if pd.isna(std) or std == 0:
        z = pd.Series(np.where(s.notna(), 0.0, np.nan), index=s.index, dtype="float64")
    else:
        z = (s - mean) / std
    return z


def add_tscore(long, group_col=None):
    """T점수. group_col=None이면 전체 표본을 지표별로 표준화."""
    out = long.copy()
    keys = ["지표명"] + ([group_col] if group_col else [])
    g = out.groupby(keys, observed=True)["원자료"]
    mean = g.transform("mean")
    std = g.transform("std")
    z = (out["원자료"] - mean) / std.replace(0, np.nan)
    # 값이 있으나 분산이 0이거나 표본 1개라 표준편차가 계산되지 않으면 중립값(Z=0)
    neutral = out["원자료"].notna() & (std.isna() | std.eq(0))
    z.loc[neutral] = 0.0
    out["Z점수"] = z * out["방향"].astype(float)
    out["T점수"] = 50 + 10 * out["Z점수"]
    out["백분위"] = out.groupby(keys, observed=True)["T점수"].rank(pct=True) * 100
    return out


def add_minmax_score(long, group_col=None):
    """지표별 0~100 Min-Max 점수. 역방향 지표는 100-점수. 상수 지표는 50."""
    out = long.copy()
    keys = ["지표명"] + ([group_col] if group_col else [])
    g = out.groupby(keys, observed=True)["원자료"]
    lo = g.transform("min")
    hi = g.transform("max")
    rng = hi - lo
    mm = 100 * (out["원자료"] - lo) / rng.replace(0, np.nan)
    mm = np.where(out["방향"].astype(float) < 0, 100 - mm, mm)
    mm = pd.Series(mm, index=out.index, dtype="float64")
    const = out["원자료"].notna() & rng.eq(0)
    mm.loc[const] = 50.0
    out["MinMax점수"] = mm.clip(0, 100)
    out["MinMax백분위"] = out.groupby(keys, observed=True)["MinMax점수"].rank(pct=True) * 100
    return out


def add_all_scores(long, group_col=None, t_lo=T_AGG_LO, t_hi=T_AGG_HI):
    """개별지표에 T점수, Min-Max점수, T기반 합산용 0~100점수를 모두 추가."""
    out = add_minmax_score(long, group_col=group_col)
    out = add_tscore(out, group_col=group_col)
    denom = float(t_hi - t_lo)
    if denom <= 0:
        raise ValueError("T점수 합산 범위의 상한은 하한보다 커야 합니다.")
    out["T합산점수"] = (100 * (out["T점수"] - t_lo) / denom).clip(0, 100)
    return out


def _weighted_group_score(g, value_col):
    """결측과 가중치 0을 제외한 가중평균 및 진단값."""
    if "가중치" in g.columns:
        w = pd.to_numeric(g["가중치"], errors="coerce").fillna(1.0)
    else:
        # 이전 캐시/구버전 raw에도 안전하게 동작. 현재 기본 가중치는 모두 1.
        w = pd.Series(1.0, index=g.index, dtype="float64")
    x = pd.to_numeric(g[value_col], errors="coerce")
    ok = x.notna() & w.notna() & (w > 0)
    if not ok.any() or w.loc[ok].sum() <= 0:
        return pd.Series({"점수": np.nan, "지표수": 0, "가중치합": 0.0})
    return pd.Series({
        "점수": np.average(x.loc[ok], weights=w.loc[ok]),
        "지표수": int(ok.sum()),
        "가중치합": float(w.loc[ok].sum()),
    })


def _restandardize(df, raw_col, by=None, out_col="T점수"):
    """raw_col을 평균 50, 표준편차 10의 T점수로 재표준화."""
    out = df.copy()
    if by is None:
        z = _safe_standardize(out[raw_col])
        out[out_col] = 50 + 10 * z
        pct_col = out_col[:-2] + "백분위" if out_col.endswith("점수") else out_col + "_백분위"
        out[pct_col] = out[out_col].rank(pct=True) * 100
        return out

    def calc(g):
        g = g.copy()
        z = _safe_standardize(g[raw_col])
        g[out_col] = 50 + 10 * z
        pct_col = out_col[:-2] + "백분위" if out_col.endswith("점수") else out_col + "_백분위"
        g[pct_col] = g[out_col].rank(pct=True) * 100
        return g

    return out.groupby(by, group_keys=False, observed=True).apply(calc)


def build_sgg_scores(long, t_lo=T_AGG_LO, t_hi=T_AGG_HI):
    """
    전국 시군구 기준 점수 세트.

    반환
    ----
    detail : 개별지표 원자료 + MinMax점수 + T점수 + T합산점수 + 가중치
    field  : 지역×대분류 분야 점수
             - MinMax분야점수: 개별 MinMax점수의 가중평균
             - T분야원점수: 개별 T합산점수의 가중평균(0~100)
             - T분야점수: T분야원점수를 대분류별로 전국 재표준화
    total  : 지역별 종합 점수
             - MinMax종합점수: 전체 개별 MinMax점수의 가중평균
             - T종합원점수: 전체 개별 T합산점수의 가중평균(0~100)
             - T종합점수: T종합원점수를 전국 재표준화
    """
    detail = add_all_scores(long, group_col=None, t_lo=t_lo, t_hi=t_hi)

    # 분야별 Min-Max / T합산 원점수
    mm_field = (detail.groupby(["지역", "대분류"], observed=True)
                .apply(lambda g: _weighted_group_score(g, "MinMax점수"))
                .reset_index()
                .rename(columns={"점수": "MinMax분야점수",
                                 "지표수": "MinMax지표수",
                                 "가중치합": "MinMax가중치합"}))
    t_field = (detail.groupby(["지역", "대분류"], observed=True)
               .apply(lambda g: _weighted_group_score(g, "T합산점수"))
               .reset_index()
               .rename(columns={"점수": "T분야원점수",
                                "지표수": "T지표수",
                                "가중치합": "T가중치합"}))
    field = mm_field.merge(t_field, on=["지역", "대분류"], how="outer")
    field = _restandardize(field, "T분야원점수", by="대분류", out_col="T분야점수")

    # 지역 식별자 부착
    ids = (long.drop_duplicates("지역")
           [[c for c in ["지역", "시도명", "시군구명", "인구규모", "인구규모군", "유형구분", "권역"]
             if c in long.columns]])
    field = field.merge(ids, on="지역", how="left")

    # 종합: 분야 평균이 아니라 개별 지표 가중치를 전체 지표에 직접 적용
    mm_total = (detail.groupby("지역", observed=True)
                .apply(lambda g: _weighted_group_score(g, "MinMax점수"))
                .reset_index()
                .rename(columns={"점수": "MinMax종합점수",
                                 "지표수": "MinMax지표수",
                                 "가중치합": "MinMax가중치합"}))
    t_total = (detail.groupby("지역", observed=True)
               .apply(lambda g: _weighted_group_score(g, "T합산점수"))
               .reset_index()
               .rename(columns={"점수": "T종합원점수",
                                "지표수": "T지표수",
                                "가중치합": "T가중치합"}))
    total = mm_total.merge(t_total, on="지역", how="outer")
    total = _restandardize(total, "T종합원점수", by=None, out_col="T종합점수")
    total = total.merge(ids, on="지역", how="left")

    return detail.reset_index(drop=True), field.reset_index(drop=True), total.reset_index(drop=True)


def build_base(sheet_id):
    """시트 → 표준화 전 시군구 긴 테이블 + 분모 + 시도 실측"""
    defs = load_definitions(fetch_grid(sheet_id, DEF_GID))
    long = add_groups(load_values(defs, fetch_grid(sheet_id, DATA_GID)))
    long["출처"] = "원자료"
    long["집계상세"] = ""
    long["구성지역수"] = 1

    try:
        denom = load_denominators(fetch_grid(sheet_id, DENOM_GID))
    except Exception:
        denom = None

    return long.reset_index(drop=True), denom, load_sido_actual(sheet_id)
