"""`python3 -m coding_agent.hooks <hook-id>`: the command every host runs for a hook."""

import sys

from coding_agent.hooks import main

raise SystemExit(main(sys.argv[1:]))
