"""
Краулер izbirkom.ru: дерево комиссий +
результаты по каждому УИК — через apps.cikrf.ru API и собственный
интерпретатор JS-обфускации (election_stats_js_interpreter.py), БЕЗ браузера.
Интерпретатор поместить в одну папку с краулером!

Архитектура:
  1. challenge/get + challenge/solve — тривиальный proof-of-work
     (посчитать арифметическое выражение), выдаёт apiKey (JWT, живёт
     10 минут). apiKey + fingerprint идут в заголовках всех запросов.
  2. commissionClassifiers — дерево комиссий (регион -> округа -> ТИК ->
     УИК). Лист (УИК) — узел с "type": 5.
  3. izbirkom.ru/reports/242?data=<...> — сырой отчёт конкретного УИК
     (html+script с обфусцированными значениями).
  4. election_stats_js_interpreter.resolve_html — распознаёт 5 типов операций
     обфускации по ТЕЛУ JS-функций (не по случайным именам) и применяет
     их к дереву BeautifulSoup.

Что менять под новый парсинг:
- ELECTION_ID
- REPORT_ID (но, скорее всего, он не будет отличаться)
- ELECTION_ROOT_EXTERNAL_ID
прописать отдельную папку:
NODES_STATE_PATH = Path("имя_папки/commission_nodes.json")
...
out_csv = Path("имя_папки/results.csv")
results_done_path = Path("имя_папки/results_done.json")

Как найти новые значения для других выборов:

1. Открыть на izbirkom.ru страницу результатов выборов
(через календарь выборов на сайте — найдите нужную кампанию).
2. Посмотреть на URL — он будет вида 
izbirkom.ru/election/НОВЫЙ_ID/commission/НОВЫЙ_UUID/results?type=...&report=.... 
Отсюда сразу взять:
- НОВЫЙ_ID → это и есть новый ELECTION_ID.
- report=... в конце URL → это новый REPORT_ID (но скорее всего будет тем же 242).
3. ELECTION_ROOT_EXTERNAL_ID — это UUID региональной избирательной комиссии 
конкретно в дереве НУЖНОЙ кампании (он завязан на electionsId). 
Проще всего его найти через DevTools → Network: найти там запрос 
commissionClassifiers?electionsId=НОВЫЙ_ID (без classifierId — это самый верхний, 
корневой запрос), открыть Response — в "externalId" самого корневого узла и 
будет нужное значение.
"""

import json
import random
import re
import time
from pathlib import Path
from typing import Optional

import requests

from election_stats_js_interpreter import resolve_html


def request_with_retry(method: str, url: str, attempts: int = 4, **kwargs) -> requests.Response:
    """
    Сайт сейчас периодически обрывает соединение (ConnectionResetError и
    подобное) — это чисто сетевая нестабильность под нагрузкой, не ошибка
    в запросе. Повторяем с нарастающей паузой вместо того, чтобы падать
    с первого раза.
    """
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.request(method, url, timeout=20, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            if attempt < attempts:
                wait = random.uniform(2.0, 4.0) * attempt
                print(f"    сетевая ошибка ({e}), повтор через {wait:.1f}с ({attempt}/{attempts})")
                time.sleep(wait)
    raise last_err

BASE = "http://apps.cikrf.ru/service/ik-inp-service-pbcopy"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"

ELECTION_ID = 587813923 # заменяем под конкретные выборы
REPORT_ID = 242 # меняем, если отличается

NODES_STATE_PATH = Path("имя_папки/commission_nodes.json") # меняем папку под нужные выборы

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": UA,
    "Origin": "http://izbirkom.ru",
    "Referer": "http://izbirkom.ru/",
}


# ---------- challenge / apiKey ----------

def solve_js_task(js_task: str) -> int:
    m = re.search(r"return\s+(.+?);", js_task)
    if not m:
        raise ValueError(f"не удалось разобрать jsTask: {js_task!r}")
    expr = m.group(1)
    if not re.fullmatch(r"[\d\s+\-*/().]+", expr):
        raise ValueError(f"неожиданное выражение в challenge: {expr!r}")
    return eval(expr)  # безопасно: expr уже проверен regex-ом выше


