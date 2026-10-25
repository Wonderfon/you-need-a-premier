"""Stand-in for the official CLI, used only by tests. It makes no network call and needs no sign-in.

Behaviour comes from environment variables:
    FAKE_CLAUDE_LOG    file to which every launch appends one JSON line (arguments, working directory, prompt file facts)
    FAKE_CLAUDE_AUTH   JSON printed by `auth status --json` (default: signed in with a subscription login)
    FAKE_CLAUDE_SAVE   JSON file with [[block, answer], ...]; a launch saves the first FAKE_CLAUDE_STEPS of them that
                       are not accepted yet, the way an interview session would, through the real setup writer
    FAKE_CLAUDE_STEPS  how many blocks one launch saves (default 0)
    FAKE_CLAUDE_FLAGS  words listed by `--help` (default: every option the workers rely on)
    FAKE_CLAUDE_ROLES  set: an executor session writes out.txt and names it, a reviewer session answers VERDICT:
                       FAKE_CLAUDE_VERDICT (default pass); each worker launch logs its role, prompt file and session id
The identity fields in the default auth output are made up; tests assert they are never echoed back.
"""
import json
import os
import sys

DEFAULT_AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                "email": "someone@example.invalid", "orgName": "Example Org", "subscriptionType": "max"}


DEFAULT_FLAGS = ("--append-system-prompt-file --add-dir --allowedTools --disallowedTools --permission-mode dontAsk "
                 "--session-id --output-format --settings")


def main(argv):
    if argv[:1] == ["--version"]:
        print("9.9.9 (fake)")
        return 0
    if argv[:2] == ["auth", "status"]:
        print(os.environ.get("FAKE_CLAUDE_AUTH") or json.dumps(DEFAULT_AUTH))
        return 0
    if argv[:1] == ["--help"]:
        print("Usage: claude [options] [prompt]\n" + "\n".join(
            f"  {flag}  (fake)" for flag in os.environ.get("FAKE_CLAUDE_FLAGS", DEFAULT_FLAGS).split()))
        return 0
    if argv[:1] == ["-p"]:  # a worker session: log the launch, write the named file, answer like `--output-format json`
        prompt = os.path.basename(argv[argv.index("--append-system-prompt-file") + 1])
        role = "reviewer" if prompt.startswith("reviewer") else "executor"
        with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "env_names": sorted(os.environ), "role": role,
                                  "prompt": prompt, "session": argv[argv.index("--session-id") + 1]}) + "\n")
        if os.environ.get("FAKE_CLAUDE_WRITE"):
            open(os.environ["FAKE_CLAUDE_WRITE"], "w", encoding="utf-8").write("Summary: done\nSources: none\n")
        result = os.environ.get("FAKE_CLAUDE_RESULT", "")
        if os.environ.get("FAKE_CLAUDE_ROLES"):  # answer each role the way its prompt asks
            if role == "executor":
                open("out.txt", "w", encoding="utf-8").write("Summary: done\n")
                result = "Summary: done\nEVIDENCE: out.txt"
            else:
                result = "Reads fine.\nVERDICT: " + os.environ.get("FAKE_CLAUDE_VERDICT", "pass")
        print(json.dumps({"result": result}))
        return int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))
    prompt_file = argv[argv.index("--append-system-prompt-file") + 1]
    root = argv[argv.index("--add-dir") + 1]
    text = open(prompt_file, encoding="utf-8").read()
    record = {"argv": argv, "cwd": os.getcwd(), "pythonpath": os.environ.get("PYTHONPATH", ""),
              "prompt_has_placeholder": "{{" in text, "prompt_names_instance": root in text}
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    steps = int(os.environ.get("FAKE_CLAUDE_STEPS", "0"))
    if steps:
        from pathlib import Path
        from orgcore import setup as guided
        answers = json.load(open(os.environ["FAKE_CLAUDE_SAVE"], encoding="utf-8"))
        state = guided.load_state(Path(root))
        for block, answer in answers:
            if steps and state["blocks"][block]["status"] != "accepted":
                guided.save(Path(root), block, answer)
                steps -= 1
    return int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
