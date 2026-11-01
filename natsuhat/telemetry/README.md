# Local instrumentation

Natsuhat does not ship a remote analytics collector, analytics credentials, or
usage-beacon transport. Instrumentation hooks remain for engine bookkeeping and
local logs; they do not send events over the network.

Run artifacts and local logs can contain sensitive scan data. Keep them private.
Explicitly configured OTEL exporters, LLM providers, search providers, and MCP
servers are separate integrations; configure them only when authorized.
