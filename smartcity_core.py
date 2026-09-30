"""공개용 구글시트 → 원자료 긴 테이블 + 집계 수준별 재집계"""
import io
import urllib.request
import pandas as pd
import numpy as np

DEF_GID = 0                 # 지표정의
DATA_GID = 23863734         # 가공된 지표정리_요약과 순서 일치
DENOM_GID = 1824252943      # 분모
SIDO_DEF_GID = 263049616    # 시도지표정의 (아직 비어 있을 수 있음)
SIDO_DATA_GID = 726884330   # 시도데이터

POP_BINS = [-np.inf, 100_000, 300_000, 500_000, 1_000_000, np.inf]
POP_LABELS = ["10만 미만", "10만~30만", "30만~50만", "50만~100만", "100만 이상"]
CAPITAL = ["서울특별시", "인천광역시", "경기도"]

LEVELS = {"시군구별": None, "시도별": "시도명",
          "도시규모별": "인구규모군", "수도권-비수도권": "권역"}


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
                         .str.replace(",", "").str.replace("%", "").str.strip(),
                         errors="coerce")


def load_definitions(raw):
    df = pd.DataFrame(raw[1:], columns=raw[0])
    df.columns = [str(c).strip() for c in df.columns]

    # 시군구지표정의의 고정 위치(J=집계 분모, K=집계 방식)를 원문 그대로 보존한다.
    # 구글시트 CSV 헤더에 보이지 않는 공백/개행 등이 섞여 정확한 헤더명이
    # 매칭되지 않는 경우에도 J/K 값을 사용할 수 있도록 하는 최소 안전장치다.
    pos_denom = df.iloc[:, 9].astype(str) if df.shape[1] > 9 else pd.Series("", index=df.index)
    pos_how = df.iloc[:, 10].astype(str) if df.shape[1] > 10 else pd.Series("", index=df.index)

    df = df.rename(columns={"테이터유형": "데이터유형", "지표 계산": "지표계산",
                            "데이터시트 열번호": "열번호",
                            "합계_분모": "집계_분모", "합계_방식": "집계방식",
                            "집계_분포": "집계_분모"})
    for c in ["집계_분모", "집계방식", "열번호"]:
        if c not in df.columns:
            df[c] = ""

    # 헤더 인식이 실패했거나 일부 행이 비어 있으면 J/K의 실제 셀값으로 보완한다.
    den_blank = df["집계_분모"].astype(str).str.strip().eq("")
    how_blank = df["집계방식"].astype(str).str.strip().eq("")
    df.loc[den_blank, "집계_분모"] = pos_denom.loc[den_blank].values
    df.loc[how_blank, "집계방식"] = pos_how.loc[how_blank].values

    # 시군구지표정의 L열의 가중치 사용. 헤더가 없으면 L열 값을 직접 사용하고,
    # 값이 비어 있으면 기본값 1로 처리한다.
    if "가중치" not in df.columns:
        df["가중치"] = df.iloc[:, 11] if df.shape[1] > 11 else 1.0
    df["가중치"] = pd.to_numeric(df["가중치"], errors="coerce").fillna(1.0)

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
             "집계_분모", "집계방식", "가중치"]], on="지표명", how="left")


