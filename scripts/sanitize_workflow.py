#!/usr/bin/env python3
"""Strip secrets and personal data from exported n8n workflow JSON files.

Usage:
    scripts/sanitize_workflow.py FILE...          rewrite files in place
    scripts/sanitize_workflow.py --check FILE...  exit 1 if anything would change

What gets replaced:
  * values of parameters whose name looks like a secret (apiKey, token,
    password, secret, authorization, ...) -> YOUR_<NODE>_KEY style placeholders
  * secret-looking query params inside URLs (?apiKey=...&token=...)
  * fromEmail / toEmail / ccEmail / bccEmail / replyTo -> placeholders
  * any other literal e-mail address -> you@example.com
  * pinData (pinned execution data can hold real API responses) -> {}
  * meta.instanceId (identifies your n8n install) -> removed

n8n expressions (values starting with "=") are left alone, since they
reference data at runtime rather than holding a literal secret. Credential
references ({"id": ..., "name": ...}) are also kept: the actual secrets live
encrypted inside n8n and are never exported.
"""
import json
import re
import sys

SECRET_NAME = re.compile(
    r"(api[_-]?key|apikey|token|secret|passw(or)?d|authorization|auth[_-]?key|access[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_SECRET_PARAM = re.compile(
    r"([?&](?:api[_-]?key|apikey|key|token|access_token|secret|password)=)[^&#\s\"]+",
    re.IGNORECASE,
)
EMAIL_FIELDS = {
    "fromEmail": "FROM EMAIL",
    "toEmail": "TO EMAIL",
    "ccEmail": "CC EMAIL",
    "bccEmail": "BCC EMAIL",
    "replyTo": "REPLY TO EMAIL",
}
# Well-known token formats; anything matching after sanitizing is reported.
KNOWN_TOKENS = re.compile(
    r"(gsk_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AIza[0-9A-Za-z_-]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|Bearer\s+[A-Za-z0-9._-]{20,})"
)
EMAIL_PLACEHOLDER = "you@example.com"


def is_expression(value):
    return isinstance(value, str) and value.startswith("=")


def placeholder(node_name, field_name):
    node = re.sub(r"[^A-Za-z0-9]+", "_", node_name).strip("_").upper() or "NODE"
    lowered = field_name.lower()
    if "token" in lowered:
        kind = "TOKEN"
    elif "pass" in lowered:
        kind = "PASSWORD"
    elif "secret" in lowered:
        kind = "SECRET"
    else:
        kind = "KEY"
    return f"YOUR_{node}_{kind}"


def scrub_string(value):
    if is_expression(value):
        return value
    value = URL_SECRET_PARAM.sub(r"\1REDACTED", value)
    return EMAIL.sub(EMAIL_PLACEHOLDER, value)


def scrub(obj, node_name):
    """Recursively sanitize a node's parameters."""
    if isinstance(obj, dict):
        # {"name": "apiKey", "value": "..."} pairs used by query/header params.
        name = obj.get("name")
        if (
            isinstance(name, str)
            and SECRET_NAME.search(name)
            and isinstance(obj.get("value"), str)
            and obj["value"]
            and not is_expression(obj["value"])
        ):
            obj["value"] = placeholder(node_name, name)

        for key, value in obj.items():
            if key in EMAIL_FIELDS and isinstance(value, str) and not is_expression(value):
                obj[key] = EMAIL_FIELDS[key]
            elif (
                isinstance(value, str)
                and SECRET_NAME.search(key)
                and value
                and not is_expression(value)
            ):
                obj[key] = placeholder(node_name, key)
            elif isinstance(value, str):
                obj[key] = scrub_string(value)
            else:
                scrub(value, node_name)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                obj[i] = scrub_string(item)
            else:
                scrub(item, node_name)


def sanitize(workflow):
    for node in workflow.get("nodes", []):
        scrub(node.get("parameters", {}), node.get("name", ""))
    if workflow.get("pinData"):
        workflow["pinData"] = {}
    if isinstance(workflow.get("meta"), dict):
        workflow["meta"].pop("instanceId", None)
    return workflow


def process(path, check):
    with open(path, encoding="utf-8") as f:
        original = f.read()
    try:
        workflow = json.loads(original)
    except json.JSONDecodeError:
        return False, []  # not JSON; nothing to do
    if not isinstance(workflow, dict) or "nodes" not in workflow:
        return False, []  # not an n8n workflow export

    output = json.dumps(sanitize(workflow), indent=2, ensure_ascii=False)
    if original.endswith("\n"):
        output += "\n"

    leftovers = sorted(set(m.group(0)[:8] + "..." for m in KNOWN_TOKENS.finditer(output)))
    changed = output != original
    if changed and not check:
        with open(path, "w", encoding="utf-8") as f:
            f.write(output)
    return changed, leftovers


def main(argv):
    check = "--check" in argv
    paths = [a for a in argv if a != "--check"]
    if not paths:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    status = 0
    for path in paths:
        changed, leftovers = process(path, check)
        if changed:
            print(f"{'needs sanitizing' if check else 'sanitized'}: {path}")
            if check:
                status = 1
        for token in leftovers:
            print(f"possible secret left in {path}: {token}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
