#!/usr/bin/env python3
"""Python client for the authenticated grading service."""

import csv
import http.client
import json
import os
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from value_codec import encode_value


SERVER_URL = os.environ.get("GRADE_SERVER_URL", "http://127.0.0.1:5050").rstrip("/")
TOKEN = os.environ.get("GRADE_TOKEN", "")
USER_AGENT = "GradeClient/1.0"


def _create_connection_ipv4_first(
    address,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
    **_kwargs,
):
    """Create a socket while avoiding long IPv6 timeouts when IPv4 is available."""
    host, port = address
    addresses = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    addresses.sort(key=lambda result: result[0] != socket.AF_INET)
    last_error = None

    for family, socktype, protocol, _, socket_address in addresses:
        connection = None
        try:
            connection = socket.socket(family, socktype, protocol)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(socket_address)
            return connection
        except OSError as error:
            last_error = error
            if connection is not None:
                connection.close()

    if last_error is not None:
        raise last_error
    raise OSError("getaddrinfo returned no addresses")


class _IPv4FirstHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection_ipv4_first


class _IPv4FirstHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection_ipv4_first


class _IPv4FirstHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_IPv4FirstHTTPConnection, request)


class _IPv4FirstHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(
            _IPv4FirstHTTPSConnection,
            request,
            context=self._context,
        )


_OPENER = urllib.request.build_opener(
    _IPv4FirstHTTPHandler,
    _IPv4FirstHTTPSHandler,
)


class GradeAPIError(ConnectionError):
    """HTTP error returned by the grading API."""

    def __init__(self, status: int, url: str, payload: dict):
        self.status = status
        self.payload = payload
        super().__init__(f"HTTP {status} from {url}: {payload.get('error', '')}")


