"""Mailbox names reach the IMAP server as quoted strings, so folders with a
space in the name can be opened. The server is a local fake; nothing touches
the network."""
import imaplib, socketserver, threading, unittest

from jb_email import EmailMixin


class FakeIMAP(socketserver.StreamRequestHandler):
    """Just enough IMAP to log in and select a mailbox, rejecting an unquoted
    name that contains a space the way a real server does."""
    def handle(self):
        self.wfile.write(b"* OK fake ready\r\n")
        for raw in self.rfile:
            tag, _, rest = raw.decode().strip().partition(" ")
            cmd = rest.split(" ")[0].upper()
            if cmd == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n" + tag.encode() + b" OK done\r\n")
            elif cmd == "LOGIN":
                self.wfile.write(tag.encode() + b" OK welcome\r\n")
            elif cmd in ("SELECT", "EXAMINE"):
                arg = rest[len(cmd):].strip()
                parses = (arg.startswith('"') and arg.endswith('"')) or " " not in arg
                self.wfile.write(b"* 0 EXISTS\r\n" + tag.encode() + b" OK [READ-ONLY] done\r\n" if parses
                                 else tag.encode() + b" BAD Could not parse command\r\n")
            elif cmd == "LOGOUT":
                self.wfile.write(b"* BYE\r\n" + tag.encode() + b" OK bye\r\n")
                return
            else:
                self.wfile.write(tag.encode() + b" BAD unknown\r\n")


class MailboxNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeIMAP)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_names_are_quoted_and_escaped(self):
        self.assertEqual(EmailMixin.imap_mailbox("[Gmail]/Sent Mail"), '"[Gmail]/Sent Mail"')
        self.assertEqual(EmailMixin.imap_mailbox("INBOX"), '"INBOX"')
        self.assertEqual(EmailMixin.imap_mailbox(""), '"INBOX"')
        self.assertEqual(EmailMixin.imap_mailbox('"Already quoted"'), '"Already quoted"')
        self.assertEqual(EmailMixin.imap_mailbox('Say "hi"'), '"Say \\"hi\\""')

    def test_a_folder_with_a_space_opens_only_when_quoted(self):
        conn = imaplib.IMAP4("127.0.0.1", self.port)
        try:
            conn.login("me@example.com", "pw")
            with self.assertRaisesRegex(imaplib.IMAP4.error, "Could not parse"):   # the bare name, as before
                conn.select("[Gmail]/Sent Mail", readonly=True)
            status, _ = conn.select(EmailMixin.imap_mailbox("[Gmail]/Sent Mail"), readonly=True)
            self.assertEqual(status, "OK")
        finally:
            conn.logout()


if __name__ == "__main__":
    unittest.main()
