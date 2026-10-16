"""Verified CASDA signed-URL and ephemeral-credential adapter, with explicit settings."""
from __future__ import annotations
import logging
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse
logger = logging.getLogger(__name__)

class CasdaCredentialError(RuntimeError):
    """Login configuration/authentication failed; do not consume science retries."""

_URL_WITH_QUERY_RE = re.compile(r"https?://[^\s<>]+", flags=re.IGNORECASE)
_AWS_QUERY_VALUE_RE = re.compile(r"(?i)(X-Amz-[A-Za-z-]+)=([^&\s'\"]+)")

def _read_password(path: Path, env_name: str) -> str | None:
    """Read a runtime password from the environment or a mode-600 file.

    English: The environment variable is a one-process override; an insecure
    or symlinked file is rejected.

    中文：环境变量是当前进程的覆盖值；不安全或符号链接密码文件会被拒绝。
    """

    value = os.environ.get(env_name)
    if value:
        return value.rstrip("\r\n")
    if path.exists():
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError(
                f"Refusing insecure CASDA password file {path}; use mode 600"
            )
        return path.read_text(encoding="utf-8").strip()
    return None

def write_password_file(path: Path, password: str) -> Path:
    """Atomically store the CASDA password in a mode-600 file.

    English: The reader strips surrounding whitespace, so such passwords are
    refused here instead of being silently altered. A symbolic link is never
    written through.

    中文：读取时会去掉首尾空白，因此这里拒绝首尾含空白的密码，而不是悄悄改变它；
    不会通过符号链接写入。
    """

    path = Path(path).expanduser()
    if not password:
        raise ValueError("No password entered; nothing was saved")
    if password != password.strip():
        raise ValueError("The password starts or ends with whitespace, which the password file "
                         "cannot keep; export CASDA_PASSWORD instead. Nothing was saved")
    if path.is_symlink():
        raise RuntimeError(f"Refusing to write the CASDA password through a symbolic link: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)  # created as mode 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(password + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path

def _redact_url(url: str) -> str:
    """Remove query/fragment credentials or tokens from a logged URL.

    中文：从日志 URL 中移除 query/fragment 里的凭据或 token。
    """

    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

def _encode_casda_staged_url(url: str) -> str:
    """Encode literal characters that invalidate or ambiguate staged URLs.

    English: CASDA can return a mixed pair in which the archive URL already
    contains ``%2B/%20/%22`` while its checksum URL still contains a literal
    ``+``. These replacements are intentionally narrow and idempotent for
    already percent-encoded URLs; Requests can then transmit the strings via
    the existing ``Casda.download_files`` implementation.

    中文：CASDA 有时返回混合编码的一对 URL：archive 已包含
    ``%2B/%20/%22``，checksum 路径却仍包含字面 ``+``。这里仅编码这三类
    字面字符；已经百分号编码的 URL 不会被二次编码，后续仍使用现有的
    ``Casda.download_files``。
    """

    return str(url).replace("+", "%2B").replace(" ", "%20").replace('"', "%22")

def _redact_download_error(error: BaseException, urls: list[str]) -> str:
    """Return useful download diagnostics without retaining signed queries.

    English: Requests may reproduce the full prepared S3 URL in an exception.
    Replace both the staged form and any URL-shaped query text, then mask any
    detached ``X-Amz-*`` values left by a malformed URL containing spaces.

    中文：Requests 的异常可能包含完整的 S3 预签名 URL。这里同时清理原始 staged
    URL、异常中的 query URL，并遮蔽因含空格 URL 而残留的 ``X-Amz-*`` 参数。
    """

    message = str(error)
    for url in sorted((str(value) for value in urls), key=len, reverse=True):
        message = message.replace(url, _redact_url(url))

    def redact_match(match: re.Match[str]) -> str:
        value = match.group(0)
        if "?" not in value:
            return value
        trailing = ""
        while value and value[-1] in ".,;:)]}":
            trailing = value[-1] + trailing
            value = value[:-1]
        return f"{value.split('?', 1)[0]}?<redacted>{trailing}"

    message = _URL_WITH_QUERY_RE.sub(redact_match, message)
    return _AWS_QUERY_VALUE_RE.sub(r"\1=<redacted>", message)

def build_casda_client(settings):
    """Create a non-interactive CASDA client for batch downloads.

    English: A protected runtime password is exposed only through an in-memory
    keyring backend; it is not written to persistent keyring storage.

    中文：受保护的运行时密码只通过内存 keyring backend 暴露，不写入持久化 keyring。
    """

    if not settings["casda_username"]:
        raise CasdaCredentialError("Configure casda_username before downloading")
    import astroquery
    from astroquery.casda import CasdaClass
    from keyring.backend import KeyringBackend
    import keyring

    class _PercentEncodedUrlCasda(CasdaClass):
        """Backport the Astroquery 0.4.12 CASDA signed-URL fix.

        Astroquery 0.4.11 calls ``unquote()`` on each staged URL in
        ``_complete_job``. That changes signed path/query bytes such as ``%2B``
        and invalidates AWS S3 signatures. Start with the upstream 0.4.12
        behaviour, then encode only any literal ``+``, spaces, or quotes that
        CASDA still returns (observed on checksum URLs). The later
        ``download_files()`` call remains unchanged and still downloads one
        archive/checksum pair at a time.

        中文：Astroquery 0.4.11 会在 ``_complete_job`` 中对 staged URL 调用
        ``unquote()``，从而改变 ``%2B`` 等已签名字符并破坏 AWS S3 签名。这里先按
        0.4.12 上游实现保留编码，再仅编码 CASDA 仍返回的字面 ``+``、空格或引号；
        后续 ``download_files()`` 逻辑不变。
        """

        def _complete_job(self, job_url, verbose):
            final_status = self._run_job(
                job_url,
                verbose,
                poll_interval=self.POLL_INTERVAL,
            )
            if final_status != "COMPLETED":
                if verbose:
                    logger.info("CASDA staging job ended with status %s", final_status)
                raise ValueError(
                    "Data staging job did not complete successfully. "
                    f"Status was {final_status}"
                )

            job_details = self._get_job_details_xml(job_url)
            fileurls: list[str] = []
            results = job_details.find("uws:results", self._uws_ns)
            if results is None:
                raise ValueError("CASDA staging job returned no result URL collection")
            href_key = "{http://www.w3.org/1999/xlink}href"
            for result in results.findall("uws:result", self._uws_ns):
                file_location = result.get(href_key)
                if file_location:
                    fileurls.append(_encode_casda_staged_url(file_location))
            return fileurls

    class _RuntimeKeyring(KeyringBackend):
        """Expose the protected batch password only for this Python process.

        中文：只在当前 Python 进程中提供受保护的批处理密码。
        """

        priority = 1

        def __init__(self, username: str, password: str):
            self.username = username
            self.password = password

        def get_password(self, service: str, username: str) -> str | None:
            if service == "astroquery:casda.csiro.au" and username == self.username:
                return self.password
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            return None

        def delete_password(self, service: str, username: str) -> None:
            return None

    try:
        password = _read_password(Path(settings["casda_password_file"]).expanduser(), settings["casda_password_env"])
    except (OSError, RuntimeError) as exc:
        raise CasdaCredentialError(str(exc)) from None
    if not password:
        raise CasdaCredentialError(
            "CASDA credentials are missing. Set CASDA_PASSWORD or create "
            f"a protected password file at {settings['casda_password_file']}."
        )
    # Current astroquery.casda reads keyring credentials and rejects the older
    # password= keyword. Keep the file value in an in-memory backend only.
    # 当前 astroquery.casda 从 keyring 读取凭据并拒绝旧的 password= 参数；只使用内存
    # backend，避免把密码文件复制到持久化 keyring。
    keyring.set_keyring(_RuntimeKeyring(settings["casda_username"], password))
    client = _PercentEncodedUrlCasda()
    try:
        authenticated = client.login(username=settings["casda_username"], store_password=False)
    except Exception:
        raise CasdaCredentialError("CASDA authentication failed; check account and credential source") from None
    if authenticated is False:
        raise CasdaCredentialError("CASDA authentication failed")
    logger.info(
        "CASDA client ready (astroquery %s; staged URL percent-encoding preserved)",
        astroquery.__version__,
    )
    return client
