"""Internal signed APK packaging; public CLI delegates to the full build flow."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from maintenance.archive.memory_pack import build as build_memory_pack
from maintenance.core.process_lock import AlreadyRunning, process_lock
from maintenance.core.schema import reject_synthetic_database
from maintenance.daily_memories import build_daily_pool, reject_synthetic_pool, validate_prepared_pool
from maintenance.model_config import load_model_config
from maintenance.paths import ROOT

VERSION = re.compile(r"(?m)^(version:\s*)(\d+)\.(\d+)\.[0-9A-Za-z.-]+\+(\d+)(\s*(?:#.*)?)$")


class ReleaseError(RuntimeError):
    pass


def validate_version_inputs(*, version_name=None, software_version=None):
    """Validate user-facing versions before any archive or configuration writes."""
    if version_name is not None and software_version is not None:
        raise ReleaseError("完整版本号和软件版本不能同时填写。")
    if software_version is not None and (
        not isinstance(software_version, str)
        or not re.fullmatch(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})", software_version)
    ):
        raise ReleaseError("软件版本应为 major.minor，例如 0.1。")
    if version_name is not None:
        if not isinstance(version_name, str) or not re.fullmatch(
            r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.([0-9]{6})", version_name
        ):
            raise ReleaseError("完整版本号应为 major.minor.YYMMDD，例如 0.1.260927；不要填写 + 构建号。")
        date = version_name.rsplit(".", 1)[1]
        try:
            # YY always denotes 20YY; strptime's 1969 pivot is not appropriate.
            datetime(2000 + int(date[:2]), int(date[2:4]), int(date[4:]))
        except ValueError:
            raise ReleaseError("版本号中的 YYMMDD 必须是真实日期。") from None


def _properties(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*([^#!\s=:]+)\s*[=:]\s*(.*)", line)
        if match:
            values[match[1]] = match[2].strip().replace(r"\:", ":").replace("\\\\", "\\")
    return values


def _redact(value: object, secrets: tuple[str, ...]) -> str:
    text = str(value)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if not secret:
            continue
        forms = (
            secret,
            json.dumps(secret)[1:-1],
            quote(secret, safe=""),
            base64.b64encode(secret.encode()).decode(),
            base64.b64encode(f"DEEPSEEK_API_KEY={secret}".encode()).decode(),
        )
        for form in sorted(set(forms), key=len, reverse=True):
            text = text.replace(form, "[已隐藏]")
    return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[已隐藏]", text)


def _tool_paths(app: Path) -> tuple[Path, Path]:
    local_file = app / "android/local.properties"
    local = _properties(local_file) if local_file.is_file() else {}
    flutter = shutil.which("flutter")
    if not flutter and local.get("flutter.sdk"):
        flutter = str(Path(local["flutter.sdk"]) / "bin" / ("flutter.bat" if os.name == "nt" else "flutter"))
    if not flutter or not Path(flutter).is_file():
        raise ReleaseError("没有找到 Flutter，请检查 PATH 或 android/local.properties。")
    sdk = local.get("sdk.dir") or os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    signers = (
        list((Path(sdk) / "build-tools").glob("*/apksigner" + (".bat" if os.name == "nt" else "")))
        if sdk
        else []
    )
    if not signers:
        raise ReleaseError("没有找到 apksigner，请检查 Android SDK build-tools。")
    signers.sort(key=lambda path: tuple(int(part) for part in re.findall(r"\d+", path.parent.name)))
    return Path(flutter), signers[-1]


def _run(command: list[str], *, cwd: Path, env: dict[str, str], emit) -> str:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    lines = []
    assert process.stdout is not None
    for line in process.stdout:
        emit(line.rstrip())
        lines.append(line)
    if process.wait():
        raise ReleaseError(f"{Path(command[0]).name} 执行失败；已有发布包未修改。")
    return "".join(lines)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect_apk(apk: Path, aapt: Path, apksigner: Path, *, app: Path, env, emit) -> dict:
    badging = _run([str(aapt), "dump", "badging", str(apk)], cwd=app, env=env, emit=emit)
    package = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging)
    if package is None or package[1] != "com.sevenu.app" or not package[2].isdigit():
        raise ReleaseError("APK 包名或内部版本不合法。")
    signature = _run(
        [str(apksigner), "verify", "--verbose", "--print-certs", str(apk)], cwd=app, env=env, emit=emit
    )
    if re.search(r"CN\s*=\s*Android Debug", signature, re.IGNORECASE):
        raise ReleaseError("APK 使用 debug 证书，已停止发布。")
    certificate = re.search(r"Signer #1 certificate SHA-256 digest:\s*([0-9a-fA-F]{64})", signature)
    if certificate is None:
        raise ReleaseError("无法确认 APK 签名证书，已停止发布。")
    return {
        "version_code": int(package[2]),
        "version_name": package[3],
        "certificate_sha256": certificate[1].lower(),
    }


def _configured_certificate(keystore: Path, signing: dict, *, app: Path, env) -> str:
    """Verify the initial release identity without putting its password in argv."""
    keytool = shutil.which("keytool")
    if not keytool and env.get("JAVA_HOME"):
        candidate = Path(env["JAVA_HOME"]) / "bin" / ("keytool.exe" if os.name == "nt" else "keytool")
        if candidate.is_file():
            keytool = str(candidate)
    if not keytool:
        raise ReleaseError("首次发布需要 keytool 校验证书，请检查 JDK 或 JAVA_HOME。")
    result = subprocess.run(
        [
            keytool,
            "-exportcert",
            "-keystore",
            str(keystore),
            "-alias",
            signing["keyAlias"],
            "-storepass:env",
            "SEVENU_CERTIFICATE_PASSWORD",
        ],
        cwd=app,
        env={**env, "SEVENU_CERTIFICATE_PASSWORD": signing["storePassword"]},
        capture_output=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode or not result.stdout:
        raise ReleaseError("无法从永久 keystore 校验证书，请检查签名配置。")
    return hashlib.sha256(result.stdout).hexdigest()


def _publish(source: Path, destination: Path) -> None:
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".7u-", suffix=".pending", delete=False
        ) as stream:
            staged = Path(stream.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # Linking within the same directory publishes a complete file atomically
        # and fails if the final filename already exists. Never overwrite an APK.
        os.link(staged, destination)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _save_version(path: Path, value: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=".pubspec-",
            suffix=".tmp",
            mode="w",
            encoding="utf-8",
            newline="",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_release(
    root: Path = ROOT,
    *,
    version_name=None,
    software_version=None,
    prepared_pool_sha256=None,
    emit=print,
    _maintenance_lock_held=False,
) -> dict:
    """Package an archive. The UI must call maintenance.build.run_build instead.

    prepared_pool_sha256 is only for an explicitly reviewed, already prepared
    pool; never exposed by the CLI or maintenance window. It is still verified
    against the current archive, and again before publishing the APK.
    """
    validate_version_inputs(version_name=version_name, software_version=software_version)
    if prepared_pool_sha256 is not None and (
        not isinstance(prepared_pool_sha256, str) or not re.fullmatch("[0-9a-f]{64}", prepared_pool_sha256)
    ):
        raise ReleaseError("已审查短句池的 SHA-256 不合法。")
    root = Path(root).resolve()
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    secrets: tuple[str, ...] = ()

    def output(value):
        return emit(_redact(value, secrets))

    try:
        lock = nullcontext() if _maintenance_lock_held else process_lock(data / "maintenance.lock")
        with lock:
            app = root / "app"
            reject_synthetic_database(data / "couple_chat.db")
            reject_synthetic_database(app / "assets/memory/memory_pack.db")
            reject_synthetic_pool(app / "assets/memory/daily_memories.json")
            config = load_model_config(data / "model_config.dpapi")
            if config is None:
                raise ReleaseError("请先在维护窗口安全保存模型 API Key。")
            secrets = (config.api_key,)
            app = root / "app"
            properties_path = app / "android/key.properties"
            if not properties_path.is_file():
                raise ReleaseError("缺少永久签名 android/key.properties；正式发布不允许 debug 签名。")
            signing = _properties(properties_path)
            secrets += (signing.get("storePassword", ""), signing.get("keyPassword", ""))
            if any(
                not signing.get(field) for field in ("storeFile", "storePassword", "keyAlias", "keyPassword")
            ):
                raise ReleaseError("永久签名配置不完整。")
            keystore = (app / "android/app" / signing["storeFile"]).resolve()
            if (
                not keystore.is_file()
                or keystore.name.lower() == "debug.keystore"
                or signing["keyAlias"].lower() == "androiddebugkey"
            ):
                raise ReleaseError("永久签名文件不可用或指向 debug 签名，已停止发布。")
            flutter, apksigner = _tool_paths(app)
            aapt = apksigner.with_name("aapt.exe" if os.name == "nt" else "aapt")
            if not aapt.is_file():
                raise ReleaseError("没有找到 aapt，无法核验 APK 版本。")
            environment = {**os.environ, "SEVENU_ALLOW_DEBUG_SIGNING": "false"}
            environment.pop("ORG_GRADLE_PROJECT_allowDebugSigning", None)
            pubspec = app / "pubspec.yaml"
            previous_text = pubspec.read_text(encoding="utf-8")
            version = VERSION.search(previous_text)
            if version is None:
                raise ReleaseError("pubspec.yaml 需要 major.minor.patch+build 格式的版本号。")
            releases = root / "releases"
            history = []
            for previous in releases.glob("7u-*.apk"):
                try:
                    inspected = _inspect_apk(
                        previous, aapt, apksigner, app=app, env=environment, emit=lambda _: None
                    )
                except ReleaseError:
                    output(f"跳过无法验证的历史安装包：{previous.name}")
                    continue
                history.append(inspected)
            existing_codes = [item["version_code"] for item in history]
            if history:
                latest_code = max(existing_codes)
                certificates = {
                    item["certificate_sha256"] for item in history if item["version_code"] == latest_code
                }
                if len(certificates) != 1:
                    raise ReleaseError("同一最新内部版本存在不同签名，无法确定升级基准。")
                expected_certificate = certificates.pop()
            else:
                expected_certificate = _configured_certificate(keystore, signing, app=app, env=environment)
            number = max([int(version[4]), *existing_codes]) + 1
            if number > 2100000000:
                raise ReleaseError("Android versionCode 已达到上限。")
            output("[3/4] 正在重新生成记忆包与全新每日短句池……")
            memory = build_memory_pack(data / "couple_chat.db", app / "assets/memory/memory_pack.db")
            reject_synthetic_database(app / "assets/memory/memory_pack.db")
            timestamp = memory.get("latest_message_ts")
            if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp <= 0:
                raise ReleaseError("没有可读聊天日期，无法生成发布版本号。")
            pool_path = app / "assets/memory/daily_memories.json"
            if prepared_pool_sha256 is None:
                daily_pool = build_daily_pool(
                    data / "couple_chat.db", pool_path, config=config, fresh=True, emit=output
                )
            else:
                daily_pool = validate_prepared_pool(data / "couple_chat.db", pool_path)
                if daily_pool["sha256"] != prepared_pool_sha256:
                    raise ReleaseError("短句池已不是人工审查的版本，停止打包。")
            reject_synthetic_pool(pool_path)
            date = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%y%m%d")
            name = version_name or f"{software_version or f'{version[2]}.{version[3]}'}.{date}"
            validate_version_inputs(version_name=name)
            destination = releases / f"7u-{name}+{number}.apk"
            if destination.exists():
                raise ReleaseError("目标发布包已存在，拒绝覆盖。")
            apk = app / "build/app/outputs/flutter-apk/app-release.apk"
            output(f"[4/4] 正在构建 7u {name}（内部版本 {number}）……")
            with tempfile.TemporaryDirectory(prefix=".release-", dir=data) as folder:
                # A prior failed attempt can have the same next versionCode.
                # Require this invocation to produce its own artifact instead
                # of trusting an old app-release.apk with matching metadata.
                if apk.exists():
                    os.replace(apk, Path(folder) / "previous-build.apk")
                defines = Path(folder) / "defines.json"
                defines.write_text(
                    json.dumps(
                        {
                            "DEEPSEEK_API_KEY": config.api_key,
                            "DEEPSEEK_BASE_URL": config.base_url,
                            "DEEPSEEK_MODEL": config.model,
                        }
                    ),
                    encoding="utf-8",
                )
                _run(
                    [
                        str(flutter),
                        "build",
                        "apk",
                        "--release",
                        f"--build-name={name}",
                        f"--build-number={number}",
                        f"--dart-define-from-file={defines}",
                    ],
                    cwd=app,
                    env=environment,
                    emit=output,
                )
            if not apk.is_file() or apk.stat().st_size == 0:
                raise ReleaseError("构建没有生成有效 APK；已有发布包未修改。")
            digest = _sha256(apk)
            inspected = _inspect_apk(apk, aapt, apksigner, app=app, env=environment, emit=output)
            if (inspected["version_code"], inspected["version_name"]) != (number, name):
                raise ReleaseError("APK 包名或版本不符，拒绝把旧产物当作新版本发布。")
            if inspected["certificate_sha256"] != expected_certificate:
                raise ReleaseError("APK 签名与永久发布证书不一致，无法覆盖升级；旧安装包未修改。")
            if pubspec.read_text(encoding="utf-8") != previous_text:
                raise ReleaseError("构建期间版本文件发生变化，请重新发布。")
            if prepared_pool_sha256 is not None:
                final_pool = validate_prepared_pool(data / "couple_chat.db", pool_path)
                if final_pool["sha256"] != prepared_pool_sha256:
                    raise ReleaseError("构建期间已审查短句池发生变化，停止发布。")
            releases.mkdir(parents=True, exist_ok=True)
            _publish(apk, destination)
            updated_text = VERSION.sub(
                lambda match: f"{match[1]}{name}+{number}{match[5]}", previous_text, count=1
            )
            version_saved = True
            try:
                _save_version(pubspec, updated_text)
            except OSError:
                version_saved = False
                output("APK 已发布，但版本文件未写回；下次会从已有发布包继续递增。")
            result = {
                "status": "released",
                "apk": str(destination),
                "version_name": name,
                "version_code": number,
                "sha256": digest,
                "bytes": destination.stat().st_size,
                "certificate_sha256": inspected["certificate_sha256"],
                "pubspec_updated": version_saved,
                "memory": memory,
                "daily_pool": daily_pool,
            }
            output(json.dumps(result, ensure_ascii=False, indent=2))
            return result
    except AlreadyRunning:
        raise ReleaseError("已有聊天维护或发布正在运行，请等待完成。") from None
    except Exception as error:
        raise ReleaseError(_redact(error, secrets)) from None


def main() -> int:
    # Keep older shortcuts safe: they now run the same complete workflow, not a
    # second, stale-data packaging route. No CLI prepared/lock bypass switches.
    from maintenance.build import main as build_main

    return build_main()


if __name__ == "__main__":
    raise SystemExit(main())
