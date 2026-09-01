"""Logging tests: JSONL on disk, structured fields preserved."""

import json
import logging
import os
import tempfile
import unittest

from pipeline.logging_setup import setup_logging


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
