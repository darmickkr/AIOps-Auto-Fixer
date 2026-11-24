import json
import os
import subprocess
import sys
import time
from pathlib import Path

from google import genai

MODEL = "gemini-4-hydrogen"
DOCKERFILE = Path("gamezone/Dockerfile")
STATE_FILE = Path("ci-result.json")
REPORT_FILE = Path("ci-report.md")
LOG_FILE = Path("ci-failure.log")

IMAGE_NAME = "gamezone-app:ci"
CONTAINER_NAME = "gamezone-ci"

MAX_REPAIR_ATTEMPTS = 3

REPAIRABLE_STAGES = {"docker_build", "deploy", "smoke"}

STAGES = {
    "build": [
        "bash", "./mvnw", "-B", "-DskipTests", "package"
    ],
    "test": [
        "bash", "./mvnw", "-B", "test"
    ],
    "docker_build": [
        "docker", "build", "--progress=plain",
        "-t", IMAGE_NAME, "."
    ],
    "deploy": [
        "docker", "run", "-d", "--name", CONTAINER_NAME,
        "-p", "8090:8090", IMAGE_NAME
    ],
}


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))

    return {
        "success": False,
        "repair_attempts": 0,
        "repair_applied": False,
        "failed_stage": None,
        "root_cause": "",
        "stage_statuses": {}
    }


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8"
    )


def write_report(state, details=""):
    lines = [
        "# AIOps CI/CD Execution Report",
        "",
        f"Pipeline result: {'SUCCESS' if state['success'] else 'IN PROGRESS / FAILED'}",
        f"AI repair attempts: {state['repair_attempts']}/{MAX_REPAIR_ATTEMPTS}",
        f"Correction applied: {state['repair_applied']}",
        "",
        "## Stage results"
    ]

    for stage, status in state["stage_statuses"].items():
        lines.append(f"- {stage}: {status}")

    lines.extend([
        "",
        f"## Failed stage: {state['failed_stage'] or 'None'}",
        "",
        f"## Root cause: {state['root_cause'] or 'None'}",
        "",
        details
    ])

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


def run_command(stage, command, timeout=1200):
    print(f"\n========== {stage.upper()} ==========")

    try:
        result = subprocess.run(
            command,
            cwd="gamezone" if stage in {"build", "test"} else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout
        )

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        print(output[-12000:])

        return result.returncode == 0, output

    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""

        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")

        return False, stdout + "\n" + stderr + "\nCommand timed out."

    except Exception as exc:
        return False, str(exc)


def run_smoke_test():
    print("\n========== SMOKE TEST ==========")

    logs = []

    for attempt in range(1, 31):
        result = subprocess.run(
            [
                "curl", "-sS", "-o", "/dev/null",
                "-w", "%{http_code}",
                "http://127.0.0.1:8090/"
            ],
            capture_output=True,
            text=True
        )

        status = result.stdout.strip()

        if result.returncode == 0 and status.startswith("2"):
            message = f"HTTP smoke test passed: {status}"
            print(message)
            return True, message

        logs.append(f"Attempt {attempt}: HTTP {status}")
        time.sleep(2)

    container_logs = subprocess.run(
        ["docker", "logs", CONTAINER_NAME],
        capture_output=True,
        text=True,
        errors="replace"
    )

    logs.append(container_logs.stdout or "")
    logs.append(container_logs.stderr or "")

    return False, "\n".join(logs)


def execute_stage(stage):
    if stage in STAGES:
        if stage == "deploy":
            subprocess.run(
                ["docker", "rm", "-f", CONTAINER_NAME],
                capture_output=True,
                text=True
            )

        return run_command(stage, STAGES[stage])

    if stage == "smoke":
        return run_smoke_test()

    raise ValueError(f"Unknown stage: {stage}")


