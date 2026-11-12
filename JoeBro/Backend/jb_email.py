"""IMAP/SMTP email: list, read, send, flags, accounts."""
from jb_core import *  # noqa: F401,F403
import jb_ext
from email.mime.base import MIMEBase
from email import encoders


class EmailMixin:
    _email_cache_lock = threading.Lock()
    _email_cache_refreshing = set()
    _email_cache_generation = 0
    _email_cache_fresh_seconds = 90

    @staticmethod
    def port_open(host, port):
        try:
            with socket.create_connection((host, int(port)), timeout=0.4):
                return True
        except OSError:
            return False

    def ensure_hydroxide_bridge(self):
        """Start a local mail bridge an extension provides (hook `email_bridge_ensure`).
        Core ships plain IMAP/SMTP only. Returns an error string, or None when ready."""
        return jb_ext.first("email_bridge_ensure", self)

    def email_sources(self):
        return self.store.rows("select * from email_sources order by datetime(created_at) asc")

    @staticmethod
    def email_source_json(row):
        return {"id": row["id"], "display": row["display"], "type": row["type"]}

    @staticmethod
    def email_account_from_login(address, password):
        email_addr = (address or "").strip()
        secret = password or ""
        domain = email_addr.rsplit("@", 1)[-1].lower() if "@" in email_addr else ""
        custom = jb_ext.first("email_account_from_login", email_addr, secret)   # e.g. a local mail bridge
        if custom:
            return custom
        bridge_domains = set()
        providers = {
            "gmail.com": ("imap.gmail.com", 993, True, "smtp.gmail.com", 465, True),
            "googlemail.com": ("imap.gmail.com", 993, True, "smtp.gmail.com", 465, True),
            "icloud.com": ("imap.mail.me.com", 993, True, "smtp.mail.me.com", 587, False),
            "me.com": ("imap.mail.me.com", 993, True, "smtp.mail.me.com", 587, False),
            "mac.com": ("imap.mail.me.com", 993, True, "smtp.mail.me.com", 587, False),
            "outlook.com": ("outlook.office365.com", 993, True, "smtp-mail.outlook.com", 587, False),
            "hotmail.com": ("outlook.office365.com", 993, True, "smtp-mail.outlook.com", 587, False),
            "live.com": ("outlook.office365.com", 993, True, "smtp-mail.outlook.com", 587, False),
            "fastmail.com": ("imap.fastmail.com", 993, True, "smtp.fastmail.com", 465, True),
            "yahoo.com": ("imap.mail.yahoo.com", 993, True, "smtp.mail.yahoo.com", 465, True),
        }
        if domain in bridge_domains:
            return {
                "type": "bridge",
                "email": email_addr,
                "imap_host": "127.0.0.1",
                "imap_port": 1143,
                "imap_ssl": False,
                "imap_user": email_addr,
                "imap_password": secret,
                "smtp_host": "127.0.0.1",
                "smtp_port": 1025,
                "smtp_ssl": False,
                "smtp_user": email_addr,
                "smtp_password": secret,
            }
        imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl = providers.get(
            domain,
            (f"imap.{domain}" if domain else "", 993, True, f"smtp.{domain}" if domain else "", 465, True),
        )
        return {
            "type": "imap",
            "email": email_addr,
            "imap_host": imap_host,
            "imap_port": imap_port,
            "imap_ssl": imap_ssl,
            "imap_user": email_addr,
            "imap_password": secret,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_ssl": smtp_ssl,
            "smtp_user": email_addr,
            "smtp_password": secret,
        }

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
        self.invalidate_email_cache()
        return account_id

    def invalidate_email_cache(self):
        # Reads and mutations may arrive on different HTTP handler threads.
        with EmailMixin._email_cache_lock:
            EmailMixin._email_cache_generation += 1
            self.store.exec("create table if not exists email_list_cache (cache_key text primary key, payload text not null, updated_at real not null)")
            self.store.exec("delete from email_list_cache")

    def split_mail_uid(self, uid):
        raw = urllib.parse.unquote(uid)
        if "::" in raw:
            account_id, msg_uid = raw.split("::", 1)
            account = self.store.one("select * from email_sources where id=?", (account_id,))
            return account, msg_uid
        sources = self.email_sources()
        return (sources[0] if sources else None), raw

    # --- pooled, persistent IMAP connections -------------------------------
    # Measured: hydroxide re-loads the whole mailbox (~35-90 s for 6.5k
    # messages) whenever its LAST IMAP session closes; while any session stays
    # logged in, every command is ~15-30 ms. The old code opened and logged out
    # a connection per request, so every cold list/read paid the reload. We now
    # keep authenticated connections pooled, always hold >=1 open per account
    # (the "anchor"), NOOP them to keep them alive, and reconnect on failure.
    _imap_pool = {}          # key -> [[conn, last_used], ...] idle connections
    _imap_out = {}           # key -> number of connections currently checked out
    _imap_lock = threading.Lock()
    _imap_thread = None
    IMAP_POOL_IDLE_MAX = 3
    IMAP_TIMEOUT = 240
    IMAP_PING_AFTER = 20
    IMAP_ERRORS = (imaplib.IMAP4.abort, OSError, EOFError)

    @staticmethod
    def _imap_key(account):
        return (account["id"], account.get("imap_host"), account.get("imap_port"),
                account.get("imap_user"), account.get("imap_password"), account.get("imap_ssl"))

    @staticmethod
    def _imap_close(conn):
        try:
            conn.logout()
        except Exception:
            pass

    def _imap_checkout(self, key):
        while True:
            with EmailMixin._imap_lock:
                idle = EmailMixin._imap_pool.get(key) or []
                entry = idle.pop() if idle else None
                if entry is not None:
                    EmailMixin._imap_out[key] = EmailMixin._imap_out.get(key, 0) + 1
            if entry is None:
                return None
            conn, last = entry
            if time.time() - last > 2 * self.IMAP_PING_AFTER:
                try:
                    conn.noop()
                except Exception:
                    self._imap_close(conn)
                    with EmailMixin._imap_lock:
                        EmailMixin._imap_out[key] -= 1
                    continue
            return conn

    def _imap_checkin(self, key, conn, healthy=True):
        close = not healthy
        with EmailMixin._imap_lock:
            EmailMixin._imap_out[key] = max(0, EmailMixin._imap_out.get(key, 0) - 1)
            idle = EmailMixin._imap_pool.setdefault(key, [])
            if healthy and len(idle) < self.IMAP_POOL_IDLE_MAX:
                idle.append([conn, time.time()])
            else:
                close = True
        if close:
            self._imap_close(conn)

    def imap_run(self, account, fn, retry=True):
        """Run fn(conn) on a pooled connection. A stale pooled connection is
        replaced and the call retried once when retry=True (reads); writes pass
        retry=False so a half-sent COPY/STORE is never replayed."""
        self.email_keepalive_start()
        key = self._imap_key(account)
        for attempt in (0, 1):
            conn = self._imap_checkout(key) if attempt == 0 else None
            fresh = conn is None
            if fresh:
                conn = self.imap_connect(account)
                with EmailMixin._imap_lock:
                    EmailMixin._imap_out[key] = EmailMixin._imap_out.get(key, 0) + 1
            try:
                result = fn(conn)
            except self.IMAP_ERRORS:
                self._imap_checkin(key, conn, healthy=False)
                if not fresh and retry:
                    continue
                raise
            except BaseException:
                self._imap_checkin(key, conn, healthy=True)   # protocol-level error, link is fine
                raise
            self._imap_checkin(key, conn)
            return result

    def email_keepalive_start(self):
        with EmailMixin._imap_lock:
            if EmailMixin._imap_thread is not None:
                return
            EmailMixin._imap_thread = threading.Thread(target=self._email_keepalive_loop, daemon=True)
            EmailMixin._imap_thread.start()

    def _email_keepalive_loop(self):
        while True:
            try:
                self.email_keepalive_once()
            except Exception:
                pass
            time.sleep(self.IMAP_PING_AFTER)

    def email_keepalive_once(self):
        """Keep one warm session per account (so hydroxide never unloads the
        mailbox) and NOOP idle pooled sessions; forget removed accounts."""
        accounts = self.email_sources()
        live = {self._imap_key(a): a for a in accounts}
        with EmailMixin._imap_lock:
            stale = [k for k in EmailMixin._imap_pool if k not in live]
            drop = [e[0] for k in stale for e in EmailMixin._imap_pool.pop(k)]
        for conn in drop:
            self._imap_close(conn)
        for key, account in live.items():
            with EmailMixin._imap_lock:
                idle = list(EmailMixin._imap_pool.get(key) or [])
                busy = EmailMixin._imap_out.get(key, 0)
            if not idle and not busy:
                self._imap_warm(account, key)
                continue
            for entry in idle:
                if time.time() - entry[1] < self.IMAP_PING_AFTER:
                    continue
                with EmailMixin._imap_lock:
                    pool = EmailMixin._imap_pool.get(key) or []
                    if entry not in pool:
                        continue
                    pool.remove(entry)
                    EmailMixin._imap_out[key] = EmailMixin._imap_out.get(key, 0) + 1
                try:
                    entry[0].noop()
                    self._imap_checkin(key, entry[0])
                except Exception:
                    self._imap_checkin(key, entry[0], healthy=False)

    def _imap_warm(self, account, key):
        """Open the anchor session and touch the mailbox so hydroxide loads it now."""
        conn = self.imap_connect(account)
        with EmailMixin._imap_lock:
            EmailMixin._imap_out[key] = EmailMixin._imap_out.get(key, 0) + 1
        try:
            status, data = conn.select("INBOX", readonly=True)
            if status == "OK" and data and int(data[0] or 0) > 0:
                conn.fetch(data[0].decode(), "(UID FLAGS)")
        except Exception:
            self._imap_checkin(key, conn, healthy=False)
            return
        self._imap_checkin(key, conn)

    def imap_connect(self, account):
        # Bridge accounts (ProtonMail/hydroxide) need the local bridge running;
        # ensure it for every IMAP op (list, mark-read, archive, …), not just connect.
        if account.get("type") in {"bridge", "hydroxide"} or str(account.get("imap_host")) == "127.0.0.1":
            self.ensure_hydroxide_bridge()
        cls = imaplib.IMAP4_SSL if int(account.get("imap_ssl") or 0) else imaplib.IMAP4
        conn = cls(account["imap_host"], int(account["imap_port"]), timeout=self.IMAP_TIMEOUT)
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
                status, rows = self.imap_run(account, lambda conn: conn.list())
                if status == "OK":
                    for raw in rows or []:
                        text = raw.decode("utf-8", errors="replace")
                        name = text.rsplit(' "/" ', 1)[-1].strip('"') if ' "/" ' in text else text.rsplit(" ", 1)[-1].strip('"')
                        if name:
                            folders.add(name)
            except Exception:
                continue
        return self.json({"folders": sorted(folders), "configured": configured})

    @staticmethod
    def fetch_email_headers(conn, selected, fields):
        """Fetch selected UIDs in pages; retry missing ones for quirky IMAP servers."""
        fetched = {}
        for start in range(0, len(selected), 50):
            batch = selected[start:start + 50]
            try:
                status, rows = conn.uid("fetch", b",".join(batch), fields)
            except Exception:
                status, rows = "NO", []
            if status == "OK":
                current_uid = None
                for item in rows or []:
                    if not isinstance(item, tuple):
                        if current_uid is not None:
                            fetched[current_uid].append(item)
                        continue
                    metadata = item[0] or b""
                    match = re.search(rb"\bUID\s+(\d+)\b", metadata, re.I)
                    if match:
                        current_uid = match.group(1)
                        fetched[current_uid] = [item]
                    else:
                        current_uid = None
            for raw_uid in batch:
                if raw_uid in fetched:
                    continue
                status, rows = conn.uid("fetch", raw_uid, fields)
                if status == "OK" and rows:
                    fetched[raw_uid] = rows
        return fetched

    def email_list(self, q):
        return self.json(self.cached_email_list(q))

    def cached_email_list(self, q):
        sources = self.email_sources()
        # The mailbox page size belongs to the backend so Mac and iOS see the
        # same recent messages. Explicit limits remain available to callers
        # such as the unread badge, which only needs one message.
        raw_limit = q.get("limit")
        if raw_limit is None:
            raw_limit = self.pref("email_load_count")
        try:
            limit = max(0, int(50 if raw_limit is None else raw_limit))
        except (TypeError, ValueError):
            limit = 50
        folder = q.get("folder") or "INBOX"
        filt = q.get("filter") or "all"
        query = {"folder": folder, "filter": filt, "limit": limit}
        key = json.dumps([query, [(s["id"], s.get("updated_at")) for s in sources]], sort_keys=True)
        self.store.exec("create table if not exists email_list_cache (cache_key text primary key, payload text not null, updated_at real not null)")
        row = self.store.one("select payload, updated_at from email_list_cache where cache_key=?", (key,))
        force = str(q.get("refresh") or "").lower() in {"1", "true", "yes"}
        if row and not force:
            payload = json.loads(row["payload"])
            stale = time.time() - row["updated_at"] >= self._email_cache_fresh_seconds
            if stale:
                with EmailMixin._email_cache_lock:
                    if key not in EmailMixin._email_cache_refreshing:
                        EmailMixin._email_cache_refreshing.add(key)
                        generation = EmailMixin._email_cache_generation
                        threading.Thread(target=self._refresh_email_cache,
                                         args=(key, query, sources, generation), daemon=True).start()
            payload["stale"] = stale
            return payload
        with EmailMixin._email_cache_lock:
            generation = EmailMixin._email_cache_generation
        payload = self._email_list_live(query, sources)
        if not payload.get("error"):
            self._store_email_cache(key, payload, generation)
        payload["stale"] = False
        return payload

    def _store_email_cache(self, key, payload, generation):
        with EmailMixin._email_cache_lock:
            if generation == EmailMixin._email_cache_generation:
                self.store.exec("insert or replace into email_list_cache(cache_key,payload,updated_at) values(?,?,?)",
                                (key, json.dumps(payload, ensure_ascii=False), time.time()))

    def _refresh_email_cache(self, key, query, sources, generation):
        try:
            payload = self._email_list_live(query, sources)
            if not payload.get("error"):
                self._store_email_cache(key, payload, generation)
        except Exception:
            pass
        finally:
            with EmailMixin._email_cache_lock:
                EmailMixin._email_cache_refreshing.discard(key)

    def _email_list_live(self, q, sources):
        limit = q["limit"]
        folder = q["folder"]
        filt = q["filter"]
        criterion = "UNSEEN" if filt == "unread" else "ALL"
        out = []
        errors = []
        total_matched = 0   # all messages matching the filter, before the page limit
        for account in sources:
            try:
                def pull(conn):
                    status, _ = conn.select(folder, readonly=True)
                    if status != "OK" and folder != "INBOX":
                        status, _ = conn.select("INBOX", readonly=True)
                    if status != "OK":
                        raise RuntimeError("Mailbox unavailable")
                    status, data = conn.uid("search", None, criterion)
                    if status != "OK":
                        raise RuntimeError("Search failed")
                    uids = (data[0] or b"").split()
                    selected = list(reversed(uids if limit == 0 else uids[-limit:]))
                    # Only the three headers the list shows; the rest of the header block is dead weight.
                    return uids, selected, self.fetch_email_headers(
                        conn, selected, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS RFC822.SIZE)")
                uids, selected, fetched = self.imap_run(account, pull)
                total_matched += len(uids)
                for raw_uid in selected:
                    rows = fetched.get(raw_uid)
                    if not rows:
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
        # `limit` is a mailbox-wide count, even when several accounts are
        # configured. The per-account UID slice above only bounds IMAP work.
        if limit:
            out = out[:limit]
        return {"emails": out, "total": total_matched, "configured": bool(sources), "error": "; ".join(errors) if errors and not out else None}

    def email_read(self, uid):
        try:
            return self.json(self.email_read_data(uid, self._query().get("folder") or "INBOX"))
        except ValueError as exc:
            return self.json({"error": str(exc)}, 400)
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
        return self.email_store_flag(uid, "", archive=True)

    def email_delete(self, uid):
        return self.email_store_flag(uid, "\\Deleted", add=True, expunge=True)

    def email_store_flag(self, uid, flag, add=True, expunge=False, archive=False):
        try:
            self.email_flag_payload(uid, flag, add=add, expunge=expunge, archive=archive,
                                    folder=self._query().get("folder") or "INBOX")
            return self.json({"success": True})
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, 400)
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, 500)

    def email_send(self, body):
        try:
            return self.json(json.loads(self.email_send_payload(body)))
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, 400)
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, 500)

    def email_list_payload(self, args):
        sources = self.email_sources()
        if not sources:
            return json.dumps({"emails": [], "total": 0, "configured": False}, ensure_ascii=False)
        limit = min(50, max(1, int(args.get("max_results") or args.get("limit") or 50)))
        folder = args.get("folder") or "INBOX"
        filt = args.get("filter") or "all"
        payload = self.cached_email_list({"folder": folder,
                                          "filter": "unread" if args.get("unread_only") or filt == "unread" else "all",
                                          "limit": limit})
        account_names = {source["id"]: source.get("display") for source in sources}
        emails = []
        for message in payload["emails"]:
            account_id = urllib.parse.unquote(message["uid"]).split("::", 1)[0]
            emails.append({key: message[key] for key in ("uid", "subject", "from_name", "from_address", "date")})
            emails[-1]["account"] = account_names.get(account_id)
        return json.dumps({"emails": emails, "total": len(emails), "configured": True,
                           "stale": payload.get("stale", False), "error": payload.get("error")}, ensure_ascii=False)

    def email_read_data(self, uid, folder="INBOX"):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            raise ValueError("No mailbox connected")
        def grab(conn):
            status, _ = conn.select(folder)
            if status != "OK":
                raise RuntimeError("Mailbox unavailable")
            status, rows = conn.uid("fetch", msg_uid, "(RFC822)")
            if status != "OK":
                raise RuntimeError("Message fetch failed")
            raw = b"".join(item[1] for item in rows if isinstance(item, tuple))
            conn.uid("STORE", msg_uid, "+FLAGS", "\\Seen")
            return raw
        raw = self.imap_run(account, grab)
        self.invalidate_email_cache()
        if True:
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

    # --- attachments (read-only: select(readonly) + BODY.PEEK, never touches flags) ---
    ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
    ATTACHMENT_TEXT_CAP = 20000
    _TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml", "application/yaml",
                   "application/rtf", "application/x-sh", "application/javascript", "application/sql"}
    _TEXT_EXTS = {".txt", ".csv", ".tsv", ".md", ".markdown", ".json", ".xml", ".yaml", ".yml", ".log",
                  ".ics", ".vcf", ".html", ".htm", ".rtf", ".sql", ".py", ".js", ".ini", ".cfg", ".eml"}

    @staticmethod
    def safe_attachment_name(name, fallback):
        """Display-only name: strip any directory part and control chars. Attachment
        bytes are never written under this name (temp files get a random name)."""
        base = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).replace("\\", "/").rsplit("/", 1)[-1].strip(" .")
        return base[:200] or fallback

    def email_attachment_parts(self, msg):
        """[(walk_index, part, safe_filename)] — walk_index matches the `index`
        email_read reports. Counts attachments and any named non-body part."""
        out = []
        for i, part in enumerate(msg.walk()):
            if part.is_multipart():
                continue
            cdisp = (part.get_content_disposition() or "").lower()
            fname = part.get_filename()
            if cdisp == "attachment" or fname:
                out.append((i, part, self.safe_attachment_name(self.decode_header_value(fname), f"attachment-{i}")))
        return out

    def email_fetch_message(self, uid, folder):
        account, msg_uid = self.split_mail_uid(uid)
        if not account:
            raise ValueError("No mailbox connected")
        def grab(conn):
            status, _ = conn.select(folder or "INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Mailbox unavailable")
            status, rows = conn.uid("fetch", msg_uid, "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError("Message fetch failed")
            return b"".join(item[1] for item in rows if isinstance(item, tuple))
        raw = self.imap_run(account, grab)
        if not raw:
            raise ValueError(f"No message found for uid {uid}")
        return email.message_from_bytes(raw, policy=email.policy.default)

    def extract_attachment_text(self, data, mime, filename, pages=None, charset=None):
        """(text, note). text is None when the format can't be extracted."""
        ext = os.path.splitext(filename)[1].lower()
        mime = (mime or "").lower()
        if mime == "application/pdf" or ext == ".pdf":
            with tempfile.TemporaryDirectory() as td:
                tp = Path(td) / "attachment.pdf"
                tp.write_bytes(data)
                return self.read_pdf_text(tp, pages), "pdf"
        if ext == ".docx" or mime.endswith("wordprocessingml.document"):
            with tempfile.TemporaryDirectory() as td:
                tp = Path(td) / "attachment.docx"
                tp.write_bytes(data)
                text = read_doc_context(tp)
            return (text, "docx") if text.strip() else (None, "docx contained no extractable text")
        if ext == ".xlsx" or mime.endswith("spreadsheetml.sheet"):
            from jb_xlsx import xlsx_to_csv
            return xlsx_to_csv(data), "xlsx (as CSV)"
        if mime.startswith("text/") or mime in self._TEXT_MIMES or ext in self._TEXT_EXTS:
            try:
                text = data.decode(charset or "utf-8", errors="replace")
            except LookupError:
                text = data.decode("utf-8", errors="replace")
            if mime == "text/html" or ext in (".html", ".htm"):
                text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
                text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>", "\n", text)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
            return text, "text"
        return None, "unsupported"

    def email_attachment_payload(self, args):
        """List a message's attachments, or extract one as text (read-only)."""
        uid = args.get("uid")
        if not uid:
            raise ValueError("uid required")
        msg = self.email_fetch_message(uid, args.get("folder") or "INBOX")
        parts = self.email_attachment_parts(msg)
        listing = []
        for i, part, fname in parts:
            size = len(part.get_payload(decode=True) or b"")
            listing.append({"index": i, "filename": fname, "content_type": part.get_content_type(), "size": size})
        sel = args.get("index")
        want_name = args.get("filename")
        if sel in (None, "") and not want_name:
            return json.dumps({"uid": uid, "count": len(listing), "attachments": listing,
                               "hint": "call again with `index` (or `filename`) to read one attachment as text"},
                              ensure_ascii=False)
        chosen = None
        for entry, (i, part, fname) in zip(listing, parts):
            if (sel not in (None, "") and str(i) == str(sel)) or \
               (want_name and fname.lower() == self.safe_attachment_name(want_name, "").lower()):
                chosen = (entry, part)
                break
        if not chosen:
            return json.dumps({"error": "attachment not found", "attachments": listing}, ensure_ascii=False)
        entry, part = chosen
        result = dict(entry)
        if entry["size"] > self.ATTACHMENT_MAX_BYTES:
            result.update(extracted=False, text="",
                          note=f"Attachment is {entry['size']} bytes, over the {self.ATTACHMENT_MAX_BYTES} byte limit; not extracted.")
            return json.dumps(result, ensure_ascii=False)
        data = part.get_payload(decode=True) or b""
        try:
            text, note = self.extract_attachment_text(data, entry["content_type"], entry["filename"], args.get("pages"), part.get_content_charset())
        except Exception as exc:
            text, note = None, f"extraction failed: {exc}"
        if text is None:
            result.update(extracted=False, text="", note=(
                f"Cannot extract text from {entry['content_type']} ({entry['filename']}): "
                + ("no extractor for this format (images/audio/video/archives/binaries are not supported)." if note == "unsupported" else note)))
            return json.dumps(result, ensure_ascii=False)
        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        total = len(text)
        chunk = text[offset:offset + self.ATTACHMENT_TEXT_CAP]
        result.update(extracted=True, format=note, text=chunk, total_chars=total)
        if offset + len(chunk) < total:
            result["truncated"] = True
            result["note"] = f"Showing chars {offset}-{offset + len(chunk)} of {total}. Call again with offset={offset + len(chunk)} for more."
        return json.dumps(result, ensure_ascii=False)

    def email_attachment(self, uid, index, folder="INBOX"):
        """Return one raw attachment for an explicit user download.

        The identifier is the MIME walk index emitted by email_read. The UID
        is resolved through the existing account mapping and fetched with
        BODY.PEEK, so downloading never marks a message read. The response
        filename is sanitized and is never used as a filesystem path.
        """
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise ValueError("attachment index required")
        if index < 0:
            raise ValueError("attachment index must be non-negative")
        msg = self.email_fetch_message(uid, folder or "INBOX")
        for walk_index, part, filename in self.email_attachment_parts(msg):
            if walk_index != index:
                continue
            data = part.get_payload(decode=True) or b""
            if len(data) > self.ATTACHMENT_MAX_BYTES:
                raise ValueError(f"Attachment exceeds {self.ATTACHMENT_MAX_BYTES} byte download limit")
            mime = part.get_content_type() or "application/octet-stream"
            return data, mime, filename
        raise ValueError("attachment not found")

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
        msg = MIMEMultipart("mixed")
        msg["From"] = account.get("smtp_user") or account.get("imap_user") or account.get("display")
        msg["To"] = body.get("to") or ""
        if body.get("cc"):
            msg["Cc"] = body.get("cc")
        if body.get("bcc"):
            msg["Bcc"] = body.get("bcc")
        msg["Subject"] = body.get("subject") or ""
        if body.get("in_reply_to"):
            msg["In-Reply-To"] = body.get("in_reply_to")
        if body.get("references"):
            msg["References"] = body.get("references")
        plain = MIMEText(body.get("body") or "", "plain", "utf-8")
        html = body.get("body_html")
        if html:
            alternative = MIMEMultipart("alternative")
            alternative.attach(plain)
            alternative.attach(MIMEText(str(html), "html", "utf-8"))
            msg.attach(alternative)
        else:
            msg.attach(plain)

        # Upload ids are opaque server-generated tokens. Resolve each through
        # the uploads table and constrain its stored path to the uploads dir;
        # never accept paths or filenames from the send request as filesystem
        # locations. Invalid tokens fail before opening an SMTP connection.
        tokens = body.get("attachment_tokens") or []
        if not isinstance(tokens, list):
            raise ValueError("attachment_tokens must be a list")
        if len(tokens) > 20:
            raise ValueError("At most 20 attachments may be sent")
        if len(set(str(token) for token in tokens)) != len(tokens):
            raise ValueError("Duplicate attachment token")
        max_total = 25 * 1024 * 1024
        total_bytes = 0
        upload_dir = (self.store.root / "uploads").resolve()
        for token in tokens:
            token = str(token or "")
            if not re.fullmatch(r"up_[0-9a-fA-F]{16}", token):
                raise ValueError("Invalid attachment token")
            row = self.store.one("select name,path,mime from uploads where id=?", (token,))
            if not row:
                raise ValueError("Uploaded attachment has expired or was not found")
            path = Path(row["path"]).resolve()
            if path.parent != upload_dir:
                raise ValueError("Uploaded attachment path is invalid")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"Uploaded attachment could not be read: {exc}")
            total_bytes += len(data)
            if total_bytes > max_total:
                raise ValueError("Total attachment size exceeds 25 MB")
            filename = self.safe_attachment_name(row["name"], "attachment")
            content_type = (row["mime"] or mimetypes.guess_type(filename)[0] or "application/octet-stream")
            maintype, _, subtype = content_type.partition("/")
            if maintype not in {"application", "audio", "image", "message", "text", "video"} or not subtype:
                maintype, subtype = "application", "octet-stream"
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
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
            self.invalidate_email_cache()
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
        def apply(conn):
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
        self.imap_run(account, apply, retry=False)
        self.invalidate_email_cache()
        return json.dumps({"success": True}, ensure_ascii=False)
