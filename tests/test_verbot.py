import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import verbot
from learn import sigmoid, train_and_gate


class LaunchRequestTests(unittest.TestCase):
    def test_natural_language_request(self):
        self.assertEqual(
            verbot.extract_launch_target("Can you open Firefox for me?"), "Firefox"
        )

    def test_rejects_non_launch_question(self):
        self.assertIsNone(verbot.extract_launch_target("What is Firefox?"))

    @patch("verbot.platform.system", return_value="Linux")
    @patch("verbot.shutil.which", return_value="/usr/bin/firefox")
    @patch("verbot.subprocess.Popen")
    def test_linux_launcher_uses_resolved_executable_without_shell(
        self, popen, _which, _system
    ):
        result = verbot.launch_application("Firefox")
        self.assertIn("Opened Firefox", result)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/firefox"])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))

    @patch("verbot.platform.system", return_value="Linux")
    @patch("verbot.shutil.which", return_value=None)
    @patch("verbot.linux_desktop_entries", return_value=[])
    def test_unavailable_application_returns_guidance(self, _entries, _which, _system):
        result = verbot.launch_application("firefox")
        self.assertIn("couldn’t find an installed app", result)

    @patch("verbot.platform.system", return_value="Linux")
    @patch("verbot.shutil.which", return_value=None)
    def test_finds_installed_desktop_app_by_display_name(self, _which, _system):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "music.desktop"
            entry.write_text(
                "[Desktop Entry]\nType=Application\nName=Music Studio\n"
                "Exec=/usr/bin/music-studio --name %c %U\n",
                encoding="utf-8",
            )
            with patch("verbot.linux_desktop_entries", return_value=[entry]), \
                 patch("verbot.subprocess.Popen") as popen:
                response = verbot.launch_application("Music Studio")
        self.assertEqual(response, "Opened Music Studio.")
        self.assertEqual(
            popen.call_args.args[0], ["/usr/bin/music-studio", "--name", "Music Studio"]
        )

    @patch("verbot.platform.system", return_value="Linux")
    @patch("verbot.shutil.which", return_value="/usr/bin/xdg-open")
    @patch("verbot.subprocess.Popen")
    def test_default_browser_opens_a_url(self, popen, _which, _system):
        response = verbot.launch_application("browser")
        self.assertIn("Opened", response)
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/xdg-open", "https://www.google.com"])

    @patch("verbot.platform.system", return_value="Linux")
    def test_dbus_desktop_app_uses_gtk_launcher(self, _system):
        with tempfile.TemporaryDirectory() as directory:
            apps = Path(directory) / "applications"
            apps.mkdir()
            entry = apps / "org.example.Editor.desktop"
            entry.write_text(
                "[Desktop Entry]\nType=Application\nName=Example Editor\n"
                "DBusActivatable=true\n",
                encoding="utf-8",
            )
            with patch("verbot.linux_desktop_entries", return_value=[entry]), \
                 patch("verbot.shutil.which", side_effect=lambda name: "/usr/bin/gtk-launch" if name == "gtk-launch" else None), \
                 patch("verbot.subprocess.Popen") as popen:
                result = verbot.launch_application("Example Editor")
        self.assertEqual(result, "Opened Example Editor.")
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/gtk-launch", "org.example.Editor"])

    @patch("verbot.platform.system", return_value="Windows")
    def test_windows_browser_uses_default_app(self, _system):
        with patch.object(verbot.os, "startfile", create=True) as startfile:
            result = verbot.launch_application("browser")
        self.assertIn("Windows default app", result)
        startfile.assert_called_once_with("https://www.google.com")

    @patch("verbot.platform.system", return_value="Windows")
    def test_windows_can_find_start_menu_shortcut(self, _system):
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            start_menu = appdata / "Microsoft/Windows/Start Menu/Programs"
            start_menu.mkdir(parents=True)
            shortcut = start_menu / "Sample Editor.lnk"
            shortcut.touch()
            with patch.dict(os.environ, {"APPDATA": str(appdata), "PROGRAMDATA": str(appdata / "ProgramData")}), \
                 patch.object(verbot.os, "startfile", create=True) as startfile:
                result = verbot.launch_application("Sample Editor")
        self.assertEqual(result, "Opened Sample Editor.")
        startfile.assert_called_once_with(str(shortcut))

    def test_splits_multiple_apps_and_keeps_app_names(self):
        self.assertEqual(
            verbot.extract_launch_targets("open Firefox and Spotify"),
            ["Firefox", "Spotify"],
        )

    def test_close_parser_does_not_match_ordinary_questions(self):
        self.assertEqual(verbot.extract_close_target("close the Telegram app"), "Telegram")
        self.assertIsNone(verbot.extract_close_target("How do I close Telegram?"))

    @patch("verbot.platform.system", return_value="Linux")
    @patch("verbot.subprocess.run")
    def test_close_uses_exact_process_match_and_reports_missing(self, run, _system):
        run.return_value.returncode = 1
        result = verbot.close_application("Firefox")
        self.assertIn("does not appear to be running", result)
        self.assertEqual(run.call_args.args[0], ["pkill", "-TERM", "-x", "firefox"])

    @patch("verbot.platform.system", return_value="Windows")
    @patch("verbot.subprocess.run")
    def test_windows_close_does_not_force_kill(self, run, _system):
        run.return_value.returncode = 0
        result = verbot.close_application("Firefox")
        self.assertIn("Closed Firefox", result)
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["taskkill", "/im"])
        self.assertNotIn("/f", args)

    @patch("verbot.launch_application", return_value="Opened telegram.")
    def test_telegram_message_request_never_claims_to_send(self, launch):
        result = verbot.handle_telegram_message_request("открой телеграм и напиши Амину lol")
        launch.assert_called_once_with("telegram")
        self.assertIn("did not send a message", result)

    @patch("builtins.input", side_effect=["Can you open Firefox for me?", "/exit"])
    @patch("verbot.platform.system", return_value="Linux")
    @patch("verbot.shutil.which", return_value="/usr/bin/firefox")
    @patch("verbot.subprocess.Popen")
    def test_natural_request_routes_without_model_call(
        self, popen, _which, _system, _input
    ):
        output = io.StringIO()
        with patch.object(verbot.Verbot, "ask", side_effect=AssertionError("model called")):
            with contextlib.redirect_stdout(output):
                verbot.main()
        self.assertIn("Opened Firefox", output.getvalue())
        popen.assert_called_once()


