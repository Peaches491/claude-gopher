import datetime
import io
import json
import subprocess

import pytest

from claude_gopher import schedule
from claude_gopher.cli import main
from claude_gopher.config import load_config
from claude_gopher.roots import create_project, create_root


def run(*argv, cwd):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "root"
    create_root(root)
    return load_config(root)


def at(text: str) -> float:
    return datetime.datetime.fromisoformat(text).timestamp()


def write(config, name: str, text: str) -> None:
    (config.schedule_dir / f"{name}.md").write_text(text)


def test_new_roots_get_a_schedule_with_save_and_update_check(config):
    names = sorted(path.name for path in config.schedule_dir.iterdir())
    assert names == ["README.md", "save.md", "update-check.md"]
    tasks, problems = schedule.load_tasks(config)
    assert problems == {}
    by_name = {task.name: task for task in tasks}
    assert by_name["save"].every == 2 * 3600 and by_name["save"].run == "gopher save"
    assert by_name["update-check"].at == (0, 3) and by_name["update-check"].timing() == "at 00:03"
    assert run("git", "ls-files", "schedule", cwd=config.root).splitlines() == ["schedule/README.md", "schedule/save.md", "schedule/update-check.md"]


def test_parse_task_validates_fields(config):
    write(config, "bad", "---\nevery: soon\n---\nx\n")
    write(config, "both", "---\nevery: 1h\nat: 09:00\n---\nx\n")
    write(config, "typo", "---\nevry: 1h\n---\nx\n")
    write(config, "days", "---\nat: 09:00\ndays: mon,funday\n---\nx\n")
    _, problems = schedule.load_tasks(config)
    assert "10m, 2h or 1d" in problems["bad"]
    assert "exactly one" in problems["both"]
    assert "unknown field evry" in problems["typo"]
    assert "days must be" in problems["days"]


def test_every_task_runs_its_command_then_waits(config):
    write(config, "save", "---\nevery: 2h\nrun: echo saved\n---\nReport it.\n")
    (config.schedule_dir / "update-check.md").unlink()
    now = at("2026-11-18T10:00:00")
    text = schedule.tick(config, now).text()
    assert "## save (every 2h)\n$ echo saved  [exit 0]\nsaved\nReport it." in text
    assert schedule.tick(config, now + 3600).sections == []
    assert "## save" in schedule.tick(config, now + 2 * 3600).text()


def test_at_task_waits_for_its_time_and_catches_up_once(config):
    (config.schedule_dir / "save.md").unlink()
    (config.schedule_dir / "update-check.md").unlink()
    write(config, "standup", '---\nat: "09:00"\ndays: weekdays\n---\nPost the standup.\n')
    tuesday_early = at("2026-11-17T08:00:00")
    assert "standup" not in schedule.tick(config, tuesday_early).text()
    assert "Post the standup." in schedule.tick(config, at("2026-11-17T09:20:00")).text()
    assert "standup" not in schedule.tick(config, at("2026-11-17T15:00:00")).text()
    assert "Post the standup." in schedule.tick(config, at("2026-11-21T10:00:00")).text()
    assert "standup" not in schedule.tick(config, at("2026-11-22T10:00:00")).text()
    assert "Post the standup." in schedule.tick(config, at("2026-11-24T09:05:00")).text()


def test_first_sight_of_an_at_task_does_not_fire_a_past_slot(config):
    (config.schedule_dir / "save.md").unlink()
    write(config, "nightly", '---\nat: "00:03"\n---\nCheck.\n')
    assert "nightly" not in schedule.tick(config, at("2026-11-18T10:00:00")).text()
    assert "Check." in schedule.tick(config, at("2026-11-19T00:13:00")).text()


def test_until_ends_a_task_and_reports_it_once(config):
    (config.schedule_dir / "save.md").unlink()
    write(config, "watch", "---\nevery: 10m\nuntil: 2026-11-18\n---\nRead the thread.\n")
    assert "Read the thread." in schedule.tick(config, at("2026-11-18T23:00:00")).text()
    text = schedule.tick(config, at("2026-11-19T09:00:00")).text()
    assert "watch has ended" in text and "Read the thread." not in text
    assert "watch" not in schedule.tick(config, at("2026-11-19T09:30:00")).text()


def test_disabled_tasks_and_problems_are_reported_once(config):
    write(config, "save", "---\nevery: 2h\nenabled: false\nrun: echo saved\n---\n")
    write(config, "broken", "---\nevery: never\n---\n")
    now = at("2026-11-18T10:00:00")
    text = schedule.tick(config, now).text()
    assert "saved" not in text and "Problem in schedule/broken.md" in text
    assert "broken" not in schedule.tick(config, now + 600).text()


def test_dry_run_records_nothing(config):
    write(config, "save", "---\nevery: 2h\nrun: echo saved\n---\n")
    now = at("2026-11-18T10:00:00")
    assert "would run: echo saved" in schedule.tick(config, now, dry_run=True).text()
    assert "$ echo saved" in schedule.tick(config, now).text()


