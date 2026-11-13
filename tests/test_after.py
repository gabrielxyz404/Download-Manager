"""What happens when a download finishes: checksum, extract, command, order and safety."""

import hashlib
import os
import sys
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace

from flowdl import after


def settings(**kw):
    base = dict(after_open=False, after_extract=False, after_scan=False, after_command="")
    base.update(kw)
    return SimpleNamespace(**base)


class ChecksumTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "jogo.zip")
        self.data = os.urandom(300_000)
        with open(self.path, "wb") as f:
            f.write(self.data)

    def tearDown(self):
        self.dir.cleanup()

    def test_kind_told_by_length_and_formats_accepted(self):
        self.assertEqual(after.algo_for("a" * 32), "md5")
        self.assertEqual(after.algo_for("a" * 40), "sha1")
        self.assertEqual(after.algo_for("A" * 64), "sha256")
        self.assertIsNone(after.algo_for("xyz"))
        self.assertEqual(after.clean_hash("SHA256: ABCdef  jogo.zip"), "abcdef")

    def test_right_and_wrong(self):
        good = hashlib.sha256(self.data).hexdigest()
        self.assertTrue(after.verify(self.path, good.upper())[0])
        self.assertTrue(after.verify(self.path, hashlib.md5(self.data).hexdigest())[0])
        ok, msg = after.verify(self.path, "0" * 64)
        self.assertFalse(ok)
        self.assertIn("NÃO confere", msg)


class ExtractTests(unittest.TestCase):
    def test_extracts_next_to_it_and_never_outside(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mod.zip")
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("leia-me.txt", "olá")
                z.writestr("pasta/dados.bin", b"x" * 100)
                z.writestr("../../fora.txt", "tentativa de sair da pasta")
            msg = after.extract_zip(path)
            self.assertIn("extraído", msg)
            self.assertTrue(os.path.isfile(os.path.join(d, "mod", "leia-me.txt")))
            self.assertTrue(os.path.isfile(os.path.join(d, "mod", "pasta", "dados.bin")))
            self.assertFalse(os.path.exists(os.path.join(d, "fora.txt")))
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(d), "fora.txt")))
            self.assertIn("extraído", after.extract_zip(path))  # again: a new folder, nothing overwritten
            self.assertTrue(os.path.isdir(os.path.join(d, "mod (2)")))

    def test_not_a_zip_or_with_password(self):
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, "nao.zip")
            with open(plain, "wb") as f:
                f.write(b"isto nao e um zip")
            self.assertEqual(after.extract_zip(plain), "")
            locked = os.path.join(d, "fechado.zip")
            with zipfile.ZipFile(locked, "w") as z:
                z.writestr("a.txt", "x")
            with open(locked, "r+b") as f:  # mark the entry as encrypted (flag bit 0)
                data = bytearray(f.read())
                for sig in (b"PK\x03\x04", b"PK\x01\x02"):
                    i = data.find(sig)
                    off = 6 if sig == b"PK\x03\x04" else 8
                    data[i + off] |= 1
                f.seek(0)
                f.write(data)
            self.assertIn("palavra-passe", after.extract_zip(locked))


class CommandTests(unittest.TestCase):
    def test_names_with_spaces_and_symbols_stay_one_argument(self):
        path = os.path.join(tempfile.gettempdir(), "o meu ficheiro & outro.zip")
        cmd = after.command_for("programa {ficheiro} --nome {nome}", path, "https://x.com/a")
        if os.name == "nt":
            self.assertIn('"' + path + '"', cmd)
        else:
            self.assertEqual(cmd, ["programa", path, "--nome", os.path.basename(path)])

    def test_command_really_runs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pronto.bin")
            open(path, "wb").close()
            marker = os.path.join(d, "corri.txt")
            py = sys.executable
            template = f'"{py}" -c "open(r\'{marker}\', \'w\').write(\'ok\')" {{ficheiro}}' if os.name == "nt" \
                else f"{py} -c \"open('{marker}', 'w').write('ok')\" {{ficheiro}}"
            self.assertEqual(after.run_command(template, path), "")
            for _ in range(50):
                if os.path.exists(marker):
                    break
                time.sleep(0.1)
            self.assertTrue(os.path.exists(marker))


class OrderTests(unittest.TestCase):
    def test_wrong_checksum_stops_everything_else(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mod.zip")
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("a.txt", "x")
            task = SimpleNamespace(path=path, url="https://x.com/mod.zip", expected_hash="0" * 64)
            notes = after.run(task, settings(after_extract=True))
            self.assertIn("NÃO confere", notes[0])
            self.assertFalse(os.path.exists(os.path.join(d, "mod")))  # not extracted
            task.expected_hash = hashlib.sha256(open(path, "rb").read()).hexdigest()
            notes = after.run(task, settings(after_extract=True))
            self.assertIn("confere", notes[0])
            self.assertTrue(os.path.isfile(os.path.join(d, "mod", "a.txt")))


if __name__ == "__main__":
    unittest.main()
