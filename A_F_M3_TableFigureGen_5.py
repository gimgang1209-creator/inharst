import os, re, subprocess, time
from openai import OpenAI
from openai import RateLimitError
import json
import numpy as np

# --- OpenAI 설정 ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))  # 실제 키 넣기

BASE_DIR = "analysis_result"
FIG_DIR = os.path.join(BASE_DIR, "figures")
DISPLAY_CODE_DIR = os.path.join(BASE_DIR, "display_code")


def ensure_dirs():
    """필요 시 디렉터리 생성 (lazy 방식)"""
    os.makedirs(FIG_DIR, exist_ok=True)
    os.makedirs(DISPLAY_CODE_DIR, exist_ok=True)


FINAL_MD = os.path.join(BASE_DIR, "final_analysis.md")
CODE_PY = "final_analysis.py"


# --- 파일 로드 ---
def load_text(path):
    if os.path.exists(path):
        return open(path, "r", encoding="utf-8").read()
    return ""


# --- figure/table 추천 ---
def ask_items(final_text, code_text, feedback=""):
    prompt = f"""
    당신은 임상의학 연구 통계전문가입니다. 아래의 final_analysis.md(결과요약)과 final_analysis.py(일부 코드)를 참고하여,
    학술논문 Results 섹션에 실릴 도표/그림 목록을 추천하세요.

    필수 원칙:
    - 반드시 final_analysis.md에 등장하는 분석 결과/지표/변수/그룹만 사용하세요.
    - final_analysis.py는 변수명 확인용 레퍼런스입니다(새 변수 가정 금지).
    - 논문 Results 관례에 맞춘 구성으로 제안하세요: (1) 기술통계표, (2) 주요결과표, (3) 주요그림, (4) 부가/감도분석, (5) 모델진단 중 최소 3가지 이상 포함.
    - 설명에는 구체적 변수·그룹·지표명을 넣고, 목적에는 해당 시각화/표가 어떤 해석을 돕는지 1문장으로 요약하세요.
    - 존재하지 않는 변수/분석 결과는 포함하지 마세요.

    출력 형식(한 줄당 하나, 세 부분은 하이픈으로 구분):
    1. display_type - 설명 - 목적
    (display_type 예: baseline_table, regression_table, survival_curve, forest_plot, ROC_curve, bar_chart, scatter_plot, calibration_curve, confusion_matrix 등)

    [사용자 누적 피드백]
    {feedback}

    [final_analysis.md]
    {final_text[:4000]}

    [final_analysis.py 일부]
    {code_text[:2000]}
    """
    # 모델 폴백 + 낮은 temperature로 구조화된 결과 유도
    models = [("gpt-5", 0.2), ("gpt-4o-mini", 0.2)]
    last_err = None

    for mdl, temp in models:
        for i in range(3):
            try:
                r = client.chat.completions.create(
                    model=mdl,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temp,
                )
                return (r.choices[0].message.content or "").strip()
            except RateLimitError as e:
                last_err = e
                time.sleep(1.2 * (i + 1))
            except Exception as e:
                last_err = e
                if "insufficient_quota" in str(e):
                    break
                time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"ask_items() LLM 호출 실패: {last_err}")


# --- 리스트 파싱 ---
def parse_items(text):
    """LLM이 출력한 추천 목록을 최대한 유연하게 파싱 (3-필드 또는 2-필드)"""
    if text is None:
        return []
    t = str(text).strip()

    # 코드펜스 제거
    m = re.search(r"```(?:json|python)?\s*(.*?)```", t, re.S | re.I)
    if m:
        t = m.group(1).strip()

    # 1) JSON 시도
    try:
        obj = json.loads(t)
        if isinstance(obj, list):
            items = []
            for it in obj:
                if not isinstance(it, dict):
                    continue
                dt = (
                    it.get("display_type") or it.get("type") or it.get("kind") or ""
                ).strip()
                desc = (it.get("desc") or it.get("description") or "").strip()
                purpose = (it.get("purpose") or it.get("goal") or "").strip()
                if dt and desc:
                    items.append({"display_type": dt, "desc": desc, "purpose": purpose})
            if items:
                return _sort_items(items)
    except Exception:
        pass

    # 2) 라인 기반 파싱
    items = []
    for line in t.splitlines():
        line = line.strip(" -*•\t")
        if not line:
            continue
        # 1) 세 칼럼: "1. type - desc - purpose"
        m3 = re.match(
            r"(?:\d+\.\s*)?([A-Za-z0-9_\-가-힣]+)\s*-\s*([^-\n]+?)\s*-\s*(.+)", line
        )
        if m3:
            items.append(
                {
                    "display_type": m3.group(1).strip(),
                    "desc": m3.group(2).strip(),
                    "purpose": m3.group(3).strip(),
                }
            )
            continue
        # 2) 두 칼럼: "1. type - desc"
        m2 = re.match(r"(?:\d+\.\s*)?([A-Za-z0-9_\-가-힣]+)\s*-\s*(.+)", line)
        if m2:
            items.append(
                {
                    "display_type": m2.group(1).strip(),
                    "desc": m2.group(2).strip(),
                    "purpose": "",
                }
            )
    return _sort_items(items)


