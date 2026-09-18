"""Apply an OCI Resource Manager stack once and notify on success.

Schedule this script from Windows Task Scheduler (for example, 08:00, 13:00,
and 22:00). The script intentionally performs one attempt per invocation;
it does not create a tight retry loop.
"""

from __future__ import annotations

import json
import ssl
import uuid
from contextlib import contextmanager
import os
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SUCCESS_MARKER = ROOT / "success.marker"
STOP_MARKER = ROOT / "stop.marker"
STATE_FILE = ROOT / "retry-state.json"
POLL_SECONDS = 30
POLL_TIMEOUT_SECONDS = 1800
CLI_TIMEOUT_SECONDS = 120
ACTIVE_STATES = {"ACCEPTED", "IN_PROGRESS", "CANCELING"}


def load_env_file() -> None:
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません。")
    return value


def run_oci(*args: str) -> str:
    command = ["oci", *args, "--output", "json"]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"OCI CLI失敗: {' '.join(command)}\n{detail}")
    return result.stdout


def latest_active_job(stack_id: str) -> tuple[str, str] | None:
    output = run_oci(
        "resource-manager",
        "job",
        "list",
        "--stack-id",
        stack_id,
        "--all",
        "--sort-by",
        "TIMECREATED",
        "--sort-order",
        "DESC",
    )
    jobs = json.loads(output).get("data", [])
    for job in jobs:
        state = job.get("lifecycle-state", "")
        if state in ACTIVE_STATES:
            return job["id"], state
    return None


def create_apply_job(stack_id: str, display_name: str) -> str:
    output = run_oci(
        "resource-manager",
        "job",
        "create-apply-job",
        "--stack-id",
        stack_id,
        "--display-name",
        display_name,
        "--execution-plan-strategy",
        "AUTO_APPROVED",
        "--no-retry",
    )
    return json.loads(output)["data"]["id"]


def wait_for_job(job_id: str) -> str:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        output = run_oci("resource-manager", "job", "get", "--job-id", job_id)
        state = json.loads(output)["data"]["lifecycle-state"]
        print(f"job={job_id} state={state}")
        if state in {"SUCCEEDED", "FAILED", "CANCELED"}:
            return state
        if STOP_MARKER.exists():
            raise RuntimeError("停止要求を検出しました。OCI上のジョブは継続します。")
        if state not in ACTIVE_STATES:
            raise RuntimeError(f"未対応のジョブ状態: {state}")
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"ジョブが{POLL_TIMEOUT_SECONDS}秒以内に完了しませんでした。")


def get_job_log_text(job_id: str) -> str:
    output = run_oci(
        "resource-manager",
        "job",
        "get-job-logs",
        "--job-id",
        job_id,
        "--all",
    )
    entries = json.loads(output).get("data", [])
    return "\n".join(str(entry.get("message", "")) for entry in entries)


def send_success_mail(job_id: str) -> None:
    message = EmailMessage()
    message["Subject"] = os.getenv("MAIL_SUBJECT", "OCIインスタンス作成成功")
    message["From"] = required("MAIL_FROM")
    message["To"] = required("MAIL_TO")
    message.set_content(
        "OCI Resource Managerの適用ジョブが成功しました。\n"
        f"Job ID: {job_id}\n"
        f"UTC: {datetime.now(timezone.utc).isoformat()}\n"
        "OCIコンソールでインスタンスのパブリックIPを確認してください。\n"
    )

    host = required("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = required("SMTP_USER")
    password = required("SMTP_PASSWORD")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(username, password)
        smtp.send_message(message)


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def save_state(state: dict) -> None:
    atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


@contextmanager
def single_instance():
    # OS releases the byte lock even when the process is killed. Never unlink it.
    handle = (ROOT / "retry.lock").open("a+b")
    acquired = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        handle.close()


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} は正の整数にしてください。")
    return value


def notify_success(state: dict) -> int:
    # Record completion BEFORE contacting SMTP: never apply again on mail failure.
    atomic_write(SUCCESS_MARKER, f"job_id={state['job_id']}\n")
    if not state.get("notified"):
        send_success_mail(state["job_id"])
        state["notified"] = True
        save_state(state)
    print("適用成功。通知済みのため以後の適用は停止します。")
    return 0


def run_once() -> int:
    if STOP_MARKER.exists():
        print("stop.marker があるため停止します。")
        return 0
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if SUCCESS_MARKER.exists() and not state:
        print("既存の success.marker があるため停止します。")
        return 0
    stack_id = required("OCI_STACK_ID")
    if state and state.get("stack_id") != stack_id:
        raise RuntimeError("保存状態のスタックと OCI_STACK_ID が異なります。設定を確認してください。")
    if state.get("succeeded"):
        return notify_success(state)
    if SUCCESS_MARKER.exists():
        print("success.marker があるため停止します。")
        return 0
    # Validate notification configuration before starting a cloud mutation.
    for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO"):
        required(name)
    positive_int("SMTP_PORT", 587)

    if state.get("creating"):
        jobs = json.loads(run_oci("resource-manager", "job", "list",
                                 "--stack-id", stack_id, "--all"))["data"]
        matches = [j for j in jobs if j.get("display-name") == state["display_name"]
                   and j.get("operation") == "APPLY"]
        if len(matches) != 1:
            raise RuntimeError("作成要求の結果が未確定です。自動再作成は行いません。"
                               "OCIコンソールと retry-state.json を確認してください。")
        state["job_id"] = matches[0]["id"]
        state["creating"] = False
        save_state(state)

    if not state.get("job_id"):
        active = latest_active_job(stack_id)
        if active:
            print(f"別の既存ジョブが実行中のためスキップ: {active[0]}")
            return 0
        state = {"stack_id": stack_id, "creating": True,
                 "display_name": os.getenv("OCI_JOB_NAME", "nora-bot-capacity-retry")
                 + "-" + uuid.uuid4().hex}
        save_state(state)
        state["job_id"] = create_apply_job(stack_id, state["display_name"])
        state["creating"] = False
        save_state(state)

    job_id = state["job_id"]
    result = wait_for_job(job_id)
    if result == "SUCCEEDED":
        state["succeeded"] = True
        save_state(state)
        return notify_success(state)

    log_text = get_job_log_text(job_id)
    atomic_write(ROOT / "last-job.log", f"job_id={job_id}\nstate={result}\n{log_text}\n")
    if result == "FAILED" and "out of host capacity" in log_text.lower():
        save_state({"stack_id": stack_id})
        print("ホスト容量不足。次回のスケジュール実行で再試行します。")
        return 2
    atomic_write(STOP_MARKER, f"job_id={job_id}\nstate={result}\nlast-job.log を確認してください。\n")
    print("容量不足以外の失敗のため停止します。", file=sys.stderr)
    return 3


def main() -> int:
    global POLL_SECONDS, POLL_TIMEOUT_SECONDS, CLI_TIMEOUT_SECONDS
    try:
        load_env_file()
        POLL_SECONDS = positive_int("OCI_POLL_SECONDS", 30)
        POLL_TIMEOUT_SECONDS = positive_int("OCI_POLL_TIMEOUT_SECONDS", 1800)
        CLI_TIMEOUT_SECONDS = positive_int("OCI_CLI_TIMEOUT_SECONDS", 120)
        with single_instance() as acquired:
            if not acquired:
                print("別プロセスが実行中のためスキップします。")
                return 0
            return run_once()
    except Exception as exc:
        print(f"{datetime.now(timezone.utc).isoformat()} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
