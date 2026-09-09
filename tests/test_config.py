"""Loading `.env`: what is parsed, what wins, and what must not leak."""

import os
import tempfile
import unittest

from pipeline.config import load_dotenv, parse_env

KEY = "SERPER_API_KEY"
SECRET = "s3rper-not-a-real-key"


class TestParsing(unittest.TestCase):
    def test_a_plain_assignment(self):
        self.assertEqual(parse_env("A=1"), {"A": "1"})

    def test_export_prefixed_lines_are_accepted(self):
        """The form people paste out of shell instructions."""
        self.assertEqual(parse_env("export A=1"), {"A": "1"})

    def test_comments_and_blank_lines_are_skipped(self):
        self.assertEqual(parse_env("# note\n\n  \nA=1\n"), {"A": "1"})

    def test_whitespace_around_the_equals_is_trimmed(self):
        self.assertEqual(parse_env("  A  =  1  "), {"A": "1"})

    def test_double_quotes_are_stripped(self):
        self.assertEqual(parse_env('A="has spaces"'), {"A": "has spaces"})

    def test_single_quotes_are_stripped(self):
        self.assertEqual(parse_env("A='v'"), {"A": "v"})

    def test_only_one_layer_of_quotes_comes_off(self):
        self.assertEqual(parse_env("""A='"v"'"""), {"A": '"v"'})

    def test_mismatched_quotes_are_left_alone(self):
        self.assertEqual(parse_env("""A='v\""""), {"A": "'v\""})

    def test_a_line_with_no_equals_is_skipped_not_raised(self):
        """A stray line must not stop a run that needs no credential."""
        self.assertEqual(parse_env("nonsense\nA=1"), {"A": "1"})

    def test_a_line_with_no_name_is_skipped(self):
        self.assertEqual(parse_env("=orphan\nA=1"), {"A": "1"})

    def test_an_empty_value_is_kept(self):
        """An unfilled template should read as 'set but blank', not absent."""
        self.assertEqual(parse_env("A="), {"A": ""})

    def test_a_value_may_contain_equals_signs(self):
        self.assertEqual(parse_env("A=b=c"), {"A": "b=c"})

    def test_interpolation_is_not_performed(self):
        """Deliberately unsupported: a `.env` that computes must be traced."""
        self.assertEqual(parse_env("A=${B}"), {"A": "${B}"})


class EnvIsolated(unittest.TestCase):
    """Every test here restores the process environment it started with."""

    def setUp(self):
        before = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(before)

        self.addCleanup(restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        path = os.path.join(self.tmp.name, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


class TestLoading(EnvIsolated):
    def test_a_name_is_placed_in_the_environment(self):
        load_dotenv(self.write(f"{KEY}={SECRET}\n"))
        self.assertEqual(os.environ[KEY], SECRET)

    def test_the_count_of_names_set_is_returned(self):
        self.assertEqual(load_dotenv(self.write("A=1\nB=2\n")), 2)

    def test_an_existing_environment_variable_is_not_overwritten(self):
        """`KEY=... python3 run.py` and CI secrets must still win."""
        os.environ[KEY] = "from-the-shell"
        loaded = load_dotenv(self.write(f"{KEY}={SECRET}\n"))
        self.assertEqual(os.environ[KEY], "from-the-shell")
        self.assertEqual(loaded, 0)

    def test_a_missing_file_is_success(self):
        """No vendor configured is the common case, not an error."""
        self.assertEqual(load_dotenv(os.path.join(self.tmp.name, "absent")), 0)

    def test_a_directory_in_place_of_the_file_does_not_raise(self):
        self.assertEqual(load_dotenv(self.tmp.name), 0)

    def test_undecodable_bytes_do_not_stop_the_caller(self):
        path = os.path.join(self.tmp.name, ".env")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe\x00binary")
        self.assertEqual(load_dotenv(path), 0)

    def test_a_malformed_file_still_yields_its_good_lines(self):
        loaded = load_dotenv(self.write("garbage\n%%%\nA=1\n"))
        self.assertEqual((loaded, os.environ["A"]), (1, "1"))


class TestTheKeyDoesNotReachTheDatabase(EnvIsolated):
    """The reason the key is environment-only.

    `run.py` writes `vars(args)` into the `runs` table as JSON, so anything on
    the command line is persisted. `--env-file` carries a path; the credential
    itself must never appear in the parsed arguments.
    """

    def test_the_parsed_args_contain_the_path_and_not_the_secret(self):
        import run

        path = self.write(f"{KEY}={SECRET}\n")
        args = run.build_parser().parse_args(
            ["--phase", "2", "--env-file", path, "--search-provider", "serper"])
        load_dotenv(args.env_file)

        self.assertEqual(os.environ[KEY], SECRET)
        self.assertEqual(args.env_file, path)
        self.assertNotIn(SECRET, [str(v) for v in vars(args).values()])

    def test_the_secret_is_absent_from_the_serialised_args(self):
        """`start_run` persists exactly this JSON."""
        import json

        import run

        path = self.write(f"{KEY}={SECRET}\n")
        args = run.build_parser().parse_args(["--env-file", path])
        load_dotenv(args.env_file)
        self.assertNotIn(SECRET, json.dumps(vars(args), default=str))


class TestTheProviderReadsWhatWasLoaded(EnvIsolated):
    def test_a_key_from_the_file_reaches_the_provider(self):
        from pipeline.search import SerperProvider

        os.environ.pop(KEY, None)
        load_dotenv(self.write(f"{KEY}={SECRET}\n"))
        provider = SerperProvider(cache_dir=self.tmp.name, delay=0)
        self.assertEqual(provider.api_key, SECRET)

    def test_an_unfilled_template_still_reports_a_missing_key(self):
        """`SERPER_API_KEY=` should read as absent, not as a blank key."""
        from pipeline.search import SearchProviderError, SerperProvider

        os.environ.pop(KEY, None)
        load_dotenv(self.write(f"{KEY}=\n"))
        with self.assertRaises(SearchProviderError):
            SerperProvider(cache_dir=self.tmp.name, delay=0)


if __name__ == "__main__":
    unittest.main()