def _sort_items(items):
    # 중요도: 회귀/모델요약 > 생존/포레스트 > 베이스라인 > ROC/교정 > 기타
    def score(it):
        t = it["display_type"].lower()
        desc = it.get("desc", "").lower()
        s = 0
        if any(k in t for k in ["regression", "model_summary", "logistic", "cox"]):
            s += 5
        if any(k in t for k in ["survival", "km", "forest"]):
            s += 4
        if "baseline" in t:
            s += 3
        if any(k in t for k in ["roc", "calibration"]):
            s += 2
        if any(k in desc for k in ["adjusted", "다변량", "보정"]):
            s += 1
        return s

    return sorted(items, key=score, reverse=True)


# --- 코드 블록 클린업 ---
def clean_code_block(code_output: str) -> str:
    match = re.search(r"```(?:python)?(.*?)```", code_output, re.DOTALL)
    if match:
        return match.group(1).strip()
    return code_output.strip()


# --- GPT 코드 생성 (append용) ---
def generate_code(item, code_text="", feedback=None):
    prompt = f"""
    당신은 Python 데이터 시각화 전문가입니다.
    아래 설명에 맞는 코드를 작성하세요.

    설명: {item['display_type']} - {item['desc']}

    필수 규칙:
    - 반드시 final_analysis.py에서 이미 로드된 DataFrame(df 또는 data)을 사용하세요.
    - pd.read_csv 등 데이터 재로딩 금지.
    - 존재하지 않는 변수/열 이름 사용 금지.
    - 중간 집계/파생이 필요하면 groupby/pivot/agg로 생성하세요.
    - import와 print 문은 포함하지 마세요. 필요한 경우 fig, ax 생성만 하세요.
    - fig, ax를 만들고 _format_axes(ax)를 호출하세요.
    - 범례는 오른쪽 바깥에 배치하세요: leg = ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5))
    - 플롯을 마친 뒤 반드시 m_tidy_finalize(name="파일이름", fig=fig, ax=ax, legend=leg, width="double" 또는 "single")로 저장까지 완료하세요.
    - plt.savefig 직접 호출 금지. (save_pub / m_tidy_finalize만 사용)
    - 흑백(단색) 저널 스타일을 가정하고 임의 컬러 팔레트 사용 금지.
    - 표는 table_to_files(df_out, "파일이름")로 저장하세요.
    - 주요 플롯 가이드:
      * 라인/점: linewidth≈1.8, 산점도 alpha≈0.8
      * 레이블에 단위 포함(가능 시)
      * 필요 시 _format_axes(ax, xrotate=30)

    출력 형식:
    - 실행 가능한 코드만 출력 (주석/print 금지).
    - 마지막에 반드시 m_tidy_finalize(...) 또는 table_to_files(...) 호출.
    """
    if feedback:
        prompt += f"\n이전 오류 피드백: {feedback}\n→ 수정된 코드 작성:"

    # ✅ 모델 폴백 + 안정 백오프
    model_candidates = [
        ("gpt-5", 0.2),  # 되면 최고
        ("gpt-4o", 0.2),  # 일반적으로 개통
        ("gpt-4o-mini", 0.2),  # 경량 폴백
    ]
    last_err = None

    for mdl, temp in model_candidates:
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=mdl,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temp,
                )
                code_output = (resp.choices[0].message.content or "").strip()
                return clean_code_block(code_output)
            except RateLimitError as e:
                # 429: 점진 백오프
                last_err = e
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:
                # 상태코드 분기(가능하면)
                last_err = e
                msg = str(e).lower()
                # 모델 미개통/오타 → 다음 모델로 폴백
                if (
                    "model_not_found" in msg
                    or "not found" in msg
                    or "does not exist" in msg
                ):
                    break  # 이 모델은 더 시도해봤자 의미 없음 → 다음 후보로
                if "insufficient_quota" in msg or "quota" in msg:
                    break  # 쿼터 부족 → 다음 후보 모델
                if "unauthorized" in msg or "invalid_api_key" in msg or "401" in msg:
                    # 키 문제는 폴백해도 동일하니 즉시 중단
                    raise RuntimeError(
                        "OpenAI API 인증 오류(키/프로젝트 권한)를 확인하세요."
                    ) from e
                # 그 외 일시 오류는 재시도
                time.sleep(1.2 * (attempt + 1))

    # 여기 오면 전부 실패
    raise RuntimeError(f"코드 생성 실패: {last_err}")


