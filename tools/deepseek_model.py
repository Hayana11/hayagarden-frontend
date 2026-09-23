"""Runtime DeepSeek model lookup for standalone utility scripts."""
import os
import sys


def get_deepseek_chat_model() -> str:
    """Read the shared runtime setting on every request; never cache it."""
    root = os.environ.get('FRONTEND_ROOT') or '/opt/frontend'
    if not os.path.isdir(root):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    if root not in sys.path:
        sys.path.insert(0, root)
    import config_store

    return config_store.get_deepseek_chat_model()
