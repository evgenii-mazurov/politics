"""
Полный конвейер по выборам в Госдуму (участки СПб) 2026: от сырых файлов до
набора, готового к загрузке для построения дашборда.

Входные файлы (все — из /mnt/user-data/uploads/, ничего не меняются):
    results.csv                — партсписок: протокол + голоса (protocol_num 1=округ, 2=список)
    spb_mandate.json           — одномандатные округа: протокол + голоса по кандидатам
    candidates_gd.csv          — кандидаты-одномандатники, колонки id_cand_gd/candidate/party
    parties_gd.csv             — партийный список (для читаемых названий партий)
    first_day_gd_uik.csv       — справочник УИК: tik_id, oik_gd_id, число избирателей
                                  (в источнике есть опечатка — см. шаг 0 ниже)
    uik_coords.csv             — координаты УИК (сырые, общие с ЗакС — те же физические адреса)
    tik_coords.csv             — координаты ТИК (сырые, общие с ЗакС)
    gd_oik_coords.csv          — полигоны округов ГД (сырые, долгота/широта, есть опечатка)
    gd_turnouts_uik.csv        — явка по времени замера (для графика «явка х размер участка»)

Выходные файлы: dim_party.csv,
gd_oik_coords_latlon.csv, turnout_timeline_uik_duma.csv, fact_votes_duma.csv,
map_layer_duma_party.csv, map_layer_duma_party_tik.csv, map_layer_duma_mandate.csv,
map_layer_duma_mandate_oik.csv, candidates_gd.csv (с party_key),
compare_party_mandate_duma.csv.

!!! Важно: uik_coords.csv/tik_coords.csv — общий для ЗакС и ГД физический список
адресов участков (те же здания). Если уже запускали build_zaks.py и файлы
uik_coords_jitter.csv/tik_coords_jitter.csv лежат в DST — пересчитывать не
обязательно, но этот скрипт всё равно их пересоздаст (результат идентичен).

Запускать одним файлом: python3 build_duma.py
"""
import json
import math
import re

import pandas as pd

SRC = "/mnt/user-data/uploads/"
DST = "/mnt/user-data/outputs/"  # поменяйте на свою рабочую папку


# ============================================================================
# 0. Исправление опечатки в первоисточнике: у 40 УИК в first_day_gd_uik.csv
#    указан tik_id = 47, хотя на самом деле они относятся к ТИК №57 (округ
#    там записан верно — oik_gd_id = 215 — ошибка только в номере ТИК).
#    Обнаружено пользователем вручную, детали см. в переписке.
# ============================================================================
UIK_WRONG_TIK = (
    list(range(1724, 1731)) + list(range(1893, 1899)) + list(range(1900, 1905))
    + list(range(1906, 1927)) + [2466]
)


def build_uik_ref_gd():
    u = pd.read_csv(SRC + "first_day_gd_uik.csv")
    mask = u.uik_id.isin(UIK_WRONG_TIK)
    assert mask.sum() == len(UIK_WRONG_TIK), "не все УИК из списка найдены в источнике"
    u.loc[mask, "tik_id"] = 57
    return u


# ============================================================================
# 1. Партии: сведение разных написаний к единому короткому коду
# ============================================================================
PARTY_KEY_MAP = {  # для файла кандидатов, где партия дана коротким названием
    "ЗЕЛЁНЫЕ": "green", "ЕДИНАЯ РОССИЯ": "er", "КПРФ": "kprf", "ЛДПР": "ldpr",
    "СПРАВЕДЛИВАЯ РОССИЯ": "sr", "НОВЫЕ ЛЮДИ": "np", "ЯБЛОКО": "yabloko",
    "КОММУНИСТЫ РОССИИ": "kp",
}
PARTY_KEY_PATTERNS = [
    ("er", r"ЕДИНАЯ РОССИЯ"),
    ("kprf", r"КПРФ|КОММУНИСТИЧЕСКАЯ ПАРТИЯ РОССИЙСКОЙ ФЕДЕРАЦИИ"),
    ("kp", r"КОММУНИСТЫ РОССИИ"),
    ("np", r"НОВЫЕ ЛЮДИ"),
    ("ldpr", r"ЛДПР"),
    ("green", r"ЗЕЛЁНЫЕ"),
    ("sr", r"СПРАВЕДЛИВАЯ РОССИЯ"),
    ("rodina", r"РОДИНА"),
    ("pens", r"ПЕНСИОНЕРОВ"),
    ("ppd", r"прямой демократии"),
    ("yabloko", r"ЯБЛОКО"),
]


