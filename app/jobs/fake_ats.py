"""Stand-in applicant tracking system pages on 127.0.0.1, so the form filler can be run end to end without applying anywhere.

They are written to look like the form shapes the real systems serve, from reading their public job pages, and they are not copies of any
vendor's markup. What each is for:

  /gh/jobs/1   a single page of fields in the Greenhouse style (labels with `for`, a file input for the resume, yes or no questions, an optional
               self-identification select): everything in it is something the knowledge base can know, so a run completes
  /gh/jobs/2   the same plus a required free-text question nobody can pre-answer ("Why do you want to work here?"), so a run must stop and wait
  /gh/jobs/4   the same, with the email field under a long wordy label ("Please tell us the best email address ..."), so the label is not sure enough to type
  /gh/jobs/5   the same, with a required agreement checkbox, which a rule must never tick
  /gh/jobs/3   accepts the post and answers with an error banner and no success text, the failure the filler must notice instead of reporting success
  /wd/jobs/1   a three step flow in the Workday style (name and email, then experience, then a review step with the submit button), Next buttons
  /lever/jobs/1  a Lever style form: the resume first, one Full name field, a yes or no radio group, a free text box
  /custom/jobs/1  inputs with no id, name or label element (placeholder only), the shape that sends the filler to Stagehand
  /greenhouse-embed/jobs/1  a company page with the form embedded, for the page-marker ATS detection

`server.submissions` records what each successful post carried (fields, and the name and size of an uploaded file), which is what the tests read to
prove the form really received the values.
"""
from __future__ import annotations

import email.parser
import email.policy
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

PAGE = "<!doctype html><html><head><meta charset='utf-8'><title>%s</title></head><body>%s</body></html>"


def _row(label: str, control: str, fid: str, required: bool = False) -> str:
    return "<div><label for='%s'>%s%s</label>%s</div>" % (fid, html.escape(label), " *" if required else "", control)


def _gh(job: str, errors: list[str] | None = None) -> str:
    err = "".join("<div role='alert' class='error'>%s</div>" % html.escape(e) for e in (errors or []))
    extra = ""
    if job == "2":
        extra = _row("Why do you want to work here?", "<textarea id='q_why' name='q_why' required></textarea>", "q_why", True)
    email_label = "Please tell us the best email address where our recruiting team can reach you" if job == "4" else "Email"
    consent = ("<div><label><input type='checkbox' id='consent' name='consent' required> I agree to the privacy policy and the terms of this application</label></div>" if job == "5" else "")
    body = (
        "<h1>Backend Engineer at Acme</h1>%s<form id='application' method='post' action='/gh/submit?job=%s' enctype='multipart/form-data'>"
        % (err, job)
        + _row("First Name", "<input id='first_name' name='first_name' autocomplete='given-name' required>", "first_name", True)
        + _row("Last Name", "<input id='last_name' name='last_name' autocomplete='family-name' required>", "last_name", True)
        + _row(email_label, "<input id='email' name='email' required>" if job == "4" else "<input id='email' name='email' type='email' required>", "email", True)
        + _row("Phone", "<input id='phone' name='phone' type='tel' required>", "phone", True)
        + _row("Resume/CV", "<input id='resume' name='resume' type='file' required>", "resume", True)
        + _row("LinkedIn Profile", "<input id='linkedin' name='linkedin'>", "linkedin")
        + _row("Are you legally authorized to work in the United States?",
               "<select id='q_auth' name='q_auth' required><option value=''>Select...</option><option>Yes</option><option>No</option></select>", "q_auth", True)
        + _row("How did you hear about this job?",
               "<select id='q_hear' name='q_hear'><option value=''>Select...</option><option>LinkedIn</option><option>Referral</option><option>Job board</option><option>Other</option></select>", "q_hear")
        + _row("Gender", "<select id='eeo_gender' name='eeo_gender'><option value=''>Select...</option><option>Female</option><option>Male</option><option>Decline to self identify</option></select>", "eeo_gender")
        + extra + consent
        + "<button type='submit'>Submit Application</button></form>")
    keys = "<script>window.__keys = 0; document.addEventListener('keydown', () => { window.__keys += 1; });</script>"
    return PAGE % ("Acme - Backend Engineer", body + keys)


def _wd(step: int, data: dict[str, str]) -> str:
    hidden = "".join("<input type='hidden' name='%s' value='%s'>" % (html.escape(k), html.escape(v)) for k, v in data.items())
    if step == 1:
        fields = (_row("Legal first name", "<input id='fn' name='fn' required>", "fn", True) + _row("Legal last name", "<input id='ln' name='ln' required>", "ln", True)
                  + _row("Email address", "<input id='em' name='em' type='email' required>", "em", True))
        button = "<button type='submit'>Next</button>"
    elif step == 2:
        fields = (_row("LinkedIn URL", "<input id='li' name='li'>", "li") + _row("Salary expectation", "<input id='sal' name='sal' required>", "sal", True))
        button = "<button type='submit'>Next</button>"
    else:
        fields = "<p id='summary'>Review your application for %s %s.</p>" % (html.escape(data.get("fn", "")), html.escape(data.get("ln", "")))
        button = "<button type='submit'>Submit Application</button>"
    return PAGE % ("Workday style", "<h1>Data Engineer</h1><form id='wd' method='post' action='/wd/next?step=%d'>%s%s%s</form>" % (step, hidden, fields, button))


