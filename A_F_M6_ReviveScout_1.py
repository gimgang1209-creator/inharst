#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
A_F_M6_ReviveScout_1.py
- 폴더 내 .docx / .hwp / .hwpx 논문 초안을 일괄 읽기
- LLM으로 핵심주제/갭 추출 후, PubMed 동향(최근 N년 연도별 카운트) 스카우팅
- '지금 쓰기 좋은' 신규 주제 아이디어를 랭킹/추천 + 씨앗 PMIDs/PICO/가설까지 제안
- 결과는 scout_output/ 에 JSON+MD로 저장 (M1~M5 단계와 연계)

필수/권장:
  pip install python-docx lxml olefile openai biopython pandas numpy tqdm
환경변수:
  OPENAI_API_KEY     # OpenAI 키 (gpt-4o-mini 기본)
  PUBMED_EMAIL       # Entrez.email 로 등록할 이메일(권장)
"""

import os, re, sys, json, time, math, shutil, zipfile, argparse

# ---- common retry utilities (inserted by patch) ----
import time, requests

def http_get_with_retry(url, *, headers=None, params=None, data=None, timeout=15, tries=3, backoff=2):
    last_exc = None
    for i in range(tries):
        try:
            return http_get_with_retry(url, headers=headers, params=params, data=data, timeout=timeout)
        except Exception as e:
            last_exc = e
            if i < tries-1:
                time.sleep(backoff**i)
    raise last_exc

def between(val, lo, hi):
    try:
        return lo <= float(val) <= hi
    except Exception:
        return False
# ----------------------------------------------------

from pathlib import Path
from typing import List, Dict, Tuple
from collections import defaultdict, Counter
from collections import Counter

import numpy as np
import pandas as pd
from tqdm import tqdm

# ---- Word / HWP(X) ----
from docx import Document as DocxDocument
from lxml import etree

# ---- OpenAI / LLM ----
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ---- PubMed ----
from Bio import Entrez

# ====== 하드코딩 설정(보안 유의: 절대 깃/공용저장소에 올리지 마세요) ======
OPENAI_API_KEY_HARDCODED = "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"  # 예: "sk-abc123..."
PUBMED_EMAIL_HARDCODED = "junggill0219@gmail.com"  # Entrez에 등록할 이메일
# ========================================================


def baseline_candidates_from_text(
    docs_raw: Dict[str, Dict], top_k: int = 12
) -> List[str]:
    """
    LLM 토픽이 비었을 때를 대비한 단순 n-gram 후보 생성기.
    """
    texts = []
    for row in docs_raw.values():
        t = row.get("text", "")
        if (
            not t
            or t.startswith("[HWP_READ_WARN]")
            or t.startswith("[HWP_READ_ERROR]")
            or t.startswith("[DOCX_READ_ERROR]")
            or t.startswith("[HWPX_READ_ERROR]")
        ):
            continue
        texts.append(t)
    corpus = " ".join(texts).lower()

    tokens = re.findall(r"[a-zA-Z가-힣][a-zA-Z가-힣\-/\+]+", corpus)
    stop = set(
        """
        study studies result results method methods conclusion conclusions background introduction discussion materials
        patient patients group groups control controls trial review meta analysis systematic randomized randomised
        figure table data significant outcome outcomes association correlation effect effects model models
        the and or of to in for with without between among on at from by as is are was were be have has had
        this that these those we our their its into over under using used based compared included excluded
        결과 방법 서론 고찰 자료 대상 군 대조 실험 분석 비교 연구 논문 표 그림 통계 유의 의미 수치 모델
    """.split()
    )
    tokens = [
        t
        for t in tokens
        if t not in stop and not re.fullmatch(r"\d+(\.\d+)?", t) and len(t) >= 2
    ]

    bigrams = [" ".join(pair) for pair in zip(tokens, tokens[1:])]
    bad_patterns = (r"^the ", r"^and ", r"^of ", r"^in ", r" 결과", r" 방법")
    cleaned = [
        bg for bg in bigrams if not any(re.search(bp, bg) for bp in bad_patterns)
    ]

    counts = Counter(cleaned)
    cands = [k for k, _ in counts.most_common(top_k)]

    if len(cands) < max(6, top_k // 2):
        uni_counts = Counter(tokens)
        for w, _ in uni_counts.most_common(top_k * 2):
            if w not in stop and all(w not in c for c in cands):
                cands.append(w)
            if len(cands) >= top_k:
                break

    cands = [re.sub(r"\s+", " ", c).strip() for c in cands if c.strip()]
    return cands[:top_k]


# ---------------------- I/O 설정 ----------------------
def ensure_outdirs(root: Path) -> Dict[str, Path]:
    out = root / "scout_output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "artifacts").mkdir(exist_ok=True)
    return {"root": out, "arts": out / "artifacts"}


# ---------------------- 파일 읽기 ----------------------
def read_docx_text(path: Path) -> str:
    try:
        doc = DocxDocument(str(path))
        parts = []
        for p in doc.paragraphs:
            txt = (p.text or "").strip()
            if txt:
                parts.append(txt)
        return "\n".join(parts)
    except Exception as e:
        return f"[DOCX_READ_ERROR] {path.name}: {e}"


def read_hwpx_text(path: Path) -> str:
    """
    HWPX는 zip 내 여러 섹션 XML. 텍스트 노드만 모아서 단순 추출.
    """
    try:
        with zipfile.ZipFile(str(path), "r") as z:
            xml_names = [n for n in z.namelist() if n.endswith(".xml")]
            texts = []
            for name in xml_names:
                try:
                    with z.open(name) as f:
                        tree = etree.fromstring(f.read())
                        # 모든 텍스트 노드 병합 (태그 무시, 공백 정규화)
                        texts.append(" ".join(tree.itertext()))
                except Exception:
                    continue
        return "\n".join(t.strip() for t in texts if t and t.strip())
    except Exception as e:
        return f"[HWPX_READ_ERROR] {path.name}: {e}"


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def read_hwp_text(path: Path) -> str:
    """
    .hwp (OLE) → hwp5txt가 있으면 사용. 없으면 스킵(경고).
    - Windows 환경이면 Hancom 설치/변환도 가능하지만 범용성 위해 외부툴 우선.
    """
    tool = which("hwp5txt")
    if not tool:
        return f"[HWP_READ_WARN] hwp5txt 미설치 → {path.name} 건너뜀"
    try:
        # hwp5txt path.hwp → stdout 텍스트
        import subprocess

        r = subprocess.run(
            [tool, str(path)], capture_output=True, text=True, check=True
        )
        return r.stdout
    except Exception as e:
        return f"[HWP_READ_ERROR] {path.name}: {e}"


def read_doc_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return read_docx_text(path)
    if ext == ".hwpx":
        return read_hwpx_text(path)
    if ext == ".hwp":
        return read_hwp_text(path)
    return ""  # 기타 확장자는 무시


def walk_and_collect(folder: Path) -> Dict[str, Dict]:
    """
    folder 내 문서 텍스트 수집.
    return: {file_id: {"path": str, "name": str, "ext": str, "text": str, "nchar": int}}
    """
    exts = {".docx", ".hwp", ".hwpx"}
    rows = {}
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            txt = read_doc_text(p)
            txt_clean = normalize_text(txt)
            rows[p.name] = {
                "path": str(p),
                "name": p.name,
                "ext": p.suffix.lower(),
                "text": txt_clean,
                "nchar": len(txt_clean),
            }
    return rows


def normalize_text(t: str) -> str:
    t = t or ""
    t = re.sub(r"\r\n?", "\n", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ---------------------- LLM 보조 ----------------------
def make_openai():
    # 환경변수 대신 하드코딩 사용
    key = (OPENAI_API_KEY_HARDCODED or "").strip()
    if not key or OpenAI is None:
        return None
    return OpenAI(api_key=key)


def ask_gpt(
    client, prompt: str, model="gpt-4o-mini", temperature=0.2, max_tokens=1200
) -> str:
    if client is None:
        return ""
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        return f"(LLM_ERROR) {e}"


def extract_topics_and_gaps(client, text: str, k: int = 8) -> Dict:
    """
    각 문서에서 핵심 토픽/키워드/미해결 질문(갭)을 수집.
    어떤 경우에도 항상 {"topics": [...], "keywords": [...], "gaps": [...]} 형태로 반환.
    """
    # 빈 입력 방어
    if not text:
        return {"topics": [], "keywords": [], "gaps": []}

    prompt = f"""
    다음은 의학/보건 논문 초안 일부입니다. 핵심 아이디어를 구조화하세요.

    [텍스트]
    {text[:8000]}

    [요구]
    1) '핵심 토픽' {k}개 이내 (짧은 한글 명사구)
    2) '핵심 키워드' 10~20개 (콤마로 구분)
    3) '미해결 질문/갭' 3~6개 (한문장)
    JSON으로:
    {{
      "topics": ["...", "..."],
      "keywords": ["...", "..."],
      "gaps": ["...", "..."]
    }}
    """

    out = ask_gpt(client, prompt, temperature=0.1, max_tokens=900)

    # LLM 호출이 비활성/실패한 경우 out이 빈 문자열일 수 있으므로 방어
    try:
        parsed = json.loads(extract_json(out))
        # ✅ 어떤 경우에도 dict로 정규화
        if isinstance(parsed, list):
            # 리스트로 오면 topics로 간주
            return {"topics": [str(x) for x in parsed][:k], "keywords": [], "gaps": []}
        if isinstance(parsed, dict):
            return {
                "topics": [str(x) for x in parsed.get("topics", [])][:k],
                "keywords": [str(x) for x in parsed.get("keywords", [])][:20],
                "gaps": [str(x) for x in parsed.get("gaps", [])][:6],
            }
        # 알 수 없는 타입이면 빈 구조
        return {"topics": [], "keywords": [], "gaps": []}
    except Exception:
        # JSON 파싱 실패 시, 텍스트에서 최대한 추정
        topics = re.findall(r"[-*]\s*(.+)", out or "")[:k]
        kws = [w.strip() for w in re.split(r"[,;]\s*", out or "") if 1 < len(w) < 40][
            :20
        ]
        gaps = re.findall(r"\?\s*$", out or "", re.M)[:4]
        return {"topics": topics, "keywords": kws, "gaps": gaps}


def expand_query_terms(client, topic: str, max_terms=6) -> List[str]:
    """
    주제 → PubMed 검색에 유리한 동의어/연결어 확장(간결)
    """
    prompt = f"""
    주제: "{topic}"
    PubMed에서 잘 잡히도록 간결한 동의어/연결어 {max_terms}개 이내로 제시.
    불필요한 수식어 제외, 핵심 명사/약어 위주. JSON 배열만 출력.
    """
    out = ask_gpt(client, prompt, temperature=0.2, max_tokens=200)
    try:
        arr = json.loads(extract_json(out))
        return [a.strip() for a in arr if a and isinstance(a, str)]
    except Exception:
        return [topic]


def draft_pico_and_hypothesis(client, topic: str, seed_pmids: List[str]) -> Dict:
    pmid_str = ", ".join(seed_pmids[:5]) if seed_pmids else "없음"
    prompt = f"""
    주제: {topic}
    관련 PMIDs: {pmid_str}

    요청:
    1) 간결한 한국어 제목 1줄
    2) 제안 PICO (P, I, C, O 각 1~2행)
    3) 검증 가능한 가설 1~2개
    JSON으로:
    {{
      "title": "...",
      "pico": {{"P":"...", "I":"...", "C":"...", "O":"..."}},
      "hypotheses": ["...", "..."]
    }}
    """
    out = ask_gpt(client, prompt, temperature=0.3, max_tokens=500)

    # 기본값 (LLM 실패/무응답 시 사용)
    default = {
        "title": topic,
        "pico": {"P": "", "I": "", "C": "", "O": ""},
        "hypotheses": [],
    }

    try:
        parsed = json.loads(extract_json(out))
        if isinstance(parsed, dict):
            return {
                "title": str(parsed.get("title") or topic),
                "pico": {
                    "P": str((parsed.get("pico") or {}).get("P", "")),
                    "I": str((parsed.get("pico") or {}).get("I", "")),
                    "C": str((parsed.get("pico") or {}).get("C", "")),
                    "O": str((parsed.get("pico") or {}).get("O", "")),
                },
                "hypotheses": [str(h) for h in (parsed.get("hypotheses") or [])][:2],
            }
        if isinstance(parsed, list):
            # 리스트가 오면 가설로 간주해서 최대 2개만 사용
            return {
                "title": topic,
                "pico": {"P": "", "I": "", "C": "", "O": ""},
                "hypotheses": [str(h) for h in parsed][:2],
            }
        return default
    except Exception:
        return default


def extract_json(s: str) -> str:
    m = re.search(r"\{.*\}|\[.*\]", s or "", re.S)
    return m.group(0) if m else "[]"


# ---------------------- PubMed 스카우팅 ----------------------
def setup_entrez():
    # 환경변수 대신 하드코딩 사용
    Entrez.email = PUBMED_EMAIL_HARDCODED or "you@example.com"


def pubmed_count(query: str, mindate: int, maxdate: int) -> Dict[int, int]:
    """
    연도별 카운트. 년도별로 쿼리해서 count만 수집.
    """
    counts = {}
    for y in range(mindate, maxdate + 1):
        q = f'({query}) AND ("{y}"[Date - Publication] : "{y}"[Date - Publication])'
        try:
            handle = Entrez.esearch(db="pubmed", term=q, retmode="xml", retmax=0)
            rec = Entrez.read(handle)
            handle.close()
            counts[y] = int(rec.get("Count", "0"))
        except Exception:
            counts[y] = 0
        time.sleep(0.34)  # rate-limit 완화
    return counts


def pubmed_sample_pmids(query: str, n=5) -> List[str]:
    try:
        handle = Entrez.esearch(
            db="pubmed", term=query, retmode="xml", sort="pub+date", retmax=n
        )
        rec = Entrez.read(handle)
        handle.close()
        return list(rec.get("IdList", []))
    except Exception:
        return []


def growth_score(year_counts: Dict[int, int]) -> float:
    """
    최근성/성장성 가중 스코어. (마지막년도 / 평균) + 단순 선형 추세 근사
    """
    if not year_counts:
        return 0.0
    years = sorted(year_counts)
    vals = np.array([year_counts[y] for y in years], dtype=float)
    if vals.sum() == 0:
        return 0.0
    recent_ratio = (vals[-1] + 1) / (np.mean(vals[:-1]) + 1) if len(vals) > 1 else 1.0
    # 선형 추세 기울기 표준화
    x = np.arange(len(vals))
    slope = 0.0
    try:
        A = np.vstack([x, np.ones_like(x)]).T
        slope = float(np.linalg.lstsq(A, vals, rcond=None)[0][0])
        slope = slope / (np.mean(vals) + 1)
    except Exception:
        pass
    return float(0.6 * recent_ratio + 0.4 * slope)


# ---------------------- 파이프라인 ----------------------
def aggregate_topics(doc_infos: Dict[str, Dict]) -> Dict[str, Dict]:
    """
    문서별 추출 토픽 → 전역 레벨로 집계/정규화
    """
    bag = []
    for fid, info in doc_infos.items():
        for t in info.get("topics", []):
            t0 = canonical_topic(t)
            if t0:
                bag.append((t0, fid))
    counts = Counter([t for t, _ in bag])
    by_topic_docs = defaultdict(set)
    for t, fid in bag:
        by_topic_docs[t].add(fid)
    return {
        "counts": counts,
        "coverage": {t: len(s) for t, s in by_topic_docs.items()},
        "doclists": {t: sorted(list(s)) for t, s in by_topic_docs.items()},
    }


def canonical_topic(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w가-힣/+\-\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def pick_candidates(agg: Dict, max_k=12, min_docs=1) -> List[str]:
    """
    자주 등장하거나(coverage) + 전체 빈도 높은 토픽을 우선 후보로.
    """
    items = []
    for t, c in agg["counts"].items():
        cov = agg["coverage"].get(t, 0)
        if cov >= min_docs:
            score = 0.5 * c + 0.5 * cov
            items.append((t, score, c, cov))
    items.sort(key=lambda x: x[1], reverse=True)
    return [t for t, *_ in items[:max_k]]


def build_pubmed_query(topic: str, expansions: List[str]) -> str:
    """
    단순 OR 묶음 + [Title/Abstract] 제한 (과도한 노이즈 방지)
    """
    terms = sorted(set([topic] + [e for e in expansions if e.lower() != topic.lower()]))
    joined = " OR ".join([f'"{t}"[Title/Abstract]' for t in terms])
    return f"({joined})"


def rank_ideas(candidates: List[Dict], doc_coverage: Dict[str, int]) -> List[Dict]:
    """
    성장성 + 커버리지(초안과의 연계성) + 최근성 PMIDs 보유 여부 등을 종합 점수화
    """
    out = []
    for item in candidates:
        t = item["topic"]
        g = growth_score(item["year_counts"])
        cov = math.log1p(doc_coverage.get(t, 0))
        pmids = item.get("seed_pmids", [])
        has_seed = 1.0 if pmids else 0.0
        score = 0.6 * g + 0.3 * cov + 0.1 * has_seed
        item["score"] = round(score, 4)
        out.append(item)
    return sorted(out, key=lambda x: x["score"], reverse=True)


def fetch_dois_for_pmids(pmids: List[str]) -> List[str]:
    """
    PubMed PMID 리스트에서 DOI만 뽑아 반환 (중복 제거).
    Entrez.efetch XML의 ArticleIdList / ELocationID를 모두 스캔.
    """
    if not pmids:
        return []
    ids = ",".join(pmids)
    dois = []
    try:
        handle = Entrez.efetch(db="pubmed", id=ids, retmode="xml")
        rec = Entrez.read(handle)
        handle.close()
        # 레코드 구조가 상황마다 달라서 두 경로 모두 스캔
        arts = rec.get("PubmedArticle", []) or rec.get("PubmedArticleSet", [])
        for art in arts:
            # 1) PubmedData → ArticleIdList → IdType="doi"
            try:
                id_list = art.get("PubmedData", {}).get("ArticleIdList", [])
                for it in id_list:
                    # Biopython이 dict나 str로 들어올 수 있어 방어
                    if isinstance(it, dict):
                        if it.get("IdType", "").lower() == "doi" and it.get("#text"):
                            dois.append(it["#text"].strip())
                    elif isinstance(it, str) and it.startswith("10."):
                        dois.append(it.strip())
            except Exception:
                pass
            # 2) MedlineCitation → Article → ELocationID(doi)
            try:
                elocs = (
                    art.get("MedlineCitation", {})
                    .get("Article", {})
                    .get("ELocationID", [])
                )
                if isinstance(elocs, dict):  # 단일 항목일 수 있음
                    elocs = [elocs]
                for e in elocs:
                    val = e.get("#text", "") if isinstance(e, dict) else str(e)
                    if isinstance(val, str) and val.lower().startswith("10."):
                        dois.append(val.strip())
            except Exception:
                pass
    except Exception:
        # 네트워크/파싱 오류 시 빈 리스트
        return []
    # 정리: 공백/중복 제거
    out = []
    seen = set()
    for d in dois:
        d0 = d.strip()
        if not d0 or d0 in seen:
            continue
        seen.add(d0)
        out.append(d0)
    return out


def generate_final_hypothesis_report(
    client,
    ideas_json_path: Path,
    md_path: Path,
    out_md_path: Path,
    top_k: int = 1,
    model: str = "gpt-4o-mini",
):
    """
    revive_scout_ideas.json + new_topic_ideas.md를 읽어
    최종 제안 가설 리포트(Final_topic_suggest.md)를 생성한다.
    """
    try:
        ideas = json.loads(Path(ideas_json_path).read_text(encoding="utf-8"))
    except Exception:
        ideas = []
    try:
        md_text = Path(md_path).read_text(encoding="utf-8")
    except Exception:
        md_text = ""

    if not ideas:
        # 아이디어가 없으면 종료
        content = "# Final Topic Suggestion\n\n(생성할 아이디어가 없습니다. revive_scout_ideas.json을 확인하세요.)\n"
        Path(out_md_path).write_text(content, encoding="utf-8")
        return

    # 점수 상위 top_k 추림
    ideas_sorted = sorted(ideas, key=lambda x: x.get("score", 0.0), reverse=True)[
        : max(1, top_k)
    ]

    # LLM 프롬프트 구성
    # 안전을 위해 필요한 정보만 압축 전달
    brief_rows = []
    for i, it in enumerate(ideas_sorted, 1):
        brief_rows.append(
            {
                "rank": i,
                "topic": it.get("topic", ""),
                "score": it.get("score", 0.0),
                "pico": (it.get("proposal") or {}).get("pico", {}),
                "hypotheses": (it.get("proposal") or {}).get("hypotheses", []),
                "seed_dois": it.get("seed_dois", []),
                "year_counts": it.get("year_counts", {}),
            }
        )
    prompt = f"""
