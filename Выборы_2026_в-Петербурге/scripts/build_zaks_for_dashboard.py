"""
Полный конвейер по выборам в региональный парламент (Законодательное собрание): от сырых файлов до набора,
готового к загрузке для построения дашборда (на примере ЗАКС СПб).

Входные файлы (все — из /mnt/user-data/uploads/, ничего не меняются):
    results.csv                — протокол + голоса (список и округ вместе, protocol_num 1/2)
    candidates_spb.csv         — кандидаты-одномандатники, колонки id_cand_spb/candidate/party
    parties_spb.csv            — партийный список (для читаемых названий партий)
    first_day_spb_uik.csv      — справочник УИК: tik_id, oik_spb_id, число избирателей
    uik_coords.csv             — координаты УИК (сырые, долгота/широта, с дублями по адресу)
    tik_coords.csv             — координаты ТИК (сырые)
    spb_oik_coords.csv         — полигоны округов (сырые, долгота/широта)
    spb_turnouts_uik.csv       — явка по времени замера (для графика «явка х размер участка»)

Выходные файлы: dim_party.csv, spb_oik_coords_latlon.csv, turnout_timeline_uik_zaks.csv,
fact_votes_zaks.csv, map_layer_zaks_party.csv, map_layer_zaks_party_tik.csv,
map_layer_zaks_mandate.csv, map_layer_zaks_mandate_oik.csv, candidates_spb.csv (с party_key),
compare_party_mandate_zaks.csv.

Запускать одним файлом: python3 build_zaks.py
"""
import json
import math
import re

import pandas as pd

SRC = "/mnt/user-data/uploads/"
DST = "/home/claude/verify_out/"  # поменяйте на свою рабочую папку


