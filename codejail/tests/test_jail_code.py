"""Test jail_code.py"""

import os
import shutil
import sys
import textwrap
import tempfile
import unittest

from nose.plugins.skip import SkipTest

from codejail.jail_code import jail_code, is_configured, Jail, configure, auto_configure

auto_configure()


def jailpy(code=None, *args, **kwargs):
    """Run `jail_code` on Python."""
    if code:
        code = textwrap.dedent(code)
    result = jail_code("python", code, *args, **kwargs)
    if isinstance(result.stdout, bytes):
        result.stdout = result.stdout.decode()
    if isinstance(result.stderr, bytes):
        result.stderr = result.stderr.decode()
    return result


def file_here(fname):
    """Return the full path to a file alongside this code."""
    return os.path.join(os.path.dirname(__file__), fname)


class JailCodeHelpers(unittest.TestCase):
    """Assert helpers for jail_code tests."""

    def setUp(self):
        super(JailCodeHelpers, self).setUp()
        if not is_configured("python"):
            raise SkipTest

    def assertResultOk(self, res):
        """Assert that `res` exited well (0), and had no stderr output."""
        self.assertEqual(res.stderr, "")
        self.assertEqual(res.status, 0)


class TestFeatures(JailCodeHelpers):
    """Test features of how `jail_code` runs Python."""

    def test_hello_world(self):
        res = jailpy(code="print('Hello, world!')")
        self.assertResultOk(res)
        self.assertEqual(res.stdout, 'Hello, world!\n')

    def test_argv(self):
        res = jailpy(
            code="import sys; print(':'.join(sys.argv[1:]))",
            argv=["Hello", "world", "-x"]
        )
        self.assertResultOk(res)
        self.assertEqual(res.stdout, "Hello:world:-x\n")

    def test_ends_with_exception(self):
        res = jailpy(code="""raise Exception('FAIL')""")
        self.assertNotEqual(res.status, 0)
        self.assertEqual(res.stdout, "")
        self.assertEqual(res.stderr, textwrap.dedent("""\
            Traceback (most recent call last):
              File "jailed_code", line 1, in <module>
                raise Exception('FAIL')
            Exception: FAIL
            """))

    def test_stdin_is_provided(self):
        res = jailpy(
            code="import json,sys; print(sum(json.load(sys.stdin)))",
            stdin="[1, 2.5, 33]"
        )
        self.assertResultOk(res)
        self.assertEqual(res.stdout, "36.5\n")

    def test_files_are_copied(self):
        res = jailpy(
            code="print('Look:', open('hello.txt').read())",
            files=[file_here("hello.txt")]
        )
        self.assertResultOk(res)
        self.assertEqual(res.stdout, 'Look: Hello there.\n\n')

    def test_directories_are_copied(self):
        res = jailpy(
            code="""\
                import os
                for path, dirs, files in os.walk("."):
                    print((path, sorted(dirs), sorted(files)))
                """,
            files=[file_here("hello.txt"), file_here("pylib")]
        )
        self.assertResultOk(res)
        self.assertIn("hello.txt", res.stdout)
        self.assertIn("pylib", res.stdout)
        self.assertIn("module.py", res.stdout)

    def test_executing_a_copied_file(self):
        res = jailpy(
            files=[file_here("doit.py")],
            argv=["doit.py", "1", "2", "3"]
        )
        self.assertResultOk(res)
        self.assertEqual(
            res.stdout,
            "This is doit.py!\nMy args are ['doit.py', '1', '2', '3']\n"
        )

    def test_context_managers(self):
        first = textwrap.dedent("""
            with open("hello.txt", "w") as f:
                f.write("Hello, second")
        """)
        second = textwrap.dedent("""
            with open("hello.txt") as f:
                print(f.read())
        """)

        limits = {"TIME": 1, "MEMORY": 128*1024*1024,
                  "CAN_FORK": True, "FILE_SIZE": 256}
        configure("unconfined_python", sys.prefix + "/bin/python", limits_conf=limits)
        with Jail() as j:
            res = j.run_code("unconfined_python", first)
            self.assertEqual(res.status, 0)
            res = j.run_code("python", second)
        self.assertEqual(res.status, 0)
        self.assertEqual(res.stdout.decode().strip(), "Hello, second")


