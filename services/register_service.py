from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from curl_cffi import requests


MAILTM_BASE = "https://api.mail.tm"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"


@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str


class RegisterService:
    def __init__(
        self,
        event_hook: Callable[[str, str, str, str, dict[str, Any] | None], None] | None = None,
    ) -> None:
        self._event_hook = event_hook

    def _emit(
        self,
        level: str,
        event: str,
        message: str,
        *,
        account_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._event_hook:
            self._event_hook(level, event, message, account_id, extra)

    def _mailtm_headers(self, *, token: str = "", use_json: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if use_json:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _mailtm_domains(self, proxies: Any = None) -> list[str]:
        resp = requests.get(
            f"{MAILTM_BASE}/domains",
            headers=self._mailtm_headers(),
            proxies=proxies,
            impersonate="chrome",
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"fetch mail.tm domains failed: {resp.status_code}")

        data = resp.json()
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("hydra:member") or data.get("items") or []
        else:
            items = []

        domains: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").strip()
            is_active = item.get("isActive", True)
            is_private = item.get("isPrivate", False)
            if domain and is_active and not is_private:
                domains.append(domain)
        return domains

    def _get_email_and_token(self, proxies: Any = None) -> tuple[str, str]:
        domains = self._mailtm_domains(proxies)
        if not domains:
            self._emit("error", "mailtm.domains.empty", "no available mail.tm domains")
            return "", ""

        domain = random.choice(domains)
        for _ in range(5):
            local = f"oc{secrets.token_hex(5)}"
            email = f"{local}@{domain}"
            password = secrets.token_urlsafe(18)

            create_resp = requests.post(
                f"{MAILTM_BASE}/accounts",
                headers=self._mailtm_headers(use_json=True),
                json={"address": email, "password": password},
                proxies=proxies,
                impersonate="chrome",
                timeout=15,
            )
            if create_resp.status_code not in (200, 201):
                continue

            token_resp = requests.post(
                f"{MAILTM_BASE}/token",
                headers=self._mailtm_headers(use_json=True),
                json={"address": email, "password": password},
                proxies=proxies,
                impersonate="chrome",
                timeout=15,
            )
            if token_resp.status_code == 200:
                token = str(token_resp.json().get("token") or "").strip()
                if token:
                    return email, token

        self._emit("error", "mailtm.token.failed", "mail.tm token creation failed")
        return "", ""

    def _get_oai_code(self, token: str, email: str, proxies: Any = None) -> str:
        url_list = f"{MAILTM_BASE}/messages"
        regex = r"(?<!\d)(\d{6})(?!\d)"
        seen_ids: set[str] = set()

        for _ in range(40):
            try:
                resp = requests.get(
                    url_list,
                    headers=self._mailtm_headers(token=token),
                    proxies=proxies,
                    impersonate="chrome",
                    timeout=15,
                )
                if resp.status_code != 200:
                    time.sleep(3)
                    continue

                data = resp.json()
                if isinstance(data, list):
                    messages = data
                elif isinstance(data, dict):
                    messages = data.get("hydra:member") or data.get("messages") or []
                else:
                    messages = []

                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    msg_id = str(msg.get("id") or "").strip()
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    read_resp = requests.get(
                        f"{MAILTM_BASE}/messages/{msg_id}",
                        headers=self._mailtm_headers(token=token),
                        proxies=proxies,
                        impersonate="chrome",
                        timeout=15,
                    )
                    if read_resp.status_code != 200:
                        continue

                    mail_data = read_resp.json()
                    sender = str(((mail_data.get("from") or {}).get("address") or "")).lower()
                    subject = str(mail_data.get("subject") or "")
                    intro = str(mail_data.get("intro") or "")
                    text = str(mail_data.get("text") or "")
                    html = mail_data.get("html") or ""
                    if isinstance(html, list):
                        html = "\n".join(str(x) for x in html)

                    content = "\n".join([subject, intro, text, str(html)])
                    if "openai" not in sender and "openai" not in content.lower():
                        continue

                    match = re.search(regex, content)
                    if match:
                        return match.group(1)
            except Exception:
                pass

            time.sleep(3)

        self._emit("error", "mailtm.otp.timeout", f"mail otp timeout: {email}")
        return ""

    @staticmethod
    def _b64url_no_pad(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _sha256_b64url_no_pad(s: str) -> str:
        return RegisterService._b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())

    @staticmethod
    def _random_state(nbytes: int = 16) -> str:
        return secrets.token_urlsafe(nbytes)

    @staticmethod
    def _pkce_verifier() -> str:
        return secrets.token_urlsafe(64)

    @staticmethod
    def _parse_callback_url(callback_url: str) -> dict[str, str]:
        candidate = callback_url.strip()
        if not candidate:
            return {"code": "", "state": "", "error": "", "error_description": ""}

        if "://" not in candidate:
            if candidate.startswith("?"):
                candidate = f"http://localhost{candidate}"
            elif any(ch in candidate for ch in "/?#") or ":" in candidate:
                candidate = f"http://{candidate}"
            elif "=" in candidate:
                candidate = f"http://localhost/?{candidate}"

        parsed = urllib.parse.urlparse(candidate)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)

        for key, values in fragment.items():
            if key not in query or not query[key] or not (query[key][0] or "").strip():
                query[key] = values

        def get1(key: str) -> str:
            values = query.get(key, [""])
            return (values[0] or "").strip()

        code = get1("code")
        state = get1("state")
        error = get1("error")
        error_description = get1("error_description")

        if code and not state and "#" in code:
            code, state = code.split("#", 1)

        if not error and error_description:
            error, error_description = error_description, ""

        return {
            "code": code,
            "state": state,
            "error": error,
            "error_description": error_description,
        }

    @staticmethod
    def _jwt_claims_no_verify(id_token: str) -> dict[str, Any]:
        if not id_token or id_token.count(".") < 2:
            return {}
        payload_b64 = id_token.split(".")[1]
        pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
        try:
            payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
            return json.loads(payload.decode("utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _decode_jwt_segment(seg: str) -> dict[str, Any]:
        raw = (seg or "").strip()
        if not raw:
            return {}
        pad = "=" * ((4 - (len(raw) % 4)) % 4)
        try:
            decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
            return json.loads(decoded.decode("utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _to_int(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _post_form(url: str, data: dict[str, str], timeout: int = 30) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.status != 200:
                    raise RuntimeError(
                        f"token exchange failed: {resp.status}: {raw.decode('utf-8', 'replace')}"
                    )
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise RuntimeError(
                f"token exchange failed: {exc.code}: {raw.decode('utf-8', 'replace')}"
            ) from exc

    def _generate_oauth_url(
        self,
        *,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        scope: str = DEFAULT_SCOPE,
    ) -> OAuthStart:
        state = self._random_state()
        code_verifier = self._pkce_verifier()
        code_challenge = self._sha256_b64url_no_pad(code_verifier)

        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }

        auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
        return OAuthStart(
            auth_url=auth_url,
            state=state,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )

    def _submit_callback_url(
        self,
        *,
        callback_url: str,
        expected_state: str,
        code_verifier: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
    ) -> dict[str, Any]:
        cb = self._parse_callback_url(callback_url)
        if cb["error"]:
            desc = cb["error_description"]
            raise RuntimeError(f"oauth error: {cb['error']}: {desc}".strip())

        if not cb["code"]:
            raise ValueError("callback url missing ?code=")
        if not cb["state"]:
            raise ValueError("callback url missing ?state=")
        if cb["state"] != expected_state:
            raise ValueError("state mismatch")

        token_resp = self._post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": cb["code"],
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )

        access_token = (token_resp.get("access_token") or "").strip()
        refresh_token = (token_resp.get("refresh_token") or "").strip()
        id_token = (token_resp.get("id_token") or "").strip()
        expires_in = self._to_int(token_resp.get("expires_in"))

        claims = self._jwt_claims_no_verify(id_token)
        email = str(claims.get("email") or "").strip()
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()

        now = int(time.time())
        expired_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0)))
        now_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

        return {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
            "last_refresh": now_rfc3339,
            "email": email,
            "type": "codex",
            "expired": expired_rfc3339,
        }

    def register_account(self, proxy: str | None = None) -> dict[str, Any] | None:
        proxies: Any = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}

        session = requests.Session(proxies=proxies, impersonate="chrome")

        try:
            trace_resp = session.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
            loc_match = re.search(r"^loc=(.+)$", trace_resp.text, re.MULTILINE)
            loc = loc_match.group(1) if loc_match else None
            self._emit("info", "network.loc", f"current IP region: {loc}")
            if loc in {"CN", "HK"}:
                raise RuntimeError("unsupported region, set proxy")
        except Exception as exc:
            self._emit("error", "network.check.failed", f"network check failed: {exc}")
            return None

        email, dev_token = self._get_email_and_token(proxies)
        if not email or not dev_token:
            return None

        self._emit("info", "mailtm.account.ready", f"temporary mailbox ready: {email}")
        oauth = self._generate_oauth_url()

        try:
            session.get(oauth.auth_url, timeout=15)
            did = session.cookies.get("oai-did")

            signup_body = (
                f'{{"username":{{"value":"{email}","kind":"email"}},"screen_hint":"signup"}}'
            )
            sentinel_req_body = f'{{"p":"","id":"{did}","flow":"authorize_continue"}}'

            sentinel_resp = requests.post(
                "https://sentinel.openai.com/backend-api/sentinel/req",
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=sentinel_req_body,
                proxies=proxies,
                impersonate="chrome",
                timeout=15,
            )
            if sentinel_resp.status_code != 200:
                self._emit(
                    "error",
                    "sentinel.blocked",
                    f"sentinel blocked: {sentinel_resp.status_code}",
                )
                return None

            sentinel_token = sentinel_resp.json().get("token")
            sentinel_header = (
                f'{{"p": "", "t": "", "c": "{sentinel_token}", "id": "{did}", "flow": "authorize_continue"}}'
            )

            signup_resp = session.post(
                "https://auth.openai.com/api/accounts/authorize/continue",
                headers={
                    "referer": "https://auth.openai.com/create-account",
                    "accept": "application/json",
                    "content-type": "application/json",
                    "openai-sentinel-token": sentinel_header,
                },
                data=signup_body,
            )
            if signup_resp.status_code >= 400:
                self._emit("error", "register.signup.failed", f"signup failed: {signup_resp.status_code}")
                return None

            otp_resp = session.post(
                "https://auth.openai.com/api/accounts/passwordless/send-otp",
                headers={
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
            )
            if otp_resp.status_code >= 400:
                self._emit("error", "register.otp.send.failed", f"otp send failed: {otp_resp.status_code}")
                return None

            code = self._get_oai_code(dev_token, email, proxies)
            if not code:
                return None

            validate_resp = session.post(
                "https://auth.openai.com/api/accounts/email-otp/validate",
                headers={
                    "referer": "https://auth.openai.com/email-verification",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=f'{{"code":"{code}"}}',
            )
            if validate_resp.status_code >= 400:
                self._emit(
                    "error",
                    "register.otp.validate.failed",
                    f"otp validate failed: {validate_resp.status_code}",
                )
                return None

            create_resp = session.post(
                "https://auth.openai.com/api/accounts/create_account",
                headers={
                    "referer": "https://auth.openai.com/about-you",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data='{"name":"Neo","birthdate":"2000-02-20"}',
            )
            if create_resp.status_code != 200:
                self._emit(
                    "error",
                    "register.create_account.failed",
                    f"create account failed: {create_resp.status_code}",
                    extra={"body": create_resp.text[:1000]},
                )
                return None

            auth_cookie = session.cookies.get("oai-client-auth-session")
            if not auth_cookie:
                self._emit("error", "register.cookie.missing", "missing auth cookie")
                return None

            auth_json = self._decode_jwt_segment(auth_cookie.split(".")[0])
            workspaces = auth_json.get("workspaces") or []
            if not workspaces:
                self._emit("error", "register.workspace.missing", "workspace list missing")
                return None

            workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
            if not workspace_id:
                self._emit("error", "register.workspace.invalid", "workspace_id invalid")
                return None

            select_resp = session.post(
                "https://auth.openai.com/api/accounts/workspace/select",
                headers={
                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "content-type": "application/json",
                },
                data=f'{{"workspace_id":"{workspace_id}"}}',
            )
            if select_resp.status_code != 200:
                self._emit(
                    "error",
                    "register.workspace.select.failed",
                    f"workspace select failed: {select_resp.status_code}",
                    extra={"body": select_resp.text[:1000]},
                )
                return None

            continue_url = str((select_resp.json() or {}).get("continue_url") or "").strip()
            if not continue_url:
                self._emit("error", "register.continue_url.missing", "continue_url missing")
                return None

            current_url = continue_url
            for _ in range(6):
                redirect_resp = session.get(current_url, allow_redirects=False, timeout=15)
                location = redirect_resp.headers.get("Location") or ""
                if redirect_resp.status_code not in {301, 302, 303, 307, 308}:
                    break
                if not location:
                    break

                next_url = urllib.parse.urljoin(current_url, location)
                if "code=" in next_url and "state=" in next_url:
                    token_data = self._submit_callback_url(
                        callback_url=next_url,
                        code_verifier=oauth.code_verifier,
                        redirect_uri=oauth.redirect_uri,
                        expected_state=oauth.state,
                    )
                    self._emit(
                        "info",
                        "register.success",
                        f"registered account: {token_data.get('email', '')}",
                        account_id=str(token_data.get("account_id") or ""),
                    )
                    return token_data
                current_url = next_url

            self._emit("error", "register.callback.missing", "final callback URL missing")
            return None

        except Exception as exc:
            self._emit("error", "register.exception", f"register failed: {exc}")
            return None
