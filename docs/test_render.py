"""Check public documentation output without modifying Helm repository artifacts."""
from pathlib import Path
import tempfile
import unittest

import render


class DocumentationTests(unittest.TestCase):
    def test_bilingual_pages_preserve_repository_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            artifacts = {"index.yaml": b"entries: {}\n", "previous.tgz": b"existing chart"}
            for name, content in artifacts.items():
                (output / name).write_bytes(content)
            render.render(output)
            english = (output / "index.html").read_text(encoding="utf-8")
            chinese = (output / "readme.zh.html").read_text(encoding="utf-8")
            self.assertIn('<html lang="en">', english)
            self.assertIn('<html lang="zh-CN">', chinese)
            for page in (english, chinese):
                self.assertIn('href="index.html"', page)
                self.assertIn('href="readme.zh.html"', page)
                self.assertIn('href="index.yaml"', page)
                self.assertIn("<table>", page)
                self.assertIn('<pre><code class="language-bash">', page)
                self.assertIn("&lt;REMOVE_TOKEN&gt;", page)
            for name, content in artifacts.items():
                self.assertEqual((output / name).read_bytes(), content)
            for name in ("readme.md", "readme.zh.md"):
                self.assertEqual((output / name).read_bytes(), (render.ROOT / name).read_bytes())
            self.assertTrue((output / ".nojekyll").exists())
            self.assertEqual(len(list(output.iterdir())), 7)

    def test_chart_root_cannot_be_used_as_output(self):
        with self.assertRaises(ValueError):
            render.render(render.ROOT)


if __name__ == "__main__":
    unittest.main()
