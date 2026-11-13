"""Email account setup: servers derived from the address, TLS mode inferred from
the port, and the login check that runs before an account is saved. The IMAP
server is a local fake; nothing touches the network."""
import socket, socketserver, threading, unittest

from jb_email import EmailMixin


class FakeIMAP(socketserver.StreamRequestHandler):
    """Just enough IMAP for imaplib to connect and attempt one LOGIN."""
    def handle(self):
        self.wfile.write(b"* OK fake ready\r\n")
        for raw in self.rfile:
            tag, _, rest = raw.decode().strip().partition(" ")
            cmd = rest.split(" ")[0].upper()
            if cmd == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n" + tag.encode() + b" OK done\r\n")
            elif cmd == "LOGIN":
                ok = rest.endswith('"right-password"') or rest.endswith("right-password")
                self.wfile.write(tag.encode() + (b" OK welcome\r\n" if ok else b" NO [AUTHENTICATIONFAILED] nope\r\n"))
            elif cmd == "LOGOUT":
                self.wfile.write(b"* BYE\r\n" + tag.encode() + b" OK bye\r\n")
                return
            else:
                self.wfile.write(tag.encode() + b" BAD unknown\r\n")


class EmailSetupTests(unittest.TestCase):
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

    def account(self, password, port=None):
        return {"imap_host": "127.0.0.1", "imap_port": port or self.port, "imap_ssl": 0,
                "imap_user": "me@example.com", "imap_password": password}

    def test_known_provider_needs_only_the_address(self):
        a = EmailMixin.email_account_from_login("someone@gmail.com", "pw")
        self.assertEqual((a["imap_host"], a["imap_port"], a["smtp_host"], a["smtp_port"]),
                         ("imap.gmail.com", 993, "smtp.gmail.com", 465))
        self.assertTrue(a["smtp_ssl"])

    def test_icloud_submits_with_starttls_not_implicit_tls(self):
        a = EmailMixin.email_account_from_login("someone@icloud.com", "pw")
        self.assertEqual((a["smtp_port"], a["smtp_ssl"]), (587, False))

    def test_manual_setup_infers_tls_from_the_port(self):
        self.assertFalse(EmailMixin.email_infer_ssl({"smtp_port": 587})["smtp_ssl"])
        self.assertTrue(EmailMixin.email_infer_ssl({"smtp_port": 465})["smtp_ssl"])
        self.assertTrue(EmailMixin.email_infer_ssl({})["imap_ssl"])
        self.assertFalse(EmailMixin.email_infer_ssl({"imap_port": 143})["imap_ssl"])
        # an explicit choice is never overridden
        self.assertTrue(EmailMixin.email_infer_ssl({"smtp_port": 587, "smtp_ssl": True})["smtp_ssl"])

    def test_login_check_passes_with_the_right_password(self):
        self.assertIsNone(EmailMixin().email_login_error(self.account("right-password")))

    def test_login_check_reports_a_rejected_password(self):
        err = EmailMixin().email_login_error(self.account("wrong"))
        self.assertIn("rejected", err)
        self.assertIn("app-specific password", err)

    def test_login_check_reports_an_unreachable_server(self):
        with socket.socket() as s:          # a port nothing is listening on
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        err = EmailMixin().email_login_error(self.account("right-password", port=closed))
        self.assertIn("Couldn't reach", err)
        self.assertIn("Advanced", err)

    def test_login_check_needs_a_server(self):
        self.assertIn("Advanced", EmailMixin().email_login_error({"imap_host": ""}))


if __name__ == "__main__":
    unittest.main()
