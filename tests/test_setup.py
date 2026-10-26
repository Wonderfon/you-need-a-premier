import json
import shutil
import tempfile
import unittest
from pathlib import Path

from orgcore import check, config
from orgcore import setup as guided
from orgcore.config import SETUP_BLOCKS

from .support import run_cli

ANSWERS = [
    ["identity", {"organization_name": "Sample Org", "owner_name": "Sam Owner", "instance_id": "sample-org"}],
    ["goal", {"goal": "Keep a small garden and a bike in good shape."}],
    ["lines", {"lines": [{"id": "home", "name": "Home"}, {"id": "study", "name": "Study"}]}],
    ["projects", {"projects": [{"id": "garden", "name": "Garden", "line": "home", "path": None},
                               {"id": "bike", "name": "Bike", "line": "home"},
                               {"id": "course", "name": "Course", "line": "study", "path": "notes/course"}]}],
    ["weights", {"lines": {"home": 60, "study": 40}, "projects": {"garden": 50, "bike": 50, "course": 100}}],
    ["time", {"timezone": "UTC"}],
    ["reporting", {"report_times": ["09:00", "21:00"], "quiet_hours": {"start": "23:00", "end": "08:00"}, "preferred": "local"}],
    ["spending", {"currency": "USD", "period": "month", "cap": 0}],
    ["subscription", {"windows": [{"name": "5-hour", "reserve_percent": 20, "max_share_percent": 50,
                                   "max_observation_age_minutes": 30}], "max_concurrency": 1}],
    ["rules", {"text": "Ask me before anything irreversible."}],
    ["install", {"auth_method": "claude.ai"}],
    ["confirm", {"confirmed": True}],
]


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orgcore-setup-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "inst"
        guided.start(self.root)

    def fill(self, upto=None):
        for block, answer in ANSWERS:
            if block == upto:
                break
            guided.save(self.root, block, answer)

    def status(self, block):
        return guided.load_state(self.root)["blocks"][block]["status"]

    def errors(self, block, answer, **kw):
        with self.assertRaises(guided.SetupError) as caught:
            guided.save(self.root, block, answer, **kw)
        return "\n".join(caught.exception.errors)


class PaceLine(SetupCase):
    def paced(self, **extra):
        window = {**ANSWERS[8][1]["windows"][0], **extra}
        return {**ANSWERS[8][1], "windows": [window]}

    def test_the_coordinator_is_told_to_ask_about_the_pace_line(self):
        prompt = (Path(__file__).resolve().parent.parent / "prompts" / "setup-coordinator.md").read_text(encoding="utf-8")
        self.assertIn("pace line on", prompt)
        self.assertIn("pacing_slack_percent", prompt)

    def test_a_paced_window_is_saved_and_reaches_policy_json(self):
        self.fill(upto="subscription")
        guided.save(self.root, "subscription", self.paced(length_hours=5, pacing_slack_percent=5))
        for block, answer in ANSWERS[9:]:
            guided.save(self.root, block, answer)
        guided.compose(self.root)
        window = json.loads((self.root / "policy.json").read_text(encoding="utf-8"))["subscription_windows"][0]
        self.assertEqual((window["length_hours"], window["pacing_slack_percent"]), (5, 5))

    def test_one_of_the_two_pace_fields_alone_is_refused(self):
        self.fill(upto="subscription")
        self.assertIn("go together", self.errors("subscription", self.paced(length_hours=5)))


