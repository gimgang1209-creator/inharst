# -*- coding: utf-8 -*-
import os
import re
import time
import json
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from collections import Counter
from nltk.stem import WordNetLemmatizer
from sklearn.metrics.pairwise import cosine_similarity

from Bio import Entrez
from openai import OpenAI

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# =========================
# HTTP 유틸 (수정: 실제 requests 사용)
# =========================
def http_get_with_retry(
    url, *, headers=None, params=None, data=None,
    timeout=15, tries=3, backoff=2, allow_redirects=True
):
    last_exc = None
    for i in range(tries):
        try:
            r = requests.get(
                url,
                headers=headers,
                params=params,
                data=data,
                timeout=timeout,
                allow_redirects=allow_redirects,
            )
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if i < tries - 1:
                time.sleep(backoff ** i)
    raise last_exc


def between(val, lo, hi):
    try:
        return lo <= float(val) <= hi
    except Exception:
        return False


# =========================
# OpenAI & Entrez 설정
# =========================
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
Entrez.email = os.getenv("ENTREZ_EMAIL", "") or "you@example.com"  # 최소 기본값


# =========================
# 경로/저장소 설정 (lazy 생성)
# =========================
BASE_DIR = "agent_sub_result"
FULLTEXT_DIR = os.path.join(BASE_DIR, "fulltexts")

def ensure_dirs():
    """필요할 때만 생성"""
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(FULLTEXT_DIR, exist_ok=True)

# 파일명
INPUT_CSV = os.path.join(BASE_DIR, "pubmed_articles_with_pico.csv")
EMB_NPY = os.path.join(BASE_DIR, "pico_embeddings.npy")
OUTPUT_CSV = os.path.join(BASE_DIR, "filtered_articles_semantic.csv")


# =========================
# PubMed 검색/파싱
# =========================
def search_pubmed(query, retmax=1000):
    handle = Entrez.esearch(db="pubmed", term=query, retmax=retmax)
    record = Entrez.read(handle)
    handle.close()
    return record.get("IdList", [])


def fetch_details(id_list):
    if not id_list:
        return []
    ids = ",".join(id_list)
    handle = Entrez.efetch(db="pubmed", id=ids, retmode="xml")
    records = Entrez.read(handle)
    handle.close()
    return records.get("PubmedArticle", [])


def parse_article(article):
    try:
        pmid = str(article["MedlineCitation"]["PMID"])
        title = article["MedlineCitation"]["Article"]["ArticleTitle"]

        abstract = ""
        art = article["MedlineCitation"]["Article"]
        if "Abstract" in art:
            abstract_text = art["Abstract"]["AbstractText"]
            abstract = " ".join(abstract_text) if isinstance(abstract_text, list) else abstract_text

        doi = "NA"
        id_list = article.get("PubmedData", {}).get("ArticleIdList", [])
        for item in id_list:
            try:
                if item.attributes.get("IdType") == "doi":
                    doi = str(item)
                    break
            except Exception:
                pass

        journal = ""
        j = art.get("Journal", {})
        journal = j.get("Title", "") if isinstance(j, dict) else ""

        return {
            "PMID": pmid,
            "Title": title,
            "Abstract": abstract,
            "DOI": doi,
            "Journal": journal,
        }
    except Exception as e:
        print(f"Error parsing article: {e}")
        return None


# =========================
# JSON 파싱
# =========================
def json_parse(content):
    try:
        return json.loads(content)
    except Exception:
        match = re.search(r"\{.*\}", content or "", re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                return {"P": None, "I": None, "C": None, "O": None, "error": "Invalid JSON structure"}
        return {"P": None, "I": None, "C": None, "O": None, "error": "No JSON found"}


# =========================
# PICO 추출 (LLM)
# =========================
def extract_pico(abstract, retries=3):
    if not isinstance(abstract, str) or not abstract.strip():
        return {"P": None, "I": None, "C": None, "O": None}

    prompt = f"""
You are a medical research assistant.
Read the following abstract and summarize PICO elements using only SHORT KEYWORDS.

Abstract:
\"\"\"{abstract}\"\"\"

Return ONLY a valid JSON object with exactly 4 keys:
- P : Patient/Population (short keyword, normalized if possible)
- I : Intervention (short keyword, method or treatment)
- C : Control (short keyword, or "none" if not mentioned)
- O : Outcome (short keywords)

Example:
{{"P": "Stroke", "I": "EMG", "C": "Healthy", "O": "Motor recovery"}}
"""
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            content = (response.choices[0].message.content or "").strip()
            return json_parse(content)
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"P": None, "I": None, "C": None, "O": None, "error": str(e)}


