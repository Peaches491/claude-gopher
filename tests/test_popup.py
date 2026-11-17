from claude_gopher import popup


class Done:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


def fake_tmux(monkeypatch, replies):
    calls = []

    def run(*argv):
        calls.append(argv)
        for key, reply in replies.items():
            if argv[: len(key)] == key:
                return Done(reply)
        return Done()

    monkeypatch.setattr(popup, "tmux", run)
    return calls


def test_client_for_pane_skips_popup_clients(monkeypatch):
    fake_tmux(monkeypatch, {
        ("display-message", "-p", "-t", "%1", "#{window_id}"): "@5\n",
        ("list-clients",): "/dev/pts/9\t@5\tgopher\n/dev/pts/2\t@5\t0\n",
    })
    assert popup.client_for_pane("%1") == "/dev/pts/2"


def test_cleanup_command_restores_window_and_options():
    command = popup.cleanup_command("/tmp/sock", "@5", "%3", unzoom=True)
    assert "set -wu -t @5 window-style" in command
    assert all(f"set -gu {option}" in command for option in popup.OPTIONS)
    assert "resize-pane -Z -t %3" in command
    assert "resize-pane" not in popup.cleanup_command("/tmp/sock", "@5", "%3", unzoom=False)


def test_attach_command_uses_grouped_session_for_agents(monkeypatch):
    fake_tmux(monkeypatch, {
        ("display-message", "-p", "-t", "%3", "#{session_name}"): "0\n",
        ("display-message", "-p", "-t", "%3", "#{window_id}"): "@7\n",
    })
    assert popup.attach_command("%3", "gopher") == "TMUX= tmux attach -t '=gopher'"
    command = popup.attach_command("%3", "agent")
    assert "new-session -t '=0' -s gopher-peek-" in command and "destroy-unattached on" in command and "select-window -t @7" in command


def test_autoclose_only_for_the_target_pane(monkeypatch):
    calls = fake_tmux(monkeypatch, {
        ("show", "-gv", popup.AUTOCLOSE_OPTION): "1\n",
        ("show", "-gv", popup.TARGET_OPTION): "%3\n",
        ("list-clients",): "/dev/pts/9\tgopher-peek-1\n/dev/pts/2\t0\n",
    })
    assert not popup.autoclose_after_submit("%4")
    assert popup.autoclose_after_submit("%3")
    assert ("detach-client", "-t", "/dev/pts/9") in calls
    assert ("detach-client", "-t", "/dev/pts/2") not in calls


def test_jump_from_popup_switches_outer_client(monkeypatch):
    calls = fake_tmux(monkeypatch, {
        ("show", "-gv", popup.CLIENT_OPTION): "/dev/pts/2\n",
        ("list-clients",): "/dev/pts/9\tgopher\n/dev/pts/2\t0\n",
    })
    popup.jump_to("%9")
    assert ("switch-client", "-c", "/dev/pts/2", "-t", "%9") in calls
    assert ("detach-client", "-t", "/dev/pts/9") in calls


def test_jump_without_popup(monkeypatch):
    calls = fake_tmux(monkeypatch, {("list-clients",): "/dev/pts/2\t0\n"})
    popup.jump_to("%9")
    assert calls[-1] == ("switch-client", "-t", "%9")


def test_command_popup_runs_in_checkout_and_restores_dim(monkeypatch):
    calls = fake_tmux(monkeypatch, {
        ("display-message", "-p", "#{socket_path}"): "/tmp/sock\n",
        ("display-message", "-p", "-t", "%1", "#{window_id}"): "@5\n",
    })
    launched = []
    monkeypatch.setattr(popup.subprocess, "Popen", lambda argv, **kwargs: launched.append(argv))
    popup.command_popup("%1", "/co", "git status", {"GOPHER_PROJECT": "alpha"}, title=" Status ", client="/dev/pts/2")
    assert ("set", "-w", "-t", "@5", "window-style", popup.DIM_STYLE) in calls
    argv = launched[0]
    assert argv[argv.index("-d") + 1] == "/co"
    assert {"GOPHER_PROJECT=alpha"} == {argv[i + 1] for i, a in enumerate(argv) if a == "-e"}
    assert "git status" in argv[-1] and "set -wu -t @5 window-style" in argv[-1]


def test_guarded_restores_even_when_interrupted(tmp_path):
    marker = tmp_path / "cleaned"
    script = popup.guarded("kill -INT $$; sleep 5", f"touch {marker}")
    result = popup.subprocess.run(script, shell=True, capture_output=True, text=True, timeout=10)
    assert marker.exists() and result.returncode == 130
