import json
import os
import subprocess
import sys
import time
from pathlib import Path

from google import genai

MODEL = "gemini-4-hydrogen"
GAMEZONE_DIR = Path("gamezone")
DOCKERFILE = GAMEZONE_DIR / "Dockerfile"
IMAGE_NAME = "gamezone-app:ci"
CONTAINER_NAME = "gamezone-ci"

MAX_REPAIR_ATTEMPTS = 3
REPORT_FILE = Path("ci-report.md")
RESULT_FILE = Path("ci-result.json")
LOG_FILE = Path("ci-failure.log")


def run_command(stage, command, cwd=None, timeout=1200):
    print(f"\n========== {stage} ==========")

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout
        )

        output = (result.stdout or "") + "\n" + (result.stderr or "")

        print(output[-12000:])

        return result.returncode == 0, output

    except subprocess.TimeoutExpired as exc:
        output = (
            (exc.stdout or "")
            + "\n"
            + (exc.stderr or "")
            + "\nCommand timed out."
        )
        return False, output

    except Exception as exc:
        return False, str(exc)


def run_pipeline():
    all_logs = []

    # Stage 1: Maven build and tests
    success, output = run_command(
        "MAVEN BUILD AND TEST",
        ["bash", "./mvnw", "-B", "clean", "verify"],
        cwd=GAMEZONE_DIR
    )

    all_logs.append("MAVEN BUILD AND TEST\n" + output)

    if not success:
        return False, "Maven build/test", "\n".join(all_logs)

    # Stage 2: Docker image build
    success, output = run_command(
        "DOCKER IMAGE BUILD",
        [
            "docker",
            "build",
            "--progress=plain",
            "-t",
            IMAGE_NAME,
            "."
        ],
        cwd=GAMEZONE_DIR
    )

    all_logs.append("DOCKER IMAGE BUILD\n" + output)

    if not success:
        return False, "Docker image build", "\n".join(all_logs)

    # Remove any previous test container
    subprocess.run(
        ["docker", "rm", "-f", CONTAINER_NAME],
        capture_output=True,
        text=True
    )

    # Stage 3: Start application container
    success, output = run_command(
        "CONTAINER START",
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "-p",
            "8090:8090",
            IMAGE_NAME
        ],
        timeout=120
    )

    all_logs.append("CONTAINER START\n" + output)

    if not success:
        return False, "Container start", "\n".join(all_logs)

    # Stage 4: HTTP smoke test
    print("\n========== APPLICATION HTTP TEST ==========")

    for attempt in range(1, 31):
        response = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null",
             "-w", "%{http_code}", "http://127.0.0.1:8090/"],
            capture_output=True,
            text=True
        )

        status_code = response.stdout.strip()

        if response.returncode == 0 and status_code.startswith("2"):
            output = f"Application responded successfully. HTTP {status_code}"
            print(output)
            all_logs.append(output)
            return True, "Complete pipeline", "\n".join(all_logs)

        print(f"Waiting for application... ({attempt}/30)")
        time.sleep(2)

    logs = subprocess.run(
        ["docker", "logs", CONTAINER_NAME],
        capture_output=True,
        text=True,
        errors="replace"
    )

    output = (
        "Application did not return a successful HTTP response.\n"
        + (logs.stdout or "")
        + "\n"
        + (logs.stderr or "")
    )

    all_logs.append("APPLICATION HTTP TEST\n" + output)

    return False, "Container HTTP test", "\n".join(all_logs)