# =========================
# 분포 시각화
# =========================
lemmatizer = WordNetLemmatizer()

def plot_distribution(df, column, output_dir):
    values = df[column].dropna().astype(str)
    all_tokens = []
    for val in values:
        tokens = re.split(r"[,\s;]+", val.strip())
        tokens = [lemmatizer.lemmatize(t.lower()) for t in tokens if t]
        all_tokens.extend(tokens)

    counter = Counter(all_tokens)
    counts = pd.Series(counter).sort_values(ascending=False)

    top_n = min(10, len(counts))
    counts_top = counts.head(top_n)

    plt.figure(figsize=(10, 6))
    counts_top.plot(kind="barh")
    plt.title(f"Top {top_n} Token Distribution in {column}")
    plt.xlabel("Frequency")
    plt.ylabel("Token")
    plt.gca().invert_yaxis()
    plt.tight_layout()

    ensure_dirs()
    plot_path = os.path.join(output_dir, f"{column}_token_distribution_top{top_n}.png")
    plt.savefig(plot_path)
    print(f"{column} top-{top_n} token distribution plot saved to {plot_path}")

    return counts


# =========================
# 임베딩 & 의미검색
# =========================
def get_embedding(text, model="text-embedding-3-small"):
    if not isinstance(text, str) or not text.strip():
        # text-embedding-3-small = 1536 차원
        return np.zeros(1536)
    response = client.embeddings.create(input=text, model=model)
    return np.array(response.data[0].embedding)


def build_embeddings(input_csv=INPUT_CSV, output_npy=EMB_NPY):
    df = pd.read_csv(input_csv)
    df["combined_text"] = df[["P", "I", "C", "O", "Title", "Abstract"]].fillna("").agg(" ".join, axis=1)

    embeddings = []
    for i, text in enumerate(df["combined_text"]):
        print(f"[{i+1}/{len(df)}] Embedding...")
        embeddings.append(get_embedding(text))

    embeddings = np.array(embeddings)
    ensure_dirs()
    np.save(output_npy, embeddings)

    print(f"Embeddings 저장 완료 → {output_npy}")
    return df, embeddings


