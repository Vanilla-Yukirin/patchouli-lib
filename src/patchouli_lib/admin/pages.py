from __future__ import annotations

from html import escape

from patchouli_lib.admin.service import DeliveredCredential
from patchouli_lib.auth.schemas import SectionAction

STYLESHEET = """
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, sans-serif;
  background: #f3f5f1;
  color: #1d2a21;
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; }
a { color: #355f43; }
header {
  background: #183c2a;
  color: #fff;
  padding: 1rem max(1rem, calc((100% - 70rem) / 2));
}
header a { color: #fff; text-decoration: none; }
nav { display: flex; flex-wrap: wrap; gap: .8rem; align-items: center; }
nav form { margin-left: auto; }
main { max-width: 70rem; margin: 0 auto; padding: 2rem 1rem 4rem; }
.narrow { max-width: 30rem; }
.grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr)); }
.card {
  background: #fff;
  border: 1px solid #d6ddd5;
  border-radius: .7rem;
  box-shadow: 0 .25rem 1rem rgb(24 60 42 / 8%);
  padding: 1.2rem;
}
label { display: block; font-weight: 650; margin-top: .8rem; }
input, textarea, select {
  border: 1px solid #87988b;
  border-radius: .35rem;
  display: block;
  font: inherit;
  margin-top: .25rem;
  padding: .55rem;
  width: 100%;
}
fieldset { border: 1px solid #d6ddd5; margin-top: .8rem; }
fieldset label { display: flex; gap: .5rem; font-weight: 400; }
fieldset input { width: auto; }
button {
  background: #2f6845;
  border: 0;
  border-radius: .35rem;
  color: #fff;
  cursor: pointer;
  font: inherit;
  font-weight: 700;
  margin-top: 1rem;
  padding: .6rem .9rem;
}
.secondary { background: #587063; margin-top: 0; }
.notice { border-left: .3rem solid #c57d14; padding: .7rem 1rem; background: #fff7e8; }
.error { border-left-color: #a92f2f; background: #fff0f0; }
.secret {
  display: block;
  overflow-wrap: anywhere;
  padding: 1rem;
  background: #17231c;
  color: #dff6e6;
  user-select: all;
}
pre { overflow-x: auto; background: #17231c; color: #dff6e6; padding: 1rem; }
dt { font-weight: 700; }
dd { margin: 0 0 .7rem; overflow-wrap: anywhere; }
small { color: #526259; }
""".strip()


def login_page(*, message: str | None = None) -> str:
    notice = "" if message is None else _notice(message, error=True)
    content = f"""
<main class="narrow">
  <section class="card">
    <h1>PatchouliLib administration</h1>
    <p>This console manages local application state. It cannot deploy images,
    run host commands, or control Docker.</p>
    {notice}
    <form method="post" action="/admin/login" autocomplete="off">
      <label for="password">Administration password</label>
      <input id="password" name="password" type="password"
        minlength="12" maxlength="1024" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </section>
</main>
"""
    return _document("Administration sign in", content)


def dashboard_page(csrf_token: str, *, message: str | None = None) -> str:
    csrf = escape(csrf_token, quote=True)
    notice = "" if message is None else _notice(message, error=True)
    grants = "".join(
        (
            '<label><input type="checkbox" name="grants" '
            f'value="{escape(action.value, quote=True)}"> '
            f"{escape(action.value)}</label>"
        )
        for action in SectionAction
    )
    content = f"""
{_header(csrf)}
<main>
  <h1>Administration</h1>
  <p class="notice">New credentials are displayed once. Keep them out of URLs,
  screenshots, logs, and chat. Operator credentials entered below are used for
  one request and are not placed in the browser session.</p>
  {notice}
  <div class="grid">
    <section class="card">
      <h2>Initialize the library</h2>
      <p>Creates the first Library, Section, Book, and local operator.</p>
      <form method="post" action="/admin/bootstrap" autocomplete="off">
        {_csrf(csrf)}
        {_text("library_name", "Library name")}
        {_text("section_name", "Section name")}
        {_textarea("section_description", "Section description")}
        {_text("book_name", "Book name")}
        {_textarea("book_summary", "Book summary")}
        {_text("operator_name", "Operator name")}
        {_textarea("operator_description", "Operator description")}
        {_number("credential_ttl_seconds", "Credential lifetime in seconds", 3600)}
        <button type="submit">Initialize</button>
      </form>
    </section>
    <section class="card">
      <h2>Recover the operator</h2>
      <p>Revokes active operator credentials and issues one replacement.</p>
      <form method="post" action="/admin/recover" autocomplete="off">
        {_csrf(csrf)}
        {_text("library_name", "Library name")}
        {_number("credential_ttl_seconds", "Credential lifetime in seconds", 3600)}
        <button type="submit">Recover credential</button>
      </form>
    </section>
    <section class="card">
      <h2>Provision an Agent</h2>
      <form method="post" action="/admin/agents/provision" autocomplete="off">
        {_csrf(csrf)}
        {_secret("operator_token", "Current operator credential")}
        {_text("library_name", "Library name")}
        {_text("section_name", "Section name")}
        {_text("agent_name", "Agent name")}
        {_textarea("agent_description", "Agent description")}
        {_number("credential_ttl_seconds", "Credential lifetime in seconds", 3600)}
        <fieldset><legend>Exact Section permissions</legend>{grants}</fieldset>
        <button type="submit">Create Agent credential</button>
      </form>
    </section>
    <section class="card">
      <h2>Revoke an Agent credential</h2>
      <form method="post" action="/admin/agents/revoke" autocomplete="off">
        {_csrf(csrf)}
        {_secret("operator_token", "Current operator credential")}
        {_text("library_name", "Library name")}
        {_text("caller_id", "Agent caller ID")}
        {_text("credential_id", "Agent credential ID")}
        <button type="submit">Revoke credential</button>
      </form>
    </section>
  </div>
</main>
"""
    return _document("Administration", content)