class OllamaErrorTests(unittest.TestCase):
    @patch("verbot.urllib.request.urlopen")
    @patch.object(verbot.Verbot, "ensure_ollama", return_value=None)
    def test_missing_model_404_suggests_pull_command(self, _ensure, urlopen):
        body = io.BytesIO(b'{"error":"model qwen2.5:1.5b not found, try pulling it first"}')
        urlopen.side_effect = urllib.error.HTTPError(
            "http://localhost:11434/api/chat", 404, "Not Found", {}, body
        )
        response = verbot.Verbot().ask("Hello")
        self.assertIn("Ollama is running, but model 'qwen2.5:1.5b' is not installed", response)
        self.assertIn("ollama pull qwen2.5:1.5b", response)

    @patch("verbot.urllib.request.urlopen")
    @patch.object(verbot.Verbot, "ensure_ollama", return_value=None)
    def test_unrelated_404_explains_endpoint(self, _ensure, urlopen):
        body = io.BytesIO(b"404 page not found")
        urlopen.side_effect = urllib.error.HTTPError(
            "http://localhost:11434/wrong", 404, "Not Found", {}, body
        )
        assistant = verbot.Verbot()
        assistant.url = "http://localhost:11434/wrong"
        response = assistant.ask("Hello")
        self.assertIn("endpoint may be wrong", response)
        self.assertIn("/api/chat", response)

    def test_starts_server_when_local_service_is_stopped(self):
        probe_results = iter([
            urllib.error.URLError(ConnectionRefusedError("connection refused")),
            None,
        ])
        output = io.StringIO()
        with patch("verbot.shutil.which", return_value="/usr/bin/ollama"), \
             patch("verbot.subprocess.Popen") as popen, \
             patch.object(verbot.Verbot, "_probe_ollama", side_effect=lambda *_: next(probe_results)), \
             contextlib.redirect_stdout(output):
            result = verbot.Verbot().ensure_ollama()
        self.assertIsNone(result)
        self.assertIn("starting it in the background", output.getvalue())
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/ollama", "serve"])

    @patch.object(verbot.Verbot, "ensure_ollama", return_value=None)
    @patch("verbot.urllib.request.urlopen")
    def test_chat_keeps_context_between_turns(self, urlopen, _ensure):
        urlopen.side_effect = [
            io.BytesIO(b'{"message":{"content":"Hello there."},"eval_count":2}'),
            io.BytesIO(b'{"message":{"content":"You said hello."},"eval_count":4}'),
        ]
        assistant = verbot.Verbot()
        self.assertEqual(assistant.ask("Hello"), "Hello there.")
        self.assertEqual(assistant.ask("What did I say?"), "You said hello.")
        second_payload = json.loads(urlopen.call_args_list[1].args[0].data)
        messages = second_payload["messages"]
        self.assertEqual(messages[-3]["content"], "Hello")
        self.assertEqual(messages[-2]["content"], "Hello there.")
        self.assertEqual(messages[-1]["content"], "What did I say?")

    @patch.object(verbot.Verbot, "ensure_ollama", return_value=None)
    @patch("verbot.urllib.request.urlopen")
    def test_custom_generate_endpoint_remains_supported(self, urlopen, _ensure):
        urlopen.return_value = io.BytesIO(b'{"response":"Generated reply","eval_count":3}')
        assistant = verbot.Verbot()
        assistant.url = "http://localhost:11434/api/generate"
        self.assertEqual(assistant.ask("Hi"), "Generated reply")
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["prompt"], "Hi")
        self.assertIn("system", payload)

    @patch.object(verbot.Verbot, "ensure_ollama", return_value=None)
    def test_invalid_model_url_returns_guidance(self, _ensure):
        assistant = verbot.Verbot()
        assistant.url = "not a URL"
        self.assertIn("not a valid", assistant.ask("Hi"))