class TestLimits(JailCodeHelpers):
    """Tests of the resource limits, and changing them."""

    def test_cant_use_too_much_memory(self):
        # This will fail after setting the limit to 30Mb.
        res = jailpy(code="print(len(bytearray(50000000)))", limits={'MEMORY': 30000000})
        self.assertEqual(res.stdout, "")
        self.assertNotEqual(res.status, 0)

    def test_changing_vmem_limit(self):
        # Up the limit, it will succeed.
        res = jailpy(code="print(len(bytearray(50000000)))", limits={'MEMORY': 80000000})
        self.assertEqual(res.stdout, "50000000\n")
        self.assertEqual(res.status, 0)

    def test_disabling_vmem_limit(self):
        # Disable the limit, it will succeed.
        res = jailpy(code="print(len(bytearray(50000000)))", limits={'MEMORY': None})
        self.assertEqual(res.stdout, "50000000\n")
        self.assertEqual(res.status, 0)

    def test_cant_use_too_much_cpu(self):
        res = jailpy(code="print(sum(range(10**9)))")
        self.assertEqual(res.stdout, "")
        self.assertNotEqual(res.status, 0)
        self.assertTrue(res.time_limit_exceeded)

    def test_cant_use_too_much_time(self):
        # time limit is 5 * cpu_time
        res = jailpy(code="import time; time.sleep(7); print('Done!')", limits={'TIME': 1})
        self.assertNotEqual(res.status, 0)
        self.assertEqual(res.stdout, "")
        self.assertTrue(res.time_limit_exceeded)

    def test_cant_write_files(self):
        res = jailpy(code="""\
                print("Trying")
                with open("mydata.txt", "w") as f:
                    f.write("hello")
                with open("mydata.txt") as f2:
                    print("Got this:", f2.read())
                """)
        self.assertNotEqual(res.status, 0)
        self.assertEqual(res.stdout, "Trying\n")
        self.assertIn("ermission denied", res.stderr)

    def test_cant_use_network(self):
        res = jailpy(code="""\
                import urllib.request
                print("Reading google")
                u = urllib.request.urlopen("http://google.com")
                google = u.read()
                print(len(google))
                """)
        self.assertNotEqual(res.status, 0)
        self.assertEqual(res.stdout, "Reading google\n")
        self.assertIn("URLError", res.stderr)

    def test_cant_fork(self):
        res = jailpy(code="""\
                import os
                print("Forking")
                child_ppid = os.fork()
                """)
        self.assertNotEqual(res.status, 0)
        self.assertEqual(res.stdout, "Forking\n")
        self.assertIn("IOError", res.stderr)

    def test_cant_see_environment_variables(self):
        os.environ['HONEY_BOO_BOO'] = 'Look!'
        res = jailpy(code="""\
                import os
                for name, value in os.environ.items():
                    print("%s: %r" % (name, value))
                """)
        self.assertResultOk(res)
        self.assertNotIn("HONEY", res.stdout)

    def test_reading_dev_random(self):
        # We can read 10 bytes just fine.
        res = jailpy(code="x = open('/dev/random', 'rb').read(10); print(len(x))")
        self.assertResultOk(res)
        self.assertEqual(res.stdout, "10\n")

        # If we try to read all of it, we'll be killed by the real-time limit.
        res = jailpy(code="x = open('/dev/random').read(); print('Done!')")
        self.assertNotEqual(res.status, 0)


class TestSymlinks(JailCodeHelpers):
    """Testing symlink behavior."""

    def setUp(self):
        # Make a temp dir, and arrange to have it removed when done.
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir)

        # Make a directory that won't be copied into the sandbox.
        self.not_copied = os.path.join(tmp_dir, "not_copied")
        os.mkdir(self.not_copied)
        self.linked_txt = os.path.join(self.not_copied, "linked.txt")
        with open(self.linked_txt, "w") as linked:
            linked.write("Hi!")

        # Make a directory that will be copied into the sandbox, with a
        # symlink to a file we aren't copying in.
        self.copied = os.path.join(tmp_dir, "copied")
        os.mkdir(self.copied)
        self.here_txt = os.path.join(self.copied, "here.txt")
        with open(self.here_txt, "w") as here:
            here.write("012345")
        self.link_txt = os.path.join(self.copied, "link.txt")
        os.symlink(self.linked_txt, self.link_txt)
        self.herelink_txt = os.path.join(self.copied, "herelink.txt")
        os.symlink("here.txt", self.herelink_txt)

    def test_symlinks_in_directories_wont_copy_data(self):
        # Run some code in the sandbox, with a copied directory containing
        # the symlink.
        res = jailpy(
            code="""\
                print(open('copied/here.txt').read())        # can read
                print(open('copied/herelink.txt').read())    # can read
                print(open('copied/link.txt').read())        # can't read
                """,
            files=[self.copied],
        )
        self.assertEqual(res.stdout, "012345\n012345\n")
        self.assertIn("ermission denied", res.stderr)

    def test_symlinks_wont_copy_data(self):
        # Run some code in the sandbox, with a copied file which is a symlink.
        res = jailpy(
            code="""\
                print(open('here.txt').read())       # can read
                print(open('herelink.txt').read())   # can read
                print(open('link.txt').read())       # can't read
                """,
            files=[self.here_txt, self.herelink_txt, self.link_txt],
        )
        self.assertEqual(res.stdout, "012345\n012345\n")
        self.assertIn("ermission denied", res.stderr)


class TestMalware(JailCodeHelpers):
    """Tests that attempt actual malware against the interpreter or system."""

    def test_crash_cpython(self):
        # http://nedbatchelder.com/blog/201206/eval_really_is_dangerous.html
        res = jailpy(code="""\
            import types, sys
            bad_code = types.CodeType(0,0,0,0,0,b"KABOOM",(),(),(),"","",0,b"")
            crash_me = types.FunctionType(bad_code, {})
            print("Here we go...")
            sys.stdout.flush()
            crash_me()
            print("The afterlife!")
            """)
        self.assertNotEqual(res.status, 0)
        self.assertEqual(res.stderr, "")
        self.assertEqual(res.stdout, "Here we go...\n")

    def test_read_etc_passwd(self):
        res = jailpy(code="""\
            bytes = len(open('/etc/passwd').read())
            print('Gotcha', bytes)
            """)
        self.assertNotEqual(res.status, 0)
        self.assertEqual(res.stdout, "")
        self.assertIn("ermission denied", res.stderr)

    def test_find_other_sandboxes(self):
        res = jailpy(code="""
            import os
            places = [
                "..", "/tmp", "/", "/home", "/etc", "/var"
                ]
            for place in places:
                try:
                    files = os.listdir(place)
                except Exception:
                    # darn
                    pass
                else:
                    print("Files in %r: %r" % (place, files))
            print("Done.")
            """)
        self.assertResultOk(res)
        self.assertEqual(res.stdout, "Done.\n")
