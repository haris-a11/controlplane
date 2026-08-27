"""Load .env before anything reads os.environ.

Every entry point (app, eval harness, self-checks) imports through this package,
so this is the one place that has to happen.
"""
from dotenv import load_dotenv

load_dotenv()