class TokenAndMemoryTests(unittest.TestCase):
    def test_token_file_is_private_and_status_never_reveals_value(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("verbot.config_dir", return_value=Path(directory)), \
             patch.dict(os.environ, {"VERBOT_API_TOKEN": ""}):
            verbot.save_secret("sample-secret")
            self.assertEqual(verbot.read_secret(), "sample-secret")
            self.assertNotIn("sample-secret", verbot.show_token_status())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE((Path(directory) / "token").stat().st_mode), 0o600)
            verbot.save_secret(None)
            self.assertEqual(verbot.read_secret(), "")

    def test_clear_history_removes_context(self):
        assistant = verbot.Verbot()
        assistant.history.append({"role": "user", "content": "private context"})
        assistant.clear_history()
        self.assertEqual(assistant.history, [])

    @patch("verbot.update_memory")
    @patch("verbot.load_system_prompt", return_value="updated system prompt")
    @patch.object(verbot.Verbot, "ensure_ollama", return_value=None)
    @patch("verbot.urllib.request.urlopen")
    def test_memory_marker_is_saved_and_removed_from_reply(
        self, urlopen, _ensure, _prompt, update_memory
    ):
        urlopen.return_value = io.BytesIO(
            b'{"message":{"content":"I will remember. [MEMORY: concise replies]"},"eval_count":4}'
        )
        assistant = verbot.Verbot()
        response = assistant.ask("Please remember to be concise")
        self.assertEqual(response, "I will remember.")
        update_memory.assert_called_once_with("concise replies")
        self.assertEqual(assistant.system_prompt, "updated system prompt")


class LearningDemoTests(unittest.TestCase):
    def test_training_learns_and_gate(self):
        weights, bias = train_and_gate()
        predictions = [
            int(sigmoid(sum(x * w for x, w in zip(features, weights)) + bias) >= 0.5)
            for features in ((0, 0), (0, 1), (1, 0), (1, 1))
        ]
        self.assertEqual(predictions, [0, 0, 0, 1])


if __name__ == "__main__":
    unittest.main()
