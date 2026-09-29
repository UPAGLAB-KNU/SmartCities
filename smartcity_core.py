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
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"테이터유형": "데이터유형", "지표 계산": "지표계산",
                            "데이터시트 열번호": "열번호",
                            "합계_분모": "집계_분모", "합계_방식": "집계방식",
                            "집계_분포": "집계_분모"})
    for c in ["집계_분모", "집계방식", "열번호"]:
        if c not in df.columns:
            df[c] = ""

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
             "집계_분모", "집계방식"]], on="지표명", how="left")


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
