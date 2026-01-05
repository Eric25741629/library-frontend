#!/usr/bin/env python3
"""
簡易 WebPAC 爬蟲（使用 requests + BeautifulSoup）

功能：
- 自動從首頁尋找搜尋表單（heuristic），組成請求（GET/POST）並提交。
- 嘗試解析常見的結果區塊（table rows、list items、或一般連結），回傳標題與連結。
- 處理編碼、cookies、headers，並以 JSON 列印結果。

用法：
    python3 libwebpac_scraper.py "關鍵字"

注意：網站結構可能改變；本程式包含多種解析策略以提高容錯。
"""
import sys
import json
import logging
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

BASE_URL = "https://www.libwebpac.yuntech.edu.tw/"


def discover_search_form(soup: BeautifulSoup) -> Optional[dict]:
    # Heuristics: look for form with input name commonly used for keywords
    candidates = []
    for form in soup.find_all('form'):
        inputs = {inp.get('name'): inp for inp in form.find_all(['input', 'select', 'textarea']) if inp.get('name')}
        # if any common names appear, consider it
        common = set(inputs.keys()) & set(['search', 'q', 'keyword', 'searchArg', 'query', 'key'])
        if common:
            candidates.append((form, inputs))

    if candidates:
        form, inputs = candidates[0]
        return {'form': form, 'inputs': inputs}

    # fallback: return the first form if exists
    form = soup.find('form')
    if form:
        inputs = {inp.get('name'): inp for inp in form.find_all(['input', 'select', 'textarea']) if inp.get('name')}
        return {'form': form, 'inputs': inputs}

    return None


def build_payload(inputs: dict, query: str) -> dict:
    payload = {}
    for name, elem in inputs.items():
        val = elem.get('value', '') or ''
        # try to identify keyword field heuristically
        lname = name.lower()
        if any(k in lname for k in ('search', 'q', 'keyword', 'query', 'key', 'searcharg')):
            payload[name] = query
        else:
            payload[name] = val
    return payload


def parse_results(soup: BeautifulSoup, base: str) -> List[Dict]:
    results = []

    # Strategy 1: table rows in a results table
    tables = soup.find_all('table')
    for table in tables:
        # try to find rows that look like results
        rows = table.find_all('tr')
        for r in rows:
            a = r.find('a')
            if a and a.get_text(strip=True):
                title = a.get_text(strip=True)
                href = a.get('href')
                results.append({'title': title, 'link': urljoin(base, href) if href else None})
        if results:
            return results

    # Strategy 2: list items
    items = soup.find_all(['li', 'div'], class_=lambda c: c and 'result' in c.lower())
    for it in items:
        a = it.find('a')
        if a and a.get_text(strip=True):
            results.append({'title': a.get_text(strip=True), 'link': urljoin(base, a.get('href')) if a.get('href') else None})
    if results:
        return results

    # Strategy 3: generic links in main content area
    main = soup.find('main') or soup.find(id=lambda v: v and 'content' in v.lower()) or soup
    for a in main.find_all('a'):
        text = a.get_text(strip=True)
        href = a.get('href')
        if text and href and not href.startswith('#'):
            results.append({'title': text, 'link': urljoin(base, href)})
        if len(results) >= 50:
            break

    return results


def search_libwebpac(query: str, max_results: int = 20) -> List[Dict]:
    session = requests.Session()
    headers = {
        'User-Agent': 'libwebpac-scraper/1.0 (+https://example.local)'
    }

    # GET homepage
    r = session.get(BASE_URL, headers=headers, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, 'lxml')

    form_info = discover_search_form(soup)
    if not form_info:
        logging.warning('找不到搜尋表單，將直接對首頁進行關鍵字查找。')
        # fallback: search homepage links/text
        return parse_results(soup, BASE_URL)[:max_results]

    form = form_info['form']
    inputs = form_info['inputs']
    payload = build_payload(inputs, query)

    action = form.get('action') or ''
    method = (form.get('method') or 'get').lower()
    submit_url = urljoin(BASE_URL, action)

    logging.info('提交搜尋到: %s (method=%s)', submit_url, method.upper())

    if method == 'post':
        resp = session.post(submit_url, data=payload, headers=headers, timeout=30)
    else:
        resp = session.get(submit_url, params=payload, headers=headers, timeout=30)

    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    rsoup = BeautifulSoup(resp.text, 'lxml')

    results = parse_results(rsoup, submit_url)
    return results[:max_results]


def main(argv):
    if len(argv) < 2:
        print('Usage: python3 libwebpac_scraper.py "關鍵字"')
        return 2

    query = argv[1]
    try:
        results = search_libwebpac(query)
    except Exception as e:
        logging.error('搜尋失敗: %s', e)
        return 1

    print(json.dumps({'query': query, 'count': len(results), 'results': results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