# --- 실행 (final_analysis.py 복사 + append) ---
def run_with_retry(extra_code, fname, base_code, max_retries=3):

    # ✅ PRELUDE: 저널 스타일 + 자동 겹침/잘림 보정 + 안전 저장 (inline)
    PRELUDE = r"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib as mpl
from pathlib import Path
import pandas as pd
import numpy as np

# figures 경로 보장
Path(r"{FIG_DIR}").mkdir(parents=True, exist_ok=True)

# --- Journal-grade defaults (B/W, serif, wide) ---
INCH = 1.0
COL_WIDTH_SINGLE = 3.35 * INCH   # ~85 mm
COL_WIDTH_DOUBLE = 7.0 * INCH    # ~178 mm

from cycler import cycler
mpl.rcParams.update({
    "figure.dpi": 150,
    "savefig.dpi": 600,
    "figure.figsize": (COL_WIDTH_SINGLE, 2.4*INCH),

    # Fonts / monochrome
    "font.family": "Times New Roman",
    "image.cmap": "Greys",
    "axes.prop_cycle": cycler("color", ["black"]) + cycler("linestyle", ["-"]),

    # Axes/labels
    "axes.titlesize": 10,
    "axes.labelsize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,

    # Lines & errorbars
    "lines.linewidth": 2.2,
    "errorbar.capsize": 3,
    "errorbar.linewidth": 0.8,

    # Spines & grid
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.15,
    "grid.linestyle": "-",
    "grid.linewidth": 0.4,

    # Legend outside right, no frame
    "legend.loc": "center left",
    "legend.frameon": False,
})

# ==== Utility: safe canvas draw ====
def _draw(fig):
    try:
        fig.canvas.draw()
    except Exception:
        pass

# ==== Basic axes formatting ====
def _format_axes(ax, xrotate=0):
    ax.tick_params(axis="both", which="both", direction="out", length=3)
    if xrotate:
        for label in ax.get_xticklabels():
            label.set_rotation(xrotate)
            label.set_horizontalalignment("right")
    ax.margins(x=0.02)
    return ax

# ==== Table exporters ====
def table_to_files(df: pd.DataFrame, name: str, index=False, floatfmt="{:.2f}"):
    base = Path(r"{FIG_DIR}") / name
    _df = df.copy()
    # 숫자 서식 통일
    for col in _df.select_dtypes(include=[np.number]).columns:
        _df[col] = _df[col].map(lambda x: floatfmt.format(x) if pd.notnull(x) else "")
    (base.with_suffix(".md")).write_text(_df.to_markdown(index=index), encoding="utf-8")
    _df.to_csv(base.with_suffix(".csv"), index=index, encoding="utf-8")
    try:
        latex = _df.to_latex(index=index, escape=True, na_rep="", column_format="l" + "r"*(_df.shape[1]-1), bold_rows=False)
        (base.with_suffix(".tex")).write_text("\\usepackage{booktabs}\n" + latex, encoding="utf-8")
    except Exception:
        pass