def configure(server_url: str | None = None, token: str | None = None) -> None:
    """Configure the service URL and personal token for this process."""
    global SERVER_URL, TOKEN
    if server_url is not None:
        if not server_url.startswith(("http://", "https://")):
            raise ValueError("server_url must start with http:// or https://")
        SERVER_URL = server_url.rstrip("/")
    if token is not None:
        TOKEN = token


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    authenticated: bool = True,
) -> dict:
    url = f"{SERVER_URL}{path}"
    payload = json.dumps(encode_value(body)).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if authenticated:
        if not TOKEN:
            raise ConnectionError(
                "No grading token configured. Set GRADE_TOKEN or call configure(token=...)."
            )
        request.add_header("Authorization", f"Bearer {TOKEN}")

    try:
        with _OPENER.open(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            raw = error.read().decode("utf-8", errors="replace")
        finally:
            error.close()
        try:
            parsed = json.loads(raw)
            payload = parsed if isinstance(parsed, dict) else {"error": raw}
        except json.JSONDecodeError:
            payload = {"error": raw}
        raise GradeAPIError(error.code, url, payload) from error
    except urllib.error.URLError as error:
        raise ConnectionError(f"Cannot connect to {url}: {error.reason}") from error


def define_challenges(challenge_id: str, exercises: dict) -> dict:
    """Create a challenge. The configured token must belong to an admin."""
    result = _request("POST", "/challenges/register", {
        "challenge_id": challenge_id,
        "exercises": exercises,
    })
    print(result["message"])
    return result


def edit_challenge(challenge_id: str, exercises: dict) -> dict:
    """Replace a challenge definition while preserving unchanged completions."""
    encoded_id = urllib.parse.quote(challenge_id, safe="")
    result = _request("PUT", f"/challenges/{encoded_id}", {"exercises": exercises})
    print(result["message"])
    return result


def delete_challenge(challenge_id: str) -> dict:
    """Delete a challenge and all associated student progress."""
    encoded_id = urllib.parse.quote(challenge_id, safe="")
    result = _request("DELETE", f"/challenges/{encoded_id}")
    print(result["message"])
    return result


def list_challenges() -> dict:
    """List challenge identifiers and exercise counts as an admin."""
    response = _request("GET", "/admin/challenges")
    for challenge_id, details in response["challenges"].items():
        print(f"{challenge_id}: {details['exercise_count']} exercises")
    return response["challenges"]


def _set_exercise_availability(
    challenge_id: str,
    question_id: str,
    changes: dict,
) -> dict:
    encoded_challenge = urllib.parse.quote(challenge_id, safe="")
    encoded_question = urllib.parse.quote(question_id, safe="")
    result = _request(
        "PATCH",
        f"/challenges/{encoded_challenge}/exercises/{encoded_question}/availability",
        changes,
    )
    print(result["message"])
    return result["availability"]


def set_exercise_deadline(
    challenge_id: str,
    question_id: str,
    closes_at: str,
) -> dict:
    """Set an ISO deadline; date-only values mean end-of-day in CDMX."""
    return _set_exercise_availability(
        challenge_id, question_id, {"closes_at": closes_at}
    )


def clear_exercise_deadline(challenge_id: str, question_id: str) -> dict:
    """Remove an exercise's automatic deadline."""
    return _set_exercise_availability(
        challenge_id, question_id, {"closes_at": None}
    )


def close_exercise(challenge_id: str, question_id: str) -> dict:
    """Stop accepting submissions for an exercise immediately."""
    return _set_exercise_availability(
        challenge_id, question_id, {"submissions_enabled": False}
    )


def open_exercise(
    challenge_id: str,
    question_id: str,
    clear_deadline: bool = True,
) -> dict:
    """Reopen an exercise, optionally preserving its existing deadline."""
    changes = {"submissions_enabled": True}
    if clear_deadline:
        changes["closes_at"] = None
    return _set_exercise_availability(challenge_id, question_id, changes)


def list_exercise_availability(challenge_id: str) -> dict:
    """List deadlines and open/closed state. Requires an admin token."""
    encoded_id = urllib.parse.quote(challenge_id, safe="")
    response = _request(
        "GET", f"/admin/challenges/{encoded_id}/availability"
    )
    for question_id, availability in response["exercises"].items():
        state = "open" if availability["accepting_submissions"] else "closed"
        deadline = availability.get("closes_at") or "no deadline"
        print(f"{question_id}: {state}; deadline: {deadline}")
    return response["exercises"]


def grade(answer, question_id: str, challenge_id: str) -> dict:
    """Submit an answer using the configured user's identity."""
    print("Submitting your answer. Please wait...")
    try:
        result = _request("POST", "/items/answers", {
            "question_name": question_id,
            "challenge_id": challenge_id,
            "content": answer,
        })
    except GradeAPIError as error:
        if error.status != 409 or error.payload.get("code") != "submissions_closed":
            raise
        message = error.payload["error"]
        print(f"Submission CLOSED. {message}")
        return {"data": {
            "grading_validation": "closed",
            "grading_score": 0.0,
            "grading_error": message,
            "availability": error.payload.get("availability", {}),
        }}
    data = result.get("data", {})
    score = data.get("grading_score", 0)
    if data.get("grading_validation") == "valid":
        print(f"Congratulations! Your answer is correct. (score: {score})")
    else:
        print(f"Submission INCORRECT. (score: {score})")
        if data.get("grading_error"):
            print(f"  Reason: {data['grading_error']}")
    return result


def check_lab_completion_status(challenge_id: str) -> dict:
    """Return and print progress for the configured user."""
    encoded_id = urllib.parse.quote(challenge_id, safe="")
    response = _request("GET", f"/stats/{encoded_id}/progress")
    for lab_name, stats in response.items():
        completed = stats["completed"]
        total = stats["total"]
        percentage = f"{completed / total:.0%}" if total else "0%"
        print(f"{lab_name}: {completed}/{total} exercises completed ({percentage})")
    return response


def check_all_users_completion_status(challenge_id: str) -> dict:
    """Return and print every student's progress. Requires an admin token."""
    encoded_id = urllib.parse.quote(challenge_id, safe="")
    response = _request("GET", f"/admin/stats/{encoded_id}/progress")
    students = response["students"]
    for user_id, details in students.items():
        state = "active" if details["active"] else "disabled"
        identity = details.get("name") or user_id
        email = f" <{details['email']}>" if details.get("email") else ""
        print(f"{identity}{email} [{user_id}] ({state})")
        for lab_name, stats in details["labs"].items():
            completed = stats["completed"]
            total = stats["total"]
            percentage = f"{completed / total:.0%}" if total else "0%"
            print(f"  {lab_name}: {completed}/{total} ({percentage})")
    return students


def _safe_csv_cell(value: str) -> str:
    """Prevent spreadsheet applications from interpreting metadata as formulas."""
    if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return f"'{value}"
    return value


def export_all_users_progress_csv(
    challenge_id: str,
    output_path: str | os.PathLike | None = None,
) -> str:
    """Export every student's progress to a private CSV file as an admin."""
    if output_path is None:
        safe_id = urllib.parse.quote(challenge_id, safe="")
        output_path = f"progress-{safe_id}.csv"
    output_path = os.fspath(output_path)
    if os.path.exists(output_path):
        raise ValueError(f"Output file already exists: {output_path}")

    encoded_id = urllib.parse.quote(challenge_id, safe="")
    students = _request("GET", f"/admin/stats/{encoded_id}/progress")["students"]
    lab_names = sorted({
        lab_name
        for details in students.values()
        for lab_name in details["labs"]
    })
    fieldnames = ["user_id", "name", "email", "active"]
    for lab_name in lab_names:
        fieldnames.extend([
            f"{lab_name}_completed",
            f"{lab_name}_total",
            f"{lab_name}_percentage",
        ])
    fieldnames.extend([
        "overall_completed", "overall_total", "overall_percentage"
    ])

    rows = []
    for user_id, details in sorted(students.items()):
        row = {
            "user_id": _safe_csv_cell(user_id),
            "name": _safe_csv_cell(details.get("name", "")),
            "email": _safe_csv_cell(details.get("email", "")),
            "active": str(details["active"]).lower(),
        }
        overall_completed = 0
        overall_total = 0
        for lab_name in lab_names:
            stats = details["labs"].get(lab_name, {"completed": 0, "total": 0})
            completed = stats["completed"]
            total = stats["total"]
            row[f"{lab_name}_completed"] = completed
            row[f"{lab_name}_total"] = total
            row[f"{lab_name}_percentage"] = (
                f"{completed / total:.2%}" if total else "0.00%"
            )
            overall_completed += completed
            overall_total += total
        row["overall_completed"] = overall_completed
        row["overall_total"] = overall_total
        row["overall_percentage"] = (
            f"{overall_completed / overall_total:.2%}" if overall_total else "0.00%"
        )
        rows.append(row)

    directory = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".progress-", dir=directory, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, output_path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

    print(f"Progress report saved to '{output_path}'")
    return output_path


def health_check() -> bool:
    """Check the public health endpoint."""
    try:
        return _request("GET", "/server/health", authenticated=False).get("status") == "ok"
    except ConnectionError:
        return False
