"""공개용 구글시트 → 원자료·Z점수·T점수·백분위 긴 테이블"""
import io
import urllib.request
import pandas as pd
import numpy as np

DEF_GID = 0                # 지표정의
DATA_GID = 23863734        # 가공된 지표정리_요약과 순서 일치


def fetch_grid(sheet_id, gid):
    """gid로 CSV를 받아 gspread get_all_values()와 같은 문자열 2차원 배열로 반환"""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=60).read()
    df = pd.read_csv(io.BytesIO(raw), header=None, dtype=str, keep_default_na=False)
    return df.values.tolist()


def load_definitions(raw):
    df = pd.DataFrame(raw[1:], columns=raw[0])
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"테이터유형": "데이터유형", "지표 계산": "지표계산"})

    df = df.iloc[4:].reset_index(drop=True)      # 5번째 행부터 실제 지표
    df["col_idx"] = df.index + 4                 # 데이터시트 E열 = 인덱스 4
    df = df[df["지표명"].str.strip() != ""]

    df["방향"] = np.where(df["의미"].str.contains("작을수록", na=False), -1, 1)
    df["유형"] = np.select(
        [df["의미"].str.contains("1은 있음", na=False),
         df["의미"].str.contains("독자시스템", na=False)],
        ["binary", "ordinal"], default="continuous")
    return df


def load_values(defs, raw):
    data = pd.DataFrame(raw[4:])                 # 5행부터 값
    use = defs[defs["사용여부"] == "O"]

    need = int(use["col_idx"].max()) + 1
    if data.shape[1] < need:
        raise ValueError(f"데이터 시트 열 수 부족: {data.shape[1]}개, 최소 {need}개 필요")

    cols = [0, 1, 2, 3] + list(use["col_idx"])
    df = data.iloc[:, cols].copy()
    df.columns = ["시도명", "시군구명", "지역", "인구규모"] + list(use["지표명"])
    df = df[df["지역"].str.strip() != ""]

    long = df.melt(id_vars=["시도명", "시군구명", "지역", "인구규모"],
                   var_name="지표명", value_name="원자료")
    for c in ["원자료", "인구규모"]:
        long[c] = pd.to_numeric(
            long[c].astype(str).str.replace(",", "").str.strip(), errors="coerce")

    valid = long.groupby("지표명")["원자료"].transform("count") > 0   # 전 지역 결측 지표 제외
    long = long[valid]
    return long.merge(use[["지표명", "대분류", "데이터유형", "의미", "방향", "유형"]],
                      on="지표명", how="left")


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


def add_groups(long):
    """비교집단용 파생 컬럼"""
    out = long.copy()
    out["유형구분"] = np.where(
        out["시도명"].str.contains("특별시|광역시|특별자치시", na=False), "특별·광역시",
        np.where(out["시군구명"].str.endswith("군", na=False), "군 지역", "시 지역"))

    pop = out.drop_duplicates("지역")[["지역", "인구규모"]]
    q = pop["인구규모"].quantile([.25, .5, .75]).values
    labels = ["소규모(하위 25%)", "중소규모", "중규모", "대규모(상위 25%)"]
    pop["인구규모군"] = pd.cut(pop["인구규모"], [-np.inf, *q, np.inf], labels=labels)
    return out.merge(pop[["지역", "인구규모군"]], on="지역", how="left")


OUT_COLS = ["시도명", "시군구명", "지역", "인구규모", "유형구분", "인구규모군",
            "대분류", "지표명", "데이터유형", "의미", "방향", "유형",
            "원자료", "Z점수", "T점수", "백분위"]


def build_base(sheet_id):
    """시트 → 표준화 전 긴 테이블(비교집단 컬럼 포함). T점수는 앱에서 집단별로 계산."""
    defs = load_definitions(fetch_grid(sheet_id, DEF_GID))
    long = load_values(defs, fetch_grid(sheet_id, DATA_GID))
    return add_groups(long).reset_index(drop=True)