# ==== Two-group helpers ====
def add_mean_labels(ax, xs, ys):
    for x, y in zip(xs, ys):
        try:
            ax.text(x, y, f"{float(y):.2f}", ha="center", va="bottom", fontsize=8)
        except Exception:
            pass

def apply_two_group_style(lines):
    lines = list(lines)
    if len(lines) >= 2:
        try:
            lines[0].set_linestyle("-")    # solid
            lines[1].set_linestyle("--")   # dashed
        except Exception:
            pass

# ==== Auto layout helpers: prevent overlaps & clipping ====
from matplotlib.text import Text

def _labels_overlap(labels):
    bxs = []
    for l in labels:
        if isinstance(l, Text) and l.get_visible():
            try:
                bxs.append(l.get_window_extent())
            except Exception:
                pass
    for i in range(len(bxs)-1):
        if bxs[i].overlaps(bxs[i+1]):
            return True
    return False

import textwrap as _tw

def wrap_ticklabels(ax, axis="x", width=12, max_lines=2):
    labs = ax.get_xticklabels() if axis == "x" else ax.get_yticklabels()
    for t in labs:
        s = str(t.get_text())
        if len(s) > width:
            s = _tw.fill(s, width=width, max_lines=max_lines, placeholder="…")
            t.set_text(s)

def smart_xticks(ax, steps=(0, 20, 30, 45, 60), min_fs=7):
    fig = ax.figure
    _draw(fig)

    wrap_ticklabels(ax, 'x', width=12, max_lines=2)
    _draw(fig)

    for deg in steps:
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(deg)
            if deg:
                lbl.set_horizontalalignment('right')
        _draw(fig)
        if not _labels_overlap(ax.get_xticklabels()):
            break

    if _labels_overlap(ax.get_xticklabels()):
        for sz in range(9, min_fs-1, -1):
            for lbl in ax.get_xticklabels():
                lbl.set_fontsize(sz)
            _draw(fig)
            if not _labels_overlap(ax.get_xticklabels()):
                break

def expand_ylim_for_labels(ax, top_pad=0.08):
    try:
        ymin, ymax = ax.get_ylim()
        ax.set_ylim(ymin, ymax * (1 + top_pad))
    except Exception:
        pass

def ensure_margins(ax, x=0.02, y=0.05):
    try:
        ax.margins(x=x, y=y)
    except Exception:
        pass

def finalize_axes(ax, legend=None, leave_right=0.82):
    fig = ax.figure
    _draw(fig)

    smart_xticks(ax)
    ensure_margins(ax)
    expand_ylim_for_labels(ax)

    try:
        if legend is not None:
            fig.tight_layout(rect=(0, 0, leave_right, 1))
        else:
            fig.tight_layout()
    except Exception:
        pass


def save_pub(name, fig=None, tight=True, width="single", transparent=True, legend=None, right_pad=0.18):
    if fig is None:
        fig = plt.gcf()
    if width == 'double':
        fig.set_size_inches(COL_WIDTH_DOUBLE, fig.get_size_inches()[1], forward=True)
    try:
        if tight:
            if legend is not None:
                fig.tight_layout(rect=(0, 0, 1-right_pad, 1))
            else:
                fig.tight_layout()
    except Exception:
        pass
    base = Path(r"{FIG_DIR}") / name
    for ext in ('.pdf', '.svg', '.png'):
        try:
            fig.savefig(
                str(base.with_suffix(ext)),
                bbox_inches='tight',
                transparent=transparent,
                dpi=600 if ext == '.png' else mpl.rcParams.get('savefig.dpi', 600),
                pad_inches=0.02,
            )
        except Exception:
            pass
    try:
        plt.close(fig)
    except Exception:
        pass
    print(f"[Saved] Figure saved to {base.with_suffix('.png')}")
    return base

# 원샷 편의 함수 (이 파일 내부 전용 이름)
def m_tidy_finalize(name='Figure_1', fig=None, ax=None, legend=None, width='single', transparent=True):
    if fig is None:
        fig = plt.gcf()
    if ax is None:
        try:
            ax = fig.axes[0]
        except Exception:
            ax = None
    if ax is not None:
        finalize_axes(ax, legend=legend)
    return save_pub(name, fig=fig, width=width, transparent=transparent, legend=legend)
