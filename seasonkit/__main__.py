"""Command line for the in-season tools.

    python3 -m seasonkit refresh      pull from ESPN, value players, write site/data.json
    python3 -m seasonkit serve        preview the page at http://localhost:8766 (refresh first)
    python3 -m seasonkit priors       freeze the draft board's numbers into data/priors.json (done once)
    python3 -m seasonkit takes        encrypt data/takes.json to data/takes.enc (commit that; the plain file stays local)
"""
import sys


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "refresh":
        from .build import build
        build(with_news="--no-news" not in argv)
    elif cmd == "serve":
        from .serve import serve
        serve(open_browser="--no-browser" not in argv)
    elif cmd == "takes":
        from .build import seal_takes
        seal_takes()
    elif cmd == "priors":
        from .priors import freeze
        freeze()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
