"""Questions and menus read off an agent's terminal (answered from the apps)."""
import unittest
from jb_core import Store  # noqa: F401  (import order: jb_core before jb_coding)
from jb_coding import CodingAgentsMixin

P = CodingAgentsMixin.parse_menu

ASK = """❯ Use the AskUserQuestion tool to ask me two questions
────────────────────────────────────────
←  ☐ Colour  ☐ Size  ✔ Submit  →
Which colour would you like?
❯ 1. Red
     Pick red
  2. Green
     Pick green
  3. Type something.
────────────────────────────────────────
  4. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""
TRUST = """ Quick safety check: Is this a project you created or one you trust? (Like your own code). If not,
 take a moment to review what's in this folder first.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""
REVIEW = """←  ☒ Colour  ☒ Size  ✔ Submit  →
Review your answers
 ● Which colour would you like?
   → Green
Ready to submit your answers?
❯ 1. Submit answers
  2. Cancel
"""
CLAUDE_PERM = """ Bash command
   rm -rf build
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in /x
   3. No, and tell Claude what to do differently (esc)
 Esc to cancel · Tab to amend
"""
CODEX_PERM = """  Would you like to run the following command?
  $ rm -rf build
› 1. Yes, proceed (y)
  2. Yes, and don't ask again for this command (a)
  3. No, and tell Codex what to do differently (esc)
  Press enter to confirm or esc to cancel
"""


class Menus(unittest.TestCase):
    def test_ask_user_question(self):
        m = P(ASK)
        self.assertEqual((m["question"], m["header"]), ("Which colour would you like?", "Colour · Size"))
        self.assertEqual([o["label"] for o in m["options"]], ["Red", "Green", "Type something."])
        self.assertEqual(m["options"][0]["description"], "Pick red")
        self.assertTrue(m["options"][2]["freeform"] and m["numbered"])

    def test_unnumbered_trust_prompt(self):
        m = P(TRUST)
        self.assertFalse(m["numbered"])
        self.assertEqual([(o["label"], o["selected"]) for o in m["options"]], [("No, exit", True), ("Yes, I trust this folder", False)])
        self.assertTrue(m["question"].endswith("one you trust?"))

    def test_review_screen_without_hint(self):
        m = P(REVIEW)
        self.assertEqual(m["question"], "Review your answers")
        self.assertEqual([o["key"] for o in m["options"]], ["1", "2"])

    def test_permission_prompts(self):
        for screen, who in ((CLAUDE_PERM, "Claude"), (CODEX_PERM, "Codex")):
            m = P(screen)
            self.assertIn("?", m["question"])
            self.assertEqual(m["options"][0]["label"], "Yes" if who == "Claude" else "Yes, proceed")
            self.assertTrue(m["options"][2]["freeform"], who)

    def test_plain_text_is_not_a_menu(self):
        self.assertIsNone(P("Here's the plan:\n1. Write tests\n2. Ship it\n"))
        self.assertIsNone(P(""))


if __name__ == "__main__":
    unittest.main()
