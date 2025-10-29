import os, re, json, pandas as pd, subprocess, tkinter as tk, importlib.util, runpy
from openai import BadRequestError

# ---- common retry utilities (inserted by patch) ----
import time, requests

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
# ----------------------------------------------------

from tkinter import filedialog
from openai import OpenAI

# OpenAI API 설정
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def gpt_request_code(prompt: str) -> str:
    """
    코드 생성/수정 전용: gpt-5 사용 (chat.completions에서 temperature 미전달)
    - 코드펜스 제거까지 처리
    """
    try:
        r = client.chat.completions.create(
            model="gpt-5",
            messages=[{"role": "user", "content": prompt}],
            # temperature 미전달 (gpt-5는 chat.completions에서 거부함)
            max_tokens=1600,
        )
    except BadRequestError as e:
        # (안전망) temperature 관련 등 파라미터 이슈 시 재시도 로직 예비
        r = client.chat.completions.create(
            model="gpt-5",
            messages=[{"role": "user", "content": prompt}],
        )
    text = (r.choices[0].message.content or "").strip()
    # 코드펜스 제거
    text = re.sub(r"^```(?:python)?\s*|\s*```$", "", text, flags=re.I)
    return text



# 결과 저장 폴더
RESULT_DIR = "analysis_result"
def ensure_result_dir():
    os.makedirs(RESULT_DIR, exist_ok=True)
    os.makedirs(os.path.join(RESULT_DIR, "figures"), exist_ok=True)  # 필요시
    os.makedirs(os.path.join(RESULT_DIR, "display_code"), exist_ok=True)  # 필요시