def ask_gemini(client, stage, dockerfile, error_log):
    prompt = f"""
You are a DevOps CI/CD troubleshooting agent.

The GitHub Actions pipeline failed.

FAILED STAGE:
{stage}

YOUR PERMITTED SCOPE:
You may modify ONLY gamezone/Dockerfile.

You must NOT modify:
- Java application source code
- Maven pom.xml
- Application business logic
- GitHub workflow files
- Any other repository file

Rules:
1. Analyse the actual failure and its root cause.
2. Only propose a Dockerfile correction if the failure is genuinely
   caused by Dockerfile configuration.
3. Maven compilation or application test failures must be reported
   without attempting to change application code.
4. Container startup or HTTP failures may be corrected only when
   the Dockerfile is demonstrably responsible.
5. Preserve the application's purpose and port 8090.
6. Do not hide errors, skip tests, or bypass failed stages.
7. Do not invent unrelated changes.
8. If a safe Dockerfile correction is not possible, use report_only.
9. Return the complete Dockerfile when proposing a correction.

CURRENT gamezone/Dockerfile:
{dockerfile}

PIPELINE FAILURE LOG:
{error_log[-25000:]}

Return ONLY valid JSON.

For a safe Dockerfile correction:
{{
  "action": "repair",
  "root_cause": "Explanation",
  "corrected_dockerfile": "Complete corrected Dockerfile"
}}

For failures outside the permitted scope:
{{
  "action": "report_only",
  "root_cause": "Explanation",
  "reason": "Why automatic correction is not appropriate"
}}
"""

    for attempt in range(1, 4):
        try:
            print(f"\nContacting Gemini ({MODEL}), request {attempt}/3...")

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt
            )

            if not response.text:
                raise ValueError("Gemini returned an empty response.")

            text = response.text.strip()

            if text.startswith("```"):
                lines = text.splitlines()
                if lines[-1].strip() == "```":
                    text = "\n".join(lines[1:-1])

            result = json.loads(text)

            if not isinstance(result, dict):
                raise ValueError("Invalid Gemini JSON response.")

            return result

        except Exception as exc:
            print(f"Gemini request failed: {exc}")

            if attempt < 3:
                time.sleep(attempt * 10)

    raise RuntimeError("Gemini unavailable after 3 request attempts.")


def write_report(success, stage, root_cause, attempts, repaired, reason):
    status = "SUCCESS" if success else "FAILED"

    report = f"""# AIOps CI/CD Execution Report

## Result
{status}

## Failed stage
{stage}

## Root cause
{root_cause}

## AI repair attempts
{attempts}/{MAX_REPAIR_ATTEMPTS}

## Correction applied
{"Yes" if repaired else "No"}

## Details
{reason}
"""

    REPORT_FILE.write_text(report, encoding="utf-8")

    RESULT_FILE.write_text(
        json.dumps(
            {
                "success": success,
                "failed_stage": stage,
                "repair_attempts": attempts,
                "repair_applied": repaired,
                "root_cause": root_cause
            },
            indent=2
        ),
        encoding="utf-8"
    )


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    if not DOCKERFILE.is_file():
        raise FileNotFoundError(
            "gamezone/Dockerfile was not found."
        )

    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"]
    )

    repair_attempts = 0
    repaired = False
    root_cause = "No failure detected."
    reason = ""
    last_stage = "Pipeline"

    for cycle in range(MAX_REPAIR_ATTEMPTS + 1):
        success, stage, error_log = run_pipeline()

        if success:
            print("\nPIPELINE SUCCESS")

            write_report(
                True,
                "Complete pipeline",
                root_cause,
                repair_attempts,
                repaired,
                "Maven build, tests, Docker build and container HTTP test passed."
            )

            return 0

        last_stage = stage
        LOG_FILE.write_text(error_log, encoding="utf-8")

        print(f"\nPIPELINE FAILURE: {stage}")

        if cycle == MAX_REPAIR_ATTEMPTS:
            reason = "Maximum AI repair attempts reached."
            break

        current_dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        try:
            result = ask_gemini(
                client,
                stage,
                current_dockerfile,
                error_log
            )

        except Exception as exc:
            reason = f"Gemini diagnosis failed: {exc}"
            break

        root_cause = result.get("root_cause", "Not identified.")

        if result.get("action") != "repair":
            reason = result.get(
                "reason",
                "AI determined that automatic correction is not appropriate."
            )
            break

        corrected = result.get("corrected_dockerfile")

        if not isinstance(corrected, str) or not corrected.strip():
            reason = "Gemini returned an invalid Dockerfile."
            break

        if corrected.strip() == current_dockerfile.strip():
            reason = "Gemini returned an unchanged Dockerfile."
            break

        DOCKERFILE.write_text(
            corrected.rstrip() + "\n",
            encoding="utf-8"
        )

        repair_attempts += 1
        repaired = True

        print(
            f"\nAI correction applied. "
            f"Repair attempt {repair_attempts}/{MAX_REPAIR_ATTEMPTS}."
        )

    print("\nPIPELINE FAILED")

    write_report(
        False,
        last_stage,
        root_cause,
        repair_attempts,
        repaired,
        reason or "Pipeline could not be recovered."
    )

    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nAIOPS AGENT ERROR: {exc}")

        write_report(
            False,
            "Agent execution",
            str(exc),
            0,
            False,
            "Agent could not complete execution."
        )

        sys.exit(1)
