"""IMAP/SMTP email: list, read, send, flags, accounts."""
from jb_core import *  # noqa: F401,F403


class EmailMixin:

    @staticmethod
    def port_open(host, port):
        try:
            with socket.create_connection((host, int(port)), timeout=0.4):
                return True
        except OSError:
            return False

    def ensure_hydroxide_bridge(self):
        # Production ships manual IMAP/SMTP only — no Proton/hydroxide bridge.
        # Kept as a no-op so the (now unreachable) bridge code paths stay valid.
        return None

    def email_sources(self):
        return self.store.rows("select * from email_sources order by datetime(created_at) asc")

    @staticmethod
    def email_source_json(row):
        return {"id": row["id"], "display": row["display"], "type": row["type"]}


    def upsert_email_account(self, kind, body):
        is_bridge = kind in {"bridge", "hydroxide"}
        display = body.get("email") or body.get("imap_user") or body.get("host") or "Email account"
        account_id = body.get("id") or "mail_" + os.urandom(6).hex()
        now = now_iso()
        self.store.exec(
            """
            insert or replace into email_sources(
                id,type,display,imap_host,imap_port,imap_ssl,imap_user,imap_password,
                smtp_host,smtp_port,smtp_ssl,smtp_user,smtp_password,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                account_id, kind, display,
                body.get("imap_host") or body.get("host") or "",
                int(body.get("imap_port") or (1143 if is_bridge else 993)),
                1 if bool(body.get("imap_ssl", not is_bridge)) else 0,
                body.get("imap_user") or body.get("email") or "",
                body.get("imap_password") or "",
                body.get("smtp_host") or (body.get("imap_host") if is_bridge else ""),
                int(body.get("smtp_port") or (1025 if is_bridge else 465)),
                1 if bool(body.get("smtp_ssl", not is_bridge)) else 0,
                body.get("smtp_user") or body.get("email") or "",
                body.get("smtp_password") or body.get("imap_password") or "",
                now, now,
            ),
        )
        return account_id

    def split_mail_uid(self, uid):
        raw = urllib.parse.unquote(uid)
        if "::" in raw:
            account_id, msg_uid = raw.split("::", 1)
            account = self.store.one("select * from email_sources where id=?", (account_id,))
            return account, msg_uid
        sources = self.email_sources()
        return (sources[0] if sources else None), raw

    def imap_connect(self, account):
        # Bridge accounts (ProtonMail/hydroxide) need the local bridge running;
        # ensure it for every IMAP op (list, mark-read, archive, …), not just connect.
        if account.get("type") in {"bridge", "hydroxide"} or str(account.get("imap_host")) == "127.0.0.1":
            self.ensure_hydroxide_bridge()
        cls = imaplib.IMAP4_SSL if int(account.get("imap_ssl") or 0) else imaplib.IMAP4
        conn = cls(account["imap_host"], int(account["imap_port"]))
        if account.get("imap_user") or account.get("imap_password"):
            conn.login(account.get("imap_user") or "", account.get("imap_password") or "")
        return conn

    @staticmethod
    def decode_header_value(value):
        if not value:
            return ""
        parts = email.header.decode_header(value)
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                out.append(text.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out)

    def message_addresses(self, value):
        parsed = email.utils.parseaddr(self.decode_header_value(value))
        return self.decode_header_value(parsed[0]), parsed[1]

    @staticmethod
    def composite_mail_uid(account, uid):
        return urllib.parse.quote(f"{account['id']}::{uid}", safe=":")

    def email_folders(self):
        folders = {"INBOX"}
        configured = False
        for account in self.email_sources():
            configured = True
            try:
                conn = self.imap_connect(account)
                status, rows = conn.list()
                if status == "OK":
                    for raw in rows or []:
                        text = raw.decode("utf-8", errors="replace")
                        name = text.rsplit(' "/" ', 1)[-1].strip('"') if ' "/" ' in text else text.rsplit(" ", 1)[-1].strip('"')
                        if name:
                            folders.add(name)
                conn.logout()
            except Exception:
                continue
        return self.json({"folders": sorted(folders), "configured": configured})

    def email_list(self, q):
        sources = self.email_sources()
        limit = max(0, int(q.get("limit") or 0))
        folder = q.get("folder") or "INBOX"
        filt = q.get("filter") or "all"
        criterion = "UNSEEN" if filt == "unread" else "ALL"
        out = []
        errors = []
        total_matched = 0   # all messages matching the filter, before the page limit
        for account in sources:
            try:
                conn = self.imap_connect(account)
                status, _ = conn.select(folder, readonly=True)
                if status != "OK" and folder != "INBOX":
                    status, _ = conn.select("INBOX", readonly=True)
                if status != "OK":
                    raise RuntimeError("Mailbox unavailable")
                status, data = conn.uid("search", None, criterion)
                if status != "OK":
                    raise RuntimeError("Search failed")
                uids = (data[0] or b"").split()
                total_matched += len(uids)
                selected = list(reversed(uids if limit == 0 else uids[-limit:]))
                for raw_uid in selected:
                    status, rows = conn.uid("fetch", raw_uid, "(BODY.PEEK[HEADER] FLAGS RFC822.SIZE)")
                    if status != "OK" or not rows:
                        continue
                    header_bytes = b""
                    flags = ""
                    size = 0
                    # FLAGS / RFC822.SIZE can land in the tuple's metadata OR in a
                    # trailing standalone bytes element (after the body literal),
                    # depending on the server. Accumulate ALL of it before parsing,
                    # otherwise read messages look unread (no \Seen captured).
                    for item in rows:
                        if isinstance(item, tuple):
                            flags += item[0].decode("utf-8", errors="replace")
                            header_bytes += item[1] or b""
                        elif isinstance(item, (bytes, bytearray)):
                            flags += item.decode("utf-8", errors="replace")
                    if "RFC822.SIZE" in flags:
                        try:
                            size = int(flags.split("RFC822.SIZE", 1)[1].split()[0].strip(")"))
                        except Exception:
                            size = 0
                    msg = email.message_from_bytes(header_bytes, policy=email.policy.default)
                    from_name, from_addr = self.message_addresses(msg.get("From", ""))
                    flags_lc = flags.lower()
                    out.append({
                        "uid": self.composite_mail_uid(account, raw_uid.decode()),
                        "subject": self.decode_header_value(msg.get("Subject", "")) or "(No subject)",
                        "from_name": from_name,
                        "from_address": from_addr,
                        "date": msg.get("Date", ""),
                        "is_read": "\\seen" in flags_lc,
                        "is_flagged": "\\flagged" in flags_lc,
                        "has_attachments": False,
                        "cached_summary": "",
                        "size": size,
                    })
                conn.close()
                conn.logout()
            except Exception as exc:
                errors.append(f"{account.get('display') or 'Mailbox'}: {exc}")
        def sort_key(row):
            try:
                dt = email.utils.parsedate_to_datetime(row.get("date") or "")
                return dt.timestamp() if dt else 0
            except Exception:
                return 0
        out.sort(key=sort_key, reverse=True)
        if filt == "unread":
            # Belt-and-suspenders: some IMAP servers (notably the ProtonMail
            # bridge) don't honour SEARCH UNSEEN reliably, so also drop anything
            # our own \Seen-flag parse says is read. (total_matched stays the
            # server's count so the sidebar badge isn't skewed by the page limit.)
            out = [r for r in out if not r.get("is_read")]
        return self.json({"emails": out, "total": total_matched, "configured": bool(sources), "error": "; ".join(errors) if errors and not out else None})

    def email_read(self, uid):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            return self.json({"error": "No mailbox connected"}, 400)
        folder = self._query().get("folder") or "INBOX"
        try:
            conn = self.imap_connect(account)
            conn.select(folder)
            status, rows = conn.uid("fetch", msg_uid, "(RFC822)")
            if status != "OK":
                raise RuntimeError("Message fetch failed")
            raw = b"".join(item[1] for item in rows if isinstance(item, tuple))
            conn.uid("STORE", msg_uid, "+FLAGS", "\\Seen")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            from_name, from_addr = self.message_addresses(msg.get("From", ""))
            body, body_html, attachments = self.extract_email_body(msg)
            conn.logout()
            return self.json({
                "uid": self.composite_mail_uid(account, msg_uid),
                "message_id": msg.get("Message-ID", ""),
                "subject": self.decode_header_value(msg.get("Subject", "")) or "(No subject)",
                "from_name": from_name,
                "from_address": from_addr,
                "to": msg.get("To", ""),
                "cc": msg.get("Cc", ""),
                "date": msg.get("Date", ""),
                "references": msg.get("References", ""),
                "body": body,
                "body_html": body_html,
                "attachments": attachments,
            })
        except Exception as exc:
            return self.json({"error": str(exc)}, 500)

    def extract_email_body(self, msg):
        text = ""
        html = ""
        attachments = []
        parts = msg.walk() if msg.is_multipart() else [msg]
        for i, part in enumerate(parts):
            cdisp = (part.get_content_disposition() or "").lower()
            ctype = part.get_content_type()
            if cdisp == "attachment":
                attachments.append({
                    "filename": part.get_filename() or f"attachment-{i}",
                    "size": len(part.get_payload(decode=True) or b""),
                    "index": i,
                    "content_type": ctype,
                })
                continue
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ctype == "text/plain" and not text:
                text = content
            elif ctype == "text/html" and not html:
                html = content
        return text or html, html, attachments

    def email_set_read(self, uid, read):
        return self.email_store_flag(uid, "\\Seen", add=read)

    def email_archive(self, uid):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            return self.json({"success": False, "error": "No mailbox connected"}, 400)
        folder = self._query().get("folder") or "INBOX"
        try:
            conn = self.imap_connect(account)
            conn.select(folder)
            typ, _ = conn.create("Archive")
            conn.uid("COPY", msg_uid, "Archive")
            conn.uid("STORE", msg_uid, "+FLAGS", "\\Deleted")
            conn.expunge()
            conn.logout()
            return self.json({"success": True})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, 500)

    def email_delete(self, uid):
        return self.email_store_flag(uid, "\\Deleted", add=True, expunge=True)

    def email_store_flag(self, uid, flag, add=True, expunge=False):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            return self.json({"success": False, "error": "No mailbox connected"}, 400)
        folder = self._query().get("folder") or "INBOX"
        try:
            conn = self.imap_connect(account)
            conn.select(folder)
            conn.uid("STORE", msg_uid, "+FLAGS" if add else "-FLAGS", flag)
            if expunge:
                conn.expunge()
            conn.logout()
            return self.json({"success": True})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, 500)

    def email_send(self, body):
        sources = self.email_sources()
        if not sources:
            return self.json({"success": False, "error": "No mailbox connected"}, 400)
        account = sources[0]
        try:
            if account.get("type") in ("bridge", "hydroxide"):
                err = self.ensure_hydroxide_bridge()
                if err:
                    raise RuntimeError(err)
            msg = MIMEMultipart()
            msg["From"] = account.get("smtp_user") or account.get("imap_user") or account.get("display")
            msg["To"] = body.get("to") or ""
            if body.get("cc"):
                msg["Cc"] = body.get("cc")
            msg["Subject"] = body.get("subject") or ""
            if body.get("in_reply_to"):
                msg["In-Reply-To"] = body.get("in_reply_to")
            if body.get("references"):
                msg["References"] = body.get("references")
            msg.attach(MIMEText(body.get("body") or "", "plain", "utf-8"))
            host = account.get("smtp_host") or account.get("imap_host")
            port = int(account.get("smtp_port") or 465)
            use_ssl = bool(int(account.get("smtp_ssl") or 0))
            if use_ssl:
                smtp = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                # Plain connection upgraded via STARTTLS before AUTH (covers
                # submission port 587 and the local hydroxide bridge on :1025).
                smtp = smtplib.SMTP(host, port, timeout=30)
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
            user = account.get("smtp_user") or account.get("imap_user") or ""
            password = account.get("smtp_password") or account.get("imap_password") or ""
            if user or password:
                smtp.login(user, password)
            addr_fields = [v for v in (msg.get("To", ""), msg.get("Cc", "")) if v]
            recipients = [a for _, a in email.utils.getaddresses(addr_fields) if a]
            refused = smtp.sendmail(msg["From"], recipients, msg.as_string())
            smtp.quit()
            if refused:
                raise RuntimeError("SMTP refused recipients: " + json.dumps(refused))
            return self.json({"success": True})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, 500)

    def email_list_payload(self, args):
        sources = self.email_sources()
        if not sources:
            return json.dumps({"emails": [], "total": 0, "configured": False}, ensure_ascii=False)
        limit = max(1, int(args.get("max_results") or args.get("limit") or 20))
        folder = args.get("folder") or "INBOX"
        filt = args.get("filter") or "all"
        criterion = "UNSEEN" if args.get("unread_only") or filt == "unread" else "ALL"
        out = []
        for account in sources:
            conn = self.imap_connect(account)
            try:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    raise RuntimeError("Mailbox unavailable")
                status, data = conn.uid("search", None, criterion)
                if status != "OK":
                    raise RuntimeError("Search failed")
                for raw_uid in list(reversed((data[0] or b"").split()))[:limit]:
                    status, rows = conn.uid("fetch", raw_uid, "(BODY.PEEK[HEADER] FLAGS)")
                    if status != "OK":
                        continue
                    header = b"".join(item[1] for item in rows if isinstance(item, tuple))
                    msg = email.message_from_bytes(header, policy=email.policy.default)
                    from_name, from_addr = self.message_addresses(msg.get("From", ""))
                    out.append({
                        "uid": self.composite_mail_uid(account, raw_uid.decode()),
                        "account": account.get("display"),
                        "subject": self.decode_header_value(msg.get("Subject", "")) or "(No subject)",
                        "from_name": from_name,
                        "from_address": from_addr,
                        "date": msg.get("Date", ""),
                    })
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        return json.dumps({"emails": out, "total": len(out), "configured": True}, ensure_ascii=False)

    def email_read_data(self, uid, folder="INBOX"):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            raise ValueError("No mailbox connected")
        conn = self.imap_connect(account)
        try:
            status, _ = conn.select(folder)
            if status != "OK":
                raise RuntimeError("Mailbox unavailable")
            status, rows = conn.uid("fetch", msg_uid, "(RFC822)")
            if status != "OK":
                raise RuntimeError("Message fetch failed")
            raw = b"".join(item[1] for item in rows if isinstance(item, tuple))
            conn.uid("STORE", msg_uid, "+FLAGS", "\\Seen")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            from_name, from_addr = self.message_addresses(msg.get("From", ""))
            body, body_html, attachments = self.extract_email_body(msg)
            return {
                "uid": self.composite_mail_uid(account, msg_uid),
                "subject": self.decode_header_value(msg.get("Subject", "")) or "(No subject)",
                "from_name": from_name,
                "from_address": from_addr,
                "to": msg.get("To", ""),
                "cc": msg.get("Cc", ""),
                "date": msg.get("Date", ""),
                "message_id": msg.get("Message-ID", ""),
                "references": msg.get("References", ""),
                "body": body,
                "body_html": body_html,
                "attachments": attachments,
            }
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def email_read_payload(self, uid, folder="INBOX"):
        if not uid:
            raise ValueError("uid required")
        return json.dumps(self.email_read_data(uid, folder), ensure_ascii=False)

    def email_send_payload(self, body):
        sources = self.email_sources()
        if not sources:
            raise ValueError("No mailbox connected")
        account = sources[0]
        # For the ProtonMail/hydroxide bridge the SMTP server on :1025 must be
        # running before we can send. Starting it is idempotent and cheap.
        if account.get("type") in ("bridge", "hydroxide"):
            err = self.ensure_hydroxide_bridge()
            if err:
                raise RuntimeError(err)
        msg = MIMEMultipart()
        msg["From"] = account.get("smtp_user") or account.get("imap_user") or account.get("display")
        msg["To"] = body.get("to") or ""
        if body.get("cc"):
            msg["Cc"] = body.get("cc")
        if body.get("bcc"):
            msg["Bcc"] = body.get("bcc")
        msg["Subject"] = body.get("subject") or ""
        msg.attach(MIMEText(body.get("body") or "", "plain", "utf-8"))
        host = account.get("smtp_host") or account.get("imap_host")
        port = int(account.get("smtp_port") or 465)
        use_ssl = bool(int(account.get("smtp_ssl") or 0))
        if use_ssl:
            # Implicit TLS (e.g. port 465).
            smtp = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            # Plain connection that must be upgraded with STARTTLS before AUTH.
            # This covers submission port 587 AND the local hydroxide bridge on
            # :1025, which also requires STARTTLS+AUTH.
            smtp = smtplib.SMTP(host, port, timeout=30)
        try:
            if not use_ssl:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
            user = account.get("smtp_user") or account.get("imap_user") or ""
            password = account.get("smtp_password") or account.get("imap_password") or ""
            if user or password:
                smtp.login(user, password)
            # NB: pass only non-empty header values — getaddresses() on Python
            # 3.9 returns a bogus empty pair when the list contains "" entries,
            # which silently drops all recipients.
            addr_fields = [v for v in (msg.get("To", ""), msg.get("Cc", ""), msg.get("Bcc", "")) if v]
            recipients = [a for _, a in email.utils.getaddresses(addr_fields) if a]
            if not recipients:
                raise ValueError("No recipients")
            # sendmail raises SMTPException on failure; refused recipients come
            # back in the returned dict — treat any refusals as a hard error so
            # the tool reports exit_code=1 instead of a silent success.
            refused = smtp.sendmail(msg["From"], recipients, msg.as_string())
            if refused:
                raise RuntimeError("SMTP refused recipients: " + json.dumps(refused))
            return json.dumps({"success": True, "sent": len(recipients)}, ensure_ascii=False)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    def email_flag_payload(self, uid, flag, add=True, expunge=False, archive=False, folder="INBOX"):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            raise ValueError("No mailbox connected")
        conn = self.imap_connect(account)
        try:
            conn.select(folder or "INBOX")
            if archive:
                conn.create("Archive")
                conn.uid("COPY", msg_uid, "Archive")
                conn.uid("STORE", msg_uid, "+FLAGS", "\\Deleted")
                conn.expunge()
            else:
                conn.uid("STORE", msg_uid, "+FLAGS" if add else "-FLAGS", flag)
                if expunge:
                    conn.expunge()
            return json.dumps({"success": True}, ensure_ascii=False)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