class ApiKeyManager:
    def __init__(self):
        self._api_key = None
        self._expires_at = 0

    def get(self) -> str:
        if self._api_key is None or time.time() > self._expires_at - 60:
            self._refresh()
        return self._api_key

    def _refresh(self):
        r = request_with_retry("GET", f"{BASE}/challenge/get", headers=BASE_HEADERS)
        data = r.json()
        pub_token = data["pubToken"]
        answer = solve_js_task(data["jsTask"])
        r2 = request_with_retry(
            "POST", f"{BASE}/challenge/solve",
            headers={**BASE_HEADERS, "Content-Type": "application/json"},
            json={"pubToken": pub_token, "answer": str(answer), "fingerprint": UA},
        )
        self._api_key = r2.json()["apiKey"]
        self._expires_at = time.time() + 600


api_keys = ApiKeyManager()


def auth_headers() -> dict:
    return {**BASE_HEADERS, "X-Api-Key": api_keys.get(), "X-Client-Fingerprint": UA}


# ---------- дерево комиссий ----------

def fetch_commission_children(classifier_id: Optional[str]) -> dict:
    params = {"electionsId": ELECTION_ID}
    if classifier_id is not None:
        params["classifierId"] = classifier_id
    r = request_with_retry("GET", f"{BASE}/commissionClassifiers", params=params,
                            headers=auth_headers())
    return r.json()


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


ELECTION_ROOT_EXTERNAL_ID = "781f7be4-3cdd-4a70-bcd6-2bdf40b20643" # меняем корень дерева комиссий


def build_commission_tree() -> dict:
    all_nodes = load_json(NODES_STATE_PATH, {})
    expanded = {k for k, v in all_nodes.items() if v.get("_expanded")}
    queue, queued = [], set()

    def consider(node: dict):
        key = node["externalId"]
        if key not in all_nodes:
            node["_expanded"] = False
            all_nodes[key] = node
        is_leaf = all_nodes[key].get("type") == 5
        if not is_leaf and key not in expanded and key not in queued:
            queue.append(key)
            queued.add(key)

    if ELECTION_ROOT_EXTERNAL_ID not in all_nodes:
        consider({"externalId": ELECTION_ROOT_EXTERNAL_ID, "name": "ROOT", "type": 0, "hasChildren": True})
    else:
        consider(all_nodes[ELECTION_ROOT_EXTERNAL_ID])

    while queue:
        key = queue.pop(0)
        queued.discard(key)
        if key in expanded:
            continue
        print(f"[узлов всего: {len(all_nodes)}] раскрываю {key} ...")
        try:
            data = fetch_commission_children(key)
        except Exception as e:
            print(f"  ОШИБКА: {e} — пропускаю, перезапустите позже")
            continue

        node_self = {k: v for k, v in data.items() if k != "children"}
        node_self["externalId"] = key
        node_self["_expanded"] = True
        all_nodes[key] = node_self
        expanded.add(key)

        for child in data.get("children", []):
            consider(child)

        save_json(NODES_STATE_PATH, all_nodes)
        time.sleep(0.3)

    return all_nodes


# ---------- результаты УИК ----------

def fetch_report_raw(commission_classifier_id: str, protocol_num: int, show_percent: bool) -> dict:
    data = {
        "specialColumnsIndex": -1,
        "requestParams": {"commissionClassifierId": commission_classifier_id, "protocolNum": protocol_num},
        "isReferendum": False,
        "isUik": True,
        "showPercent": show_percent,
        "is": {"tablet": True, "mobile": False},
    }
    r = request_with_retry(
        "GET", f"http://izbirkom.ru/reports/{REPORT_ID}",
        params={"data": json.dumps(data, separators=(",", ":"))},
        headers=auth_headers(),
    )
    payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(f"reports/{REPORT_ID} вернул success=false: {payload}")
    return payload


