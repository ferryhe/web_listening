from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


def result(request: dict, **updates) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        key: request[key]
        for key in (
            "site_key", "site_skill_id", "site_skill_version", "site_skill_digest",
            "recipe_id", "run_id", "scope_id", "request_id", "executor_id",
        )
    }
    payload.update(schema_version="capture-result.v1", state="succeeded", started_at=now,
                   finished_at=now, final_url=request["url"], status_code=200,
                   content={"media_type": "text/html", "text": "captured", "metadata": {}},
                   error=None, metadata={})
    payload.update(updates)
    return payload


mode = sys.argv[1]
if mode == "stdin_block":
    time.sleep(30)
request = json.loads(sys.stdin.buffer.read())
if mode == "success":
    sys.stdout.write(json.dumps(result(request)))
elif mode == "environment":
    initial = open("/proc/self/environ", "rb").read().split(b"\0") if os.path.exists("/proc/self/environ") else []
    names = sorted(item.split(b"=", 1)[0].decode() for item in initial if item)
    sys.stdout.write(json.dumps(result(request, metadata={"environment_names": names})))
elif mode == "timeout":
    time.sleep(30)
elif mode == "tree":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    sys.stderr.write(f"child_pid={child.pid}")
    sys.stderr.flush()
    time.sleep(30)
elif mode == "tree_stdout_large":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    sys.stderr.write(f"child_pid={child.pid}")
    sys.stderr.flush()
    os.write(sys.stdout.fileno(), b"x" * 100_000)
elif mode == "leader_exit_pipes":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    sys.stderr.write(f"child_pid={child.pid}")
    sys.stderr.flush()
elif mode == "success_with_devnull_descendant":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sys.stdout.write(json.dumps(result(request, metadata={"child_pid": child.pid})))
elif mode in {"late_stdout_large", "late_stderr_large"}:
    stream = "stdout" if mode == "late_stdout_large" else "stderr"
    code = (
        "import os,time; time.sleep(.1); "
        f"os.write(1 if {stream!r} == 'stdout' else 2, b'x' * 100000); time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", code])
    sys.stderr.write(f"child_pid={child.pid}\n")
    sys.stderr.flush()
elif mode == "nonzero":
    sys.stderr.write("token=top-secret")
    raise SystemExit(7)
elif mode == "nonzero_zero_slash_url":
    sys.stderr.write("https:user:zero-secret@example.com/path")
    raise SystemExit(7)
elif mode == "nonzero_one_slash_url":
    sys.stderr.write("https:/user:one-secret@example.com/path")
    raise SystemExit(7)
elif mode == "stderr_credentials":
    sys.stderr.write(
        "Authorization: Bearer supersecret\n"
        "Authorization: Basic dXNlcjpwYXNz\n"
        "Bearer standalonesecret\n"
        "https://uri-user:uri-pass@example.com/path\n"
        '{"token":"top-secret","cookie":"cookie-secret","password":"pass-secret","apiKey":"api-secret"}\n'
        "token=token-secret cookie=cookie-assignment password=pass-assignment apiKey=api-assignment"
    )
    raise SystemExit(7)
elif mode == "stderr_convergence_credentials":
    sys.stderr.write(
        "Bearer standalone-url-secret\n"
        "https://url-user:url-password@example.com/private\n"
        "http://[\n"
        "AWS_ACCESS_KEY_ID=aws-id-secret\n"
        "awsSecretAccessKey=aws-secret-value\n"
        "PRIVATE_KEY=private-key-value\n"
        "clientSecret=client-secret-value\n"
        "authorization=Bearer authorization-secret\n"
    )
    raise SystemExit(7)
elif mode == "stderr_limit_credential":
    sys.stderr.write("x" * 48 + " https://cut-user:cut-password@example.com/private")
    raise SystemExit(7)
elif mode == "stderr_pem_private_key":
    sys.stderr.write(
        "before\n-----BEGIN RSA PRIVATE KEY-----\n"
        "pem-private-secret\n-----END RSA PRIVATE KEY-----\nafter"
    )
    raise SystemExit(7)
elif mode == "malformed":
    sys.stdout.write("not json")
elif mode == "mixed":
    sys.stdout.write("log line\n" + json.dumps(result(request)))
elif mode == "multiple":
    value = json.dumps(result(request))
    sys.stdout.write(value + value)
elif mode == "invalid_utf8":
    sys.stdout.buffer.write(b"\xff")
elif mode == "stdout_large":
    sys.stdout.write("x" * 100_000)
elif mode == "stderr_large":
    sys.stderr.write("password=hunter2 " + "x" * 100_000)
elif mode == "mismatch":
    sys.stdout.write(json.dumps(result(request, request_id="wrong")))
elif mode in {"duplicate_top", "duplicate_nested"}:
    value = json.dumps(result(request, metadata={"safe": "first"}))
    if mode == "duplicate_top":
        value = value.replace('"request_id":', '"request_id":"duplicate","request_id":', 1)
    else:
        value = value.replace('"safe":', '"safe":"duplicate","safe":', 1)
    sys.stdout.write(value)
elif mode == "excessively_nested":
    value = json.dumps(result(request))
    prefix, _separator, _metadata = value.rpartition('"metadata": {}')
    value = prefix + '"metadata": {"nested": ' + "[" * 1200 + '"token=nested-secret"' + "]" * 1200 + "}}"
    sys.stdout.write(value)
elif mode == "failed_secret":
    sys.stdout.write(json.dumps(result(
        request,
        state="failed",
        content=None,
        error={"code": "child_failed", "message": "token=child-top-secret", "retryable": False},
        final_url=None,
        status_code=None,
    )))
elif mode == "failed_unsafe_diagnostic":
    sys.stdout.write(json.dumps(result(
        request,
        state="failed",
        content=None,
        error={
            "code": "child_failed",
            "message": "http://[ clientSecret=child-client-secret\u0001",
            "retryable": False,
        },
        final_url=None,
        status_code=None,
    )))
elif mode == "failed_diagnostic_metadata":
    sys.stdout.write(json.dumps(result(
        request,
        state="failed",
        content=None,
        error={
            "code": "Unsafe Child Code!",
            "message": "child failed",
            "retryable": False,
            "metadata": {
                "nested": ["token=error-token-secret", {"detail": "password=error-password-secret"}],
                "url": "https:error-user:error-url-secret@example.com/private",
                "number": 17,
                "enabled": True,
            },
        },
        metadata={
            "nested": {"url": "https:/top-user:top-url-secret@example.com/private"},
            "detail": "token=top-metadata-secret",
            "items": [None, 23, False],
        },
        final_url=None,
        status_code=None,
    )))
elif mode == "failed_diagnostic_metadata_key":
    sys.stdout.write(json.dumps(result(
        request,
        state="failed",
        content=None,
        error={"code": "child_failed", "message": "child failed", "retryable": False},
        metadata={"https://key-user:key-secret@example.com/private": "safe"},
        final_url=None,
        status_code=None,
    )))
elif mode == "failed_diagnostic_metadata_key_collision":
    sys.stdout.write(json.dumps(result(
        request,
        state="failed",
        content=None,
        error={"code": "child_failed", "message": "child failed", "retryable": False},
        metadata={
            "https://first-user:first-secret@example.com/private": "first",
            "https://second-user:second-secret@example.com/private": "second",
        },
        final_url=None,
        status_code=None,
    )))
elif mode == "empty":
    pass
else:
    raise RuntimeError("fake executor exception")
