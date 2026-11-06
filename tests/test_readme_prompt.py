import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def block(text):
    return re.search(r'```text\n.*?\n```', text, re.S).group(0)


class ReadmePromptInSync(unittest.TestCase):
    def test_readme_copy_matches_prompt_file(self):
        for readme, prompt in [('README.md', 'quick-setup.md'), ('README.zh-CN.md', 'quick-setup.zh-CN.md')]:
            r = (ROOT / readme).read_text(encoding='utf-8')
            inline = r.split('<!-- prompt:start -->', 1)[1].split('<!-- prompt:end -->', 1)[0]
            self.assertEqual(block(inline), block((ROOT / 'prompts' / prompt).read_text(encoding='utf-8')), readme)


if __name__ == '__main__':
    unittest.main()