def _lever(errors: list[str] | None = None) -> str:
    """A Lever style posting form: the resume first, a single Full name field, a radio group for a yes or no question, a free text box at the end."""
    err = "".join("<div role='alert' class='error'>%s</div>" % html.escape(e) for e in (errors or []))
    body = (
        "<h1>Automation Engineer at Initech</h1>%s<form id='application-form' method='post' action='/lever/submit' enctype='multipart/form-data'>" % err
        + _row("Resume/CV", "<input id='resume' name='resume' type='file' required>", "resume", True)
        + _row("Full name", "<input id='name' name='name' autocomplete='name' required>", "name", True)
        + _row("Email", "<input id='email' name='email' type='email' required>", "email", True)
        + _row("Phone", "<input id='phone' name='phone' type='tel'>", "phone")
        + _row("Current company", "<input id='org' name='org'>", "org")
        + _row("LinkedIn URL", "<input id='li' name='urls[LinkedIn]'>", "li")
        + "<fieldset><legend>Are you legally authorized to work in the United States? *</legend>"
          "<label><input type='radio' name='auth' value='yes' required> Yes</label><label><input type='radio' name='auth' value='no'> No</label></fieldset>"
        + _row("Additional information", "<textarea id='comments' name='comments'></textarea>", "comments")
        + "<button id='btn-submit' type='submit'>Submit application</button></form>")
    return PAGE % ("Lever style", body)


def _custom() -> str:
    body = ("<h1>Platform Engineer</h1><div id='root'><input class='x' placeholder='First name'><input class='x' placeholder='Last name'>"
            "<input class='x' placeholder='Email address'><button id='go' type='button'>Submit Application</button></div>"
            "<script>document.getElementById('go').onclick=()=>{const v=[...document.querySelectorAll('input.x')].map(i=>i.value);"
            "fetch('/custom/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:v})})"
            ".then(()=>{document.body.innerHTML='<h2>Thank you for applying</h2>'})}</script>")
    return PAGE % ("Custom", body)


class FakeATS:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self) -> "FakeATS":
        ats = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, body: str, status: int = 200) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                p = urlparse(self.path).path
                if p.startswith("/gh/jobs/"):
                    self._send(_gh(p.rsplit("/", 1)[-1]))
                elif p == "/wd/jobs/1":
                    self._send(_wd(1, {}))
                elif p == "/lever/jobs/1":
                    self._send(_lever())
                elif p == "/custom/jobs/1":
                    self._send(_custom())
                elif p == "/greenhouse-embed/jobs/1":
                    self._send(PAGE % ("Careers", "<h1>Careers</h1><div id='grnhse_app'></div>" + _gh("1").split("<body>")[1].split("</body>")[0]))
                else:
                    self._send("not found", 404)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self) -> None:
                u = urlparse(self.path)
                raw = self._body()
                if u.path == "/gh/submit":
                    job = parse_qs(u.query).get("job", ["1"])[0]
                    fields, files = _multipart(self.headers.get("Content-Type", ""), raw)
                    need = ["first_name", "last_name", "email", "phone", "q_auth"] + (["q_why"] if job == "2" else []) + (["consent"] if job == "5" else [])
                    missing = [n for n in need if not fields.get(n)] + ([] if files.get("resume") else ["resume"])
                    if job == "3":
                        self._send(_gh(job, ["Something went wrong while saving your application. Please try again later."]))
                        return
                    if missing:
                        self._send(_gh(job, ["Please fill in: " + ", ".join(missing)]))
                        return
                    ats.submissions.append({"kind": "gh", "job": job, "fields": fields, "files": files})
                    self._send(PAGE % ("Thanks", "<h2 data-testid='application-success'>Thank you for applying</h2><p>Your application has been received.</p>"))
                elif u.path == "/wd/next":
                    step = int(parse_qs(u.query).get("step", ["1"])[0])
                    data = {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
                    if step >= 3:
                        ats.submissions.append({"kind": "wd", "fields": data})
                        self._send(PAGE % ("Thanks", "<h2>Application submitted</h2>"))
                    else:
                        self._send(_wd(step + 1, data))
                elif u.path == "/lever/submit":
                    fields, files = _multipart(self.headers.get("Content-Type", ""), raw)
                    missing = [n for n in ("name", "email", "auth") if not fields.get(n)] + ([] if files.get("resume") else ["resume"])
                    if missing:
                        self._send(_lever(["Please fill in: " + ", ".join(missing)]))
                        return
                    ats.submissions.append({"kind": "lever", "fields": fields, "files": files})
                    self._send(PAGE % ("Thanks", "<h2>Application submitted</h2><p>We have received your application.</p>"))
                elif u.path == "/custom/submit":
                    ats.submissions.append({"kind": "custom", "fields": json.loads(raw or b"{}")})
                    self._send("{}")
                else:
                    self._send("not found", 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _multipart(content_type: str, raw: bytes) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + raw)
    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fn = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if fn:
            if payload:
                files[name] = {"filename": fn, "size": len(payload), "head": payload[:8]}
        else:
            fields[name] = payload.decode("utf-8")
    return fields, files
