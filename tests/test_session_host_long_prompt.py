"""A launch whose system prompts pass Windows' command-line limit still starts:
the prompt arguments go to the CLI as files (WinError 206 otherwise)."""

from engine.session_host import ARGV_FILE_THRESHOLD, argv_with_prompt_files


def test_short_argv_is_left_alone(tmp_path):
    argv = ["claude", "--append-system-prompt", "be brief", "--model", "x"]
    out, files = argv_with_prompt_files(argv, tmp_path, "s1")
    assert out == argv and files == []


def test_long_prompts_become_files_holding_the_exact_text(tmp_path):
    sys_p = "Line one\r\nünïcode ✓ " + "a" * ARGV_FILE_THRESHOLD
    app_p = "rules\n" * 4000
    argv = ["claude", "--output-format", "stream-json", "--system-prompt", sys_p,
            "--append-system-prompt", app_p, "--model", "m"]
    out, files = argv_with_prompt_files(argv, tmp_path, "s1")
    assert out[:3] == ["claude", "--output-format", "stream-json"]
    assert out[3] == "--system-prompt-file" and out[5] == "--append-system-prompt-file"
    assert out[-2:] == ["--model", "m"]
    with open(out[4], encoding="utf-8", newline="") as fh:
        assert fh.read() == sys_p
    with open(out[6], encoding="utf-8", newline="") as fh:
        assert fh.read() == app_p
    assert len(files) == 2
    assert sum(len(a) + 3 for a in out) < 2000
