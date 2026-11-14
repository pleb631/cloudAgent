"""Pinned policy shim around the unmodified OpenHands agent-server CLI.

The SDK 1.62.0 automatically discovers workspace/home plugins and file-based
agents, independently of AgentContext's skill-loading flags. Those ambient
files must not expand this service's operator-configured tools, hooks, MCP
servers or child-agent registry. Disable only these discovery call sites;
explicit controller-supplied agent definitions and inline skills still work.

This is configuration policy, not a security boundary against code executing
inside the sandbox. Changing SDK versions requires reviewing these call sites
and updating the contract tests before changing the pin.
"""

import sys
from importlib.metadata import version

PINNED_VERSION = "1.62.0"


def _no_plugins(*args, **kwargs):
    return {}


def _no_file_agents(*args, **kwargs):
    return []


def apply_policy():
    # Fail closed before importing any server initialization code. All three
    # distributions participate in remote serialization and tool construction.
    for distribution in ("openhands-sdk", "openhands-tools", "openhands-agent-server"):
        if version(distribution) != PINNED_VERSION:
            raise RuntimeError(f"{distribution} must be exactly {PINNED_VERSION}")

    from openhands.sdk.conversation.impl import local_conversation

    # Patch the symbols actually used by LocalConversation, not only their
    # defining modules (which would leave previously imported aliases active).
    local_conversation.load_available_plugins = _no_plugins
    local_conversation.register_file_agents = _no_file_agents


def main():
    if "--reload" in sys.argv[1:]:
        raise RuntimeError("Reload is disabled: every server must start through the policy shim")
    apply_policy()
    from openhands.agent_server.__main__ import main as native_main

    native_main()


if __name__ == "__main__":
    main()
