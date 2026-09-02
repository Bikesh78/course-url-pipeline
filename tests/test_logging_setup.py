"""Logging tests: JSONL on disk, structured fields preserved."""

import json
import logging
import os
import tempfile
import unittest

from pipeline.logging_setup import (
    prune_old_runs, set_current_site, setup_logging,
)


class LoggingCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)
            h.close()
        self._dir.cleanup()

    def _lines(self, path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class TestJsonlOutput(LoggingCase):
    def test_writes_one_json_object_per_line(self):
        path = setup_logging("r1", self._dir.name, quiet=True)
        logging.getLogger("t").info("hello")
        logging.shutdown()
        lines = self._lines(path)
        self.assertTrue(any(r["msg"] == "hello" for r in lines))

    def test_extra_fields_are_promoted_to_top_level(self):
        """This is the point of the format: runs must be greppable by field."""
        path = setup_logging("r2", self._dir.name, quiet=True)
        logging.getLogger("t").info(
            "site done", extra={"site_key": "aber.ac.uk", "candidates": 809})
        logging.shutdown()
        rec = next(r for r in self._lines(path) if r["msg"] == "site done")
        self.assertEqual(rec["site_key"], "aber.ac.uk")
        self.assertEqual(rec["candidates"], 809)

    def test_every_record_is_timestamped_and_levelled(self):
        path = setup_logging("r3", self._dir.name, quiet=True)
        logging.getLogger("t").warning("thin")
        logging.shutdown()
        rec = next(r for r in self._lines(path) if r["msg"] == "thin")
        self.assertEqual(rec["level"], "WARNING")
        self.assertIn("T", rec["ts"])

    def test_unserialisable_extra_does_not_lose_the_record(self):
        path = setup_logging("r4", self._dir.name, quiet=True)
        logging.getLogger("t").info("odd", extra={"obj": object()})
        logging.shutdown()
        rec = next(r for r in self._lines(path) if r["msg"] == "odd")
        self.assertIsInstance(rec["obj"], str)

    def test_exception_is_captured(self):
        path = setup_logging("r5", self._dir.name, quiet=True)
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("t").exception("failed")
        logging.shutdown()
        rec = next(r for r in self._lines(path) if r["msg"] == "failed")
        self.assertIn("ValueError", rec["exc"])


class TestReadableFileLog(LoggingCase):
    """The readable log must land in logs/ without a shell redirect.

    Relying on one put `stage1.log` in the repo root, untracked and unignored —
    and a redirect cannot name a file after a run_id that does not exist until
    the process starts.
    """

    def test_both_forms_are_written(self):
        path = setup_logging("r10", self._dir.name, quiet=True)
        logging.shutdown()
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(os.path.join(self._dir.name, "r10.log")))

    def test_readable_log_holds_the_console_line_only(self):
        setup_logging("r11", self._dir.name, quiet=True)
        logging.getLogger("run").info("  [1/100] Somewhere  filled=3/4",
                                      extra={"site_key": "x.ac.uk"})
        logging.shutdown()
        with open(os.path.join(self._dir.name, "r11.log"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("[1/100] Somewhere  filled=3/4", body)
        # Structured fields belong in the JSONL, not here.
        self.assertNotIn("site_key", body)

    def test_it_is_named_for_the_run(self):
        setup_logging("run-abc", self._dir.name, quiet=True)
        logging.shutdown()
        self.assertTrue(os.path.exists(
            os.path.join(self._dir.name, "run-abc.log")))

    def test_quiet_still_writes_the_file(self):
        """--dry-run silences the console; the file is not the console."""
        setup_logging("r12", self._dir.name, quiet=True)
        logging.getLogger("run").info("still recorded")
        logging.shutdown()
        with open(os.path.join(self._dir.name, "r12.log"), encoding="utf-8") as fh:
            self.assertIn("still recorded", fh.read())


class TestHandlerSetup(LoggingCase):
    def test_log_file_is_named_for_the_run(self):
        path = setup_logging("run-abc", self._dir.name, quiet=True)
        self.assertTrue(path.endswith("run-abc.jsonl"))
        self.assertTrue(os.path.exists(path))

    def test_calling_twice_does_not_duplicate_records(self):
        setup_logging("first", self._dir.name, quiet=True)
        path = setup_logging("second", self._dir.name, quiet=True)
        logging.getLogger("t").info("once")
        logging.shutdown()
        self.assertEqual(
            sum(1 for r in self._lines(path) if r["msg"] == "once"), 1)

    def test_verbose_admits_debug_records(self):
        path = setup_logging("v", self._dir.name, quiet=True, verbose=True)
        logging.getLogger("t").debug("detail")
        logging.shutdown()
        self.assertTrue(any(r["msg"] == "detail" for r in self._lines(path)))

    def test_default_level_excludes_debug_from_console_but_not_file(self):
        # The file handler is DEBUG so a trace survives even at default level.
        path = setup_logging("d", self._dir.name, quiet=True)
        logging.getLogger("t").debug("quiet detail")
        logging.shutdown()
        self.assertFalse(any(r["msg"] == "quiet detail"
                             for r in self._lines(path)),
                         "root level gates it before the handler sees it")


if __name__ == "__main__":
    unittest.main()


class TestSiteContext(LoggingCase):
    """`site_key` reaches every record without being passed as a parameter."""

    def test_it_reaches_a_record_from_another_module(self):
        path = setup_logging("s1", self._dir.name, quiet=True)
        set_current_site("aber.ac.uk")
        logging.getLogger("pipeline.catalog").info("seeds found")
        logging.shutdown()
        rec = next(r for r in self._lines(path) if r["msg"] == "seeds found")
        self.assertEqual(rec["site_key"], "aber.ac.uk")

    def test_an_explicit_site_key_is_not_overwritten(self):
        path = setup_logging("s2", self._dir.name, quiet=True)
        set_current_site("aber.ac.uk")
        logging.getLogger("run").info("x", extra={"site_key": "other.ac.uk"})
        logging.shutdown()
        rec = next(r for r in self._lines(path) if r["msg"] == "x")
        self.assertEqual(rec["site_key"], "other.ac.uk")

    def test_each_worker_thread_carries_its_own(self):
        """16 workers run concurrently; records must not cross-contaminate."""
        import threading
        path = setup_logging("s3", self._dir.name, quiet=True)
        started = threading.Barrier(4)

        def work(site):
            set_current_site(site)
            started.wait()                      # force real interleaving
            for _ in range(20):
                logging.getLogger("pipeline.catalog").info("tick")

        threads = [threading.Thread(target=work, args=(f"s{i}.ac.uk",))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        logging.shutdown()

        seen = {r.get("site_key") for r in self._lines(path)
                if r["msg"] == "tick"}
        self.assertEqual(seen, {f"s{i}.ac.uk" for i in range(4)})


class TestPruneOldRuns(LoggingCase):
    """Retention: bounded `logs/`, without deleting the run in progress."""

    def _make(self, run, when):
        for suffix in (".jsonl", ".log", ".log.1"):
            path = os.path.join(self._dir.name, run + suffix)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.utime(path, (when, when))

    def test_keeps_the_requested_number(self):
        for i in range(6):
            self._make(f"2026-09-0{i + 1}T10-00-00Z-r{i}", 1000 + i * 100)
        removed = prune_old_runs(self._dir.name, keep=3)
        self.assertEqual(len(removed), 3)
        self.assertEqual(len({f.split(".")[0]
                              for f in os.listdir(self._dir.name)}), 3)

    def test_a_run_is_removed_whole(self):
        """Never leave a run half-deleted with one form missing."""
        self._make("2026-09-01T10-00-00Z-old", 1000)
        self._make("2026-09-02T10-00-00Z-new", 2000)
        prune_old_runs(self._dir.name, keep=1)
        left = os.listdir(self._dir.name)
        self.assertFalse(any("old" in f for f in left), left)
        self.assertEqual(sorted(f.rsplit("-", 1)[-1] for f in left),
                         ["new.jsonl", "new.log", "new.log.1"])

    def test_the_current_run_is_never_removed(self):
        for i in range(5):
            self._make(f"2026-09-0{i + 1}T10-00-00Z-r{i}", 1000 + i * 100)
        # The current run is also the oldest, so ordering alone would doom it.
        removed = prune_old_runs(self._dir.name, keep=1,
                                 current="2026-09-01T10-00-00Z-r0")
        self.assertNotIn("2026-09-01T10-00-00Z-r0", removed)
        self.assertTrue(os.path.exists(
            os.path.join(self._dir.name, "2026-09-01T10-00-00Z-r0.jsonl")))

    def test_oldest_is_by_mtime_not_by_name(self):
        """The two id formats do not sort against each other.

        "2026-09-02..." < "20260901..." because "-" precedes "0", so a lexical
        sort would delete the newest logs first whenever both are present.
        """
        self._make("20260801T100000Z-old", 1000)          # older, compact id
        self._make("2026-09-02T10-00-00Z-new", 5000)      # newer, readable id
        removed = prune_old_runs(self._dir.name, keep=1)
        self.assertEqual(removed, ["20260801T100000Z-old"])

    def test_keep_zero_prunes_nothing(self):
        self._make("2026-09-01T10-00-00Z-a", 1000)
        self._make("2026-09-02T10-00-00Z-b", 2000)
        self.assertEqual(prune_old_runs(self._dir.name, keep=0), [])
        self.assertEqual(len(os.listdir(self._dir.name)), 6)

    def test_an_absent_directory_is_a_no_op(self):
        self.assertEqual(prune_old_runs("/nonexistent/logs", keep=3), [])

    def test_an_empty_directory_is_a_no_op(self):
        self.assertEqual(prune_old_runs(self._dir.name, keep=3), [])


class TestVerbosity(LoggingCase):
    def test_debug_is_absent_by_default(self):
        path = setup_logging("v1", self._dir.name, quiet=True)
        logging.getLogger("pipeline.catalog").debug("per-page detail")
        logging.shutdown()
        self.assertFalse(any(r["msg"] == "per-page detail"
                             for r in self._lines(path)))

    def test_debug_is_present_when_verbose(self):
        path = setup_logging("v2", self._dir.name, quiet=True, verbose=True)
        logging.getLogger("pipeline.catalog").debug("per-page detail")
        logging.shutdown()
        self.assertTrue(any(r["msg"] == "per-page detail"
                            for r in self._lines(path)))