def party_key(text):
    """Определяет короткий код партии по произвольному тексту (полное
    название из протокола или короткое из файла кандидатов)."""
    if not isinstance(text, str):
        return None
    for key, pat in PARTY_KEY_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            return key
    return None


def strip_name(s):
    """Убирает суффикс вида ' (ПАРТИЯ)' из строки 'Имя (ПАРТИЯ)' — нужно для
    JSON-источника округов, где имя и партия даны одной строкой."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", s).strip()


# ============================================================================
# 2. Координаты: перестановка долгота/широта -> широта/долгота + починка
#    известной опечатки в полигоне округа 211 + разведение точек на одном
#    адресе по окружности
# ============================================================================
def swap_lonlat(node):
    if node and isinstance(node[0], (int, float)):
        return [node[1], node[0]]
    return [swap_lonlat(x) for x in node]


def fix_broken_polygon_211(s):
    # недописанная точка и «(» вместо скобки в источнике
    return s.replace(
        "[30.5201703, 59.8238237,, ([30.52022, 59.823933]",
        "[30.5201703, 59.8238237]], [[30.52022, 59.823933]",
    )


def build_oik_polygons_gd():
    df = pd.read_csv(SRC + "gd_oik_coords.csv")
    df["coords_polygon"] = df["coords_polygon"].map(
        lambda s: json.dumps(swap_lonlat(json.loads(fix_broken_polygon_211(s))), separators=(",", ":"))
    )
    df.to_csv(DST + "gd_oik_coords_latlon.csv", index=False, encoding="utf-8")
    return df


def jitter_points(df, coord_col, id_col, radius_m=None):
    pts = df[coord_col].map(json.loads)
    df["lon_orig"] = pts.map(lambda p: p[0])
    df["lat_orig"] = pts.map(lambda p: p[1])
    df["lon"], df["lat"] = df["lon_orig"], df["lat_orig"]
    df["place_size"] = 1
    key = df["lon_orig"].round(6).astype(str) + "|" + df["lat_orig"].round(6).astype(str)
    for _, idx in df.groupby(key).groups.items():
        n = len(idx)
        if n == 1:
            continue
        r = radius_m or (6 if n <= 3 else 9)
        order = df.loc[idx].sort_values(id_col).index
        for k, i in enumerate(order):
            a = 2 * math.pi * k / n
            lat0 = df.at[i, "lat_orig"]
            df.at[i, "lat"] = lat0 + (r * math.cos(a)) / 111_320
            df.at[i, "lon"] = df.at[i, "lon_orig"] + (r * math.sin(a)) / (
                111_320 * math.cos(math.radians(lat0))
            )
            df.at[i, "place_size"] = n
    df["geopoint"] = [f"[{la:.7f},{lo:.7f}]" for la, lo in zip(df["lat"], df["lon"])]
    return df


def build_uik_coords():
    u = pd.read_csv(SRC + "uik_coords.csv", dtype=str)
    u = u.loc[:, ~u.columns.str.startswith("Unnamed")]
    u["uik_id"] = u["uik_name"].str.extract(r"(\d+)")[0].astype(int)
    u["district"] = u["district"].str.strip().replace({"Кронштадсткий": "Кронштадтский"})
    u = jitter_points(u, "coord", "uik_id")
    u.drop(columns=["coord"], inplace=True)
    u.to_csv(DST + "uik_coords_jitter.csv", index=False, encoding="utf-8")
    return u


def build_tik_coords():
    t = pd.read_csv(SRC + "tik_coords.csv")
    t = jitter_points(t, "coords", "tik_id")
    t.drop(columns=["coords"], inplace=True)
    t.to_csv(DST + "tik_coords_jitter.csv", index=False, encoding="utf-8")
    return t


# ============================================================================
# 3. Справочник партий (party_key -> читаемое название)
# ============================================================================
def build_dim_party():
    parties = pd.read_csv(SRC + "parties_gd.csv")
    parties["party_key"] = parties["party"].map(party_key)
    assert parties.party_key.notna().all(), "не распознана партия в parties_gd.csv"
    dim = parties[["party_key", "party", "id_party_gd"]].rename(columns={"party": "name_gd"})
    dim.to_csv(DST + "dim_party.csv", index=False)
    return dim


# ============================================================================
# 4. Кандидаты: добавляем party_key
# ============================================================================
def build_candidates():
    c = pd.read_csv(SRC + "candidates_gd.csv")
    c = c.loc[:, ~c.columns.str.startswith("Unnamed")]
    c["party_key"] = c["party"].map(lambda p: PARTY_KEY_MAP.get(p, party_key(p)))
    # опечатка в источнике: слипшиеся фамилия и имя у одного кандидата
    c["candidate"] = c["candidate"].replace({"ЮркевичОльга Изяславовна": "Юркевич Ольга Изяславовна"})
    c.to_csv(DST + "candidates_gd.csv", index=False)
    return c


# ============================================================================
# 5. Партсписок из results.csv (тот же формат, что у ЗакС results.csv,
#    protocol_num 2 = список; кандидаты округов там тоже есть, но для них
#    используем более подробный spb_mandate.json — см. шаг 6)
# ============================================================================
PROTOCOL_LINES_12 = [  # у ГД протокол на строку длиннее — есть «досрочное» голосование
    "Число избирателей, внесенных в список избирателей на момент окончания голосования",
    "Число избирательных бюллетеней, полученных участковой избирательной комиссией",
    "Число избирательных бюллетеней, выданных избирателям, проголосовавшим досрочно",
    "Число избирательных бюллетеней, выданных участковой избирательной комиссией избирателям в помещении для голосования в день голосования",
    "Число избирательных бюллетеней, выданных избирателям, проголосовавшим вне помещения для голосования в день голосования",
    "Число погашенных избирательных бюллетеней",
    "Число избирательных бюллетеней, содержащихся в переносных ящиках для голосования",
    "Число избирательных бюллетеней, содержащихся в стационарных ящиках для голосования",
    "Число недействительных избирательных бюллетеней",
    "Число действительных избирательных бюллетеней",
    "Число утраченных избирательных бюллетеней",
    "Число избирательных бюллетеней, не учтенных при получении",
]
SHORT_12 = {
    PROTOCOL_LINES_12[0]: "voters", PROTOCOL_LINES_12[1]: "received",
    PROTOCOL_LINES_12[2]: "issued_early", PROTOCOL_LINES_12[3]: "issued_station",
    PROTOCOL_LINES_12[4]: "issued_mobile", PROTOCOL_LINES_12[5]: "cancelled",
    PROTOCOL_LINES_12[6]: "box_mobile", PROTOCOL_LINES_12[7]: "box_station",
    PROTOCOL_LINES_12[8]: "invalid", PROTOCOL_LINES_12[9]: "valid",
    PROTOCOL_LINES_12[10]: "lost", PROTOCOL_LINES_12[11]: "unaccounted",
}
# для протокола округов (из JSON) нужны те же три поля «выдано», но по их
# полным русским названиям, как они приходят в spb_mandate.json
ISSUED_COLS_MANDATE = [
    "Число избирательных бюллетеней, выданных избирателям, проголосовавшим досрочно",
    "Число избирательных бюллетеней, выданных участковой избирательной комиссией избирателям в помещении для голосования в день голосования",
    "Число избирательных бюллетеней, выданных избирателям, проголосовавшим вне помещения для голосования в день голосования",
]
VOTERS_COL_MANDATE = "Число избирателей, внесенных в список избирателей на момент окончания голосования"
INVALID_COL_MANDATE = "Число недействительных избирательных бюллетеней"


def build_party_list(uik_ref):
    rg = pd.read_csv(SRC + "results.csv")
    rg["uik_id"] = rg["uik_name"].str.extract(r"(\d+)").astype(int)
    is_line = rg["label"].isin(PROTOCOL_LINES_12)

    pg = rg[(rg.protocol_num == 2) & is_line]
    proto = pg.pivot_table(index="uik_id", columns="label", values="value", aggfunc="first")
    proto = proto.rename(columns=SHORT_12).reset_index()
    proto.insert(0, "election", "duma")
    proto.insert(1, "ballot", "party_list")
    proto = proto.merge(uik_ref[["uik_id", "tik_id", "oik_gd_id"]], on="uik_id", how="left")

    vg = rg[(rg.protocol_num == 2) & ~is_line].copy()
    vg["party_key"] = vg["label"].map(party_key)
    assert vg.party_key.notna().all(), "не распознана партия в results_with_id.csv (список)"
    votes = vg[["uik_id", "party_key", "value"]].rename(columns={"value": "votes"})
    votes.insert(0, "election", "duma")
    votes = votes.merge(uik_ref[["uik_id", "tik_id", "oik_gd_id"]], on="uik_id", how="left")

    return proto, votes


# ============================================================================
# 6. Одномандатные округа из spb_mandate.json
# ============================================================================
def build_mandate(uik_ref, cand_gd):
    with open(SRC + "spb_mandate.json", encoding="utf-8") as fh:
        j = json.load(fh)
    lines = [l.strip() for l in j["protocolLines"]]

    proto_rows, vote_rows = [], []
    for u in j["uiks"]:
        row = {"uik_id": u["num"]}
        row.update(dict(zip(lines, u["protocol"])))
        proto_rows.append(row)
        for c in u["candidates"]:
            vote_rows.append({
                "uik_id": u["num"],
                "cand_name": strip_name(c["name"]),
                "votes": c["votes"],
            })

    proto = pd.DataFrame(proto_rows)
    proto.insert(0, "election", "duma")
    proto.insert(1, "ballot", "mandate")
    proto = proto.merge(uik_ref[["uik_id", "tik_id", "oik_gd_id"]], on="uik_id", how="left")

    cand_lookup = cand_gd.copy()
    cand_lookup["cand_name"] = cand_lookup["candidate"]
    votes = pd.DataFrame(vote_rows).merge(cand_lookup[["id_cand_gd", "cand_name"]], on="cand_name", how="left")
    assert votes.id_cand_gd.notna().all(), "не найден кандидат ГД по имени"
    votes = votes[["uik_id", "id_cand_gd", "votes"]].rename(columns={"id_cand_gd": "cand_id"})
    votes.insert(0, "election", "duma")
    votes = votes.merge(uik_ref[["uik_id", "tik_id", "oik_gd_id"]], on="uik_id", how="left")

    return proto, votes


# ============================================================================
# 7. Карта по УИК, партсписок
# ============================================================================
def build_map_layer_party_uik(votes_party, proto_party, uik_ref, uik_coords, dim_party):
    party_names = dim_party.set_index("party_key")["name_gd"].to_dict()
    protocol = proto_party

    wide = votes_party.pivot_table(index="uik_id", columns="party_key", values="votes", aggfunc="sum").fillna(0)
    valid = wide.sum(axis=1)

    proto_idx = protocol.set_index("uik_id")
    invalid = proto_idx["invalid"].reindex(wide.index)
    ballots = valid + invalid

    share = wide.div(ballots, axis=0) * 100
    winner = wide.idxmax(axis=1)
    winner_share = pd.Series(
        share.values[range(len(share)), [share.columns.get_loc(w) for w in winner]], index=wide.index
    )

    turnout_abs = proto_idx[["issued_mobile", "issued_station"]].sum(axis=1)
    voters = proto_idx["voters"]
    turnout_pct = turnout_abs / voters * 100

    out = pd.DataFrame({"uik_id": wide.index})
    out = out.merge(uik_ref, on="uik_id", how="left")
    out["election"] = "duma"
    out["winner_party"] = winner.values
    out["winner_party_name"] = out["winner_party"].map(party_names)
    out["winner_share_pct"] = out["uik_id"].map(winner_share).round(2)
    out["winner_votes"] = out.apply(lambda r: int(wide.loc[r["uik_id"], r["winner_party"]]), axis=1)
    out["voters"] = out["uik_id"].map(voters)
    out["turnout_abs"] = out["uik_id"].map(turnout_abs)
    out["turnout_pct"] = out["uik_id"].map(turnout_pct).round(2)
    out["invalid"] = out["uik_id"].map(invalid)
    out["valid"] = out["uik_id"].map(valid)
    for p in wide.columns:
        out[f"votes_{p}"] = out["uik_id"].map(wide[p]).astype(int)
        out[f"share_{p}_pct"] = out["uik_id"].map(share[p]).round(2)
    out = out.merge(
        uik_coords[["uik_id", "lat", "lon", "lat_orig", "lon_orig", "place_size"]], on="uik_id", how="left"
    )
    out["has_coords"] = out["lat"].notna()
    out["geopoint"] = out.apply(lambda r: f"[{r.lat:.7f},{r.lon:.7f}]" if r.has_coords else None, axis=1)
    out.to_csv(DST + "map_layer_duma_party.csv", index=False)
    return out


# ============================================================================
# 8. Карта по ТИК, партсписок
# ============================================================================
def build_map_layer_party_tik(votes_party, proto_party, tik_coords, dim_party):
    party_names = dim_party.set_index("party_key")["name_gd"].to_dict()

    wide = votes_party.pivot_table(index="tik_id", columns="party_key", values="votes", aggfunc="sum").fillna(0)
    valid = wide.sum(axis=1)

    agg = proto_party.groupby("tik_id")[["issued_mobile", "issued_station", "voters", "invalid"]].sum()
    turnout_abs = agg["issued_mobile"] + agg["issued_station"]
    voters, invalid = agg["voters"], agg["invalid"]
    turnout_pct = (turnout_abs / voters * 100).round(2)

    ballots = valid + invalid.reindex(wide.index)
    share = wide.div(ballots, axis=0) * 100
    winner = wide.idxmax(axis=1)
    winner_share = pd.Series(
        share.values[range(len(share)), [share.columns.get_loc(w) for w in winner]], index=wide.index
    )

    out = pd.DataFrame({"tik_id": wide.index})
    out["election"] = "duma"
    out["winner_party"] = winner.values
    out["winner_party_name"] = out["winner_party"].map(party_names)
    out["winner_share_pct"] = out["tik_id"].map(winner_share).round(2)
    out["voters"] = out["tik_id"].map(voters)
    out["turnout_abs"] = out["tik_id"].map(turnout_abs)
    out["turnout_pct"] = out["tik_id"].map(turnout_pct)
    out["invalid"] = out["tik_id"].map(invalid)
    out["valid"] = out["tik_id"].map(valid)
    for p in wide.columns:
        out[f"votes_{p}"] = out["tik_id"].map(wide[p]).astype(int)
        out[f"share_{p}_pct"] = out["tik_id"].map(share[p]).round(2)
    out = out.merge(tik_coords[["tik_id", "lat", "lon", "place_size"]], on="tik_id", how="left")
    out["has_coords"] = out["lat"].notna()
    out.to_csv(DST + "map_layer_duma_party_tik.csv", index=False)
    return out


# ============================================================================
# 9. Карта по УИК, одномандатники
# ============================================================================
def build_map_layer_mandate_uik(votes_mandate, proto_mandate, uik_ref, cand_gd, uik_coords):
    protocol = proto_mandate.set_index("uik_id")

    v = votes_mandate.merge(
        cand_gd[["id_cand_gd", "candidate", "party", "party_key"]],
        left_on="cand_id", right_on="id_cand_gd", how="left",
    )
    assert v.candidate.notna().all(), "не сматчен кандидат"

    turnout_abs = protocol[ISSUED_COLS_MANDATE].sum(axis=1)
    voters = protocol[VOTERS_COL_MANDATE]
    turnout_pct = (turnout_abs / voters * 100).round(2)
    invalid = protocol[INVALID_COL_MANDATE]

    valid = v.groupby("uik_id")["votes"].sum()
    v = v.merge(valid.rename("uik_valid"), on="uik_id")
    v["invalid"] = v["uik_id"].map(invalid)
    v["ballots"] = v["uik_valid"] + v["invalid"]
    v["share_pct"] = (v["votes"] / v["ballots"] * 100).round(1)

    idx = v.groupby("uik_id")["votes"].idxmax()
    winner = v.loc[idx, ["uik_id", "candidate", "party_key", "votes", "share_pct"]].set_index("uik_id")
    winner.columns = ["winner_candidate", "winner_party_key", "winner_votes", "winner_share_pct"]

    def fmt_line(row):
        return f"{row.candidate} ({row.party}): {row.votes} ({row.share_pct}%)"

    v_sorted = v.sort_values(["uik_id", "votes"], ascending=[True, False])
    candidates_info = v_sorted.groupby("uik_id").apply(
        lambda g: "\n".join(fmt_line(r) for _, r in g.iterrows())
    ).rename("candidates_info")

    out = uik_ref[["uik_id", "tik_id", "oik_gd_id"]].rename(columns={"oik_gd_id": "oik_id"})
    out["election"] = "duma"
    out = out.merge(winner, on="uik_id", how="inner")
    out = out.merge(valid.rename("votes_total"), on="uik_id", how="left")
    out["valid"] = out["votes_total"]
    out["voters"] = out["uik_id"].map(voters)
    out["turnout_abs"] = out["uik_id"].map(turnout_abs)
    out["turnout_pct"] = out["uik_id"].map(turnout_pct)
    out["invalid"] = out["uik_id"].map(invalid)
    out = out.merge(candidates_info, on="uik_id", how="left")
    out = out.merge(uik_coords[["uik_id", "lat", "lon", "place_size"]], on="uik_id", how="left")
    out["has_coords"] = out["lat"].notna()
    out.to_csv(DST + "map_layer_duma_mandate.csv", index=False)
    return out


# ============================================================================
# 10. Карта по ОИК, одномандатники
# ============================================================================
def build_map_layer_mandate_oik(votes_mandate, proto_mandate, cand_gd, oik_polygons):
    v = votes_mandate.merge(
        cand_gd[["id_cand_gd", "candidate", "party", "party_key"]],
        left_on="cand_id", right_on="id_cand_gd", how="left",
    )
    assert v.candidate.notna().all(), "не сматчен кандидат"

    proto_agg = proto_mandate.groupby("oik_gd_id")[[VOTERS_COL_MANDATE, INVALID_COL_MANDATE] + ISSUED_COLS_MANDATE].sum()
    turnout_abs = proto_agg[ISSUED_COLS_MANDATE].sum(axis=1)
    voters, invalid = proto_agg[VOTERS_COL_MANDATE], proto_agg[INVALID_COL_MANDATE]
    turnout_pct = (turnout_abs / voters * 100).round(2)

    agg = v.groupby(["oik_gd_id", "candidate", "party", "party_key"], as_index=False)["votes"].sum()
    valid = agg.groupby("oik_gd_id")["votes"].sum()
    ballots = valid + invalid
    agg["share_pct"] = agg.apply(lambda r: round(r.votes / ballots[r.oik_gd_id] * 100, 2), axis=1)

    idx = agg.groupby("oik_gd_id")["votes"].idxmax()
    winner = agg.loc[idx, ["oik_gd_id", "candidate", "party_key", "share_pct"]].set_index("oik_gd_id")
    winner.columns = ["winner_candidate", "winner_party_key", "winner_share_pct"]

    def fmt_line(row):
        return f"{row.candidate} ({row.party}): {row.votes} ({row.share_pct}%)"

    agg_sorted = agg.sort_values(["oik_gd_id", "votes"], ascending=[True, False])
    candidates_info = agg_sorted.groupby("oik_gd_id").apply(
        lambda g: "\n".join(fmt_line(r) for _, r in g.iterrows())
    ).rename("candidates_info")

    out = oik_polygons.rename(columns={"id_gd_oik": "oik_gd_id"})[["oik_gd_id", "coords_polygon"]].copy()
    out["election"] = "duma"
    out = out.merge(winner, on="oik_gd_id", how="left")
    out = out.merge(valid.rename("votes_total"), on="oik_gd_id", how="left")
    out["valid"] = out["votes_total"]
    out["voters"] = out["oik_gd_id"].map(voters)
    out["turnout_abs"] = out["oik_gd_id"].map(turnout_abs)
    out["turnout_pct"] = out["oik_gd_id"].map(turnout_pct)
    out["invalid"] = out["oik_gd_id"].map(invalid)
    out = out.merge(candidates_info, on="oik_gd_id", how="left")
    out = out.rename(columns={"oik_gd_id": "oik_id"})
    out.to_csv(DST + "map_layer_duma_mandate_oik.csv", index=False)
    return out


# ============================================================================
# 11. Единый датасет для баров с фильтром по УИК/ТИК/ОИК
# ============================================================================
def build_fact_votes(votes_party, votes_mandate, proto_party, proto_mandate, cand_gd, dim_party):
    vp = votes_party.copy(); vp["ballot"] = "party_list"; vp["cand_id"] = pd.NA
    vm = votes_mandate.copy(); vm["ballot"] = "mandate"
    vm["party_key"] = vm["cand_id"].map(cand_gd.set_index("id_cand_gd")["party_key"])
    votes = pd.concat([vp, vm], ignore_index=True, sort=False)

    party_names = dim_party.set_index("party_key")["name_gd"].to_dict()
    extra_party_names = {"yabloko": "ЯБЛОКО", "kp": "Политическая партия КОММУНИСТИЧЕСКАЯ ПАРТИЯ КОММУНИСТЫ РОССИИ"}
    full_party_names = {**extra_party_names, **party_names}
    votes["party_name"] = votes["party_key"].map(full_party_names)
    votes["candidate_name"] = votes["cand_id"].map(cand_gd.set_index("id_cand_gd")["candidate"])

    # invalid по (uik_id, ballot): у партсписка — колонка invalid, у округов — русское имя
    proto_party_inv = proto_party.set_index(["uik_id", "ballot"])["invalid"]
    proto_mandate_inv = proto_mandate.set_index(["uik_id", "ballot"])[INVALID_COL_MANDATE]
    invalid_lookup = pd.concat([proto_party_inv, proto_mandate_inv.rename("invalid")])
    votes["invalid"] = votes.set_index(["uik_id", "ballot"]).index.map(invalid_lookup)

    ballots = votes.groupby(["uik_id", "ballot"])["votes"].transform("sum") + votes["invalid"]
    votes["share_pct"] = (votes["votes"] / ballots * 100).round(2)

    votes.to_csv(DST + "fact_votes_duma.csv", index=False)
    return votes


# ============================================================================
# 12. Явка по времени замера
# ============================================================================
def build_turnout_timeline(uik_ref):
    t = pd.read_csv(SRC + "gd_turnouts_uik.csv")
    pct_cols = [c for c in t.columns if c.endswith("процент")]

    long = t.melt(
        id_vars=["oik_gd_id", "tik_id", "uik_id"], value_vars=pct_cols,
        var_name="time_label", value_name="turnout_pct",
    )
    long["turnout_pct"] = long["turnout_pct"].str.rstrip("%").astype(float)
    long["time_label"] = long["time_label"].str.replace(" - процент", "", regex=False)
    order = {lbl.replace(" - процент", ""): i for i, lbl in enumerate(pct_cols)}
    long["time_order"] = long["time_label"].map(order)

    voters = uik_ref.rename(columns={"Кол-во избирателей, внесенныхив списки избирателей": "voters"})
    long = long.merge(voters[["uik_id", "voters"]], on="uik_id", how="left")
    long = long.rename(columns={"oik_gd_id": "oik_id"})
    long.insert(0, "election", "duma")
    long.to_csv(DST + "turnout_timeline_uik_duma.csv", index=False)
    return long


# ============================================================================
# 13. Сравнение победителей: единый список vs одномандатный округ
# ============================================================================
def build_compare(map_party, map_mandate):
    cmp = map_party[["uik_id", "tik_id", "winner_party", "winner_party_name", "winner_votes", "winner_share_pct"]].merge(
        map_mandate[["uik_id", "winner_candidate", "winner_party_key", "winner_votes", "winner_share_pct"]],
        on="uik_id", how="inner", suffixes=("_party", "_mandate"),
    )
    cmp = cmp.rename(columns={
        "winner_party": "party_winner", "winner_party_name": "party_winner_name",
        "winner_votes_party": "party_winner_votes", "winner_share_pct_party": "party_winner_pct",
        "winner_candidate": "mandate_winner_candidate", "winner_party_key": "mandate_winner_party",
        "winner_votes_mandate": "mandate_winner_votes", "winner_share_pct_mandate": "mandate_winner_pct",
    })
    cmp.insert(0, "election", "duma")
    cmp["diff_pct"] = (cmp["party_winner_pct"] - cmp["mandate_winner_pct"]).round(2)
    cmp["party_changed"] = cmp["party_winner"] != cmp["mandate_winner_party"]

    cmp = cmp[(cmp["party_winner_votes"] > 0) & (cmp["mandate_winner_votes"] > 0)]

    cols = ["election", "tik_id", "uik_id",
            "party_winner", "party_winner_name", "party_winner_votes", "party_winner_pct",
            "mandate_winner_candidate", "mandate_winner_party", "mandate_winner_votes", "mandate_winner_pct",
            "diff_pct", "party_changed"]
    cmp = cmp[cols].sort_values(["tik_id", "uik_id"])
    cmp.to_csv(DST + "compare_party_mandate_duma.csv", index=False)
    return cmp


# ============================================================================
# Запуск всей цепочки по порядку
# ============================================================================
if __name__ == "__main__":
    uik_ref = build_uik_ref_gd()  # с исправленным tik_id для 40 УИК

    dim_party = build_dim_party()
    cand_gd = build_candidates()
    uik_coords = build_uik_coords()
    tik_coords = build_tik_coords()
    oik_polygons = build_oik_polygons_gd()

    proto_party, votes_party = build_party_list(uik_ref)
    proto_mandate, votes_mandate = build_mandate(uik_ref, cand_gd)

    map_party = build_map_layer_party_uik(votes_party, proto_party, uik_ref, uik_coords, dim_party)
    build_map_layer_party_tik(votes_party, proto_party, tik_coords, dim_party)
    map_mandate = build_map_layer_mandate_uik(votes_mandate, proto_mandate, uik_ref, cand_gd, uik_coords)
    build_map_layer_mandate_oik(votes_mandate, proto_mandate, cand_gd, oik_polygons)

    build_fact_votes(votes_party, votes_mandate, proto_party, proto_mandate, cand_gd, dim_party)
    build_turnout_timeline(uik_ref)
    build_compare(map_party, map_mandate)

    print("Готово: все файлы ГД собраны в", DST)