"""
    PRELUDE = PRELUDE.replace("{FIG_DIR}", FIG_DIR)  # ← 여기서만 경로 치환

    script_path = os.path.join(DISPLAY_CODE_DIR, fname)
    full_code = PRELUDE + base_code + "\n\n" + extra_code

    previous_code = full_code

    for attempt in range(max_retries):
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(full_code)

        try:
            subprocess.run(
                ["python", script_path], check=True, capture_output=True, text=True
            )
            print(f"{fname} 실행 성공 (저장 위치: {script_path})")
            return True

        except subprocess.CalledProcessError as e:
            print(f"{fname} 실행 실패 (시도 {attempt+1}/{max_retries})")

            error_prompt = f"""
            당신은 Python 디버깅 전문가입니다.
            아래는 실행된 코드와 에러 로그입니다.

            [실행된 코드]
            ```python
            {previous_code[:8000]}
            ```

            [에러 로그]
            {e.stderr[:8000]}

            요청:
            - NameError/KeyError면 누락된 DataFrame 선언이나 잘못된 변수명을 수정하세요.
            - df 또는 data에서 groupby/pivot 등을 사용해 중간 계산을 정의하세요.
            - 실행 가능한 코드만 출력하세요 (주석/print 금지).
            - 그림 저장은 m_tidy_finalize("이름", fig=fig, ax=ax, legend=leg)만 사용하고 plt.savefig는 사용하지 마세요.
            - 표 저장은 table_to_files(df_out, "이름")만 사용하세요.
            """

            feedback = ""
            for i in range(3):
                try:
                    resp = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": error_prompt}],
                        temperature=0,
                    )
                    feedback = (resp.choices[0].message.content or "").strip()
                    break
                except RateLimitError:
                    time.sleep(2**i)
                except Exception as ex:
                    if "insufficient_quota" in str(ex):
                        break

            fixed_code = clean_code_block(feedback) if feedback else ""
            if not fixed_code:
                print("LLM 수정 생략: 쿼터/레이트리밋으로 자동 수정 없이 종료")
                return False

            full_code = PRELUDE + base_code + "\n\n" + fixed_code
            previous_code = full_code

    print("최대 재시도 횟수를 초과했습니다.")
    return False

# --- 배치 실행: 여러 아이템을 한 번에 ---
def run_batch(items, base_code, filename="final_batch_run.py", max_retries=5):
    import textwrap, hashlib

    # 1) 아이템 중복 제거 (display_type+desc 기준)
    seen = set()
    dedup = []
    for it in items:
        key = (it.get("display_type","").strip(), it.get("desc","").strip())
        if key not in seen:
            seen.add(key)
            dedup.append(it)
    items = dedup

    # 2) 각 아이템별 코드 생성
    generated = []
    for it in items:
        fb = it.get("feedback","") or ""
        # 배치에서도 저장 강제 규칙을 주입
        forced_rule = (
            '플롯 마지막에는 반드시 m_tidy_finalize('
            'name="Figure_1", fig=fig, ax=ax, legend=leg, width="double")를 '
            '호출해 저장까지 완료하세요.'
        )
        merged_fb = (fb + "\n" + forced_rule).strip()
        code = generate_code(it, base_code, merged_fb)
        generated.append((it, code))

    # 3) 단일 스크립트 구성: PRELUDE + base_code + 함수들 + main 루프
    PRELUDE = r"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib as mpl
from pathlib import Path
import pandas as pd
import numpy as np
Path(r"{FIG_DIR}").mkdir(parents=True, exist_ok=True)

# --- 여기부터 스타일/헬퍼 (이미 인라인 개선본) ---
INCH = 1.0
COL_WIDTH_SINGLE = 3.35 * INCH
COL_WIDTH_DOUBLE = 7.0 * INCH
from cycler import cycler
mpl.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 600,
    "figure.figsize": (COL_WIDTH_SINGLE, 2.4*INCH),
    "font.family": "Times New Roman", "image.cmap": "Greys",
    "axes.prop_cycle": cycler("color", ["black"]) + cycler("linestyle", ["-"]),
    "axes.titlesize": 10, "axes.labelsize": 10,
    "xtick.labelsize": 9, "ytick.labelsize": 9,
    "lines.linewidth": 2.2, "errorbar.capsize": 3, "errorbar.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.15, "grid.linestyle": "-", "grid.linewidth": 0.4,
    "legend.loc": "center left", "legend.frameon": False,
})

def _draw(fig):
    try: fig.canvas.draw()
    except Exception: pass

def _format_axes(ax, xrotate=0):
    ax.tick_params(axis="both", which="both", direction="out", length=3)
    if xrotate:
        for label in ax.get_xticklabels():
            label.set_rotation(xrotate); label.set_horizontalalignment("right")
    ax.margins(x=0.02); return ax

def table_to_files(df: pd.DataFrame, name: str, index=False, floatfmt="{:.2f}"):
    base = Path(r"{FIG_DIR}") / name
    _df = df.copy()
    for col in _df.select_dtypes(include=[np.number]).columns:
        _df[col] = _df[col].map(lambda x: floatfmt.format(x) if pd.notnull(x) else "")
    (base.with_suffix(".md")).write_text(_df.to_markdown(index=index), encoding="utf-8")
    _df.to_csv(base.with_suffix(".csv"), index=index, encoding="utf-8")
    try:
        latex = _df.to_latex(index=index, escape=True, na_rep="", column_format="l" + "r"*(_df.shape[1]-1), bold_rows=False)
        (base.with_suffix(".tex")).write_text("\\usepackage{booktabs}\n" + latex, encoding="utf-8")
    except Exception: pass

from matplotlib.text import Text
def _labels_overlap(labels):
    bxs=[]; 
    for l in labels:
        if isinstance(l, Text) and l.get_visible():
            try: bxs.append(l.get_window_extent())
            except Exception: pass
    for i in range(len(bxs)-1):
        if bxs[i].overlaps(bxs[i+1]): return True
    return False

import textwrap as _tw
def wrap_ticklabels(ax, axis="x", width=12, max_lines=2):
    labs = ax.get_xticklabels() if axis=="x" else ax.get_yticklabels()
    for t in labs:
        s = str(t.get_text())
        if len(s) > width:
            t.set_text(_tw.fill(s, width=width, max_lines=max_lines, placeholder="…"))

def smart_xticks(ax, steps=(0,20,30,45,60), min_fs=7):
    fig = ax.figure; _draw(fig)
    wrap_ticklabels(ax, "x", 12, 2); _draw(fig)
    for deg in steps:
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(deg); 
            if deg: lbl.set_horizontalalignment("right")
        _draw(fig)
        if not _labels_overlap(ax.get_xticklabels()): break
    if _labels_overlap(ax.get_xticklabels()):
        for sz in range(9, min_fs-1, -1):
            for lbl in ax.get_xticklabels(): lbl.set_fontsize(sz)
            _draw(fig)
            if not _labels_overlap(ax.get_xticklabels()): break

def expand_ylim_for_labels(ax, top_pad=0.08):
    try:
        ymin, ymax = ax.get_ylim(); ax.set_ylim(ymin, ymax*(1+top_pad))
    except Exception: pass

def ensure_margins(ax, x=0.02, y=0.05):
    try: ax.margins(x=x, y=y)
    except Exception: pass

def finalize_axes(ax, legend=None, leave_right=0.82):
    fig = ax.figure; _draw(fig)
    smart_xticks(ax); ensure_margins(ax); expand_ylim_for_labels(ax)
    try:
        fig.tight_layout(rect=(0,0,leave_right,1) if legend is not None else None)
    except Exception: pass

def save_pub(name, fig=None, tight=True, width="single", transparent=True, legend=None, right_pad=0.18):
    if fig is None: fig = plt.gcf()
    if width=="double":
        fig.set_size_inches(COL_WIDTH_DOUBLE, fig.get_size_inches()[1], forward=True)
    try:
        if tight:
            fig.tight_layout(rect=(0,0,1-right_pad,1) if legend is not None else None)
    except Exception: pass
    base = Path(r"{FIG_DIR}")/name
    for ext in (".pdf",".svg",".png"):
        try:
            fig.savefig(str(base.with_suffix(ext)), bbox_inches="tight",
                        transparent=transparent, dpi=600 if ext==".png" else mpl.rcParams.get("savefig.dpi",600),
                        pad_inches=0.02)
        except Exception: pass
    try: plt.close(fig)
    except Exception: pass
    print(f"[Saved] Figure saved to {base.with_suffix('.png')}")
    return base

def add_mean_labels(ax, xs, ys):
    for x,y in zip(xs, ys):
        try: ax.text(x, float(y), f"{float(y):.2f}", ha="center", va="bottom", fontsize=8)
        except Exception: pass

def apply_two_group_style(lines):
    lines=list(lines)
    if len(lines)>=1: lines[0].set_linestyle("-")
    if len(lines)>=2: lines[1].set_linestyle("--")

def m_tidy_finalize(name='Figure_1', fig=None, ax=None, legend=None, width='single', transparent=True):
    if fig is None: fig = plt.gcf()
    if ax is None:
        try: ax = fig.axes[0]
        except Exception: ax = None
    if ax is not None: finalize_axes(ax, legend=legend)
    return save_pub(name, fig=fig, width=width, transparent=transparent, legend=legend)
"""
    PRELUDE = PRELUDE.replace("{FIG_DIR}", FIG_DIR)

    # 함수 래핑 유틸 (들여쓰기)
    def wrap_as_func(idx, code):
        header = f"\n\ndef plot_{idx}(df=None, data=None):\n"
        body = "    " + "\n    ".join(code.strip().splitlines()) + "\n"
        return header + body

    functions = []
    calls = []
    for i, (it, code) in enumerate(generated, start=1):
        functions.append(wrap_as_func(i, code))
        # 각 호출은 예외를 삼켜서 배치 전체를 계속 진행
        calls.append(
f"""
try:
    plot_{i}(df if 'df' in globals() else None, data if 'data' in globals() else None)
    print("[OK] {i}/{len(generated)} {it.get('display_type','?')}")
except Exception as _e:
    import traceback as _tb
    print("[FAIL] {i}/{len(generated)} {it.get('display_type','?')} ::", _e)
    _tb.print_exc()
"""
        )

    script = PRELUDE + "\n\n" + base_code + "\n" + "".join(functions) + "\n\nif __name__=='__main__':\n" + "\n".join("    "+c.strip() for c in calls)

    # 4) 한 번만 실행
    script_path = os.path.join(DISPLAY_CODE_DIR, filename)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    for attempt in range(max_retries):
        try:
            subprocess.run(["python", script_path], check=True, capture_output=True, text=True)
            print(f"{filename} 배치 실행 성공")
            return True
        except subprocess.CalledProcessError as e:
            print(f"{filename} 배치 실행 실패 (시도 {attempt+1}/{max_retries})")
            # 단순 재시도 (스크립트는 이미 기록되어 있어 디버깅 용이)
            time.sleep(1.2*(attempt+1))
    return False

