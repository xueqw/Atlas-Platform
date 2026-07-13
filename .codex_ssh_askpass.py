import os
import sys

prompt = " ".join(sys.argv[1:]).lower()
counter_file = os.environ.get("CODEX_ASKPASS_COUNTER")
call_count = 0
if counter_file:
    try:
        with open(counter_file, "r", encoding="utf-8") as fh:
            call_count = int((fh.read() or "0").strip())
    except OSError:
        call_count = 0
    with open(counter_file, "w", encoding="utf-8") as fh:
        fh.write(str(call_count + 1))

if any(token in prompt for token in ("mfa", "otp", "code", "token", "验证码", "verification")) or call_count > 0:
    log_value = "otp"
    print(os.environ.get("CODEX_SSH_OTP", ""))
else:
    log_value = "password"
    print(os.environ.get("CODEX_SSH_PASSWORD", ""))

log_file = os.environ.get("CODEX_ASKPASS_LOG")
if log_file:
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(f"call={call_count + 1} value={log_value} prompt={prompt!r}\n")