def parse_resolved_table_html(html: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody") or soup
    rows = []
    pending_row = None

    for tr in tbody.find_all("tr", recursive=False):
        style = (tr.get("style") or "").replace(" ", "")
        if "height:0px" in style:
            continue  # служебная строка-донор, не данные
        tds = tr.find_all("td", recursive=False)
        if not tds or tr.find("th"):
            continue
        if len(tds) >= 3:
            row = {"label": tds[1].get_text(strip=True),
                   "value_text": tds[2].get_text(strip=True),
                   "percent_text": None}
            rows.append(row)
            pending_row = row
        elif len(tds) == 1 and pending_row is not None:
            pending_row["percent_text"] = tds[0].get_text(strip=True)

    return rows


VALUE_RE = re.compile(r"^\d+$")
PERCENT_RE = re.compile(r"^\d+([.,]\d+)?%?$")


def _row_is_valid(row: dict) -> bool:
    return row["value"] == "" or bool(VALUE_RE.fullmatch(row["value"]))


def _clean_percent(percent):
    if percent is None:
        return None
    percent = percent.strip()
    if percent and not PERCENT_RE.fullmatch(percent.replace(" ", "")):
        return ""
    return percent


def fetch_uik_results(uik_external_id: str, protocol_num: int, show_percent: bool, max_attempts: int = 3):
    """
    protocol_num=1 (showPercent=False) — одномандатный округ,
    protocol_num=2 (showPercent=True) — единый округ/списки.
    Возвращает список {"label", "value", "percent"}. Пустой список,
    если данных для этого УИК/протокола ещё нет (легитимно).
    """
    last_out = []
    for attempt in range(1, max_attempts + 1):
        payload = fetch_report_raw(uik_external_id, protocol_num, show_percent)
        html = payload.get("html", "")
        script = payload.get("script", "")

        if not html.strip():
            return []  # нет данных — не ошибка

        resolved = resolve_html(html, script)
        rows = parse_resolved_table_html(resolved)

        out = []
        for r in rows:
            value = (r["value_text"] or "").replace("о", "0").replace("О", "0").strip()
            percent = r["percent_text"].strip() if r["percent_text"] is not None else None
            out.append({"label": r["label"], "value": value, "percent": _clean_percent(percent)})

        last_out = out
        bad = [row for row in out if not _row_is_valid(row)]
        if not rows:
            print(f"  попытка {attempt}/{max_attempts}: таблица не распозналась — повторяю запрос")
            time.sleep(random.uniform(2.0, 4.0))
            continue
        if not bad:
            return out
        print(f"  попытка {attempt}/{max_attempts}: {len(bad)} нераспознанных значений — повторяю запрос")
        time.sleep(random.uniform(2.0, 4.0))

    print(f"  ПРЕДУПРЕЖДЕНИЕ: после {max_attempts} попыток остались нераспознанные значения — проверьте вручную")
    return last_out


if __name__ == "__main__":
    import csv

    tree = build_commission_tree()
    uiks = {k: v for k, v in tree.items() if v.get("type") == 5}
    print(f"\nВсего узлов в дереве: {len(tree)}, из них УИК: {len(uiks)}")

    TEST_LIMIT = None  # поставьте None, чтобы собирать по всем УИК
    if TEST_LIMIT is not None:
        uiks = dict(list(uiks.items())[:TEST_LIMIT])
        print(f"Тестовый срез: ограничено до {len(uiks)} УИК")

    Path("имя_папки").mkdir(exist_ok=True)
    Path("имя_папки/uik_list.json").write_text(
        json.dumps(uiks, ensure_ascii=False, indent=2), encoding="utf-8"
    ) # меняем папки

    PROTOCOLS = [(1, False, "одномандатный округ"), (2, True, "единый округ")]

    results_done_path = Path("имя_папки/results_done.json") # меняем папку
    done = set(tuple(x) for x in load_json(results_done_path, []))

    out_csv = Path("имя_папки/results.csv") # меняем папку
    is_new = not out_csv.exists()
    with open(out_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["uik_external_id", "uik_name", "protocol_num", "label", "value", "percent"])

        for uik_id, node in uiks.items():
            for protocol_num, show_percent, label in PROTOCOLS:
                key = (uik_id, protocol_num)
                if key in done:
                    continue
                print(f"УИК {node['name']} / {label} ...")
                try:
                    rows = fetch_uik_results(uik_id, protocol_num, show_percent)
                except Exception as e:
                    print(f"  ОШИБКА: {e} — пропускаю, перезапустите позже")
                    continue

                for row in rows:
                    writer.writerow([uik_id, node["name"], protocol_num, row["label"], row["value"], row["percent"]])
                f.flush()

                if rows:
                    done.add(key)
                    save_json(results_done_path, [list(x) for x in done])
                else:
                    print(f"  (данных пока нет, {node['name']} будет повторно проверен в следующем запуске)")

                time.sleep(random.uniform(1.0, 2.5))  # лёгкие запросы — пауза короче, но не нулевая

    print(f"\nГотово. Результаты в {out_csv}")