# CSV 데이터 로드 및 요약
def analyze_csv(csv_path):
    ensure_result_dir()
    try:
        df = pd.read_csv(csv_path, low_memory=False)
    except Exception as e:
        return None, f"CSV 파일 읽기 오류: {e}"
    summary = {
        "columns": df.columns.tolist(),
        "dtypes": df.dtypes.astype(str).to_dict(),
        "head": df.head(3).to_dict(orient="records"),
        "shape": df.shape,
    }
    pd.DataFrame(
        {"columns": summary["columns"], "dtypes": list(summary["dtypes"].values())}
    ).to_csv(os.path.join(RESULT_DIR, "variables.csv"), index=False)
    with open(os.path.join(RESULT_DIR, "variables.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return df, summary


# GPT 요청
def gpt_request(prompt):
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return r.choices[0].message.content.strip()


# 가설 검증
def validate_hypothesis(hypothesis, method, summary, csv_path, feedback=None):
    report_path = os.path.join("agent_sub_result", "consolidated_report.md")
    report_summary = (
        open(report_path, "r", encoding="utf-8").read()
        if os.path.exists(report_path)
        else ""
    )

    feedback_block = f"\n연구자 피드백(반영 필수): {feedback}\n" if feedback else ""

    prompt = f"""
    당신은 의학 연구 보조원입니다.
    아래 연구 가설이 문헌(consolidated_report.md)과 데이터 컬럼에 근거하여 의미 있는지 평가하세요.
    가능하면 최근 피드백을 최우선으로 반영하여, 변수 선정·그룹화·분석 방향을 업데이트하세요.
    {feedback_block}
    연구 가설: {hypothesis}
    분석법: {method}

    데이터 요약:
    - 컬럼명: {summary['columns']}
    - 크기: {summary['shape']}

    문헌 분석 보고서:
    {report_summary[:40000]}

    요청:
    1. 가설이 문헌과 데이터 컬럼 기반으로 타당한지 검증하세요.
    2. 분석에 사용할 수 있는 변수 후보, 그룹 설정, 분석 방향만 제시하세요.
    3. 코드 예시는 제시하지 마세요.
    4. (중요) 상단의 '연구자 피드백'을 반드시 반영해 업데이트 사항을 명확히 기술하세요.
    """
    return gpt_request(prompt)


# 가설 저장 헬퍼
def save_hypothesis_eval(text: str):
    hv_md = os.path.join(RESULT_DIR, "hypothesis_validation.md")
    hv_json = os.path.join(RESULT_DIR, "hypothesis_validation.json")
    with open(hv_md, "w", encoding="utf-8") as f:
        f.write(text)
    with open(hv_json, "w", encoding="utf-8") as f:
        json.dump({"HypothesisValidationFinal": text}, f, ensure_ascii=False, indent=2)


# GPT 분석 코드 제안
def generate_analysis_code(hypothesis, method, summary, csv_path, hypothesis_eval):
    report_path = os.path.join("agent_sub_result", "consolidated_report.md")
    hv_path = os.path.join("agent_sub_result", "hypothesis_validaion.md")

    report_summary = (
        open(report_path, "r", encoding="utf-8").read()
        if os.path.exists(report_path)
        else ""
    )

    hv_text = (
        open(hv_path, "r", encoding="utf-8").read()
        if os.path.exists(hv_path)
        else hypothesis_eval
    )
    prompt = f"""
    당신은 의학 연구 보조원입니다.
    데이터, 가설, 문헌 분석 보고서를 참고하여 분석 코드를 작성하세요.

    연구 가설: {hypothesis}
    분석법: {method}
    가설 검증 결과 요약: {hypothesis_eval}
    데이터 요약: {summary['columns']}

    규칙:
    - 반드시 summary['columns']에 포함된 실제 컬럼명만 사용해야 합니다.
    - summary에 없는 변수를 outcome 또는 predictor로 임의 생성하지 마십시오.
    - summary에 포함된 변수로 계산 가능한 파생 변수만 허용됩니다.
    - hypothesis_validation.md 분석 결과를 반영해야 합니다.
    - consolidated_report.md 분석 결과를 반영해야 합니다.
    - 반드시 실제 CSV 경로({csv_path})에서 데이터를 불러오십시오.

    문헌 분석 보고서:
    {report_summary[:40000]}

    가설 검증(최종):
    <<HYPOTHESIS_VALIDATION_START>>
    {hv_text}
    <<HYPOTHESIS_VALIDATION_END>>


    요청:
    1. 가설 검증에 유의한 컬럼 후보를 알려주세요. {summary['columns']}에 존재하는 칼럼만으로 제시해야 합니다.
    2. 해당 컬럼을 기반으로 {method}을(를) 수행할 수 있는 Python 코드 예제를 작성하세요.
    3. python 코드 작성 시 칼럼은 반드시 {summary['columns']} 실제 칼럼명을 그대로 사용해야 합니다.
    4. 코드에는 반드시 CSV를 불러오고, 유의미한 컬럼만 추출하여 분석하는 부분을 포함하세요.
    5. 그래프 혹은 도표를 plt로 띄우지 마세요.
    """
    return prompt, gpt_request_code(prompt)


# 코드 저장 및 실행
def save_and_run_code(
    feedback,
    prompt,
    output_file="final_analysis.py",
    df=None,
    summary=None,
    input_type="CSV",
):
    ensure_result_dir()
    code_match = re.search(r"```python(.*?)```", feedback, re.DOTALL)
    final_code = code_match.group(1).strip() if code_match else feedback
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_code)
    print(f"코드가 {output_file} 파일로 저장되었습니다.")

    if input_type == "CSV" and df is not None and summary is not None:
        used_cols = [col for col in summary["columns"] if col in final_code]
        if used_cols:
            used_df = df[used_cols].copy()
            used_csv_path = os.path.join(RESULT_DIR, "used_data.csv")
            used_json_path = os.path.join(RESULT_DIR, "used_data.json")
            used_df.to_csv(used_csv_path, index=False, encoding="utf-8-sig")
            used_df.to_json(
                used_json_path, orient="records", force_ascii=False, indent=2
            )
            print(
                f"실제 분석에 사용된 데이터 저장 완료 → {used_csv_path}, {used_json_path}"
            )

    max_retries = 5
    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                ["python", output_file], capture_output=True, text=True, check=True
            )
            print("\n=== 실행 결과 ===\n")
            print(result.stdout)

            with open(
                os.path.join(RESULT_DIR, "execution_output.md"), "w", encoding="utf-8"
            ) as f:
                f.write(result.stdout)
            with open(
                os.path.join(RESULT_DIR, "execution_output.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(
                    {"stdout": result.stdout, "stderr": result.stderr},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            # 실행된 코드에서 DataFrame을 찾아 derived_data.csv로 저장
            try:
                namespace = runpy.run_path(output_file)
                for candidate in ["data", "data_filtered", "data_for_analysis", "df"]:
                    if candidate in namespace and hasattr(
                        namespace[candidate], "to_csv"
                    ):
                        derived_df = namespace[candidate]
                        derived_path = os.path.join(RESULT_DIR, "derived_data.csv")
                        derived_df.to_csv(
                            derived_path, index=False, encoding="utf-8-sig"
                        )
                        print(f"파생 데이터 저장 완료 → {derived_path}")
                        break
                else:
                    print("final_analysis.py 안에서 DataFrame 변수를 찾지 못했습니다.")
            except Exception as e:
                print(f"파생 데이터 저장 실패: {e}")

            return result.stdout

        except subprocess.CalledProcessError as e:
            print(f"\n실행 오류 발생 (시도 {attempt+1}/{max_retries})\n")

            with open(output_file, "r", encoding="utf-8") as f:
                current_code = f.read()

            error_prompt = f"""
            당신은 Python 디버깅 전문가입니다.
            아래는 실행된 코드와 에러 로그입니다.

            [입력 데이터 타입]
            {input_type}

            [실행된 코드]
            ```python
            {current_code[:10000]}
            ```

            [에러 로그]
            {e.stderr[:10000]}

            요청:
            1. 오류 원인을 데이터 타입 특성까지 고려하여 설명하세요.
            2. 이 오류를 해결할 수 있는 코드를 다시 작성하세요.
            3. 동일한 문제가 반복되지 않도록 사전 처리(예: 변수명 rename, dtype 변환 등)를 포함하세요.
            """
            feedback = gpt_request_code(error_prompt)

            code_match = re.search(r"```python(.*?)```", feedback, re.DOTALL)
            final_code = code_match.group(1).strip() if code_match else feedback
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(final_code)

            print("GPT가 수정한 코드로 다시 실행합니다.")

    print("최대 재시도 횟수를 초과했습니다.")
    return None


# 결과 해석
def gpt_analysis_results(analysis_output):
    prompt = f"""
    당신은 의학 연구 보조원입니다.
    아래는 Python 분석 코드 실행 결과입니다.
    연구자가 이해할 수 있도록 결과를 자세히 해설하세요.

    분석 결과:
    {analysis_output[:5000]}
    
    요청:
    1. 주요 지표(계수, p-value, R-squared, confusion matrix 등)를 해석해 설명하세요.
    2. 어떤 변수들이 유의미했는지 구체적으로 알려주세요.
    3. 결과가 의미하는 바를 "결과(Results)" 형식으로 서술하세요.
    """
    return gpt_request(prompt)


# 고찰
def gpt_discussion(hypothesis, analysis_summary):
    report_path = os.path.join("agent_sub_result", "consolidated_report.md")
    report_summary = (
        open(report_path, "r", encoding="utf-8").read()
        if os.path.exists(report_path)
        else ""
    )

    prompt = f"""
    당신은 의학 연구 보조원입니다.
    아래는 연구 가설과 분석 결과 요약입니다.

    연구 가설: {hypothesis}
    문헌 분석 보고서:
    {report_summary[:40000]}
    분석 요약: {analysis_summary}

    요청:
    - 가설과 비교했을 때 일치/불일치 여부를 논하시오.
    - 결과의 의미를 심도 있게 해석하세요.
    - 연구적 시사점과 임상적 의미를 기술하세요.
    - 한계점과 추가 분석 제안도 포함하세요.
    - "문헌 분석 보고서를 참고하여 기존 선행연구 내용과 비교하는 고찰(Discussion)" 형식으로 작성하세요.
    """
    return gpt_request(prompt)


# Main
if __name__ == "__main__":
    hypothesis = input("가설을 입력하세요: ")
    method = input("원하는 분석법을 입력하세요 : ")

    root = tk.Tk()
    root.lift()
    root.attributes("-topmost", True)
    csv_path = filedialog.askopenfilename(
        title="CSV 파일을 선택하세요", filetypes=[("CSV files", "*.csv")]
    )
    root.destroy()

    if not csv_path:
        print("CSV 파일을 선택하지 않았습니다.")
        exit()
    print(f"선택된 CSV: {csv_path}")

    print("\n입력된 데이터를 분석 중입니다...\n")
    df, summary = analyze_csv(csv_path)
    if df is None:
        print(summary)
        exit()

    print("\n가설을 검증 중입니다. 잠시만 기다려주세요...\n")
    hypothesis_eval = validate_hypothesis(hypothesis, method, summary, csv_path)
    print("\n=== 가설 검증 결과 ===\n")
    print(hypothesis_eval)
    save_hypothesis_eval(hypothesis_eval)

    while True:
        choice = input("취소/실행/수정 입력 : ").strip()
        if choice == "취소":
            print("프로그램 종료")
            exit()
        elif choice == "실행":
            save_hypothesis_eval(hypothesis_eval)
            break
        elif choice == "수정":
            fb = input("가설 검증 피드백 입력: ")
            # 선택: 피드백 로그 저장
            with open(os.path.join(RESULT_DIR, "last_feedback.txt"), "w", encoding="utf-8") as f:
                f.write(fb)
            hypothesis_eval = validate_hypothesis(hypothesis, method, summary, csv_path, feedback=fb)
            print("\n[수정된 가설 검증 결과]\n", hypothesis_eval)
            save_hypothesis_eval(hypothesis_eval)
        else:
            print("잘못된 입력입니다. (취소/실행/수정 중 선택하세요.)")

    prompt, feedback = generate_analysis_code(
        hypothesis, method, summary, csv_path, hypothesis_eval
    )
    print("\n=== GPT 분석 코드 제안 ===\n")
    print(feedback)

    while True:
        user_input = input("취소/실행/수정 입력 : ").strip()

        if user_input == "취소":
            print("프로그램 종료")
            break

        elif user_input == "실행":
            result_out = save_and_run_code(feedback, prompt, df=df, summary=summary)
            if result_out:
                while True:
                    next_action = input("해석/피드백/종료 입력 : ").strip()

                    if next_action == "해석":
                        analysis_summary = gpt_analysis_results(result_out)
                        print("\n=== 결과 (Results) ===\n", analysis_summary)

                        discussion = gpt_discussion(hypothesis, analysis_summary)
                        print("\n=== 고찰 (Discussion) ===\n", discussion)

                        with open(
                            os.path.join(RESULT_DIR, "final_analysis.md"),
                            "w",
                            encoding="utf-8",
                        ) as f:
                            f.write(
                                "## 가설 검증(최종)\n"
                                + hypothesis_eval
                                + "\n\n## Results\n"
                                + analysis_summary
                                + "\n\n## Discussion\n"
                                + discussion
                            )

                        with open(
                            os.path.join(RESULT_DIR, "final_analysis.json"),
                            "w",
                            encoding="utf-8",
                        ) as f:
                            json.dump(
                                {
                                    "HypothesisValidationFinal": hypothesis_eval,  # 가설 검증(최종)
                                    "Results": analysis_summary,
                                    "Discussion": discussion,
                                },
                                f,
                                ensure_ascii=False,
                                indent=2,
                            )

                        while True:
                            fb_action = input("해석피드백/코드피드백/종료 : ").strip()
                            if fb_action == "해석피드백":
                                fb = input("피드백 입력: ")
                                revised = gpt_request(
                                    f"이전 Results:\n{analysis_summary}\n\nDiscussion:\n{discussion}\n\n피드백: {fb}\n→ 반영하여 다시 작성"
                                )
                                print("\n[수정된 해석/고찰]\n", revised)
                            elif fb_action == "코드피드백":
                                fb = input("코드 피드백 입력: ")
                                prompt += f"\n코드 피드백: {fb}\n→ 반영하여 코드 재작성"
                                feedback = gpt_request_code(prompt)
                                print("\n[수정된 코드 제안]\n", feedback)
                                break
                            elif fb_action == "종료":
                                print("프로그램 종료")
                                exit()

                    elif next_action == "피드백":
                        fb = input("코드 피드백 입력: ")
                        prompt += f"\n코드 피드백: {fb}\n→ 반영하여 코드 재작성"
                        feedback = gpt_request_code(prompt)
                        print("\n[수정된 코드 제안]\n", feedback)
                        break

                    elif next_action == "종료":
                        print("프로그램 종료")
                        exit()

        elif user_input == "수정":
            fb = input("코드 피드백 입력: ")
            prompt += f"\n코드 피드백: {fb}\n→ 반영하여 코드 재작성"
            feedback = gpt_request_code(prompt)
            print("\n[수정된 코드 제안]\n", feedback)

        else:
            print("잘못된 입력 (취소/실행/수정 중 선택)")