def load_denominators(raw):
    """분모 시트 → 지역 × 분모변수 wide 테이블.

    열 이름 중복 등으로 특정 열 선택이 DataFrame이 되는 문제를 피하기 위해
    헤더명이 아니라 열 위치 기준으로 읽는다. 오류는 여기서 숨기지 않고
    호출부에서 실제 예외 메시지를 진단정보로 보존한다.
    """
    if not raw or not raw[0]:
        raise ValueError("분모 시트가 비어 있음")

    headers = [str(c).strip() for c in raw[0]]
    body = pd.DataFrame(raw[1:])
    if body.empty or body.shape[1] == 0:
        raise ValueError("분모 시트에 데이터 행이 없음")

    # 첫 열은 지역키. 열 이름이 중복되어도 위치 기준으로 안전하게 처리한다.
    region = body.iloc[:, 0].astype(str).str.strip()
    out = pd.DataFrame({"지역": region})

    seen = {"지역"}
    ncols = min(len(headers), body.shape[1])
    for j in range(1, ncols):
        name = headers[j]
        if not name or name in seen:
            continue
        out[name] = to_num(body.iloc[:, j])
        seen.add(name)

    out = out[out["지역"] != ""].reset_index(drop=True)
    if out.empty:
        raise ValueError("분모 시트의 지역키가 모두 비어 있음")
    return out


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
            # 가중평균 진단정보를 집계방식에 함께 표시한다.
            # 계산 자체는 기존과 동일하고, 왜 단순평균으로 대체되었는지만 구분한다.
            dname = str(meta["집계_분모"]).strip()
            w = None

            denom_error = (denom.attrs.get("load_error", "")
                           if isinstance(denom, pd.DataFrame) else "")
            if denom_error:
                val = v.mean()
                how = f"단순평균(분모 시트 오류: {denom_error})"
            elif denom is None:
                val = v.mean()
                how = "단순평균(분모 시트 읽기 실패)"
            elif not dname:
                val = v.mean()
                how = "단순평균(분모명 미지정)"
            elif dname not in denom.columns:
                val = v.mean()
                how = f"단순평균(분모열 없음: {dname})"
            else:
                tmp = g[["지역"]].merge(
                    denom[["지역", dname]], on="지역", how="left")
                w = pd.to_numeric(tmp[dname], errors="coerce").values

                n_group = len(g)
                n_weight = int(np.sum(~pd.isna(w)))
                m = v.notna().values & ~pd.isna(w)
                n_used = int(np.sum(m))
                weight_sum = float(np.nansum(w[m])) if n_used else 0.0

                if n_used == 0:
                    val = v.mean()
                    how = (f"단순평균(분모 지역매칭/값 없음: {dname}; "
                           f"분모매칭 {n_weight}/{n_group})")
                elif weight_sum <= 0:
                    val = v.mean()
                    how = (f"단순평균(분모합 0: {dname}; "
                           f"분모매칭 {n_weight}/{n_group}, 계산사용 {n_used}곳)")
                else:
                    val = np.nansum(v.values[m] * w[m]) / weight_sum
                    how = (f"가중평균(분모={dname}; "
                           f"분모매칭 {n_weight}/{n_group}, 계산사용 {n_used}곳)")
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


def add_tscore(long, group_col=None):
    """group_col=None이면 전체, 컬럼명을 주면 그 안에서 표준화"""
    out = long.copy()
    keys = ["지표명"] + ([group_col] if group_col else [])
    g = out.groupby(keys)["원자료"]
    z = (out["원자료"] - g.transform("mean")) / g.transform("std").replace(0, np.nan)
    out["Z점수"] = z * out["방향"]
    out["T점수"] = 50 + 10 * out["Z점수"]
    out["백분위"] = out.groupby(keys)["Z점수"].rank(pct=True) * 100
    return out


def add_minmax(long, group_col=None):
    """지표별 Min-Max 0~100 점수. 방향을 반영하고, 상수 지표는 50점 처리."""
    out = long.copy()
    keys = ["지표명"] + ([group_col] if group_col else [])
    g = out.groupby(keys)["원자료"]
    lo = g.transform("min")
    hi = g.transform("max")
    span = hi - lo
    mm = (out["원자료"] - lo) / span.replace(0, np.nan) * 100
    mm = np.where((span == 0) & out["원자료"].notna(), 50.0, mm)
    out["Min-Max"] = np.where(out["방향"] >= 0, mm, 100 - mm)
    return out


def _weighted_average(g, value_col):
    """결측을 제외한 지표 가중평균. 가중치가 없으면 모두 1."""
    v = pd.to_numeric(g[value_col], errors="coerce")
    if "가중치" in g.columns:
        w = pd.to_numeric(g["가중치"], errors="coerce").fillna(1.0)
    else:
        w = pd.Series(1.0, index=g.index, dtype=float)
    m = v.notna() & w.notna() & (w >= 0)
    if not m.any() or w[m].sum() == 0:
        return np.nan
    return float((v[m] * w[m]).sum() / w[m].sum())