다음은 사전 스카우팅 결과입니다. 점수 상위 {len(brief_rows)}개 후보의 요약을 기반으로,
임상 연구로 바로 확장 가능한 **최종 단일 연구 가설**을 한국어 마크다운으로 작성하세요.

[입력 요약(JSON)]
{json.dumps(brief_rows, ensure_ascii=False, indent=2)}

[참고 마크다운의 일부(new_topic_ideas.md 중 상단)]
{md_text[:3000]}

[요구 형식(변형 금지)]
# 연구 가설(Research Hypothesis)
(단일 가설을 한 문단으로, 개입/대조/대상/주요결과 포함. 최신성 강조)

## 근거 요약
(핵심 레퍼런스 역할만 간결히 2~4문장. DOI는 아래 목록과 매칭)


## 검증 가능한 세부 가설(Pre-specified)
1)
2)
3)
4)

## 설계·분석 핵심(요약)
- 무작위/배정/분석 원칙, 공변량, 주요 통계모형(간단)

## ‘최신성’ 포인트
- (이 연구가 지금 필요한 이유를 1~2줄)

## 씨앗 참고 DOI
- (위 JSON의 seed_dois에서 3~5개 엄선, '10.'로 시작하는 DOI만 나열)
    """.strip()

    # LLM 호출
    if client is None:
        # LLM이 없으면 최소 템플릿 생성
        fallback = "# 연구 가설(Research Hypothesis)\n\n(LLM 비활성화: OPENAI_API_KEY_HARDCODED를 설정하세요.)\n"
        Path(out_md_path).write_text(fallback, encoding="utf-8")
        return

    md_out = ask_gpt(client, prompt, model=model, temperature=0.2, max_tokens=1400)
    if not md_out or "(LLM_ERROR)" in md_out:
        md_out = "# 연구 가설(Research Hypothesis)\n\n(LLM 오류로 생성 실패. 로그를 확인하세요.)\n"

    # 파일 저장
    Path(out_md_path).write_text(md_out, encoding="utf-8")


# ---------------------- 메인 ----------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--folder", type=str, required=True, help="초안 폴더 경로 (.docx/.hwp/.hwpx)"
    )
    ap.add_argument("--years", type=int, default=5, help="최근 N년 스카우팅(기본 5)")
    ap.add_argument("--max-candidates", type=int, default=12)
    ap.add_argument("--model", type=str, default="gpt-4o-mini")
    args = ap.parse_args()

    folder = Path(args.folder).resolve()
    outs = ensure_outdirs(folder)
    setup_entrez()
    client = make_openai()

    # 1) 문서 수집
    docs_raw = walk_and_collect(folder)
    with open(outs["root"] / "collected_docs.json", "w", encoding="utf-8") as f:
        json.dump(docs_raw, f, ensure_ascii=False, indent=2)

    # 2) 문서별 토픽/갭 추출
    doc_infos = {}
    print(f"[INFO] {len(docs_raw)}개 문서 요약/토픽 추출")
    for fid, row in tqdm(docs_raw.items()):
        res = extract_topics_and_gaps(client, row["text"], k=8)
        doc_infos[fid] = {
            **row,
            **res,
        }
    with open(outs["root"] / "doc_topics.json", "w", encoding="utf-8") as f:
        json.dump(doc_infos, f, ensure_ascii=False, indent=2)

    # 3) 전역 집계 → 후보 토픽 선정
    agg = aggregate_topics(doc_infos)
    cands = pick_candidates(agg, max_k=args.max_candidates, min_docs=1)

    # 후보가 없으면 n-gram 백업 사용 (이미 함수 추가했다면 그대로 활용)
    if not cands:
        print(
            "[WARN] LLM 기반 토픽 후보 0개 → n-gram 기반 베이스라인 후보를 사용합니다.",
            flush=True,
        )
        cands = baseline_candidates_from_text(docs_raw, top_k=args.max_candidates)

    # 디버그: 후보 요약
    print(
        f"[DEBUG] 최종 후보 {len(cands)}개: {cands[:8]}{'...' if len(cands) > 8 else ''}",
        flush=True,
    )

    # 4) 토픽별 PubMed 스카우팅
    print(f"[INFO] PubMed 스카우팅 (최근 {args.years}년)", flush=True)
    ymax = int(time.strftime("%Y"))
    ymin = ymax - args.years + 1

    ideas = []
    for idx, t in enumerate(cands, start=1):
        # 확장어 생성
        expansions = expand_query_terms(client, t, max_terms=6)

        # 질의문 구성
        query = build_pubmed_query(t, expansions)

        # ✅ 디버그: 루프 입력 로그
        print(f"[PUBMED][{idx}/{len(cands)}] topic='{t}'", flush=True)
        print(f"  - expansions: {expansions}", flush=True)
        print(f"  - query: {query}", flush=True)
        print(f"  - years: {ymin}–{ymax}", flush=True)

        # 연도별 카운트
        yc = pubmed_count(query, ymin, ymax)
        print(f"  - year_counts: {yc}", flush=True)

        # 최신 PMID 샘플
        pmids = pubmed_sample_pmids(
            query
            + f' AND ("{ymin}"[Date - Publication] : "{ymax}"[Date - Publication])',
            n=5,
        )
        print(f"  - seed_pmids: {pmids}", flush=True)

        # ✅ 여기 2줄 추가: DOI 조회 후 로그
        dois = fetch_dois_for_pmids(pmids)[:5]
        print(f"  - seed_dois: {dois}", flush=True)

        ideas.append(
            {
                "topic": t,
                "expansions": expansions,
                "pubmed_query": query,
                "year_counts": yc,
                "seed_pmids": pmids,
                "seed_dois": dois,  # ← 이제 정의된 변수를 사용
            }
        )

    # 5) LLM으로 PICO/가설 제안 생성
    print("[INFO] PICO/가설 제안 생성")
    for item in ideas:
        item["proposal"] = draft_pico_and_hypothesis(
            client, item["topic"], item["seed_pmids"]
        )
        # ✅ 안전망: proposal은 반드시 dict
        if not isinstance(item.get("proposal"), dict):
            item["proposal"] = {
                "title": item["topic"],
                "pico": {"P": "", "I": "", "C": "", "O": ""},
                "hypotheses": [],
            }

    # 6) 랭킹
    ranked = rank_ideas(ideas, agg["coverage"])

    # 7) 결과 저장 (JSON + Markdown)
    with open(outs["root"] / "revive_scout_ideas.json", "w", encoding="utf-8") as f:
        json.dump(ranked, f, ensure_ascii=False, indent=2)

    md_lines = ["# 새 논문 주제 스카우팅 결과\n"]
    for i, it in enumerate(ranked, 1):
        title = (it.get("proposal", {}) or {}).get("title") or it["topic"]
        hyps = (it.get("proposal", {}) or {}).get("hypotheses") or []
        dois = it.get("seed_dois", []) or []

        md_lines += [
            f"## {i}. {title}",
            f"- 스코어: **{it['score']}**",
            "",
            "### 가설",
            *([f"- {h}" for h in hyps] if hyps else ["- (제안 없음)"]),
            "",
            "### 참고한 문헌 DOI",
            *([f"- {d}" for d in dois] if dois else ["- (해당 기간 DOI 없음)"]),
            "",
            "---",
            "",
        ]

    (outs["root"] / "new_topic_ideas.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )

    # 8) M1~M5 연계 힌트 파일 (선택)
    (outs["root"] / "next_steps.txt").write_text(
        "추천 토픽을 M1 검색(키워드) → M2 가설/분석 코드 생성 → M3 도표 생성 → M4 논문화 → M5 영문변환으로 이어질 수 있습니다.",
        encoding="utf-8",
    )

    # 9) 최종 가설 리포트 자동 생성
    try:
        generate_final_hypothesis_report(
            client=client,
            ideas_json_path=outs["root"] / "revive_scout_ideas.json",
            md_path=outs["root"] / "new_topic_ideas.md",
            out_md_path=outs["root"] / "Final_topic_suggest.md",
            top_k=1,  # 상위 1개로 단일 가설 생성 (원하면 2~3으로 조정)
            model=args.model,
        )
        print(" - Final_topic_suggest.md", flush=True)
    except Exception as e:
        print(f"[WARN] Final_topic_suggest 생성 실패: {e}", flush=True)

    print(f"\n[DONE] 결과 저장: {outs['root']}")
    print(" - revive_scout_ideas.json")
    print(" - new_topic_ideas.md")
    print(" - doc_topics.json / collected_docs.json")
    print(" - next_steps.txt")


if __name__ == "__main__":
    main()


# ---- M6 score breakdown export (inserted by patch) ----
try:
    import os, pandas as pd
    from pathlib import Path as _Path
    _wd = globals().get("WORK_DIR") or os.getenv("WORK_DIR")
    if _wd:
        out_dir = (_Path(_wd) / "R6_scout")
        out_dir.mkdir(parents=True, exist_ok=True)
        df_candidates = None
        for name in ("rank_df","candidates_df","final_df","results_df"):
            if name in globals() and isinstance(globals()[name], pd.DataFrame):
                df_candidates = globals()[name]
                break
        if df_candidates is not None:
            cols = [c for c in df_candidates.columns
                    if c.lower() in ("score","growth_score","recent_ratio","trend_slope","coverage")]
            if cols:
                md_path = out_dir / "score_breakdown.md"
                md_path.write_text(df_candidates[cols].head(50).to_markdown(index=False), encoding="utf-8")
except Exception as _e:
    print(f"[M6] score breakdown export skipped: {_e}")
# --------------------------------------------------------
