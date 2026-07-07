"""External connectors (Phase 9): narrow, audited adapters to outside services.

Kairo stays the reasoning layer; connectors are thin, our-code REST adapters (no
google-api-python-client, no MCP) that expose exactly the calls we need, behind the
existing PermissionGate and audit trail. See docs/PLAN-9-daily.md and ADR-0009.

Layout (built up over Milestone 9):

* ``base.py``      — ConnectorRegistry (the ToolContext seam) + the Notifier protocol +
                     ConnectorAuthError (friendly-reconnect-only, A6). Dependency-light so
                     config/tool wiring imports it without pulling network SDKs.
* ``tokens.py``    — TokenStore: atomic on-disk OAuth token custody + single-flight refresh
* ``oauth.py``     — OAuth2 authorization-code + PKCE loopback flow (Google + Kakao)
* ``google/``      — client + calendar/gmail/drive REST adapters (read + gmail drafts ONLY)
* ``telegram.py``  — TelegramNotifier (send-only)
* ``kakao.py``     — KakaoNotifier ("send to me" memo)
* ``demo.py``      — DemoGoogleClient / DemoNotifier: clearly-badged fake data, no egress

Two safety properties dominate (enforced structurally, not by prompt framing):

1. Data flow, not just tools: reads_private tools taint a turn so egress in that turn can't
   run silently; egress-with-agency is never unattended; connector text is framed untrusted.
2. Least authority + honest failure: read-only scopes + gmail.compose (never send); token
   files live under the hard sensitive-path floor; auth failures surface only as a friendly
   "run jarvis connect <provider>" — never a provider error body (ADR-0009).
"""