def ask_gemini(client, stage, error_log):
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    prompt = f"""
You are a DevOps CI/CD troubleshooting agent.

Failed stage: {stage}

You may modify ONLY gamezone/Dockerfile.

Never modify:
- Java source code
- Application business logic
- Tests
- pom.xml
- GitHub workflow files
- Other repository files

Only propose a repair when the actual failure is demonstrably
caused by the Dockerfile.

Do not bypass tests, hide errors, or change application behaviour.
Preserve port 8090.

Current Dockerfile:
{dockerfile}

Failure logs:
{error_log[-25000:]}

Return only valid JSON.

Repair:
{{
  "action": "repair",
  "root_cause": "Explanation",
  "corrected_dockerfile": "Complete Dockerfile"
}}

No safe repair:
{{
  "action": "report_only",
  "root_cause": "Explanation",
  "reason": "Why repair is inappropriate"
}}
"""

    for attempt in range(1, 4):
        try:
            print(f"Contacting Gemini: request {attempt}/3")

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt
            )

            if not response.text:
                raise ValueError("Empty Gemini response")

            answer = response.text.strip()

            if answer.startswith("```"):
                lines = answer.splitlines()
                answer = "\n".join(lines[1:-1])

            result = json.loads(answer)

            if not isinstance(result, dict):
                raise ValueError("Invalid Gemini response")

            return result

        except Exception as exc:
            print(f"Gemini request error: {exc}")

            if attempt < 3:
                time.sleep(attempt * 10)

    raise RuntimeError("Gemini unavailable after three requests")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "build", "test", "docker_build", "deploy", "smoke"
    }:
        raise ValueError(
            "Usage: python stage_runner.py "
            "build|test|docker_build|deploy|smoke"
        )

    stage = sys.argv[1]
    state = load_state()

    if not DOCKERFILE.is_file():
        raise FileNotFoundError("gamezone/Dockerfile not found")

    # Run the requested stage first.
    success, logs = execute_stage(stage)

    if success:
        state["stage_statuses"][stage] = "PASSED"
        save_state(state)

        if stage == "smoke":
            state["success"] = True

        save_state(state)
        write_report(state)

        return 0

    print(f"\nSTAGE FAILED: {stage}")

    LOG_FILE.write_text(
        f"FAILED STAGE: {stage}\n\n{logs}",
        encoding="utf-8"
    )

    state["stage_statuses"][stage] = "FAILED"
    state["failed_stage"] = stage
    state["success"] = False

    # Build and test failures are diagnosed but not automatically
    # repaired because application/build failures are outside the
    # currently permitted Dockerfile repair scope.
    if stage not in REPAIRABLE_STAGES:
        state["root_cause"] = (
            "Failure requires investigation outside the permitted "
            "Dockerfile repair scope."
        )
        save_state(state)
        write_report(state, logs)
        return 1

    if state["repair_attempts"] >= MAX_REPAIR_ATTEMPTS:
        state["root_cause"] = "Maximum repair attempts reached."
        save_state(state)
        write_report(state, logs)
        return 1

    if not os.environ.get("GEMINI_API_KEY"):
        state["root_cause"] = "GEMINI_API_KEY is unavailable."
        save_state(state)
        write_report(state, logs)
        return 1

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    while state["repair_attempts"] < MAX_REPAIR_ATTEMPTS:
        try:
            result = ask_gemini(client, stage, logs)
        except Exception as exc:
            state["root_cause"] = str(exc)
            save_state(state)
            write_report(state, logs)
            return 1

        state["root_cause"] = result.get(
            "root_cause", "Not identified"
        )

        if result.get("action") != "repair":
            state["root_cause"] += "\n" + result.get(
                "reason", "AI recommended reporting only."
            )
            save_state(state)
            write_report(state, logs)
            return 1

        corrected = result.get("corrected_dockerfile")

        if not isinstance(corrected, str) or not corrected.strip():
            state["root_cause"] = "Invalid Dockerfile returned by Gemini."
            save_state(state)
            write_report(state, logs)
            return 1

        current = DOCKERFILE.read_text(encoding="utf-8")

        if corrected.strip() == current.strip():
            state["root_cause"] = "Gemini returned an unchanged Dockerfile."
            save_state(state)
            write_report(state, logs)
            return 1

        DOCKERFILE.write_text(
            corrected.rstrip() + "\n",
            encoding="utf-8"
        )

        state["repair_attempts"] += 1
        state["repair_applied"] = True

        print(
            f"\nAI repair {state['repair_attempts']}/"
            f"{MAX_REPAIR_ATTEMPTS} applied."
        )

        # Retry ONLY the failed stage.
        success, logs = execute_stage(stage)

        if success:
            state["stage_statuses"][stage] = "PASSED AFTER AI REPAIR"
            state["failed_stage"] = None
            save_state(state)
            write_report(state)
            return 0

        LOG_FILE.write_text(
            f"FAILED STAGE: {stage}\n\n{logs}",
            encoding="utf-8"
        )

    state["stage_statuses"][stage] = "FAILED AFTER REPAIRS"
    save_state(state)
    write_report(state, logs)

    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"AIOPS STAGE AGENT ERROR: {exc}")
        sys.exit(1)
