"""Outbound notification transports that are not Slack.

Everything here can reach a human inbox, so every module in this package fails
closed: no configuration means no send, and an unrecognised recipient is refused
rather than attempted.
"""
