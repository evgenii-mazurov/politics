"""
Интерпретатор обфусцирующего JS с сайта izbirkom.ru — реализация 5 типов
операций напрямую в Python, без браузера и без OCR.

Идея: сайт каждый раз генерирует JS с новыми случайными именами функций
и классов, но САМА ЛОГИКА операций всегда одна из пяти (выяснено эмпирически):

  A) "set_literal"   — записать литеральную строку во все элементы класса
  B) "remove_char"    — убрать символ по индексу из текста всех элементов класса
  C) "swap"            — поменять местами текст двух <td> по их плоскому индексу
  D) "splice_copy"     — скопировать один символ из одной <td> в другую,
                          опционально вставив ещё и точку (для процентов/дробных)
  E) "overlay"         — визуально наложить один элемент поверх другого
                          (для наших целей — просто взять текст элемента-донора)

Мы классифицируем каждую JS-функцию по ТЕЛУ (характерным конструкциям),
а не по имени — имена случайны. Дальше находим последовательность вызовов
внутри функции, запускаемой на DOMContentLoaded, и применяем эти операции
к дереву BeautifulSoup, построенному из html-фрагмента ответа.

При парсинге данных из ГАС Выборы ничего менять не надо, но надо обязательно положить файл в одну папку с election_stats_crawler.py
"""

import re
from typing import Optional

from bs4 import BeautifulSoup


# ---------- разбор JS ----------

def extract_function_defs(script: str) -> dict:
    """
    Находит все `var NAME = function(params) { тело };` с корректным
    подсчётом вложенных фигурных скобок (внутри Type E есть вложенный
    setTimeout(function(){...}), наивный regex тут сломается).
    Возвращает {name: {"params": [...], "body": "..."}}.
    """
    defs = {}
    for m in re.finditer(r'var\s+(\w+)\s*=\s*function\s*\(([^)]*)\)\s*\{', script):
        name = m.group(1)
        params = [p.strip() for p in m.group(2).split(',') if p.strip()]
        start = m.end()
        depth = 1
        i = start
        while depth > 0 and i < len(script):
            if script[i] == '{':
                depth += 1
            elif script[i] == '}':
                depth -= 1
            i += 1
        body = script[start:i - 1]
        defs[name] = {"params": params, "body": body}
    return defs


def classify_function(body: str) -> Optional[str]:
    if 'setTimeout(' in body and 'color' in body:
        return "overlay"
    if '.charAt(' in body:
        return "splice_copy"
    if "getElementsByTagName('td')" in body or 'getElementsByTagName("td")' in body:
        return "swap"
    if '.splice(' in body and ".split('')" in body:
        return "remove_char"
    if 'innerHTML' in body and 'getElementsByClassName(' in body:
        return "set_literal"
    return None


def parse_js_literal(s: str):
    s = s.strip()
    if s == 'false':
        return False
    if s == 'true':
        return True
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        inner = s[1:-1]
        if re.fullmatch(r'-?\d+', inner):
            return int(inner)
        return inner
    try:
        return int(s)
    except ValueError:
        return s  # скорее всего имя переменной-контейнера — не используется напрямую


def split_js_args(argstr: str):
    args, cur, in_quote = [], '', None
    for ch in argstr:
        if in_quote:
            cur += ch
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
            cur += ch
        elif ch == ',':
            args.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return [parse_js_literal(a) for a in args]


def extract_calls(body: str, known_names: set):
    """Находит вызовы известных функций внутри тела 'a' — просто
    func(args); подряд. Вложенный resize-обработчик дублирует часть
    вызовов. Это не страшно, повторное применение идемпотентно."""
    calls = []
    for m in re.finditer(r'\b(' + '|'.join(re.escape(n) for n in known_names) + r')\(([^()]*)\)\s*;', body):
        calls.append((m.group(1), split_js_args(m.group(2))))
    return calls


# ---------- применение операций к дереву ----------

def js_lec(td):
    """document-порядок 'последний вложенный элемент' — на практике у
    наших <td> обычно нет вложенных элементов, так что это чаще всего
    просто сам td."""
    node = td
    while True:
        children = [c for c in node.children if getattr(c, "name", None)]
        if not children:
            return node
        node = children[-1]


def op_set_literal(table, args):
    class_name, value, _container = args
    for el in table.find_all(class_=class_name):
        el.string = str(value)


def op_remove_char(table, args):
    class_name, index, _container = args
    for el in table.find_all(class_=class_name):
        chars = list(el.get_text())
        idx = index if index >= 0 else len(chars) + index
        if 0 <= idx < len(chars):
            del chars[idx]
        el.string = "".join(chars)


def op_swap(table, args):
    idx1, idx2, _container = args
    tds = table.find_all("td")
    a, b = js_lec(tds[idx1]), js_lec(tds[idx2])
    a_text, b_text = a.get_text(), b.get_text()
    a.string, b.string = b_text, a_text


def op_splice_copy(table, args):
    char_idx, src_idx, insert_pos, dst_idx, dot_pos, _container = args
    tds = table.find_all("td")
    src, dst = js_lec(tds[src_idx]), js_lec(tds[dst_idx])
    chars = list(dst.get_text())
    if dot_pos is not False:
        chars.insert(dot_pos, ".")
    src_text = src.get_text().strip()
    ch = src_text[char_idx] if 0 <= char_idx < len(src_text) else ""
    chars.insert(insert_pos, ch)
    dst.string = "".join(chars)


def op_overlay(table, args):
    class_name, target_idx, _container = args
    tds = table.find_all("td")
    if target_idx >= len(tds):
        return
    target = tds[target_idx]
    source = table.find(class_=class_name)
    if source is not None:
        target.string = source.get_text()


OPS = {
    "set_literal": op_set_literal,
    "remove_char": op_remove_char,
    "swap": op_swap,
    "splice_copy": op_splice_copy,
    "overlay": op_overlay,
}


def resolve_html(html: str, script: str) -> str:
    """
    Главная функция: берёт сырые html+script из ответа
    izbirkom.ru/reports/242, применяет все операции обфускации и
    возвращает html с уже правильными значениями.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return html

    defs = extract_function_defs(script)
    classified = {name: classify_function(info["body"]) for name, info in defs.items()}
    known_names = {n for n, k in classified.items() if k is not None}

    # тело функции, запускаемой на DOMContentLoaded — обычно 'a', но на
    # всякий случай ищем последнюю определённую функцию, не входящую в
    # известные 5 типов (сама 'a' не подходит ни под одну классификацию)
    entry_name = None
    for name, info in defs.items():
        if classified[name] is None:
            calls_here = extract_calls(info["body"], known_names)
            if len(calls_here) >= 2:
                entry_name = name
                break
    if entry_name is None:
        return str(table)

    for name, args in extract_calls(defs[entry_name]["body"], known_names):
        op_type = classified.get(name)
        op_fn = OPS.get(op_type)
        if op_fn is None:
            continue
        try:
            op_fn(table, args)
        except (IndexError, KeyError, TypeError):
            continue  # не даём одной сбойной операции обрушить всё остальное

    return str(table)