def rerank_with_llm(query, candidates_df, top_n=20):
    candidate_texts = []
    for _, row in candidates_df.iterrows():
        entry = f"PMID: {row['PMID']}, Title: {row['Title']}, PICO: {row['P']}, {row['I']}, {row['C']}, {row['O']}"
        candidate_texts.append(entry)

    prompt = f"""
You are a medical research assistant.
A researcher asked: "{query}"

Below are candidate papers. Rank them by semantic relevance to the query.
Return ONLY a JSON list of the top {top_n} PMIDs in order of relevance.

Candidates:
{chr(10).join(candidate_texts)}
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()

        # 코드펜스 제거
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)

        # 1차: JSON 파싱 시도
        try:
            pmid_list = json.loads(raw)
        except Exception:
            # 2차: 대괄호 내부만 추출 후 파싱
            m = re.search(r"\[(.*?)\]", raw, re.S)
            if m:
                pmid_list = json.loads("[" + m.group(1) + "]")
            else:
                # 3차: PMID 패턴으로라도 추출
                pmid_list = re.findall(r"\b\d{6,9}\b", raw)

        # 리스트 보정
        pmid_list = [str(x).strip() for x in pmid_list][:top_n]

        # 실제 후보 교집합만 유지 + 순서 정렬
        reranked = candidates_df[candidates_df["PMID"].isin(pmid_list)]
        if not reranked.empty:
            reranked = reranked.set_index("PMID").loc[[p for p in pmid_list if p in set(reranked.index)]].reset_index()
            return reranked

        # 추출 실패 시 폴백
        return candidates_df.head(top_n)
    except Exception as e:
        print("⚠️ LLM rerank 실패(보강 후):", e)
        return candidates_df.head(top_n)


def semantic_search(
    query,
    emb_top_k=50,
    llm_top_k=20,
    input_csv=INPUT_CSV,
    emb_npy=EMB_NPY,
    output_csv=OUTPUT_CSV,
):
    df = pd.read_csv(input_csv)

    if os.path.exists(emb_npy):
        embeddings = np.load(emb_npy)
        if len(df) != embeddings.shape[0]:
            print("⚠️ .npy 파일 불일치 → 재생성")
            df, embeddings = build_embeddings(input_csv, emb_npy)
    else:
        df, embeddings = build_embeddings(input_csv, emb_npy)

    query_emb = get_embedding(query)
    sims = cosine_similarity([query_emb], embeddings)[0]
    df["similarity"] = sims

    emb_top_k = min(max(1, emb_top_k), len(df))
    candidates = df.sort_values(by="similarity", ascending=False).head(emb_top_k)

    llm_top_k = min(max(1, llm_top_k), len(candidates))
    final_results = rerank_with_llm(query, candidates, top_n=llm_top_k)

    final_results = final_results[["PMID", "Title", "Abstract", "P", "I", "C", "O", "DOI", "Journal"]]
    ensure_dirs()
    final_results.to_csv(output_csv, index=False, encoding="utf-8-sig")

    print(f"{len(final_results)}편 결과 저장 → {output_csv}")
    return final_results


# =========================
# DOI → URL / Selenium
# =========================
def get_paper_url(doi):
    try:
        r = http_get_with_retry("https://dx.doi.org/" + doi, allow_redirects=True, timeout=10)
        return r.url if r.status_code == 200 else None
    except Exception:
        return None


def setup_driver():
    driver_path = (os.getenv("CHROMEDRIVER") or "").strip()

    def build_options(headless_arg):
        opts = webdriver.ChromeOptions()
        if headless_arg:
            opts.add_argument(headless_arg)  # "--headless=new" 또는 "--headless"
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--window-size=1920,1080")
        return opts

    def start_with(headless_arg):
        opts = build_options(headless_arg)
        svc = Service(executable_path=driver_path) if driver_path else Service()  # Selenium Manager fallback
        return webdriver.Chrome(service=svc, options=opts)

    try:
        return start_with("--headless=new")
    except Exception:
        return start_with("--headless")


def extract_text_from_url(driver, url, doi, output_dir=None):
    if output_dir is None:
        output_dir = FULLTEXT_DIR
    """풀텍스트 후보 섹션 추출 후 FULLTEXT_DIR에 저장."""
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        sections = []
        for sec in soup.find_all(["section", "div"]):
            head = sec.find(["h2", "h3"])
            if head and any(k in head.get_text().lower() for k in ["abstract", "method", "materials"]):
                txt = sec.get_text(" ", strip=True)
                if len(txt) > 50:
                    sections.append(txt)

        full = "\n".join(sections)
        if not full:
            return None

        ensure_dirs()
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, doi.replace("/", "_") + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(full)
        return path
    except Exception:
        return None


# =========================
# GPT 기반 논문 분석/통합
# =========================
def analyze_paper_with_gpt(paper_text):
    prompt = f"""
다음 논문 텍스트(abstract + method)를 분석하여 아래에 답하시오:

1. 데이터는 어떤 것을 사용하는가?
2. 데이터는 어떻게 처리되는가?
3. 연구 설계 가이드라인은 무엇인가?
4. 분석은 어떻게 하였는가?
5. 주요 결과는 무엇인가?

논문 텍스트:
{paper_text[:40000]}