def _restandardize(series):
    """평균 50, 표준편차 10의 T점수로 재표준화. 분산이 0이면 50."""
    s = pd.to_numeric(series, errors="coerce")
    mean = s.mean()
    sd = s.std()
    if pd.isna(sd) or sd == 0:
        return pd.Series(np.where(s.notna(), 50.0, np.nan), index=s.index)
    return 50 + 10 * (s - mean) / sd


def build_field_total_scores(scored):
    """
    시군구(또는 현재 비교집단) 자료에서 분야별·종합 점수를 산출.
    - Min-Max: 개별 Min-Max 점수의 가중평균
    - T 방식: 개별 T를 S=clip(100*(T-20)/60, 0, 100)으로 환산해 가중평균 후
      분야 및 종합 점수를 다시 T점수화
    """
    d = scored.copy()
    if "Min-Max" not in d.columns:
        d = add_minmax(d)
    if "T점수" not in d.columns:
        d = add_tscore(d)

    d["T합산점수"] = ((d["T점수"] - 20) / 60 * 100).clip(0, 100)

    # 분야별
    rows = []
    for (region, field), g in d.groupby(["지역", "대분류"], observed=True):
        w = (pd.to_numeric(g["가중치"], errors="coerce").fillna(1.0)
             if "가중치" in g.columns else pd.Series(1.0, index=g.index))
        valid_w = w[g["Min-Max"].notna()]
        rows.append({
            "지역": region,
            "항목": field,
            "Min-Max점수": _weighted_average(g, "Min-Max"),
            "T원점수": _weighted_average(g, "T합산점수"),
            "지표수": int(g["지표명"].nunique()),
            "가중치합": float(valid_w.sum()) if len(valid_w) else 0.0,
        })
    field = pd.DataFrame(rows)
    if not field.empty:
        field["T점수"] = field.groupby("항목", group_keys=False)["T원점수"].transform(_restandardize)
        field["백분위_MinMax"] = field.groupby("항목")["Min-Max점수"].rank(pct=True) * 100
        field["백분위_T"] = field.groupby("항목")["T점수"].rank(pct=True) * 100

    # 종합: 분야평균이 아니라 전체 개별지표에 각 지표 가중치를 직접 적용
    rows = []
    for region, g in d.groupby("지역", observed=True):
        w = (pd.to_numeric(g["가중치"], errors="coerce").fillna(1.0)
             if "가중치" in g.columns else pd.Series(1.0, index=g.index))
        valid_w = w[g["Min-Max"].notna()]
        rows.append({
            "지역": region,
            "항목": "종합",
            "Min-Max점수": _weighted_average(g, "Min-Max"),
            "T원점수": _weighted_average(g, "T합산점수"),
            "지표수": int(g["지표명"].nunique()),
            "가중치합": float(valid_w.sum()) if len(valid_w) else 0.0,
        })
    total = pd.DataFrame(rows)
    if not total.empty:
        total["T점수"] = _restandardize(total["T원점수"])
        total["백분위_MinMax"] = total["Min-Max점수"].rank(pct=True) * 100
        total["백분위_T"] = total["T점수"].rank(pct=True) * 100

    return field, total


def build_base(sheet_id):
    """시트 → 표준화 전 시군구 긴 테이블 + 분모 + 시도 실측"""
    defs = load_definitions(fetch_grid(sheet_id, DEF_GID))
    long = add_groups(load_values(defs, fetch_grid(sheet_id, DATA_GID)))
    long["출처"] = "원자료"
    long["집계상세"] = ""
    long["구성지역수"] = 1

    try:
        denom = load_denominators(fetch_grid(sheet_id, DENOM_GID))
    except Exception as e:
        # 기존 반환 구조(3개)는 유지하되, 빈 DataFrame의 attrs에 실제 오류를 보존한다.
        # app/aggregate에서 이를 그대로 진단문구로 표시할 수 있다.
        denom = pd.DataFrame()
        denom.attrs["load_error"] = f"{type(e).__name__}: {e}"

    return long.reset_index(drop=True), denom, load_sido_actual(sheet_id)
