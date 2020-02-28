# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
r"""A commandline tool to discover and run all the absltest.TestCase in a dir.

Usage:
python -m tfx_bsl.test_util.run_all_tests \
    --start_dirs=<comma separated dirs to search for tests>
"""
from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

import multiprocessing
import os
import subprocess
import sys
import tempfile
import time

from absl import app
from absl import flags
from absl import logging
import six
from typing import Dict, List, Optional, Text


flags.DEFINE_list(
    "start_dirs", None,
    "Comma separated directories to recursively search test "
    "modules from. Required.")
flags.DEFINE_string("python", None,
                    "path to Python binary. If not set, use the binary that "
                    "runs this script.")
flags.DEFINE_integer(
    "parallelism", None, "number of sub-processes to run tests at "
    " the same time.")
flags.DEFINE_list("sharded_tests", None,
                  "Comma separated sharded tests, in the format of "
                  "\"<file_name>:<num_shards>\". Note that the test must "
                  "implement Bazel's test sharding protocol.")

FLAGS = flags.FLAGS
_TEST_FILENAME_SUFFIX = "_test.py"


class _Test(object):
  """Represents a test (a python executable)."""

  def __init__(self, path: Text, shard_id: int, total_shards: int):
    self.path = path
    self.subprocess = None
    self.stdout = None
    self.stderr = None
    self.begin_time = None
    self.finish_time = None
    self.shard_id = shard_id
    self.total_shards = total_shards

  def __str__(self):
    return "%s [shard %d of %d shard(s)]" % (
        self.path, self.shard_id, self.total_shards)

  def Run(self) -> None:
    """Run the test in a subprocess."""
    logging.info("Running %s in a subprocess...", self)
    self.stdout = tempfile.TemporaryFile()
    self.stderr = tempfile.TemporaryFile()
    self.begin_time = time.time()
    env = os.environ.copy()
    # Bazel's test sharding protocol:
    # https://docs.bazel.build/versions/master/test-encyclopedia.html
    if self.total_shards > 1:
      env["TEST_TOTAL_SHARDS"] = str(self.total_shards)
      env["TEST_SHARD_INDEX"] = str(self.shard_id)

    self.subprocess = subprocess.Popen(
        [_GetPython(), self.path], stdout=self.stdout, stderr=self.stderr,
        env=env)

  def Finished(self) -> bool:
    assert self.subprocess is not None
    finished = self.subprocess.poll() is not None
    if finished and self.finish_time is None:
      self.finish_time = time.time()
    return finished

  def Succeeded(self) -> bool:
    assert self.subprocess is not None
    return self.subprocess.poll() == 0

  def PrintLogs(self) -> None:
    """Prints stdout and stderr outputs of the test."""
    assert self.Finished()
    for f, stream_name in (
        (self.stdout, "STDOUT"), (self.stderr, "STDERR")):
      f.flush()
      f.seek(0)
      if six.PY2:
        sys.stdout.write("BEGIN %s of test %s\n" % (stream_name, self))
        sys.stdout.write(f.read())
        sys.stdout.write("END %s of test %s\n" % (stream_name, self))
        sys.stdout.flush()
      else:
        # Since we collected binary data, we have to write binary data.
        encoded = (stream_name.encode(), str(self).encode())
        sys.stdout.buffer.write(b"BEGIN %s of test %s\n" % encoded)
        sys.stdout.buffer.write(f.read())
        sys.stdout.buffer.write(b"END %s of test %s\n" % encoded)
        sys.stdout.buffer.flush()


def _DiscoverTests(root_dirs: List[Text],
                   test_to_shards: Dict[Text, int]) -> List[_Test]:
  """Finds tests under `root_dirs`. Creates and returns _Test objects."""
  result = []
  for d in root_dirs:
    for root, _, files in os.walk(d):
      for f in files:
        if f.endswith(_TEST_FILENAME_SUFFIX):
          shards = test_to_shards.get(f, 1)
          for shard in range(0, shards):
            result.append(_Test(os.path.join(root, f), shard, shards))
  logging.info("Discovered %d tests", len(result))
  return result


def _RunTests(tests: List[_Test], parallelism: int) -> bool:
  """Run tests. Returns True if all tests succeeded."""
  running_tests = []
  for t in tests:
    while len(running_tests) >= parallelism:
      time.sleep(0.2)  # 200ms
      running_tests = [rt for rt in running_tests if not rt.Finished()]
    t.Run()
    running_tests.append(t)

  while running_tests:
    time.sleep(0.2)  # 200ms
    running_tests = [t for t in running_tests if not t.Finished()]

  failed_tests = [t for t in tests if not t.Succeeded()]
  logging.info("Ran %d tests. %d failed.", len(tests), len(failed_tests))
  for t in tests:
    logging.info("%s\t%s\t%.1fs", t,
                 "PASSED" if t.Succeeded() else "FAILED",
                 t.finish_time - t.begin_time)
  logging.flush()

  for ft in failed_tests:
    ft.PrintLogs()

  return not failed_tests


def _GetPython() -> Text:
  return FLAGS.python if FLAGS.python else sys.executable


def _ParseShardedTests(
    sharded_tests_arg: Optional[List[Text]]) -> Dict[Text, int]:
  """Parses --sharded_tests argument."""
  result = {}
  if sharded_tests_arg is None:
    return result
  for arg in sharded_tests_arg:
    [file_name, num_shards_str] = arg.split(":")
    num_shards = int(num_shards_str)
    if num_shards <= 0:
      raise ValueError("Invalid num_shards %d for test %s" %
                       (num_shards, file_name))
    result[file_name] = num_shards
  return result


def main(argv):
  del argv
  test_to_shards = _ParseShardedTests(FLAGS.sharded_tests)
  tests = _DiscoverTests(FLAGS.start_dirs, test_to_shards)
  parallelism = FLAGS.parallelism
  if parallelism is None:
    parallelism = multiprocessing.cpu_count()
  logging.info("Parallelism = %d", parallelism)
  logging.info("Using Python: %s", _GetPython())
  all_succeeded = _RunTests(tests, parallelism)
  return 0 if all_succeeded else -1


if __name__ == "__main__":
  flags.mark_flag_as_required("start_dirs")
  app.run(main)