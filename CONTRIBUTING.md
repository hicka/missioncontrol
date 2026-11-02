# Contributing

Thanks for looking. Small, focused pull requests are the easiest to take. For
anything large, or anything that changes who may do what, open an issue first.

## Set up

```bash
git clone https://github.com/hicka/missioncontrol
cd missioncontrol
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt pytest
./venv/bin/playwright install chromium
```

To try your change without touching a Mission Control you already use, give it
its own data folder and port:

```bash
MISSIONCONTROL_HOME=/tmp/mc-dev MISSIONCONTROL_PORT=8950 ./venv/bin/python server/main.py
```

A separate data folder does not isolate the tools. Agents in a test instance still
run real commands on your machine, so be careful what you ask them and what you
approve.

## Test

```bash
./venv/bin/python -m pytest tests/ -q
```

CI runs this on Python 3.12 with the current pytest, and builds and smoke-tests
the Docker image. A UI change can also be walked through in dark, light and phone
layouts against a running instance, without submitting anything:

```bash
./venv/bin/python scripts/check_ui.py --url http://127.0.0.1:8950
```

## Changing the kernel

Read [the design](docs/super-agent.md) first, especially
[how a task ends](docs/super-agent.md#how-a-task-ends) and
[authority](docs/super-agent.md#authority). This is the part of the code where
small mistakes are serious.

- Add a test that fails without your change, and say what it catches.
- Timing bugs can hide in a full run. Run a new kernel test by itself several times too.
- Authority is settled in code, from who sent an event. Never let a change depend
  on what a model concludes about a request.
- `selfimprove.PROTECTED` lists the files that hold the limits agents run under.
  Changes to them get a closer look.

## Before you open a pull request

- `pytest` passes.
- The docs still match. If you change behaviour, update `README.md` or `docs/`.
- No keys, tokens, personal data or real customer names, including in tests and
  screenshots. Use made-up values like `acmeshop`.
- Write for someone reading the code cold: match the style around you, and comment
  on why rather than what.

By contributing, you agree that your work is released under the
[MIT license](LICENSE).
