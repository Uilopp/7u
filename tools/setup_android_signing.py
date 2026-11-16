"""Create 7u's permanent private-release identity without printing passwords."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess

from maintenance.paths import ROOT


def main() -> None:
    properties = ROOT / "app/android/key.properties"
    signing_dir = ROOT / "data/release-signing"
    keystore = signing_dir / "7u-release.p12"
    backup_properties = signing_dir / "key.properties"
    if any(path.exists() for path in (properties, keystore, backup_properties)):
        raise SystemExit("Signing files already exist; refusing to replace the release identity.")
    keytool = shutil.which("keytool")
    if keytool is None:
        raise SystemExit("Install a JDK with keytool before creating the release identity.")
    signing_dir.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(36)
    environment = {**os.environ, "SEVENU_SIGNING_PASSWORD": password}
    result = subprocess.run(
        [
            keytool,
            "-genkeypair",
            "-noprompt",
            "-storetype",
            "PKCS12",
            "-keystore",
            str(keystore),
            "-alias",
            "seven-u-release",
            "-keyalg",
            "RSA",
            "-keysize",
            "4096",
            "-validity",
            "10000",
            "-dname",
            "CN=7u, O=7u, C=CN",
            "-storepass:env",
            "SEVENU_SIGNING_PASSWORD",
            "-keypass:env",
            "SEVENU_SIGNING_PASSWORD",
        ],
        env=environment,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit("keytool failed; inspect the signing directory before retrying.")
    # Relative to android/app, keeping the identity usable when the repo moves.
    content = (
        "storeFile=../../../data/release-signing/7u-release.p12\n"
        f"storePassword={password}\n"
        "keyAlias=seven-u-release\n"
        f"keyPassword={password}\n"
    )
    for destination in (properties, backup_properties):
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    print(
        json.dumps(
            {
                "status": "created",
                "keystore": str(keystore),
                "configuration": str(properties),
                "backup_directory": str(signing_dir),
                "note": "Copy the whole backup directory to safe offline storage; never send it with the APK.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