답변:
"""
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        return f"분석 실패: {e}"


def consolidate_results(analyses):
    consolidated = {
        "데이터": [],
        "처리 방식": [],
        "연구 설계": [],
        "분석 방법": [],
        "주요 결과": [],
    }
    patterns = {
        "데이터": r"1\..*?\n(.*?)(?=\n2\.)",
        "처리 방식": r"2\..*?\n(.*?)(?=\n3\.)",
        "연구 설계": r"3\..*?\n(.*?)(?=\n4\.)",
        "분석 방법": r"4\..*?\n(.*?)(?=\n5\.)",
        "주요 결과": r"5\..*?\n(.*)",
    }
    for doi, text in analyses.items():
        for key, pattern in patterns.items():
            m = re.search(pattern, text or "", re.DOTALL)
            if m:
                consolidated[key].append(f"DOI {doi}: {m.group(1).strip()}")

    report = "# 통합 연구 방법론 분석 보고서\n\n"
    for key, answers in consolidated.items():
        report += f"## {key}\n"
        report += "\n".join(answers) if answers else "정보 없음"
        report += "\n\n"
    return report


# =========================
# Main
# =========================
def main():
    ensure_dirs()  # 저장 경로 보장

    query_1 = input("조사하고 싶은 주제의 키워드를 입력해주세요 : ").strip()
    print(f"Searching Paper about: '{query_1}'")

    inputPn = input("최대 조사 문헌 수를 입력해 주세요. 입력이 없을경우 최대 1000개의 문헌을 조사합니다. : ").strip()
    try:
        retmax = int(inputPn) if inputPn else 1000
    except Exception:
        retmax = 1000

    id_list = search_pubmed(query_1, retmax=retmax)
    print(f"Found {len(id_list)} articles.")

    # 상세 정보 수집
    articles_data = []
    for i in range(0, len(id_list), 100):
        batch_ids = id_list[i:i+100]
        print(f"Fetching details for {len(batch_ids)} articles...")
        batch_articles = fetch_details(batch_ids)
        for article in batch_articles:
            parsed = parse_article(article)
            if parsed:
                articles_data.append(parsed)
        time.sleep(0.5)

    df = pd.DataFrame(articles_data)
    ensure_dirs()
    pubmed_csv_path = os.path.join(BASE_DIR, "pubmed_articles.csv")
    df.to_csv(pubmed_csv_path, index=False, encoding="utf-8-sig")

    # PICO 추출
    pico_list = []
    for idx, abstract in enumerate(df["Abstract"].fillna("")):
        pmid = df.iloc[idx]["PMID"]
        pico_result = extract_pico(abstract)
        pico_list.append(pico_result)
        print(f"[{idx + 1}/{len(df)}] PMID {pmid} → {pico_result}")

    pico_df = pd.DataFrame(pico_list)
    result_df = pd.concat([df, pico_df], axis=1)
    pico_csv_path = os.path.join(BASE_DIR, "pubmed_articles_with_pico.csv")
    result_df.to_csv(pico_csv_path, index=False, encoding="utf-8-sig")

    # 분포 시각화 & 토큰 분포 CSV
    category_counts = {}
    for col in ["P", "I", "C", "O"]:
        counts = plot_distribution(result_df, col, BASE_DIR)
        category_counts[col] = counts

    all_counts_df = pd.DataFrame(category_counts).fillna(0).astype(int)
    all_counts_df.index.name = "Token"
    csv_path = os.path.join(BASE_DIR, "pico_token_distribution_all.csv")
    all_counts_df.to_csv(csv_path, encoding="utf-8-sig")
    print(f"Combined wide-format CSV saved to {csv_path}")

    print("Topic analysis completed.")

    # 의미검색
    query_2 = input("세부 주제를 입력해주세요. : ").strip()
    results = semantic_search(query_2, emb_top_k=100, llm_top_k=50)
    print(results.to_string(index=False))

    # DOI 목록
    df_filtered = pd.read_csv(OUTPUT_CSV)
    dois = [d for d in df_filtered["DOI"].dropna().tolist() if isinstance(d, str) and d.strip().lower() != "na"]
    print(f"총 {len(dois)}개 DOI 발견")

    # 크롤링 시도 (실패해도 abstract로 대체)
    driver = setup_driver()
    analyses = {}
    try:
        for _, row in df_filtered.iterrows():
            doi = row["DOI"]
            abstract = row["Abstract"]
            pmid = row["PMID"]

            text = None
            if isinstance(doi, str) and doi.strip().lower() != "na":
                url = get_paper_url(doi)
                if url:
                    filepath = extract_text_from_url(driver, url, doi)
                    if filepath and os.path.exists(filepath):
                        with open(filepath, "r", encoding="utf-8") as f:
                            text = f.read()

            if not text:
                print(f"[{pmid}] Fulltext 미발견 → Abstract로 대체")
                text = abstract if isinstance(abstract, str) and abstract.strip() else None

            if text:
                print(f"[{pmid}] GPT 분석 시작")
                analyses[doi] = analyze_paper_with_gpt(text)
            else:
                print(f"[{pmid}] 분석 불가 (abstract 없음)")

            time.sleep(1.5)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # 통합 보고서
    report = consolidate_results(analyses)
    ensure_dirs()
    with open(os.path.join(BASE_DIR, "consolidated_report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print("통합 보고서 생성 완료")


if __name__ == "__main__":
    main()