# --- 메인 ---
def main():
    ensure_dirs()
    final_text = load_text(FINAL_MD)
    code_text = load_text(CODE_PY)

    if not final_text and not code_text:
        print("final_analysis.md 및 final_analysis.py가 비어있습니다.")
        return

    feedback_history = []
    raw_list = ask_items(final_text, code_text)
    items = parse_items(raw_list)

    while True:
        print("\n=== 추천 리스트 ===")
        for idx, item in enumerate(items, 1):
            print(f"{idx}. {item['display_type']} - {item['desc']}")

        choice = input("\n실행/취소/수정 입력 : ").strip()

        if choice == "취소":
            print("프로그램 종료")
            return

        elif choice == "실행":
            break

        elif choice == "수정":
            fb = input("수정할 내용을 입력하세요: ").strip()
            feedback_history.append(fb)
            all_feedback = "\n".join(feedback_history)
            raw_list = ask_items(final_text, code_text, feedback=all_feedback)
            items = parse_items(raw_list)

        else:
            print("잘못된 입력입니다. (실행/취소/수정 중 선택하세요)")

    # final_analysis.py 원본 로드
    with open(CODE_PY, "r", encoding="utf-8") as f:
        base_code = f.read()

    USE_BATCH = True  # False로 하면 예전처럼 개별 실행

    if USE_BATCH:
        run_batch(items, base_code, filename="final_batch_run.py", max_retries=5)
    else:
        for item in items:
            fname = f"final_with_{item['display_type']}.py"
            code = generate_code(item, code_text)
            run_with_retry(code, fname, base_code)
            time.sleep(0.5)


if __name__ == "__main__":
    main()
