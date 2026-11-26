#!/usr/bin/env python3
"""Register one virtual coffee per GitHub account every 30 days.

Triggered by an Issue opened from the cafe.yml Issue Form. The script uses
only Python's standard library and the repository-scoped GITHUB_TOKEN.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

COUNTER_PATH = "cafe-counter.json"
EXPECTED_TITLE = "☕ Apoio virtual ao Arquimago"
CONFIRMATION = (
    "Confirmo que este é um café virtual e apoio simbólico, "
    "sem pagamento ou recompensa financeira."
)
COOLDOWN_DAYS = 30
MAX_WRITE_ATTEMPTS = 6


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def api_request(method: str, path: str, payload: dict | None = None):
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN não foi informado ao workflow.")

    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{api_url}/{path.lstrip('/')}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "torre-do-arquimago-cafe-counter",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )

    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read()
            return response.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise GitHubAPIError(error.code, details or error.reason) from error
    except URLError as error:
        raise RuntimeError(f"Falha de conexão com a API do GitHub: {error}") from error


def repository_context() -> tuple[str, str]:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
    if not re.fullmatch(r"[^/]+/[^/]+", repository):
        raise RuntimeError("GITHUB_REPOSITORY está ausente ou inválido.")
    return repository, branch


def load_counter(repository: str, branch: str) -> tuple[dict, str | None]:
    encoded_branch = quote(branch, safe="")
    query = urlencode({"ref": branch})
    path = f"repos/{repository}/contents/{COUNTER_PATH}?{query}"
    try:
        _, response = api_request("GET", path)
    except GitHubAPIError as error:
        if error.status == 404:
            # Allows the workflow to initialize the counter if the file was removed.
            return {
                "schemaVersion": 1,
                "label": "Cafés recebidos",
                "message": "0",
                "color": "orange",
                "count": 0,
                "contributors": [],
                "last_updated": None,
            }, None
        raise

    content = response.get("content", "")
    if not content:
        raise RuntimeError(f"O arquivo {COUNTER_PATH} não possui conteúdo Base64.")
    decoded = base64.b64decode(content).decode("utf-8")
    state = json.loads(decoded)
    if not isinstance(state, dict):
        raise RuntimeError(f"O arquivo {COUNTER_PATH} precisa conter um objeto JSON.")
    state.setdefault("count", 0)
    state.setdefault("contributors", [])
    if not isinstance(state["contributors"], list):
        raise RuntimeError("O campo contributors do contador precisa ser uma lista.")
    return state, response.get("sha")


def save_counter(repository: str, branch: str, state: dict, sha: str | None, login: str) -> bool:
    payload = {
        "message": f"☕ +1 café para o Arquimago (@{login})",
        "content": base64.b64encode(
            (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        ).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        api_request("PUT", f"repos/{repository}/contents/{COUNTER_PATH}", payload)
        return True
    except GitHubAPIError as error:
        if error.status in (409, 422):
            # A concurrent run may have updated the file. Caller will re-fetch
            # the latest SHA and retry, checking the 30-day cooldown again.
            return False
        raise


def find_recent_support(state: dict, login: str, now: datetime) -> datetime | None:
    cutoff = now - timedelta(days=COOLDOWN_DAYS)
    recent = []
    normalized = login.casefold()
    for entry in state.get("contributors", []):
        if not isinstance(entry, dict):
            continue
        saved_login = str(entry.get("login", "")).casefold()
        timestamp = parse_timestamp(entry.get("registered_at"))
        if saved_login == normalized and timestamp and timestamp >= cutoff:
            recent.append(timestamp)
    return max(recent) if recent else None


def comment_and_close(repository: str, issue_number: int, comment: str) -> None:
    try:
        api_request(
            "POST",
            f"repos/{repository}/issues/{issue_number}/comments",
            {"body": comment},
        )
    except Exception as error:  # The counter commit should not be rolled back by a comment failure.
        print(f"Aviso: não foi possível comentar na Issue: {error}", file=sys.stderr)

    try:
        api_request(
            "PATCH",
            f"repos/{repository}/issues/{issue_number}",
            {"state": "closed", "state_reason": "completed"},
        )
    except Exception as error:
        print(f"Aviso: não foi possível fechar a Issue: {error}", file=sys.stderr)


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("GITHUB_EVENT_PATH não foi informado.")
    with open(event_path, "r", encoding="utf-8") as event_file:
        event = json.load(event_file)

    issue = event.get("issue", {})
    issue_number = issue.get("number")
    title = issue.get("title", "")
    body = issue.get("body") or ""
    author_data = issue.get("user") or {}
    login = author_data.get("login", "")
    author_type = author_data.get("type", "")
    labels = {
        str(label.get("name", "")).casefold()
        for label in (issue.get("labels") or [])
        if isinstance(label, dict)
    }

    # Ignore every normal Issue. Only the dedicated form can register coffee.
    if "cafe-virtual" not in labels or title != EXPECTED_TITLE:
        print("Issue ignorada: não corresponde ao formulário de café virtual.")
        return 0

    checked_confirmation = re.search(
        r"(?m)^\s*-\s*\[[xX]\]\s*" + re.escape(CONFIRMATION) + r"\s*$",
        body,
    )
    if not checked_confirmation:
        repository, _ = repository_context()
        comment_and_close(
            repository,
            int(issue_number),
            "Não consegui validar a confirmação do formulário. Nenhum café foi registrado. "
            "Abra uma nova solicitação usando **☕ Oferecer um café ao Arquimago** e marque a confirmação.",
        )
        return 0

    if not login or author_type.casefold() == "bot":
        repository, _ = repository_context()
        comment_and_close(
            repository,
            int(issue_number),
            "O café virtual precisa ser enviado por uma conta GitHub pessoal autenticada. Nenhum café foi registrado.",
        )
        return 0

    repository, branch = repository_context()
    now = utc_now()
    registered_at = now.isoformat().replace("+00:00", "Z")

    for attempt in range(MAX_WRITE_ATTEMPTS):
        state, sha = load_counter(repository, branch)
        recent = find_recent_support(state, login, now)
        if recent:
            next_allowed = (recent + timedelta(days=COOLDOWN_DAYS)).date().isoformat()
            comment_and_close(
                repository,
                int(issue_number),
                f"☕ Obrigado, @{login}! Seu café anterior ainda está dentro do intervalo de {COOLDOWN_DAYS} dias. "
                f"O contador não foi alterado. Você poderá oferecer outro café a partir de **{next_allowed}**. 🧙",
            )
            print(f"Café duplicado dentro do intervalo: @{login}.")
            return 0

        contributors = state.get("contributors", [])
        if not isinstance(contributors, list):
            raise RuntimeError("O campo contributors precisa ser uma lista.")

        current_count = state.get("count", 0)
        if not isinstance(current_count, int) or current_count < 0:
            raise RuntimeError("O campo count precisa ser um inteiro não negativo.")

        state["count"] = current_count + 1
        state["contributors"] = contributors + [{
            "login": login,
            "issue": int(issue_number),
            "registered_at": registered_at,
        }]
        # Shields.io endpoint schema + auditable counter data.
        state["schemaVersion"] = 1
        state["label"] = "Cafés recebidos"
        state["message"] = str(state["count"])
        state["color"] = "orange"
        state["last_updated"] = registered_at

        if save_counter(repository, branch, state, sha, login):
            comment_and_close(
                repository,
                int(issue_number),
                f"☕ **Café registrado com sucesso, @{login}!**\n\n"
                f"O Arquimago recebeu +1 de café. O contador agora está em **{state['count']}**.\n\n"
                "Um commit automático atualizou `cafe-counter.json`. Obrigado por fortalecer a magia da Torre! 🧙✨",
            )
            print(f"Café registrado para @{login}. Total: {state['count']}.")
            return 0

        print(f"Conflito ao atualizar contador; nova tentativa {attempt + 1}/{MAX_WRITE_ATTEMPTS}.")
        time.sleep(1 + attempt)

    raise RuntimeError(
        "Não foi possível atualizar cafe-counter.json após várias tentativas. "
        "Verifique as permissões do workflow e tente novamente."
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