class Checkpoint(SetupCase):
    def test_start_is_idempotent_and_asks_identity_first(self):
        state = guided.start(self.root)
        self.assertEqual(state["pending_question"], "identity")
        self.assertEqual(state["revision"], 0)
        self.fill(upto="time")
        self.assertEqual(guided.start(self.root)["pending_question"], "time")  # start never resets saved answers

    def test_each_save_advances_the_next_question_and_binds_the_hash(self):
        guided.save(self.root, *ANSWERS[0])
        state = guided.load_state(self.root)
        self.assertEqual(state["pending_question"], "goal")
        self.assertEqual(state["blocks"]["identity"], {"status": "accepted", "hash": config.block_hash(ANSWERS[0][1])})
        self.assertEqual(state["revision"], 1)

    def test_resume_comes_from_files_not_from_a_session(self):
        self.fill(upto="spending")
        text = "\n".join(guided.describe(self.root))  # a new process reads only the instance directory
        self.assertIn("Next question: spending", text)
        self.assertIn("identity     accepted", text)

    def test_saved_answers_are_never_replaced_without_change(self):
        guided.save(self.root, *ANSWERS[0])
        other = {**ANSWERS[0][1], "organization_name": "Other"}
        self.assertIn("already saved and is kept", self.errors("identity", other))
        self.assertEqual(json.loads(guided.show(self.root, "identity"))["identity"], ANSWERS[0][1])
        guided.save(self.root, "identity", other, change=True)
        self.assertEqual(json.loads(guided.show(self.root, "identity"))["identity"]["organization_name"], "Other")
        [old] = (self.root / "state" / "answers" / "history").glob("identity.*.json")
        self.assertEqual(json.loads(old.read_text(encoding="utf-8")), ANSWERS[0][1])

    def test_saving_the_same_answer_again_with_change_is_a_no_op(self):
        guided.save(self.root, *ANSWERS[0])
        revision = guided.load_state(self.root)["revision"]
        self.assertIn("unchanged", guided.save(self.root, *ANSWERS[0], change=True))
        self.assertEqual(guided.load_state(self.root)["revision"], revision)

    def test_a_bad_answer_changes_nothing(self):
        before = (self.root / "state" / "setup.json").read_bytes()
        self.assertIn("instance_id: must be lowercase", self.errors("identity", {**ANSWERS[0][1], "instance_id": "Has Space"}))
        self.assertIn("organization_name: missing required field", self.errors("identity", {"owner_name": "x", "instance_id": "x"}))
        self.assertEqual((self.root / "state" / "setup.json").read_bytes(), before)
        self.assertFalse((self.root / "state" / "answers" / "identity.json").exists())

    def test_there_is_no_field_a_credential_could_go_into(self):
        found = self.errors("identity", {**ANSWERS[0][1], "api_key": "sk-not-real"})
        self.assertIn("api_key: unknown field", found)
        self.assertNotIn("sk-not-real", found)

    def test_an_interrupted_save_is_finished_by_accept(self):
        self.fill(upto="goal")
        path = self.root / "state" / "answers" / "goal.json"  # the process died after writing the answer, before the checkpoint
        path.write_text(json.dumps(ANSWERS[1][1]), encoding="utf-8")
        self.assertIn("goal         pending  <- an answer is on disk but not accepted", "\n".join(guided.describe(self.root)))
        self.assertEqual(guided.accept(self.root, "goal"), "goal: accepted")
        self.assertEqual(guided.load_state(self.root)["pending_question"], "lines")

    def test_an_answer_edited_behind_the_writer_is_not_trusted(self):
        self.fill(upto="time")
        path = self.root / "state" / "answers" / "goal.json"
        path.write_text(json.dumps({"goal": "edited by hand"}), encoding="utf-8")
        text = "\n".join(guided.describe(self.root))
        self.assertIn("goal         needs_action  <- the saved answer was changed outside the setup writer", text)
        self.assertEqual(guided.load_state(self.root)["blocks"]["goal"], {"status": "needs_action", "hash": None})

    def test_changing_lines_sends_the_blocks_that_depend_on_it_back(self):
        self.fill(upto="time")
        message = guided.save(self.root, "lines", {"lines": [{"id": "home", "name": "Home"}]}, change=True)
        self.assertIn("projects, weights", message)
        self.assertEqual((self.status("projects"), self.status("weights"), self.status("goal")),
                         ("needs_action", "needs_action", "accepted"))
        self.assertIn("projects[2].line: 'study' is not a saved line", "\n".join(guided.describe(self.root)) + self.errors(
            "projects", ANSWERS[3][1], change=True))

    def test_every_block_has_a_validator_and_the_list_matches_the_schema(self):
        self.assertEqual([b for b, _ in ANSWERS], list(SETUP_BLOCKS))


class TimeZone(SetupCase):
    def test_an_unresolvable_zone_needs_action_and_offers_utc_or_tzdata(self):
        self.fill(upto="time")
        found = self.errors("time", {"timezone": "Nowhere/Land"})
        self.assertIn('change it to "UTC"', found)
        self.assertIn("tzdata", found)
        self.assertEqual(self.status("time"), "needs_action")
        self.assertEqual(guided.load_state(self.root)["pending_question"], "time")
        guided.save(self.root, "time", {"timezone": "UTC"})  # the owner picks UTC
        self.assertEqual(self.status("time"), "accepted")


class Compose(SetupCase):
    def test_compose_writes_a_valid_instance_and_a_second_run_writes_nothing(self):
        self.fill()
        written = guided.compose(self.root)
        self.assertEqual(written, ["org.json", "policy.json", "reporting.json", "projects/backlog.md"])
        errors, instance = check.check_instance(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(instance.org["lines"][0]["projects"][0]["weight"], 50)
        self.assertEqual(instance.org["lines"][1]["projects"][0]["path"], "notes/course")
        self.assertFalse(instance.policy["live_enabled"])
        self.assertEqual(guided.load_state(self.root)["phase"], "validated")
        self.assertEqual(guided.compose(self.root), [])

    def test_compose_waits_for_every_block(self):
        self.fill(upto="rules")
        self.assertIn("not accepted yet: rules, install, confirm", "\n".join(self._compose_errors()))
        self.assertFalse((self.root / "org.json").exists())

    def test_confirm_needs_the_other_blocks(self):
        self.fill(upto="rules")
        self.assertIn("confirm: these blocks are not accepted yet: rules, install", self.errors("confirm", {"confirmed": True}))

    def test_an_existing_file_that_differs_is_never_replaced(self):
        self.fill()
        (self.root / "org.json").write_text('{"hand": "written"}', encoding="utf-8")
        self.assertIn("org.json already exists and differs", "\n".join(self._compose_errors()))
        self.assertEqual((self.root / "org.json").read_text(encoding="utf-8"), '{"hand": "written"}')

    def _compose_errors(self):
        with self.assertRaises(guided.SetupError) as caught:
            guided.compose(self.root)
        return caught.exception.errors


class Command(SetupCase):
    def test_save_through_the_command_line_and_resume_with_status(self):
        target = self.tmp / "cli"
        self.assertEqual(run_cli("setup", target, "start").returncode, 0)
        done = run_cli("setup", target, "save", "identity", "--json", json.dumps(ANSWERS[0][1]))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "identity: saved"), done.stderr)
        again = run_cli("setup", target, "save", "identity", "--json", json.dumps(ANSWERS[0][1]))
        self.assertEqual(again.returncode, 1)
        self.assertIn("already saved", again.stderr)
        status = run_cli("setup", target, "status")
        self.assertIn("Next question: goal", status.stdout)
        broken = run_cli("setup", target, "save", "goal", "--json", "{nope")
        self.assertEqual(broken.returncode, 1)
        self.assertIn("not valid JSON", broken.stderr)


if __name__ == "__main__":
    unittest.main()