def credential_page(
    csrf_token: str,
    *,
    heading: str,
    result: DeliveredCredential,
) -> str:
    csrf = escape(csrf_token, quote=True)
    content = f"""
{_header(csrf)}
<main class="narrow">
  <section class="card">
    <h1>{escape(heading)}</h1>
    <p class="notice">This value is shown only in this response. Store it in an
    approved secret store before leaving this page.</p>
    <code class="secret">{escape(result.value)}</code>
    <dl>
      <dt>Library ID</dt><dd>{escape(result.library_id)}</dd>
      <dt>Caller ID</dt><dd>{escape(result.caller_id)}</dd>
      <dt>Credential ID</dt><dd>{escape(result.credential_id)}</dd>
    </dl>
    <p><a href="/admin">Return to administration</a></p>
  </section>
</main>
"""
    return _document(heading, content)


def action_result_page(csrf_token: str, *, heading: str, message: str) -> str:
    csrf = escape(csrf_token, quote=True)
    content = f"""
{_header(csrf)}
<main class="narrow">
  <section class="card">
    <h1>{escape(heading)}</h1>
    {_notice(message)}
    <p><a href="/admin">Return to administration</a></p>
  </section>
</main>
"""
    return _document(heading, content)


def guide_page(csrf_token: str, page: str) -> str:
    csrf = escape(csrf_token, quote=True)
    pages = {
        "guide": (
            "Operator guide",
            """
<p>Use <strong>Initialize</strong> once. Save the returned operator credential
outside the browser. Recovery invalidates prior active operator credentials.</p>
<p>Provision each Agent for one named Section and only the actions it needs.
Record the returned caller and credential IDs so the credential can be revoked.</p>
<p>This console has no image update, rollback, Docker, backup restore, shell, or
deployment controls. Those remain separate local operator procedures.</p>
""",
        ),
        "agent": (
            "Agent instructions",
            """
<p>Install the independently packaged Python client and start by checking the
server contract and effective identity:</p>
<pre>patchouli capabilities
patchouli whoami
patchouli sections list</pre>
<p>Credentials have no command-line option. Supply them through token stdin,
the process-local <code>PATCHOULI_TOKEN</code> environment variable, or the
optional operating-system secret store. Never place a token in a prompt,
profile, URL, tracked file, or shell argument.</p>
<p>The bundled <code>patchouli-agent</code> Skill contains the complete safe
archive and exact-citation workflow.</p>
""",
        ),
        "mcp": (
            "MCP setup",
            """
<p>Install <code>patchouli-client[mcp]</code>, configure the same non-secret
client profile, and point the Agent host at the <code>patchouli-mcp</code>
executable.</p>
<pre>executable: patchouli-mcp
arguments: none
transport: stdio</pre>
<p>The adapter opens no listener and accepts no credential, endpoint,
idempotency key, journal path, or local file path in MCP tool arguments.
Configure credentials through the client environment or operating-system
secret store.</p>
""",
        ),
    }
    title, body = pages[page]
    content = f'{_header(csrf)}<main><section class="card"><h1>{title}</h1>{body}</section></main>'
    return _document(title, content)


def _header(csrf_token: str) -> str:
    return f"""
<header>
  <nav aria-label="Administration">
    <a href="/admin"><strong>PatchouliLib</strong></a>
    <a href="/admin/guide">Guide</a>
    <a href="/admin/agent">Agent</a>
    <a href="/admin/mcp">MCP</a>
    <form method="post" action="/admin/logout">
      {_csrf(csrf_token)}
      <button class="secondary" type="submit">Sign out</button>
    </form>
  </nav>
</header>
"""


def _document(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} · PatchouliLib</title>
  <link rel="stylesheet" href="/admin/style.css">
</head>
<body>{content}</body>
</html>
"""


def _notice(message: str, *, error: bool = False) -> str:
    classes = "notice error" if error else "notice"
    return f'<p class="{classes}" role="status">{escape(message)}</p>'


def _csrf(value: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{value}">'


def _text(name: str, label: str) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(label)}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="text" '
        'maxlength="200" required>'
    )


def _secret(name: str, label: str) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(label)}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="password" '
        'maxlength="256" autocomplete="off" spellcheck="false" required>'
    )


def _textarea(name: str, label: str) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(label)}</label>'
        f'<textarea id="{escaped_name}" name="{escaped_name}" '
        'maxlength="4000" rows="3"></textarea>'
    )


def _number(name: str, label: str, value: int) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(label)}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="number" '
        f'min="1" value="{value}" required>'
    )


__all__ = [
    "STYLESHEET",
    "action_result_page",
    "credential_page",
    "dashboard_page",
    "guide_page",
    "login_page",
]