def test_stale_projects_are_events_at_the_proactive_level(config):
    (config.schedule_dir / "save.md").unlink()
    (config.schedule_dir / "update-check.md").unlink()
    project = create_project(config.projects_dir, "old")
    (project / "STATUS.md").write_text("---\nsummary: x\nstate: active\nupdated: 2026-11-09\n---\n")
    now = at("2026-11-18T10:00:00")
    assert schedule.tick(config, now).sections == []
    path = config.root / "config.toml"
    path.write_text(path.read_text() + '\n[gopher]\nproactivity = "proactive"\n')
    proactive = load_config(config.root)
    text = schedule.tick(proactive, now + 60).text()
    assert "Stale project: old (STATUS.md last updated 2026-11-09, 7 working days ago)" in text
    assert schedule.tick(proactive, now + 120).sections == []


def test_backup_overdue_is_an_event_once_per_episode(config, monkeypatch):
    (config.schedule_dir / "update-check.md").unlink()
    write(config, "save", "---\nevery: 2h\nenabled: false\n---\n")
    write(config, "save", "---\nevery: 2h\nrun: true\n---\n")
    now = at("2026-11-18T10:00:00")
    stale = schedule.rootgit.BackupStatus(last_commit=now - 5 * 3600, changed=3, added=9, removed=1, unpushed=0)
    monkeypatch.setattr(schedule.rootgit, "backup_status", lambda root: stale)
    assert "Backup overdue: saved 5h ago (due every 2h)" in schedule.tick(config, now).text()
    assert "Backup overdue" not in schedule.tick(config, now + 600).text()


def test_heartbeat_summary_and_listing(config, capsys, monkeypatch):
    now = at("2026-11-18T10:00:00")
    assert schedule.heartbeat_summary(config, now) == ("no tick yet", False)
    write(config, "save", "---\nevery: 2h\nrun: true\n---\n")
    schedule.tick(config, now)
    text, stalled = schedule.heartbeat_summary(config, now + 300)
    assert text == "last tick 5m ago, next: save in 1h 55m" and not stalled
    assert schedule.heartbeat_summary(config, now + 3600)[1]
    monkeypatch.setenv("GOPHER_HOME", str(config.root))
    assert main(["schedule"]) == 0
    out = capsys.readouterr().out
    assert "save          every 2h" in out and "update-check  at 00:03" in out


def test_schedule_tick_cli_prints_nothing_when_idle(config, capsys, monkeypatch):
    monkeypatch.setenv("GOPHER_HOME", str(config.root))
    (config.schedule_dir / "save.md").unlink()
    assert main(["schedule", "tick"]) == 0
    assert capsys.readouterr().out == ""


def test_existing_roots_get_a_schedule_on_first_tick(config):
    for path in config.schedule_dir.iterdir():
        path.unlink()
    config.schedule_dir.rmdir()
    text = schedule.tick(config, at("2026-11-18T10:00:00")).text()
    assert "## Schedule created" in text and (config.schedule_dir / "save.md").is_file()


def test_save_interval_follows_the_save_task(config):
    assert schedule.save_interval_hours(config) == 2
    write(config, "save", "---\nevery: 30m\nrun: gopher save\n---\n")
    assert schedule.save_interval_hours(config) == 0.5
    write(config, "save", "---\nevery: 30m\nenabled: false\n---\n")
    assert schedule.save_interval_hours(config) == 0


def answers(*replies):
    queue = list(replies)
    asked = []

    def ask(prompt, default=None):
        asked.append(prompt)
        reply = queue.pop(0)
        return reply or default or ""

    return ask, asked


def test_timing_lines_accept_intervals_times_and_days():
    assert schedule.timing_lines("30m") == ["every: 30m"]
    assert schedule.timing_lines("weekdays at 09:00") == ['at: "09:00"', "days: weekdays"]
    assert schedule.timing_lines("mon,thu 17:30") == ['at: "17:30"', "days: mon,thu"]
    with pytest.raises(schedule.ScheduleError):
        schedule.timing_lines("soonish")
    with pytest.raises(schedule.ScheduleError):
        schedule.timing_lines("2h 09:00")


def test_interview_keeps_changes_drops_and_adds(config):
    ask, asked = answers("4h", "n", "standup", "whenever", "weekdays 09:00", "Remind me to post the standup.", "")
    said = []
    files = schedule.interview(config, ask, said.append)
    assert sorted(files) == ["save", "standup"]
    assert files["save"].startswith("---\nevery: 4h\nrun: gopher save\nnotify: failure\n---\n")
    assert files["standup"] == '---\nat: "09:00"\ndays: weekdays\n---\nRemind me to post the standup.\n'
    assert any("say when" in line for line in said)
    assert said[-1].startswith("Schedule: save, standup.")


def test_interview_defaults_keep_everything(config):
    ask, _ = answers("", "", "")
    assert sorted(schedule.interview(config, ask, lambda line: None)) == ["save", "update-check"]


