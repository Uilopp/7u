"""Explicit, billable host smoke test using the current private model config.

Run ``python -m tools.qa.check_live_followup --reverse`` for the two regression
questions plus their reverse followup. Default Flutter tests skip this test.
All model messages and answers stay in ignored data/, never app assets. The
key is DPAPI-loaded and passed only through an automatically deleted defines
file, not command arguments or diagnostic logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from maintenance.model_config import load_model_config
from maintenance.paths import ROOT
from maintenance.release import _redact, _tool_paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reverse", action="store_true", help="Also ask the reversed third question (uses API credits)."
    )
    args = parser.parse_args()
    try:
        config = load_model_config(ROOT / "data/model_config.dpapi")
    except ValueError, OSError, RuntimeError:
        raise SystemExit("无法读取模型配置；请使用当前 Windows 账户在维护窗口重新保存 Key。") from None
    if config is None:
        raise SystemExit("尚未保存模型 Key；请先在维护窗口配置。未调用模型。")

    app = ROOT / "app"
    flutter, _ = _tool_paths(app)
    if os.name == "nt" and flutter.suffix.lower() != ".bat":
        windows_flutter = flutter.with_name("flutter.bat")
        if windows_flutter.is_file():
            flutter = windows_flutter
    output = ROOT / "data" / ("live-followup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    output.mkdir()
    files = (
        app / "lib/core/question_agent.dart",
        app / "lib/core/question_history.dart",
        app / "lib/core/model_client.dart",
        app / "lib/core/memory_pack.dart",
        app / "assets/agent/prompt.md",
        app / "assets/memory/memory_pack.db",
        ROOT / "agent/prompt.md",
    )
    inputs = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    (output / "inputs.json").write_text(json.dumps(inputs, indent=2), encoding="utf-8")
    print("正在显式运行真实模型测试，将产生 API 费用；完整对话只保存在本机 ignored data。", flush=True)

    with tempfile.TemporaryDirectory(prefix=".live-followup-", dir=ROOT / "data") as temporary:
        defines = Path(temporary) / "defines.json"
        defines.write_text(
            json.dumps(
                {
                    "DEEPSEEK_API_KEY": config.api_key,
                    "DEEPSEEK_BASE_URL": config.base_url,
                    "DEEPSEEK_MODEL": config.model,
                    "LIVE_FOLLOWUP_TEST": True,
                    "LIVE_FOLLOWUP_REVERSE": args.reverse,
                    "LIVE_FOLLOWUP_OUTPUT": str(output),
                }
            ),
            encoding="utf-8",
        )
        command = [
            str(flutter),
            "test",
            "test/live_followup_test.dart",
            "--reporter=expanded",
            f"--dart-define-from-file={defines}",
        ]
        process = subprocess.Popen(
            command,
            cwd=app,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert process.stdout is not None
        with (output / "runner.log").open("w", encoding="utf-8") as log:
            for line in process.stdout:
                safe = _redact(line, (config.api_key,))
                log.write(safe)
                log.flush()
                print(safe, end="", flush=True)
        status = process.wait()
    print(json.dumps({"exit_code": status, "private_report_directory": str(output)}), flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