# ============================================================================
# 0. Партии: сведение разных написаний к единому короткому коду
# ============================================================================
# Ключи и их соответствие партиям заданы через CASE-формулу в
# DataLens — здесь та же логика, просто на стороне Python.
PARTY_KEY_MAP = {
    "ЗЕЛЁНЫЕ": "green",
    "ЕДИНАЯ РОССИЯ": "er",
    "КПРФ": "kprf",
    "ЛДПР": "ldpr",
    "СПРАВЕДЛИВАЯ РОССИЯ": "sr",
    "НОВЫЕ ЛЮДИ": "np",
    "ЯБЛОКО": "yabloko",
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


# ============================================================================
# 1. Координаты: перестановка долгота/широта -> широта/долгота + починка
#    известных опечаток в источнике + разведение точек, стоящих на одном
#    адресе (несколько УИК в одном здании), по маленькой окружности
# ============================================================================
def swap_lonlat(node):
    """[lon, lat] -> [lat, lon] на любой глубине вложенности (точки, полигоны, дырки)."""
    if node and isinstance(node[0], (int, float)):
        return [node[1], node[0]]
    return [swap_lonlat(x) for x in node]


def build_oik_polygons_zaks():
    df = pd.read_csv(SRC + "spb_oik_coords.csv") # здесь указать тот файл с полигонами округов, который нужен
    df["coords_polygon"] = df["coords_polygon"].map(
        lambda s: json.dumps(swap_lonlat(json.loads(s)), separators=(",", ":"))
    )
    df.to_csv(DST + "spb_oik_coords_latlon.csv", index=False, encoding="utf-8") # здесь меняем название под регион
    return df


def jitter_points(df, coord_col, id_col, radius_m=None):
    """Раскладывает точки с одинаковыми координатами (один адрес — несколько
    УИК/ТИК) по окружности вокруг исходной точки. Сдвиг детерминирован
    (порядок по id), исходные координаты сохраняются в *_orig."""
    pts = df[coord_col].map(json.loads)  # исходно [lon, lat]
    df["lon_orig"] = pts.map(lambda p: p[0])
    df["lat_orig"] = pts.map(lambda p: p[1])
    df["lon"], df["lat"] = df["lon_orig"], df["lat_orig"]
    df["place_size"] = 1
    key = df["lon_orig"].round(6).astype(str) + "|" + df["lat_orig"].round(6).astype(str)
    for _, idx in df.groupby(key).groups.items():
        n = len(idx)
        if n == 1:
            continue
        r = radius_m or (6 if n <= 3 else 9)  # метры: остаёмся в пределах здания
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
    # чинит опечатку «УКИ №359»:
    # u["uik_id"] = u["uik_name"].str.extract(r"(\d+)")[0].astype(int)
    # чинит опечатку в названии района
    # u["district"] = u["district"].str.strip().replace({"Кронштадсткий": "Кронштадтский"})
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
# 2. Справочник партий (party_key -> читаемое название) — используется
#    только для подписи winner_party_name на картах
# ============================================================================
def build_dim_party():
    parties = pd.read_csv(SRC + "parties_spb.csv").iloc[:, :2] # указать корректное название файла
    parties["party_key"] = parties["party"].map(party_key)
    assert parties.party_key.notna().all(), "не распознана партия в parties_spb.csv" # указать корректное название
    dim = parties[["party_key", "party"]].rename(columns={"party": "name_spb"})
    dim.to_csv(DST + "dim_party.csv", index=False)
    return dim


# ============================================================================
# 3. Кандидаты: добавляем party_key
# ============================================================================
def build_candidates():
    c = pd.read_csv(SRC + "candidates_spb.csv") # указать корректное название файла
    c = c.loc[:, ~c.columns.str.startswith("Unnamed")]
    c["party_key"] = c["party"].map(lambda p: PARTY_KEY_MAP.get(p, party_key(p)))
    c.to_csv(DST + "candidates_spb.csv", index=False) # указать корректное название файла
    return c


# ============================================================================
# 4. Протокол и голоса из results.csv (protocol_num: 1 = округ, 2 = список)
# ============================================================================
PROTOCOL_LINES_11 = [
    "Число избирателей, внесенных в список избирателей на момент окончания голосования",
    "Число избирательных бюллетеней, полученных участковой избирательной комиссией",
    "Число избирательных бюллетеней, выданных избирателям в помещении для голосования в день голосования",
    "Число избирательных бюллетеней, выданных избирателям, проголосовавшим вне помещения для голосования в день голосования",
    "Число погашенных избирательных бюллетеней",
    "Число избирательных бюллетеней, содержащихся в переносных ящиках для голосования",
    "Число избирательных бюллетеней, содержащихся в стационарных ящиках для голосования",
    "Число недействительных избирательных бюллетеней",
    "Число действительных избирательных бюллетеней",
    "Число утраченных избирательных бюллетеней",
    "Число избирательных бюллетеней, не учтенных при получении",
]
SHORT_11 = {
    PROTOCOL_LINES_11[0]: "voters", PROTOCOL_LINES_11[1]: "received",
    PROTOCOL_LINES_11[2]: "issued_station", PROTOCOL_LINES_11[3]: "issued_mobile",
    PROTOCOL_LINES_11[4]: "cancelled", PROTOCOL_LINES_11[5]: "box_mobile",
    PROTOCOL_LINES_11[6]: "box_station", PROTOCOL_LINES_11[7]: "invalid",
    PROTOCOL_LINES_11[8]: "valid", PROTOCOL_LINES_11[9]: "lost",
    PROTOCOL_LINES_11[10]: "unaccounted",
}


def build_results(cand_spb, uik_ref): # вместо cand_spb свое название
    r = pd.read_csv(SRC + "results.csv")
    r["uik_id"] = r["uik_name"].str.extract(r"(\d+)").astype(int)
    is_line = r["label"].isin(PROTOCOL_LINES_11)

    # -- протоколы (11 строк на каждый из двух бюллетеней) --
    proto = r[is_line].pivot_table(
        index=["uik_id", "protocol_num"], columns="label", values="value", aggfunc="first"
    )
    proto = proto.rename(columns=SHORT_11).reset_index()
    proto["ballot"] = proto["protocol_num"].map({1: "mandate", 2: "party_list"})
    proto = proto.drop(columns="protocol_num")
    proto.insert(0, "election", "zaks")
    proto = proto.merge(uik_ref[["uik_id", "tik_id", "oik_spb_id"]], on="uik_id", how="left")
    proto.to_csv(DST + "fact_protocol_zaks.csv", index=False)

    # -- голоса по партспискам (protocol_num == 2, не строка протокола) --
    pv = r[(r.protocol_num == 2) & ~is_line].copy()
    pv["party_key"] = pv["label"].map(party_key)
    assert pv.party_key.notna().all(), "не распознана партия в results.csv (список)"
    votes_party = pv[["uik_id", "party_key", "value"]].rename(columns={"value": "votes"})
    votes_party.insert(0, "election", "zaks")
    votes_party = votes_party.merge(uik_ref[["uik_id", "tik_id", "oik_spb_id"]], on="uik_id", how="left")

    # -- голоса по кандидатам-одномандатникам (protocol_num == 1), могут переименовываться cand_spb и столбцы --
    cv = r[(r.protocol_num == 1) & ~is_line].copy()
    cv = cv.merge(cand_spb[["id_cand_spb", "candidate"]], left_on="label", right_on="candidate", how="left")
    assert cv.id_cand_spb.notna().all(), "не найден кандидат ЗакС по имени"
    votes_mandate = cv[["uik_id", "id_cand_spb", "value"]].rename(
        columns={"id_cand_spb": "cand_id", "value": "votes"}
    )
    votes_mandate.insert(0, "election", "zaks")
    votes_mandate = votes_mandate.merge(uik_ref[["uik_id", "tik_id", "oik_spb_id"]], on="uik_id", how="left")

    return proto, votes_party, votes_mandate


# ============================================================================
# 5. Карта по УИК, партсписок: победитель, явка (issued/voters — как считает
#    ГАС Выборы), проценты (знаменатель valid+invalid), координаты
# ============================================================================
def build_map_layer_party_uik(votes_party, proto, uik_ref, uik_coords, dim_party):
    party_names = dim_party.set_index("party_key")["name_spb"].to_dict() # проверить соответствие названия столбца
    protocol = proto[proto.ballot == "party_list"]

    wide = votes_party.pivot_table(index="uik_id", columns="party_key", values="votes", aggfunc="sum").fillna(0)
    valid = wide.sum(axis=1)

    proto_idx = protocol.set_index("uik_id")
    invalid = proto_idx["invalid"].reindex(wide.index)
    ballots = valid + invalid  # знаменатель как на ГАС Выборы

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
    out["election"] = "zaks"
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
    out.to_csv(DST + "map_layer_zaks_party.csv", index=False)
    return out


# ============================================================================
# 6. Карта по ТИК, партсписок: та же логика, но данные сначала суммируются
#    по всем УИК ТИК, а потом делятся — а не усредняются в процентах
# ============================================================================
def build_map_layer_party_tik(votes_party, proto, tik_coords, dim_party):
    party_names = dim_party.set_index("party_key")["name_spb"].to_dict() # проверить соответствие названия столбца
    protocol = proto[proto.ballot == "party_list"]

    wide = votes_party.pivot_table(index="tik_id", columns="party_key", values="votes", aggfunc="sum").fillna(0)
    valid = wide.sum(axis=1)

    agg = protocol.groupby("tik_id")[["issued_mobile", "issued_station", "voters", "invalid"]].sum()
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
    out["election"] = "zaks"
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
    out.to_csv(DST + "map_layer_zaks_party_tik.csv", index=False)
    return out


# ============================================================================
# 7. Карта по УИК, одномандатники: победивший кандидат на конкретном УИК. 
#    Проверять названия столбцов!
# ============================================================================
def build_map_layer_mandate_uik(votes_mandate, proto, uik_ref, cand_spb, uik_coords):
    protocol = proto[proto.ballot == "mandate"].set_index("uik_id")

    v = votes_mandate.merge(
        cand_spb[["id_cand_spb", "candidate", "party", "party_key"]],
        left_on="cand_id", right_on="id_cand_spb", how="left",
    )
    assert v.candidate.notna().all(), "не сматчен кандидат"

    turnout_abs = protocol[["issued_mobile", "issued_station"]].sum(axis=1)
    voters = protocol["voters"]
    turnout_pct = (turnout_abs / voters * 100).round(2)
    invalid = protocol["invalid"]

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

    out = uik_ref[["uik_id", "tik_id", "oik_spb_id"]].rename(columns={"oik_spb_id": "oik_id"})
    out["election"] = "zaks"
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
    out.to_csv(DST + "map_layer_zaks_mandate.csv", index=False)
    return out


# ============================================================================
# 8. Карта по ОИК, одномандатники: реальный итог округа (сумма по всем его
#    УИК), а не локальный лидер отдельного участка — плюс полигон округа.
#    Проверять названия столбцов!
# ============================================================================
def build_map_layer_mandate_oik(votes_mandate, proto, cand_spb, oik_polygons):
    protocol = proto[proto.ballot == "mandate"]

    v = votes_mandate.merge(
        cand_spb[["id_cand_spb", "candidate", "party", "party_key"]],
        left_on="cand_id", right_on="id_cand_spb", how="left",
    )
    assert v.candidate.notna().all(), "не сматчен кандидат"

    proto_agg = protocol.groupby("oik_spb_id")[["voters", "invalid", "issued_mobile", "issued_station"]].sum()
    turnout_abs = proto_agg["issued_mobile"] + proto_agg["issued_station"]
    voters, invalid = proto_agg["voters"], proto_agg["invalid"]
    turnout_pct = (turnout_abs / voters * 100).round(2)

    agg = v.groupby(["oik_spb_id", "candidate", "party", "party_key"], as_index=False)["votes"].sum()
    valid = agg.groupby("oik_spb_id")["votes"].sum()
    ballots = valid + invalid
    agg["share_pct"] = agg.apply(lambda r: round(r.votes / ballots[r.oik_spb_id] * 100, 2), axis=1)

    idx = agg.groupby("oik_spb_id")["votes"].idxmax()
    winner = agg.loc[idx, ["oik_spb_id", "candidate", "party_key", "share_pct"]].set_index("oik_spb_id")
    winner.columns = ["winner_candidate", "winner_party_key", "winner_share_pct"]

    def fmt_line(row):
        return f"{row.candidate} ({row.party}): {row.votes} ({row.share_pct}%)"

    agg_sorted = agg.sort_values(["oik_spb_id", "votes"], ascending=[True, False])
    candidates_info = agg_sorted.groupby("oik_spb_id").apply(
        lambda g: "\n".join(fmt_line(r) for _, r in g.iterrows())
    ).rename("candidates_info")

    out = oik_polygons.rename(columns={"id_spb_oik": "oik_spb_id"})[["oik_spb_id", "coords_polygon"]].copy()
    out["election"] = "zaks"
    out = out.merge(winner, on="oik_spb_id", how="left")
    out = out.merge(valid.rename("votes_total"), on="oik_spb_id", how="left")
    out["valid"] = out["votes_total"]
    out["voters"] = out["oik_spb_id"].map(voters)
    out["turnout_abs"] = out["oik_spb_id"].map(turnout_abs)
    out["turnout_pct"] = out["oik_spb_id"].map(turnout_pct)
    out["invalid"] = out["oik_spb_id"].map(invalid)
    out = out.merge(candidates_info, on="oik_spb_id", how="left")
    out = out.rename(columns={"oik_spb_id": "oik_id"})
    out.to_csv(DST + "map_layer_zaks_mandate_oik.csv", index=False)
    return out


# ============================================================================
# 9. Единый датасет для баров с фильтром по УИК/ТИК/ОИК: длинный формат,
#    один ряд — один кандидат/партия на одном УИК, с читаемыми именами и
#    процентом (знаменатель valid+invalid, без задвоения invalid внутри
#    одной партии/кандидата)
#    Проверять названия столбцов!
# ============================================================================
def build_fact_votes(votes_party, votes_mandate, proto, cand_spb, dim_party):
    vp = votes_party.copy(); vp["ballot"] = "party_list"; vp["cand_id"] = pd.NA
    vm = votes_mandate.copy(); vm["ballot"] = "mandate"
    vm["party_key"] = vm["cand_id"].map(cand_spb.set_index("id_cand_spb")["party_key"])
    votes = pd.concat([vp, vm], ignore_index=True, sort=False)

    party_names = dim_party.set_index("party_key")["name_spb"].to_dict()
    extra_party_names = {"yabloko": "ЯБЛОКО", "kp": "Политическая партия КОММУНИСТИЧЕСКАЯ ПАРТИЯ КОММУНИСТЫ РОССИИ"}
    full_party_names = {**extra_party_names, **party_names}
    votes["party_name"] = votes["party_key"].map(full_party_names)
    votes["candidate_name"] = votes["cand_id"].map(cand_spb.set_index("id_cand_spb")["candidate"])

    invalid_lookup = proto.set_index(["uik_id", "ballot"])["invalid"]
    votes["invalid"] = votes.set_index(["uik_id", "ballot"]).index.map(invalid_lookup)
    ballots = votes.groupby(["uik_id", "ballot"])["votes"].transform("sum") + votes["invalid"]
    votes["share_pct"] = (votes["votes"] / ballots * 100).round(2)

    votes.to_csv(DST + "fact_votes_zaks.csv", index=False) # файл можно переименовать по-своему
    return votes


# ============================================================================
# 10. Явка по времени замера (для графика «явка х размер участка»)
#     Проверять названия столбцов!
# ============================================================================
def build_turnout_timeline(uik_ref):
    t = pd.read_csv(SRC + "spb_turnouts_uik.csv") # название может быть свое
    pct_cols = [c for c in t.columns if c.endswith("процент")]

    long = t.melt(
        id_vars=["oik_spb_id", "tik_id", "uik_id"], value_vars=pct_cols,
        var_name="time_label", value_name="turnout_pct",
    )
    long["turnout_pct"] = long["turnout_pct"].str.rstrip("%").astype(float)
    long["time_label"] = long["time_label"].str.replace(" - процент", "", regex=False)
    order = {lbl.replace(" - процент", ""): i for i, lbl in enumerate(pct_cols)}
    long["time_order"] = long["time_label"].map(order)

    voters = uik_ref.rename(columns={"Кол-во избирателей, внесенныхив списки избирателей": "voters"})
    long = long.merge(voters[["uik_id", "voters"]], on="uik_id", how="left")
    long = long.rename(columns={"oik_spb_id": "oik_id"})
    long.insert(0, "election", "zaks")
    long.to_csv(DST + "turnout_timeline_uik_zaks.csv", index=False)
    return long


# ============================================================================
# 11. Сравнение победителей: единый список vs одномандатный округ на одних
#     и тех же УИК (только там, где реально голосовали ОБОИМИ бюллетенями)
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
    cmp.insert(0, "election", "zaks")
    cmp["diff_pct"] = (cmp["party_winner_pct"] - cmp["mandate_winner_pct"]).round(2)
    cmp["party_changed"] = cmp["party_winner"] != cmp["mandate_winner_party"]

    # УИК, где реального голосования по одному из двух бюллетеней не было — исключаем
    cmp = cmp[(cmp["party_winner_votes"] > 0) & (cmp["mandate_winner_votes"] > 0)]

    cols = ["election", "tik_id", "uik_id",
            "party_winner", "party_winner_name", "party_winner_votes", "party_winner_pct",
            "mandate_winner_candidate", "mandate_winner_party", "mandate_winner_votes", "mandate_winner_pct",
            "diff_pct", "party_changed"]
    cmp = cmp[cols].sort_values(["tik_id", "uik_id"])
    cmp.to_csv(DST + "compare_party_mandate_zaks.csv", index=False)
    return cmp


# ============================================================================
# Запуск всей цепочки по порядку.
# Проверять названия столбцов!
# ============================================================================
if __name__ == "__main__":
    uik_ref = pd.read_csv(SRC + "first_day_spb_uik.csv") # указываем актуальное название файла

    dim_party = build_dim_party()
    cand_spb = build_candidates()
    uik_coords = build_uik_coords()
    tik_coords = build_tik_coords()
    oik_polygons = build_oik_polygons_zaks()

    proto, votes_party, votes_mandate = build_results(cand_spb, uik_ref)

    map_party = build_map_layer_party_uik(votes_party, proto, uik_ref, uik_coords, dim_party)
    build_map_layer_party_tik(votes_party, proto, tik_coords, dim_party)
    map_mandate = build_map_layer_mandate_uik(votes_mandate, proto, uik_ref, cand_spb, uik_coords)
    build_map_layer_mandate_oik(votes_mandate, proto, cand_spb, oik_polygons)

    build_fact_votes(votes_party, votes_mandate, proto, cand_spb, dim_party)
    build_turnout_timeline(uik_ref)
    build_compare(map_party, map_mandate)

    print("Готово: все файлы ЗакС собраны в", DST)