def test_new_root_with_chosen_schedule_commits_it(tmp_path, monkeypatch):
    for key, value in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                       "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": "/dev/null"}.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "chosen"
    create_root(root, schedule_files={"standup": schedule.task_text("09:00", "Standup.")})
    assert run("git", "ls-files", "schedule", cwd=root).splitlines() == ["schedule/README.md", "schedule/standup.md"]


def test_heartbeat_compact(config):
    now = at("2026-11-18T10:00:00")
    assert schedule.heartbeat_compact(config, now) == ("\U0001f552 off", False)
    write(config, "save", "---\nevery: 2h\nrun: true\n---\n")
    schedule.tick(config, now)
    assert schedule.heartbeat_compact(config, now + 300) == ("\U0001f552 5m", False)
    write(config, "standup", '---\nat: "11:00"\n---\nStandup.\n')
    assert schedule.heartbeat_compact(config, now + 300) == ("\U0001f552 5m  \u27a1\ufe0f  standup in 55m", False)


def test_notify_modes_keep_routine_runs_quiet(config):
    (config.schedule_dir / "update-check.md").unlink()
    write(config, "save", "---\nevery: 1h\nrun: echo saved\nnotify: failure\n---\nReport it.\n")
    write(config, "watch", "---\nevery: 1h\nrun: cat state.txt\nnotify: change\n---\nSay what changed.\n")
    (config.root / "state.txt").write_text("v1\n")
    now = at("2026-11-18T10:00:00")
    assert schedule.tick(config, now).sections == []
    assert schedule.tick(config, now + 3600).sections == []
    (config.root / "state.txt").write_text("v2\n")
    text = schedule.tick(config, now + 7200).text()
    assert "## watch" in text and "v2" in text and "## save" not in text
    write(config, "save", "---\nevery: 1h\nrun: exit 3\nnotify: failure\n---\nReport it.\n")
    assert "[exit 3]" in schedule.tick(config, now + 3 * 3600).text()
    write(config, "bad", "---\nevery: 1h\nnotify: sometimes\n---\n")
    assert "notify must be one of" in schedule.load_tasks(config)[1]["bad"]


def test_report_queue_and_take(config):
    schedule.queue_report(config, "# one")
    schedule.queue_report(config, "# two")
    state = schedule.load_state(config)
    state["notified_at"] = 5
    schedule.save_state(config, state)
    assert schedule.take_report(config) == "# one\n\n# two\n"
    assert schedule.take_report(config) == "" and "notified_at" not in schedule.load_state(config)


def test_input_is_empty_reads_the_prompt_line():
    assert schedule.input_is_empty("output\n\u2500\u2500\n\u276f \n\u2500\u2500\n  status")
    assert not schedule.input_is_empty("\u276f half a thought")
    assert not schedule.input_is_empty("no prompt here")


def test_deliver_types_only_when_gopher_is_idle_and_empty(config, monkeypatch):
    sent = []
    screens = {"value": "\u276f "}
    monkeypatch.setattr(schedule, "gopher_pane", lambda: "%3")
    monkeypatch.setattr(schedule, "gopher_idle", lambda config, pane: idle["value"])
    monkeypatch.setattr(schedule, "tmux_out", lambda *argv: sent.append(argv) or (screens["value"] if argv[0] == "capture-pane" else ""))
    idle = {"value": False}
    now = at("2026-11-18T10:00:00")
    assert not schedule.deliver(config, now)
    schedule.queue_report(config, "# due")
    assert not schedule.deliver(config, now)
    idle["value"] = True
    screens["value"] = "\u276f typing"
    assert not schedule.deliver(config, now)
    screens["value"] = "\u276f "
    assert schedule.deliver(config, now)
    assert ("send-keys", "-t", "%3", "-l", schedule.NOTIFY_TEXT) in sent
    assert not schedule.deliver(config, now + 60)
    assert schedule.deliver(config, now + schedule.RENOTIFY_SECONDS + 1)


def test_run_loop_ticks_queues_and_stops(config, monkeypatch):
    (config.schedule_dir / "update-check.md").unlink()
    write(config, "standup", "---\nevery: 1h\n---\nPost the standup.\n")
    monkeypatch.setattr(schedule, "deliver", lambda config, now: False)
    clock = iter([at("2026-11-18T10:00:00"), at("2026-11-18T10:00:30")])
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2

    logged = []
    schedule.run_loop(config, 600, stop=stop, clock=lambda: next(clock), sleep=lambda seconds: None, log=logged.append)
    report = schedule.take_report(config)
    assert report.count("Post the standup.") == 1
    assert len(logged) == 2
    assert logged[1].startswith("tick: ran save (exit ") and logged[1].endswith("), standup; queued a report for Gopher")


def test_hook_attaches_pending_report_to_gophers_next_prompt(config, monkeypatch, capsys):
    monkeypatch.setenv("GOPHER_HOME", str(config.root))
    monkeypatch.setenv("GOPHER_AGENT", "gopher")
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "g", "cwd": str(config.agent_dir)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook", "inbox"]) == 0
    assert capsys.readouterr().out == ""
    schedule.queue_report(config, "# due")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook", "inbox"]) == 0
    assert "gopher schedule report" in capsys.readouterr().out
